import hashlib
import hmac
import html
import io
import json
import os
import re
import time
import urllib.parse

from flask import request, jsonify
import requests

from common import (
    APP_START_TIME,
    QQ_FACES,
    TARGET_GROUP_ID,
    add_chat_message,
    chat_collection,
    chat_history,
    filter_emoji,
    images_collection,
    processed_notices_cache,
    recent_web_uploads,
    save_image,
    sign_url,
    to_jpeg_b64,
)
from qq import (
    clean_qq_name,
    get_qq_name,
    name_to_qq_cache,
    onebot,
)
from accounts import handle_admin_command
from sending import highlight_mentions


WEBHOOK_SECRET_TOKEN = os.environ["WEBHOOK_TOKEN"]
RECALLED_TEXT = "[消息已撤回]"
RECALLED_HTML = f'<span style="color:#999; font-size:12px;"><i>{RECALLED_TEXT}</i></span>'


def receive_qq_msg():
    client_ip = request.remote_addr or ""

    authorized = hmac.compare_digest(
        request.args.get("token", "").encode("utf-8"),
        WEBHOOK_SECRET_TOKEN.encode("utf-8"),
    )

    if not authorized:
        print(f"[安全拦截] 拦截未授权的 Webhook 请求！来源IP: {client_ip}")
        return jsonify({"status": "failed", "msg": "Unauthorized"}), 403

    data = request.get_json(silent=True) or {}

    if data.get("message_type") == "group" and data.get("group_id") == TARGET_GROUP_ID:
        if handle_admin_command(data):
            return jsonify({"status": "admin_command"})

        sender = data.get("sender", {})
        sender_name = clean_qq_name(sender.get("nickname", "某人")) or "某人"

        sender_qq = str(sender.get("user_id", ""))
        if sender_qq:
            name_to_qq_cache[sender_name] = sender_qq

        raw_msg = ""
        qq_reply_html = ""

        message_chain = data.get("message", [])
        for segment in message_chain:
            seg_type = segment.get("type")
            if seg_type == "text":
                raw_msg += segment.get("data", {}).get("text", "")
            elif seg_type == "image":
                img_url = segment.get("data", {}).get("url", "")
                if img_url:
                    img_id = hashlib.md5(img_url.encode("utf-8")).hexdigest()[:16]

                    if not images_collection.find_one({"_id": img_id}):
                        try:
                            headers = {"User-Agent": "Mozilla/5.0"}
                            resp = requests.get(img_url, headers=headers, timeout=5)
                            if resp.status_code == 200:
                                save_image(
                                    img_id,
                                    to_jpeg_b64(io.BytesIO(resp.content), 240, 100000, 70),
                                )
                        except Exception as e:
                            print(f"图床持久化或清理失败: {e}")

                    raw_msg += f"[图片] {{IMG:{img_id}}}"
                else:
                    raw_msg += "[图片]"
            elif seg_type == "face":
                face_data = segment.get("data", {})
                face_text = face_data.get("text")
                face_id = face_data.get("id")

                if face_text:
                    clean_text = face_text.replace("[", "").replace("]", "")
                    raw_msg += f"[{clean_text}]"
                elif face_id is not None:
                    try:
                        face_name = QQ_FACES.get(int(str(face_id)))
                        if face_name:
                            raw_msg += f"[{face_name}]"
                        else:
                            raw_msg += f"[表情{face_id}]"
                    except ValueError:
                        raw_msg += "[表情]"
                else:
                    raw_msg += "[表情]"
            elif seg_type in ["bface", "mface", "marketface"]:
                raw_msg += "[动画表情]"
            elif seg_type == "record":
                raw_msg += "[语音]"
            elif seg_type == "video":
                raw_msg += "[视频]"

            elif seg_type == "json":
                card_data_str = segment.get("data", {}).get("data", "{}")
                prompt_text = "应用卡片"
                try:
                    inner_json = json.loads(card_data_str)
                    if isinstance(inner_json, dict) and "prompt" in inner_json:
                        prompt_text = (
                            inner_json.get("prompt", "")
                            .replace("[", "")
                            .replace("]", "")
                        )
                except:
                    pass
                if not prompt_text:
                    prompt_text = "应用卡片"
                raw_msg += f"[{prompt_text}]"
            elif seg_type == "xml":
                raw_msg += "[图文卡片]"
            elif seg_type in ["forward", "node"]:
                raw_msg += "[聊天记录]"
            elif seg_type == "share":
                title = segment.get("data", {}).get("title", "链接")
                raw_msg += f"[链接: {title}]"
            elif seg_type == "music":
                title = segment.get("data", {}).get("title", "一首歌")
                raw_msg += f"[音乐: {title}]"
            elif seg_type == "location":
                title = segment.get("data", {}).get("title", "某个位置")
                raw_msg += f"[位置: {title}]"
            elif seg_type == "contact":
                raw_msg += "[名片]"
            elif seg_type == "poke":
                target_qq = str(segment.get("data", {}).get("qq", ""))
                if target_qq and target_qq != "all":
                    target_name = get_qq_name(target_qq)
                    raw_msg += f"[戳了戳 {target_name}]"
                else:
                    raw_msg += "[戳了戳]"
            elif seg_type == "at":
                target_qq = str(segment.get("data", {}).get("qq", ""))
                if target_qq == "all":
                    raw_msg += "[@全体成员] "
                else:
                    target_name = segment.get("data", {}).get("name")
                    if not target_name:
                        target_name = get_qq_name(target_qq)
                    raw_msg += f"[@{target_name}] "
            elif seg_type == "reply":
                msg_id = segment.get("data", {}).get("id")
                reply_text = ""
                reply_sender = "某人"

                if msg_id:
                    try:
                        resp = onebot("get_msg", {"message_id": msg_id}, timeout=1.5)
                        if resp.get("status") == "ok":
                            orig_data = resp.get("data", {})

                            orig_qq = orig_data.get("sender", {}).get("user_id")
                            if orig_qq:
                                reply_sender = get_qq_name(orig_qq)

                            for orig_seg in orig_data.get("message", []):
                                if orig_seg.get("type") == "text":
                                    reply_text += orig_seg.get("data", {}).get("text", "")
                                elif orig_seg.get("type") == "image":
                                    reply_text += "[图片]"
                                elif orig_seg.get("type") == "face":
                                    reply_text += "[表情]"

                            reply_text = reply_text.replace("\n", " ").replace("\r", "")
                            if len(reply_text) > 18:
                                reply_text = reply_text[:18] + "..."
                    except Exception as e:
                        print(f"提取回复内容失败: {e}")

                if reply_text:
                    qq_reply_html = f'<span class="reply-text">[回复 @{html.escape(reply_sender)}: {html.escape(reply_text)}]</span><br/>'
                else:
                    qq_reply_html = '<span class="reply-text">[回复]</span><br/>'

        raw_msg = filter_emoji(raw_msg)

        if raw_msg.strip():
            safe_text = html.escape(raw_msg).replace("\n", "<br/>")

            safe_text = re.sub(
                r"\{IMG:([A-Za-z0-9_-]+)\}",
                r'<a href="/view_image?b64=\1" target="_blank" class="action-at">[查看]</a>',
                safe_text,
            )
            safe_text = highlight_mentions(safe_text)

            safe_sender = html.escape(sender_name)

            print(f"💬 [QQ群收到]: {safe_sender} - {raw_msg}")

            add_chat_message(
                "QQ群",
                f"{qq_reply_html}{safe_sender}: {safe_text}",
                sender_name,
                raw_msg,
                qq_msg_id=data.get("message_id"),
            )

    elif data.get("post_type") == "notice" and data.get("group_id") == TARGET_GROUP_ID:
        event_time = data.get("time", int(time.time()))

        if event_time <= APP_START_TIME:
            return jsonify({"status": "ignored_historical_notice"})

        if time.time() - APP_START_TIME < 90:
            return jsonify({"status": "ignored_startup_burst"})

        notice_type = data.get("notice_type")
        user_qq = str(data.get("user_id", ""))

        event_fingerprint = f"{notice_type}_{user_qq}_{event_time}"
        if event_fingerprint in processed_notices_cache:
            return jsonify({"status": "ignored_cached_notice"})
        processed_notices_cache[event_fingerprint] = True

        sub_type = data.get("sub_type")
        operator_qq = str(data.get("operator_id", ""))

        sys_msg_text = ""
        recalled_local_id = None

        if notice_type == "notify" and sub_type == "poke":
            sender_name = get_qq_name(user_qq)
            target_qq = str(data.get("target_id", ""))
            target_name = get_qq_name(target_qq)
            sys_msg_text = f"{sender_name} 戳了戳 {target_name}"

        elif notice_type == "group_increase":
            user_name = get_qq_name(user_qq)
            sys_msg_text = f"欢迎 {user_name} 加入了群聊！"
        elif notice_type == "group_decrease":
            user_name = get_qq_name(user_qq)
            if sub_type == "leave":
                sys_msg_text = f"{user_name} 默默离开了群聊"
            elif sub_type in ["kick", "kick_me"]:
                operator_name = get_qq_name(operator_qq)
                sys_msg_text = f"{user_name} 被 {operator_name} 请出了群聊"

        elif notice_type == "group_recall":
            recalled_qq_msg_id = data["message_id"]
            id_filter = {"qq_msg_id": recalled_qq_msg_id}

            original_msg = chat_collection.find_one(id_filter)
            if original_msg and original_msg["pure_text"] != RECALLED_TEXT:
                operator_name = get_qq_name(operator_qq)
                sys_msg_text = f"{operator_name} 撤回了一条消息"

                for m in chat_history:
                    if m.get("qq_msg_id") == recalled_qq_msg_id:
                        recalled_local_id = m["id"]
                        m["text"] = RECALLED_HTML
                        m["pure_text"] = RECALLED_TEXT
                        break

                try:
                    chat_collection.update_many(
                        id_filter, {"$set": {"text": RECALLED_HTML, "pure_text": RECALLED_TEXT}}
                    )
                except Exception as e:
                    print(f"云端更新撤回状态失败: {e}")

        elif notice_type == "group_upload":
            user_name = get_qq_name(user_qq)
            file_info = data.get("file", {})
            file_name = file_info.get("name", "未知文件")

            if (
                str(user_qq) == str(data.get("self_id"))
                and file_name in recent_web_uploads
            ):
                display_name = recent_web_uploads[file_name]
                del recent_web_uploads[file_name]
            else:
                display_name = user_name

            sys_msg_text = f"{display_name} 上传了群文件: {file_name}"

            if str(user_qq) != str(data.get("self_id")):
                file_url = file_info.get("url", "")
                file_id = file_info.get("id", "")
                file_busid = file_info.get("busid", "")

                if not file_url and file_id:
                    try:
                        resp = onebot("get_group_file_url", {"group_id": TARGET_GROUP_ID, "file_id": file_id, "busid": file_busid}, timeout=2)
                        if resp.get("status") == "ok":
                            file_url = resp.get("data", {}).get("url", "")
                    except Exception:
                        pass

                safe_sender = html.escape(display_name)
                safe_filename = html.escape(file_name)

                if file_url:
                    encoded_url = urllib.parse.quote(file_url, safe="")
                    encoded_name = urllib.parse.quote(file_name)
                    proxy_url = f"/api/download_qq_file?name={encoded_name}&url={encoded_url}&sig={sign_url(file_url)}"
                    file_html = f"<span style='color:#008800;'>[文件: {safe_filename}]</span> <a href='{proxy_url}' target='_blank' style='color: blue; text-decoration:underline;'>[下载]</a>"
                else:
                    file_html = f"<span style='color:#008800;'>[文件: {safe_filename}]</span> <span style='color:#999; font-size:11px;'>(无法获取下载链接)</span>"

                add_chat_message(
                    "QQ群",
                    f"{safe_sender}: {file_html}",
                    display_name,
                    f"[文件: {file_name}]",
                    qq_msg_id=None,
                )

        elif notice_type == "group_ban":
            user_name = get_qq_name(user_qq)
            if sub_type == "ban":
                duration = data.get("duration", 0) // 60
                sys_msg_text = f"{user_name} 被管理员禁言了 {duration} 分钟"
            elif sub_type == "lift_ban":
                sys_msg_text = f"{user_name} 被解除了禁言"
        elif notice_type == "group_admin":
            user_name = get_qq_name(user_qq)
            if sub_type == "set":
                sys_msg_text = f"{user_name} 荣升为群管理员"
            elif sub_type == "unset":
                sys_msg_text = f"{user_name} 被取消了管理员"

        if sys_msg_text:
            print(f"[系统事件]: {sys_msg_text}")

            extra = {}
            if notice_type == "group_recall" and recalled_local_id is not None:
                extra["recall_target_id"] = recalled_local_id

            add_chat_message(
                "系统",
                f'<span style="color:#ff6600; font-size:12px;">{html.escape(sys_msg_text)}</span>',
                "系统",
                sys_msg_text,
                **extra,
            )

    return jsonify({"status": "ok"})
