import hashlib
import hmac
import html
import io
import json
import os
import re
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
RECALLED_HTML = f'<span style="color:#666666; font-size:small;"><i>{RECALLED_TEXT}</i></span>'


def format_duration(seconds):
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = -(-rest // 60)
    parts = [f"{days}天" if days else "", f"{hours}小时" if hours else "", f"{minutes}分钟" if minutes else ""]
    return "".join(parts)


def receive_qq_msg():
    client_ip = request.remote_addr

    authorized = hmac.compare_digest(
        request.args.get("token", "").encode("utf-8"),
        WEBHOOK_SECRET_TOKEN.encode("utf-8"),
    )

    if not authorized:
        print(f"[安全拦截] 拦截未授权的 Webhook 请求，来源IP: {client_ip}")
        return jsonify({"status": "failed", "msg": "Unauthorized"}), 403

    data = request.get_json(silent=True) or {}

    if data.get("message_type") == "group" and data.get("group_id") == TARGET_GROUP_ID:
        if handle_admin_command(data):
            return jsonify({"status": "admin_command"})

        sender = data["sender"]
        sender_name = clean_qq_name(sender.get("card") or sender.get("nickname")) or "某人"
        name_to_qq_cache[sender_name] = str(sender["user_id"])

        raw_msg = ""
        qq_reply_html = ""

        message_chain = data.get("message", [])
        for segment in message_chain:
            seg_type = segment.get("type")
            if seg_type == "text":
                raw_msg += segment.get("data", {}).get("text", "")
            elif seg_type == "image":
                img_url = segment.get("data", {}).get("url", "")
                saved = False
                if img_url:
                    img_id = hashlib.md5(img_url.encode("utf-8")).hexdigest()[:16]
                    saved = images_collection.find_one({"_id": img_id}, {"_id": 1}) is not None
                    if not saved:
                        try:
                            resp = requests.get(img_url, timeout=5)
                            resp.raise_for_status()
                            save_image(img_id, to_jpeg_b64(io.BytesIO(resp.content), 240, 100000, 70))
                            saved = True
                        except Exception as e:
                            print(f"保存群图片失败: {e}")
                raw_msg += f"[图片] {{IMG:{img_id}}}" if saved else "[图片]"
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
                try:
                    card = json.loads(segment.get("data", {}).get("data", "{}"))
                    prompt_text = str(card.get("prompt", "")).replace("[", "").replace("]", "")
                except (ValueError, AttributeError):
                    prompt_text = ""
                raw_msg += f"[{prompt_text or '应用卡片'}]"
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
                r'<a href="/view_image?b64=\1" class="action-at">[查看]</a>',
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
        event_time = data["time"]

        if event_time <= APP_START_TIME:
            return jsonify({"status": "ignored_historical_notice"})

        notice_type = data.get("notice_type")
        user_qq = str(data.get("user_id", ""))

        sub_type = data.get("sub_type")
        operator_qq = str(data.get("operator_id", ""))

        sys_msg_text = ""

        if notice_type == "notify" and sub_type == "poke":
            sender_name = get_qq_name(user_qq)
            target_qq = str(data.get("target_id", ""))
            target_name = get_qq_name(target_qq)
            sys_msg_text = f"{sender_name} 戳了戳 {target_name}"

        elif notice_type == "group_increase":
            user_name = get_qq_name(user_qq)
            sys_msg_text = f"{user_name} 加入了群聊"
        elif notice_type == "group_decrease":
            user_name = get_qq_name(user_qq)
            if sub_type == "leave":
                sys_msg_text = f"{user_name} 退出了群聊"
            elif sub_type in ["kick", "kick_me"]:
                operator_name = get_qq_name(operator_qq)
                sys_msg_text = f"{user_name} 被 {operator_name} 移出了群聊"

        elif notice_type == "group_recall":
            recalled_qq_msg_id = data["message_id"]
            id_filter = {"qq_msg_id": recalled_qq_msg_id}

            original_msg = chat_collection.find_one(id_filter)
            if original_msg and original_msg["pure_text"] != RECALLED_TEXT:
                operator_name = get_qq_name(operator_qq)
                sys_msg_text = f"{operator_name} 撤回了一条消息"

                for m in chat_history:
                    if m.get("qq_msg_id") == recalled_qq_msg_id:
                        m["text"] = RECALLED_HTML
                        m["pure_text"] = RECALLED_TEXT
                        break

                try:
                    chat_collection.update_many(
                        id_filter, {"$set": {"text": RECALLED_HTML, "pure_text": RECALLED_TEXT}}
                    )
                except Exception as e:
                    print(f"云端更新撤回状态失败: {e}")

        elif notice_type == "group_upload" and user_qq != str(data["self_id"]):
            user_name = get_qq_name(user_qq)
            file_info = data.get("file", {})
            file_name = file_info.get("name", "未知文件")
            file_url = file_info.get("url", "")

            if not file_url and file_info.get("id"):
                try:
                    resp = onebot("get_group_file_url", {"group_id": TARGET_GROUP_ID, "file_id": file_info["id"], "busid": file_info.get("busid", "")}, timeout=2)
                    if resp.get("status") == "ok":
                        file_url = resp.get("data", {}).get("url", "")
                except Exception as e:
                    print(f"获取群文件下载链接失败: {e}")

            safe_filename = html.escape(file_name)
            if file_url:
                encoded_url = urllib.parse.quote(file_url, safe="")
                encoded_name = urllib.parse.quote(file_name, safe="")
                proxy_url = f"/qqfile/{encoded_name}?url={encoded_url}&amp;sig={sign_url(file_url)}"
                file_html = f"<span style='color:#009900;'>[文件: {safe_filename}]</span> <a href='{proxy_url}'>[下载]</a>"
            else:
                file_html = f"<span style='color:#009900;'>[文件: {safe_filename}]</span> <span style='color:#666666; font-size:small;'>(无法获取下载链接)</span>"

            add_chat_message(
                "QQ群",
                f"{html.escape(user_name)}: {file_html}",
                user_name,
                f"[文件: {file_name}]",
            )

        elif notice_type == "group_ban":
            user_name = get_qq_name(user_qq)
            if sub_type == "ban":
                sys_msg_text = f"{user_name} 被禁言{format_duration(data.get('duration', 0))}"
            elif sub_type == "lift_ban":
                sys_msg_text = f"{user_name} 被解除禁言"
        elif notice_type == "group_admin":
            user_name = get_qq_name(user_qq)
            if sub_type == "set":
                sys_msg_text = f"{user_name} 成为群管理员"
            elif sub_type == "unset":
                sys_msg_text = f"{user_name} 被取消群管理员"

        if sys_msg_text:
            print(f"[系统事件]: {sys_msg_text}")
            add_chat_message(
                "系统",
                f'<span style="color:#ff6600; font-size:small;">{html.escape(sys_msg_text)}</span>',
                "",
                sys_msg_text,
            )

    return jsonify({"status": "ok"})
