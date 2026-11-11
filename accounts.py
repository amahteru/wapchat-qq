import re
import secrets
import time

from flask import request, session
from pymongo.errors import DuplicateKeyError
from werkzeug.security import check_password_hash, generate_password_hash

from common import (
    APP_START_TIME,
    LimitedDict,
    TARGET_GROUP_ID,
    chat_collection,
    files_collection,
    filter_emoji,
    format_size,
    get_online_count,
    has_banned_word,
    images_collection,
    online_sessions,
    users_collection,
)
from location import get_ip_location
from qq import bot_say, onebot, text_seg


ACCOUNT_RE = re.compile(r"^[A-Za-z0-9_]{3,15}$")


def load_users():
    return {user.pop("_id"): user for user in users_collection.find()}


users_db = load_users()


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
        ok, msg = register_account(account, password, request.form.get("reason", ""))
        return (False, None, msg) if ok else (False, msg, None)

    error = try_login(account, password)
    return error is None, error, None


def get_active_account():
    account = session.get("nokia_account")
    user = users_db.get(account) if account else None
    if not user or user["status"] in ("pending", "banned"):
        return None
    return account


def clean_nickname(raw):
    name = re.sub(r"\[.*?\]", "", filter_emoji(raw.strip()))
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
    try:
        users_collection.update_one({"_id": account}, {"$set": {"nickname": name}})
    except Exception as e:
        print(f"保存昵称失败: {e}")
        return None, "服务暂时不可用，请稍后再试。"
    users_db[account]["nickname"] = name
    return name, None


def try_login(account, password):
    if login_failures.get(account, 0) >= MAX_LOGIN_FAILURES:
        return "失败次数过多，请5分钟后再试。"

    if not verify_password(account, password):
        login_failures[account] = login_failures.get(account, 0) + 1
        return "账号或密码错误。"

    status = users_db[account]["status"]
    if status == "pending":
        return "账号正在审核中，请等待群管理员通过。"
    if status == "banned":
        return "账号已被封禁。"

    login_failures.pop(account, None)
    session.permanent = True
    session["nokia_account"] = account
    return None


def hash_password(password):
    return generate_password_hash(password, method="pbkdf2:sha256")


def verify_password(account, password):
    user = users_db.get(account)
    return user is not None and check_password_hash(user["password"], password)


def register_account(account, password, reason):
    global last_register_time

    reason = re.sub(r"\s+", " ", filter_emoji(reason)).strip()
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

    client_ip = get_real_ip(request)
    ip_reg_count = sum(1 for u in users_db.values() if u["ip"] == client_ip)
    if ip_reg_count >= MAX_ACCOUNTS_PER_IP:
        return False, "你的IP注册账号数已达上限。"

    pending_count = sum(1 for u in users_db.values() if u["status"] == "pending")
    if pending_count >= MAX_PENDING_ACCOUNTS:
        return False, "待审核人数过多，请稍后再试。"

    if time.time() - last_register_time < REGISTER_COOLDOWN:
        return False, "注册太频繁，请稍后再试。"
    last_register_time = time.time()

    location = get_ip_location(client_ip)
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
        users_collection.insert_one({"_id": account, **user_data})
    except DuplicateKeyError:
        return False, "账号已存在，请直接登录。"
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


ADMIN_HELP = (
    "[指令] 仅群主/管理员可用:\n"
    "待审核 | 通过 账号 | 拒绝 账号\n"
    "封禁 账号 | 解封 账号 | 删号 账号\n"
    "重置密码 账号 | 用户列表 | 状态\n"
    "删文件 文件名"
)


ADMIN_COMMAND_RE = re.compile(
    r"^(通过|拒绝|删号|封禁|解封|重置密码|待审核|用户列表|状态|帮助|删文件)(?:\s+(.+))?$",
    flags=re.DOTALL,
)


NO_ARG_COMMANDS = {"待审核", "状态", "帮助"}


USERS_PER_PAGE = 30


def is_group_manager(data):
    sender = data["sender"]
    role = sender.get("role")
    if not role:
        try:
            resp = onebot("get_group_member_info", {"group_id": TARGET_GROUP_ID, "user_id": int(sender["user_id"]), "no_cache": True}, timeout=2)
            role = (resp.get("data") or {}).get("role")
        except Exception as e:
            print(f"查询群成员身份失败: {e}")
    return role in ("owner", "admin")


def list_pending():
    pending = [(acc, u) for acc, u in users_db.items() if u["status"] == "pending"]
    if not pending:
        return "[审核] 目前没有待审核的账号。"
    lines = [f"[审核] 待审核{len(pending)}个:"]
    for acc, u in pending[:10]:
        lines.append(f"{acc} ({u['location']}): {u['reason']}")
    if len(pending) > 10:
        lines.append("……其余请处理完后再查看")
    return "\n".join(lines)


def list_users(arg):
    try:
        page = max(int(arg or 1), 1)
    except ValueError:
        page = 1
    users = sorted(
        ((acc, u) for acc, u in users_db.items() if u["status"] != "pending"),
        key=lambda item: item[0].lower(),
    )
    if not users:
        return "[账号] 还没有正式用户。"
    total_pages = (len(users) + USERS_PER_PAGE - 1) // USERS_PER_PAGE
    page = min(page, total_pages)
    start = (page - 1) * USERS_PER_PAGE
    lines = [f"[账号] 共{len(users)}个，第{page}/{total_pages}页:"]
    for acc, u in users[start : start + USERS_PER_PAGE]:
        mark = " (已封禁)" if u["status"] == "banned" else ""
        name = u["nickname"]
        lines.append(acc + (f" / {name}" if name != acc else "") + mark)
    if page < total_pages:
        lines.append(f"下一页请发「用户列表 {page + 1}」")
    return "\n".join(lines)


def status_report():
    counts = {"active": 0, "pending": 0, "banned": 0}
    for u in users_db.values():
        counts[u["status"]] += 1
    uptime = int(time.time()) - APP_START_TIME
    lines = [
        "[状态] 运行情况:",
        f"账号: 正式{counts['active']}，待审核{counts['pending']}，封禁{counts['banned']}",
        f"在线: {get_online_count()}人，已运行{uptime // 3600}小时{uptime % 3600 // 60}分",
    ]
    try:
        files_size = sum(d["size"] for d in files_collection.find({}, {"size": 1}))
        lines.append(
            f"聊天记录{chat_collection.estimated_document_count()}条，"
            f"图片{images_collection.estimated_document_count()}张，"
            f"网盘{files_collection.estimated_document_count()}个文件，共{format_size(files_size)}"
        )
        stats = files_collection.database.command("dbStats")
        lines.append(f"数据库占用: {format_size(stats.get('storageSize', 0) + stats.get('indexSize', 0))}/512MB")
    except Exception as e:
        print(f"统计数据库用量失败: {e}")
    return "\n".join(lines)


def send_private(qq, text):
    try:
        resp = onebot(
            "send_private_msg",
            {"user_id": int(qq), "group_id": TARGET_GROUP_ID, "message": [text_seg(text)]},
            timeout=5,
        )
        return resp.get("status") == "ok"
    except Exception as e:
        print(f"机器人私聊失败: {e}")
        return False


def set_status(account, status):
    users_collection.update_one({"_id": account}, {"$set": {"status": status}})
    users_db[account]["status"] = status


def handle_account_command(command, account, sender_qq):
    user = users_db.get(account)
    if not user:
        return f"[账号] 找不到账号 {account}。"
    status = user["status"]

    if command == "通过":
        if status != "pending":
            return f"[审核] 账号 {account} 不在待审核状态。"
        set_status(account, "active")
        return f"[审核] 账号 {account} 已通过，可以登录了。"

    if command == "拒绝" and status != "pending":
        return f"[审核] 账号 {account} 已是正式账号，如需删除请发「删号 {account}」。"

    if command in ("拒绝", "删号"):
        users_collection.delete_one({"_id": account})
        users_db.pop(account, None)
        online_sessions.pop(account, None)
        return f"[账号] {account} 已删除，名额已释放。"

    if command == "封禁":
        if status == "banned":
            return f"[账号] {account} 已经是封禁状态。"
        set_status(account, "banned")
        online_sessions.pop(account, None)
        return f"[账号] {account} 已封禁，账号名和注册IP都不会释放。"

    if command == "解封":
        if status != "banned":
            return f"[账号] {account} 没有被封禁。"
        set_status(account, "active")
        return f"[账号] {account} 已解封。"

    if command == "重置密码":
        if status == "pending":
            return f"[账号] {account} 还在待审核，不需要重置密码。"
        new_password = secrets.token_urlsafe(6)
        if not send_private(sender_qq, f"[WapChat] 账号 {account} 的新密码: {new_password}"):
            return "[账号] 私聊发送失败，密码没有改动。请先加机器人为好友再试。"
        password_hash = hash_password(new_password)
        users_collection.update_one({"_id": account}, {"$set": {"password": password_hash}})
        user["password"] = password_hash
        login_failures.pop(account, None)
        return f"[账号] {account} 的密码已重置，新密码已私聊发给你。"


def handle_admin_command(data):
    segments = data["message"]
    if not segments or any(seg["type"] != "text" for seg in segments):
        return False

    text = "".join(seg["data"]["text"] for seg in segments).strip()
    match = ADMIN_COMMAND_RE.match(text)
    if not match:
        return False

    sender_qq = str(data["sender"]["user_id"])
    command = match.group(1)
    arg = (match.group(2) or "").strip()
    if command in NO_ARG_COMMANDS and arg:
        return False
    if command not in NO_ARG_COMMANDS | {"用户列表"} and not arg:
        return False
    if command not in NO_ARG_COMMANDS | {"用户列表", "删文件"} and not ACCOUNT_RE.match(arg):
        return False

    if not is_group_manager(data):
        return False

    try:
        if command == "帮助":
            reply = ADMIN_HELP
        elif command == "待审核":
            reply = list_pending()
        elif command == "用户列表":
            reply = list_users(arg)
        elif command == "状态":
            reply = status_report()
        elif command == "删文件":
            result = files_collection.delete_many({"filename": arg})
            if result.deleted_count:
                reply = f"[网盘] 已删除{result.deleted_count}个名为 {arg} 的文件。"
            else:
                reply = f"[网盘] 找不到名为 {arg} 的文件。"
        else:
            reply = handle_account_command(command, arg, sender_qq)
    except Exception as e:
        print(f"执行群指令「{command}」失败: {e}")
        reply = "[指令] 操作失败，数据库暂时不可用。"

    bot_say(reply)
    return True


def nickname(account):
    return users_db[account]["nickname"]


def get_real_ip(req):
    forwarded = req.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    return forwarded or req.remote_addr
