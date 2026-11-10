import html
import re
import time

from common import (
    LimitedDict,
    add_chat_message,
    chat_history,
    filter_emoji,
    has_banned_word,
)
from qq import get_qq_by_name, send_group, text_seg
from accounts import get_real_ip, nickname
from location import get_ip_location


COOLDOWN_SECONDS = 5


MAX_MSG_LENGTH = 80


GLOBAL_COOLDOWN = 2


global_last_send_time = 0


last_send_time = LimitedDict(1000)


last_message = LimitedDict(1000)


MENTION_RE = r"\[@([^\]]+)\]|(?<![\w.-])@([\w一-龥·_-]+)"


def highlight_mentions(safe_text):
    return re.sub(MENTION_RE, lambda m: f'<span class="action-reply">{m.group(0)}</span>', safe_text)


def send_web_message(account, raw_text, req):
    global global_last_send_time

    msg_text = filter_emoji((raw_text or "").strip())
    if not msg_text:
        return None

    username = nickname(account)
    client_ip = get_real_ip(req)
    current_time = time.time()

    intercept_msg = None
    if len(msg_text) > MAX_MSG_LENGTH:
        intercept_msg = f"消息过长，最多{MAX_MSG_LENGTH}字。"
    elif has_banned_word(msg_text):
        intercept_msg = "消息包含违规词。"
    elif msg_text == last_message.get(account, ""):
        intercept_msg = "请勿重复发送相同内容。"
    elif current_time - last_send_time.get(account, 0) < COOLDOWN_SECONDS:
        intercept_msg = f"发送太快，请{COOLDOWN_SECONDS}秒后再试。"
    elif current_time - global_last_send_time < GLOBAL_COOLDOWN:
        intercept_msg = "当前发送人数过多，请稍后再试。"

    if intercept_msg:
        print(
            f"🛡️ [安全拦截] IP:{client_ip} 账号:{account}({username}) 尝试发送: {msg_text} -> 原因: {intercept_msg}"
        )
        return intercept_msg

    last_send_time[account] = current_time
    last_message[account] = msg_text
    global_last_send_time = current_time

    reply_prefix_qq = ""
    reply_prefix_web = ""
    reply_segments = []
    clean_text = msg_text

    reply_match = re.search(r"^\[回复:(\d+)\]\s*", msg_text)
    if reply_match:
        reply_id = int(reply_match.group(1))
        clean_text = msg_text[reply_match.end() :]
        target_msg = next((m for m in chat_history if m["id"] == reply_id), None)
        if target_msg:
            t_sender = target_msg["pure_sender"]
            t_text = target_msg["pure_text"]
            if len(t_text) > 15:
                t_text = t_text[:15] + "..."

            target_qq_msg_id = target_msg.get("qq_msg_id")
            if target_qq_msg_id:
                reply_segments = [{"type": "reply", "data": {"id": str(target_qq_msg_id)}}]
            else:
                reply_prefix_qq = f"[回复 @{t_sender}: {t_text}]\n"

            reply_prefix_web = f'<span class="reply-text">[回复 @{html.escape(t_sender)}: {html.escape(t_text)}]</span><br/>'

    location = get_ip_location(client_ip)
    header = f"{reply_prefix_qq}[{location} - {username}] "
    segments = reply_segments + [text_seg(header)]

    pos = 0
    buf = ""
    for match in re.finditer(MENTION_RE, clean_text):
        qq_uid = get_qq_by_name(match.group(1) or match.group(2))
        buf += clean_text[pos : match.start()]
        pos = match.end()
        if not qq_uid:
            buf += match.group(0)
            continue
        segments += [text_seg(buf), {"type": "at", "data": {"qq": qq_uid}}, text_seg(" ")]
        buf = ""
    buf += clean_text[pos:]
    segments.append(text_seg(buf))
    segments = [seg for seg in segments if seg["type"] != "text" or seg["data"]["text"]]

    try:
        qq_msg_id = send_group(segments, timeout=3)
    except Exception as e:
        print(f"发送到QQ失败: {e}")
        return "连接 QQ 机器人失败，请稍后再试。"

    safe_text = highlight_mentions(html.escape(clean_text).replace("\n", "<br/>"))
    add_chat_message(
        "网页",
        f"{reply_prefix_web}[网页-{html.escape(location)}]{html.escape(username)}: {safe_text}",
        username,
        clean_text,
        qq_msg_id=qq_msg_id,
    )
    return None
