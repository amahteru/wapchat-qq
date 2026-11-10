import re

import requests

from common import LimitedDict, TARGET_GROUP_ID, filter_emoji


ONEBOT_URL = "http://127.0.0.1:3000"


qq_name_cache = LimitedDict(1000)


def get_qq_name(qq):
    qq_str = str(qq)
    if qq_str in qq_name_cache:
        return qq_name_cache[qq_str]
    try:
        resp = onebot("get_group_member_info", {"group_id": TARGET_GROUP_ID, "user_id": int(qq)}, timeout=1.5)

        if resp.get("status") == "ok":
            data = resp.get("data", {})
            name = clean_qq_name(data.get("card") or data.get("nickname")) or qq_str

            qq_name_cache[qq_str] = name
            return name
    except Exception as e:
        print(f"获取QQ名字失败: {e}")
    return qq_str


def get_qq_by_name(target_name):
    if not target_name:
        return None

    if target_name in ["全体成员", "all", "全体"]:
        return "all"

    if target_name in name_to_qq_cache:
        return name_to_qq_cache[target_name]

    try:
        resp = onebot("get_group_member_list", {"group_id": TARGET_GROUP_ID}, timeout=2)

        if resp.get("status") == "ok":
            members = resp.get("data", [])
            found_uid = None
            for member in members:
                uid = str(member.get("user_id", ""))
                clean_card = clean_qq_name(member.get("card"))
                clean_nick = clean_qq_name(member.get("nickname"))

                if clean_card:
                    name_to_qq_cache[clean_card] = uid
                if clean_nick:
                    name_to_qq_cache[clean_nick] = uid

                if target_name == clean_card or target_name == clean_nick:
                    found_uid = uid

            return found_uid
    except Exception as e:
        print(f"拉取群成员列表失败: {e}")

    return None


name_to_qq_cache = LimitedDict(2000)


def onebot(action, payload, timeout=3):
    return requests.post(f"{ONEBOT_URL}/{action}", json=payload, timeout=timeout).json()


def clean_qq_name(name):
    return re.sub(r"\[.*?\]", "", filter_emoji(name or "")).strip()


def text_seg(text):
    return {"type": "text", "data": {"text": text}}


def send_group(segments, timeout=5):
    resp = onebot("send_group_msg", {"group_id": TARGET_GROUP_ID, "message": segments}, timeout=timeout)
    if resp.get("status") != "ok":
        raise RuntimeError(resp.get("wording") or resp.get("message") or "机器人返回失败")
    return (resp.get("data") or {}).get("message_id")


def bot_say(text):
    try:
        send_group([text_seg(text)])
        return True
    except Exception as e:
        print(f"机器人发送群消息失败: {e}")
        return False
