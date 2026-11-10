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
from qq import get_qq_by_name, get_qq_name, remember_echo, send_group, text_seg
from accounts import get_ip_location, get_real_ip, nickname


COOLDOWN_SECONDS = 5


MAX_MSG_LENGTH = 80


GLOBAL_COOLDOWN = 2


global_last_send_time = 0


ip_last_send_time = LimitedDict(1000)


ip_last_message = LimitedDict(1000)


MENTION_RE = r"\[@([^\]]+)\]|(?<![\w.-])@([\w一-龥·_-]+)"


def highlight_mentions(safe_text):
    safe_text = re.sub(
        r"\[@([^\]]+)\]", r'<span class="action-reply">[@\1]</span>', safe_text
    )
    return re.sub(
        r"(?<![\w.-])@([\w\u4e00-\u9fa5·_-]+)",
        r'<span class="action-reply">@\1</span>',
        safe_text,
    )


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
    elif msg_text == ip_last_message.get(client_ip, ""):
        intercept_msg = "请勿重复发送相同内容。"
    elif current_time - ip_last_send_time.get(client_ip, 0) < COOLDOWN_SECONDS:
        intercept_msg = f"发送太快，请{COOLDOWN_SECONDS}秒后再试。"
    elif current_time - global_last_send_time < GLOBAL_COOLDOWN:
        intercept_msg = "当前发送人数过多，请稍后再试。"

    if intercept_msg:
        print(
            f"🛡️ [安全拦截] IP:{client_ip} 账号:{account}({username}) 尝试发送: {msg_text} -> 原因: {intercept_msg}"
        )
        return intercept_msg

    ip_last_send_time[client_ip] = current_time
    ip_last_message[client_ip] = msg_text
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
            t_sender = target_msg.get("pure_sender", "某人")
            t_text = target_msg.get("pure_text", "")
            if len(t_text) > 15:
                t_text = t_text[:15] + "..."

            target_qq_msg_id = target_msg.get("qq_msg_id")
            if target_qq_msg_id:
                reply_segments = [{"type": "reply", "data": {"id": str(target_qq_msg_id)}}]
            else:
                reply_prefix_qq = f"[回复 @{t_sender}: {t_text}]\n"

            reply_prefix_web = f'<span class="reply-text">[回复 @{html.escape(t_sender)}: {html.escape(t_text)}]</span><br/>'

    location = get_ip_location(client_ip, req)
    header = f"{reply_prefix_qq}[{location} - {username}] "
    segments = reply_segments + [text_seg(header)]
    echo_text = header

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
        shown = "全体成员" if qq_uid == "all" else get_qq_name(qq_uid)
        echo_text += f"{buf}[@{shown}] "
        buf = ""
    buf += clean_text[pos:]
    segments.append(text_seg(buf))
    echo_text += buf
    segments = [seg for seg in segments if seg["type"] != "text" or seg["data"]["text"]]

    remember_echo(echo_text)

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
