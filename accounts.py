import ipaddress
import os
import re
import time

from flask import request, session
import requests
from werkzeug.security import check_password_hash, generate_password_hash

from common import (
    LimitedDict,
    TARGET_GROUP_ID,
    files_collection,
    filter_emoji,
    has_banned_word,
    online_sessions,
    users_collection,
)
from qq import bot_say, onebot


ACCOUNT_RE = re.compile(r"^[A-Za-z0-9_]{3,15}$")


def load_users():
    temp_db = {}
    try:
        for user in users_collection.find():
            account = user["_id"]
            temp_db[account] = {
                "password": user.get("password"),
                "nickname": user.get("nickname"),
                "ip": user.get("ip"),
                "status": user.get("status"),
                "reason": user.get("reason", ""),
                "location": user.get("location", ""),
            }
    except Exception as e:
        print(f"读取云数据库失败: {e}")
    return temp_db


users_db = load_users()


ip_location_cache = LimitedDict(1000)


MAX_TOTAL_ACCOUNTS = 500


MAX_ACCOUNTS_PER_IP = 1


MAX_PENDING_ACCOUNTS = 20


REGISTER_COOLDOWN = 30


MAX_REASON_LENGTH = 60


last_register_time = 0


MAX_LOGIN_FAILURES = 5


login_failures = LimitedDict(1000, 300)


def process_login_form():
    account = request.form.get("account", "").strip()
    password = request.form.get("password", "").strip()

    if not account or not password:
        return False, "账号和密码不能为空。", None

    if not ACCOUNT_RE.match(account):
        return False, "账号须为3-15位字母、数字或下划线。", None

    if "register_btn" in request.form:
        ok, msg = register_account(
            account, password, request.form.get("reason", ""), request
        )
        return (False, None, msg) if ok else (False, msg, None)

    error = try_login(account, password)
    return error is None, error, None


def get_active_account():
    account = session.get("nokia_account")
    user = users_db.get(account) if account else None
    if not user or user.get("status") == "pending":
        return None
    return account


def clean_nickname(raw):
    name = re.sub(r"\[.*?\]", "", filter_emoji((raw or "").strip()))
    name = re.sub(r"[\[\]\s]+", " ", name).strip()[:12].strip()
    if not name:
        return None, "昵称不能为空，也不能全是特殊字符。"
    if has_banned_word(name):
        return None, "昵称包含违规词。"
    return name, None


def set_nickname(account, raw):
    name, error = clean_nickname(raw)
    if error:
        return None, error
    users_db[account]["nickname"] = name
    try:
        users_collection.update_one({"_id": account}, {"$set": {"nickname": name}})
    except Exception as e:
        print(f"修改昵称同步云端单用户失败: {e}")
    return name, None


def try_login(account, password):
    if login_failures.get(account, 0) >= MAX_LOGIN_FAILURES:
        return "失败次数过多，请5分钟后再试。"

    if not verify_password(account, password):
        login_failures[account] = login_failures.get(account, 0) + 1
        return "账号或密码错误。"

    if users_db[account].get("status") == "pending":
        return "账号正在审核中，请等待群管理员通过。"

    login_failures.pop(account, None)
    session.permanent = True
    session["nokia_account"] = account
    return None


def hash_password(password):
    return generate_password_hash(password, method="pbkdf2:sha256")


def verify_password(account, password):
    user = users_db.get(account)
    if not user or not user.get("password"):
        return False
    return check_password_hash(user["password"], password)


def register_account(account, password, reason, req):
    global last_register_time

    reason = re.sub(r"\s+", " ", filter_emoji(reason or "")).strip()
    if not reason:
        return False, "注册需要填写申请理由。"
    if len(reason) > MAX_REASON_LENGTH:
        return False, f"申请理由不能超过{MAX_REASON_LENGTH}字。"

    if has_banned_word(reason):
        return False, "申请理由包含违规词。"

    if len(users_db) >= MAX_TOTAL_ACCOUNTS:
        return False, "注册名额已满，暂停注册。"

    if account in users_db:
        return False, "账号已存在，请直接登录。"

    client_ip = get_real_ip(req)
    ip_reg_count = sum(1 for u in users_db.values() if u.get("ip") == client_ip)
    if ip_reg_count >= MAX_ACCOUNTS_PER_IP:
        return False, "你的IP注册账号数已达上限。"

    pending_count = sum(1 for u in users_db.values() if u.get("status") == "pending")
    if pending_count >= MAX_PENDING_ACCOUNTS:
        return False, "待审核人数过多，请稍后再试。"

    if time.time() - last_register_time < REGISTER_COOLDOWN:
        return False, "注册太频繁，请稍后再试。"
    last_register_time = time.time()

    location = get_ip_location(client_ip, req)
    user_data = {
        "password": hash_password(password),
        "nickname": account,
        "ip": client_ip,
        "status": "pending",
        "reason": reason,
        "location": location,
        "time": time.time(),
    }
    try:
        users_collection.update_one({"_id": account}, {"$set": user_data}, upsert=True)
    except Exception as e:
        print(f"写入云数据库单用户失败: {e}")
        return False, "服务暂时不可用，请稍后再试。"
    users_db[account] = user_data

    bot_say(
        f"[注册申请] 账号: {account}\n"
        f"来自: {location}\n"
        f"理由: {reason}\n"
        f"群主/管理员回复「通过 {account}」或「拒绝 {account}」"
    )
    return True, "申请已提交，请等待群管理员审核。"


ADMIN_COMMAND_RE = re.compile(
    r"^(通过|拒绝|删号|待审核|删文件)(?:\s+(.+))?$", flags=re.DOTALL
)


def is_group_manager(data):
    sender = data.get("sender", {}) or {}
    role = sender.get("role")
    if not role:
        try:
            resp = onebot("get_group_member_info", {"group_id": TARGET_GROUP_ID, "user_id": int(sender.get("user_id", 0)), "no_cache": True}, timeout=2)
            role = (resp.get("data") or {}).get("role")
        except Exception as e:
            print(f"查询群成员身份失败: {e}")
    return role in ("owner", "admin")


def handle_admin_command(data):
    segments = data.get("message", [])
    if not isinstance(segments, list) or not segments:
        return False
    if any(seg.get("type") != "text" for seg in segments):
        return False

    text = "".join(seg.get("data", {}).get("text", "") for seg in segments).strip()
    match = ADMIN_COMMAND_RE.match(text)
    if not match:
        return False

    sender_qq = str((data.get("sender", {}) or {}).get("user_id", ""))
    bot_ids = {str(data.get("self_id", "")), os.environ.get("BOT_QQ", "")}
    if not sender_qq or sender_qq in bot_ids:
        return False

    if not is_group_manager(data):
        return False

    command = match.group(1)
    arg = (match.group(2) or "").strip()

    if command == "待审核":
        if arg:
            return False
        pending = [
            (acc, u) for acc, u in users_db.items() if u.get("status") == "pending"
        ]
        if not pending:
            bot_say("[审核] 目前没有待审核的账号。")
        else:
            lines = [f"[审核] 待审核 {len(pending)} 个:"]
            for acc, u in pending[:10]:
                lines.append(
                    f"{acc} ({u.get('location') or '未知地域'}): {u.get('reason') or '无'}"
                )
            if len(pending) > 10:
                lines.append("……其余请处理完后再查看")
            bot_say("\n".join(lines))
        return True

    if not arg:
        return False

    if command == "删文件":
        try:
            result = files_collection.delete_many({"filename": arg})
            if result.deleted_count:
                bot_say(f"[网盘] 已删除 {result.deleted_count} 个名为 {arg} 的文件。")
            else:
                bot_say(f"[网盘] 找不到名为 {arg} 的文件。")
        except Exception as e:
            print(f"管理员删除网盘文件失败: {e}")
            bot_say("[网盘] 删除失败，数据库暂时不可用。")
        return True

    account = arg
    if not ACCOUNT_RE.match(account):
        return False

    user = users_db.get(account)
    if not user:
        bot_say(f"[审核] 找不到账号 {account}。")
        return True

    try:
        if command == "通过":
            if user.get("status") != "pending":
                bot_say(f"[审核] 账号 {account} 不在待审核状态。")
                return True
            users_collection.update_one(
                {"_id": account}, {"$set": {"status": "active"}}
            )
            user["status"] = "active"
            bot_say(f"[审核] 账号 {account} 已通过，可以登录了。")
        else:
            if command == "拒绝" and user.get("status") != "pending":
                bot_say(f"[审核] 账号 {account} 已是正式账号，如需删除请发「删号 {account}」。")
                return True
            users_collection.delete_one({"_id": account})
            users_db.pop(account, None)
            online_sessions.pop(account, None)
            bot_say(f"[审核] 账号 {account} 已删除，名额已释放。")
    except Exception as e:
        print(f"管理员审批写入云数据库失败: {e}")
        bot_say("[审核] 操作失败，数据库暂时不可用。")
    return True


def nickname(account):
    return users_db[account].get("nickname", account)


def get_real_ip(req):
    cf_custom_ip = req.headers.get("X-Real-IP-Custom")
    if cf_custom_ip:
        return cf_custom_ip.split(",")[0].strip()

    cf_ip = req.headers.get("CF-Connecting-IP")
    if cf_ip:
        return cf_ip.split(",")[0].strip()

    forwarded_for = req.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return req.remote_addr or "127.0.0.1"


def get_ip_location(ip, req):
    try:
        if not ipaddress.ip_address(ip).is_global:
            return "未知地域"
    except ValueError:
        return "未知地域"

    if ip in ip_location_cache:
        return ip_location_cache[ip]

    location = ""
    is_ipv6 = ":" in ip
    pro, city = "", ""

    if not is_ipv6:
        try:
            url = f"https://whois.pconline.com.cn/ipJson.jsp?ip={ip}&json=true"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
            response = requests.get(url, headers=headers, timeout=2)
            response.encoding = "gbk"
            resp_data = response.json()

            pro = resp_data.get("pro", "").strip()
            city = resp_data.get("city", "").strip()
        except Exception:
            pass

    if not pro:
        try:
            fallback_url = f"http://ip-api.com/json/{ip}?lang=zh-CN"
            fallback_resp = requests.get(fallback_url, timeout=2).json()
            if fallback_resp.get("status") == "success":
                country = fallback_resp.get("country", "")
                if country == "中国":
                    pro = fallback_resp.get("regionName", "")
                    city = fallback_resp.get("city", "")
                else:
                    c_city = fallback_resp.get("city", "")
                    if c_city and c_city != country:
                        location = f"{country}-{c_city}"
                    else:
                        location = country
        except Exception:
            pass

    if pro and not location:
        pro = (
            pro.replace("省", "")
            .replace("市", "")
            .replace("自治区", "")
            .replace("维吾尔", "")
            .replace("回族", "")
            .replace("壮族", "")
        )
        city = city.replace("市", "")

        direct_cities = ["北京", "上海", "天津", "重庆"]
        special_admin = ["香港", "澳门", "台湾"]
        autonomous = ["内蒙古", "广西", "西藏", "宁夏", "新疆"]

        if pro in direct_cities:
            location = f"{pro}市"
        elif pro in special_admin:
            location = "台湾省" if pro == "台湾" else pro
        elif pro in autonomous:
            location = f"{pro}区{city}市" if city else f"{pro}区"
        else:
            location = f"{pro}省{city}市" if city else f"{pro}省"

    if not location:
        cf_country = req.headers.get("X-Real-Country")
        cf_region = req.headers.get("X-Real-Region")

        if cf_country == "CN":
            location = f"中国-{cf_region}" if cf_region else "中国"
        elif cf_country:
            location = f"海外({cf_country})"
        else:
            location = "未知地域"

    if location != "未知地域":
        ip_location_cache[ip] = location

    return location
