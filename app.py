import os
import sys
import json
import time
import requests
import html
import re
import base64
import io
import hashlib
import uuid
import urllib.parse
import threading
from PIL import Image
from collections import OrderedDict


# 日志里有 emoji，Windows 中文控制台默认的 GBK 编码打印不出来会直接崩溃
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


class LimitedDict(OrderedDict):
    def __init__(self, max_size=1000, ttl_seconds=3600):
        super().__init__()
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds

    def __setitem__(self, key, value):
        super().__setitem__(key, (value, time.time()))
        if len(self) > self.max_size:
            self.popitem(last=False)

    def __getitem__(self, key):
        value, timestamp = super().__getitem__(key)
        if time.time() - timestamp > self.ttl_seconds:
            del self[key]
            raise KeyError(key)
        return value

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def __contains__(self, key):
        try:
            self[key]
            return True
        except KeyError:
            return False


from flask import (
    Flask,
    request,
    jsonify,
    render_template_string,
    redirect,
    url_for,
    make_response,
    send_file,
    session,
    Response,
    stream_with_context,
)
import datetime
from datetime import timezone, timedelta

TZ_UTC8 = timezone(timedelta(hours=8))

from werkzeug.middleware.proxy_fix import ProxyFix
from flask_compress import Compress

app = Flask(__name__)

app.config["MAX_CONTENT_LENGTH"] = 15 * 1024 * 1024

app.config["COMPRESS_ALGORITHM"] = "gzip"
app.config["COMPRESS_MIN_SIZE"] = 100
app.config["COMPRESS_MIMETYPES"] = [
    "text/html",
    "application/json",
    "text/css",
    "application/javascript",
]
Compress(app)

app.permanent_session_lifetime = datetime.timedelta(days=30)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

import hmac
import ipaddress
import secrets
import emoji
from pymongo import MongoClient, ReturnDocument
from werkzeug.security import check_password_hash, generate_password_hash

MONGO_URI = os.environ.get("MONGO_URI", "")

# MongoClient 是惰性连接，这里不会报错；真正连不上时由各处读写自行处理
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = client["nokia_qq_db"]
users_collection = db["users"]
chat_collection = db["chat_history"]
images_collection = db["images"]
files_collection = db["files"]
settings_collection = db["settings"]


def load_secret_key():
    """会话密钥无需手动配置：首次启动自动生成并存入 MongoDB，之后每次启动复用。"""
    env_key = os.environ.get("FLASK_SECRET_KEY")
    if env_key:
        return env_key
    try:
        doc = settings_collection.find_one_and_update(
            {"_id": "flask_secret_key"},
            {"$setOnInsert": {"value": secrets.token_hex(32)}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return doc["value"]
    except Exception as e:
        print(f"读取会话密钥失败，本次启动使用临时密钥(重启后需重新登录): {e}")
        return secrets.token_hex(32)


app.secret_key = load_secret_key()


def load_users():
    """在应用启动时，从 MongoDB 把所有账号数据拉取到内存字典中"""
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

app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

def filter_emoji(text):
    if not text:
        return text

    text = emoji.demojize(text, language="zh")
    text = re.sub(r":([^:]+):", r"[\1]", text)

    text = re.sub(r"[\U00010000-\U0010ffff\u2600-\u27BF]", "", text)
    text = re.sub(r"[\ud800-\udfff]", "", text)

    return text


QQ_FACES = {}


def load_qq_faces():
    global QQ_FACES
    try:
        face_json_path = os.path.join(os.path.dirname(__file__), "face_config.json")
        with open(face_json_path, "r", encoding="utf-8") as f:
            face_data = json.load(f)

            for face in face_data.get("sysface", []):
                face_id = face.get("QSid")
                face_name = face.get("QDes", "")

                if face_id is not None and face_name:
                    clean_name = face_name.lstrip("/")
                    QQ_FACES[int(face_id)] = clean_name

        print(f"✅ 成功从 face_config.json 加载了 {len(QQ_FACES)} 个 QQ 原生表情！")
    except Exception as e:
        print(f"❌ 加载 face_config.json 失败，将使用兜底字典: {e}")
        QQ_FACES.update({14: "微笑", 13: "呲牙", 76: "强", 285: "汗"})


load_qq_faces()

KAOMOJI_LIST = []


def load_kaomojis():
    global KAOMOJI_LIST
    try:
        kmj_path = os.path.join(os.path.dirname(__file__), "all_output_result_kmj.txt")
        if os.path.exists(kmj_path):
            with open(kmj_path, "r", encoding="utf-8") as f:
                for line in f:
                    parts = line.strip().split("\t")
                    if parts and len(parts) >= 1:
                        kmj = parts[0]
                        if kmj and kmj not in KAOMOJI_LIST:
                            KAOMOJI_LIST.append(kmj)
            print(f"✅ 成功加载了 {len(KAOMOJI_LIST)} 个颜文字！")
        else:
            print("❌ 未找到 all_output_result_kmj.txt 文件，使用兜底颜文字。")
    except Exception as e:
        print(f"❌ 加载颜文字文件失败: {e}")

    if not KAOMOJI_LIST:
        KAOMOJI_LIST = ["(￣▽￣)", "(⊙_⊙)", "(T_T)", "╮(╯▽╰)╭", "(#`O′)"]


load_kaomojis()


def load_chat_history():
    """应用启动时，从 MongoDB 读取最新的 50 条聊天记录"""
    history = []
    max_id = 0
    try:
        docs = list(chat_collection.find({}, {"_id": 0}).sort("id", -1).limit(50))
        docs.reverse()
        history = docs
        if history:
            max_id = history[-1]["id"]
    except Exception as e:
        print(f"读取云端聊天记录失败: {e}")
    return history, max_id


def save_chat_message(msg_dict):
    """将单条消息存入 MongoDB，并自动清理旧数据防止撑爆免费容量"""
    try:
        chat_collection.insert_one(msg_dict.copy())
        if msg_dict["id"] > 5000:
            chat_collection.delete_many({"id": {"$lte": msg_dict["id"] - 5000}})
    except Exception as e:
        print(f"写入云端聊天记录失败: {e}")


def cleanup_old_images(max_limit=500):
    """
    检查图床容量，如果超过 max_limit，则清理最老旧的图片。
    这里我们将默认上限改小为 500 张。
    """
    try:
        img_count = images_collection.count_documents({})
        if img_count > max_limit:
            delete_count = img_count - max_limit
            old_images = (
                images_collection.find({}, {"_id": 1})
                .sort("time", 1)
                .limit(delete_count)
            )
            old_ids = [img["_id"] for img in old_images]

            if old_ids:
                images_collection.delete_many({"_id": {"$in": old_ids}})
                print(
                    f"[自动清理] 图床超过 {max_limit} 张，已自动清理 {len(old_ids)} 张远古老图！"
                )
    except Exception as e:
        print(f"图床自动清理失败: {e}")


chat_history, current_msg_id = load_chat_history()

TARGET_GROUP_ID = int(os.environ.get("TARGET_GROUP_ID", "0"))

qq_name_cache = LimitedDict(1000)


def get_qq_name(group_id, qq):
    qq_str = str(qq)
    if qq_str in qq_name_cache:
        return qq_name_cache[qq_str]
    try:
        resp = requests.post(
            "http://127.0.0.1:3000/get_group_member_info",
            json={"group_id": group_id, "user_id": int(qq)},
            timeout=1.5,
        ).json()

        if resp and resp.get("status") in ["ok", "success"]:
            data = resp.get("data", {})
            name = data.get("card") or data.get("nickname") or qq_str

            name = filter_emoji(name)
            name = re.sub(r"\[.*?\]", "", name).strip()
            if not name:
                name = qq_str

            qq_name_cache[qq_str] = name
            return name
    except Exception as e:
        print(f"获取QQ名字失败: {e}")
    return qq_str


recent_sent_messages = []


def get_qq_by_name(target_name):
    if not target_name:
        return None

    if target_name in ["全体成员", "all", "全体"]:
        return "all"

    if target_name in name_to_qq_cache:
        return name_to_qq_cache[target_name]

    try:
        resp = requests.post(
            "http://127.0.0.1:3000/get_group_member_list",
            json={"group_id": TARGET_GROUP_ID},
            timeout=2,
        ).json()

        if resp and resp.get("status") in ["ok", "success"]:
            members = resp.get("data", [])
            found_uid = None
            for member in members:
                uid = str(member.get("user_id", ""))
                card = member.get("card", "")
                nickname = member.get("nickname", "")

                clean_card = (
                    re.sub(r"\[.*?\]", "", filter_emoji(card)).strip() if card else ""
                )
                clean_nick = (
                    re.sub(r"\[.*?\]", "", filter_emoji(nickname)).strip()
                    if nickname
                    else ""
                )

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


processed_notices_cache = LimitedDict(1000, 3600)

APP_START_TIME = int(time.time())

internal_file_cache = LimitedDict(100, 300)
recent_web_uploads = LimitedDict(100, 300)


@app.route("/api/internal_download/<file_id>")
def internal_download(file_id):
    client_ip = request.remote_addr or "127.0.0.1"
    if client_ip != "127.0.0.1":
        return "Forbidden", 403

    file_data = internal_file_cache.get(file_id)
    if not file_data:
        return "File not found or expired", 404

    return send_file(
        io.BytesIO(file_data["bytes"]),
        download_name=file_data["name"],
        as_attachment=True,
    )


name_to_qq_cache = LimitedDict(2000)

COOLDOWN_SECONDS = 5
MAX_MSG_LENGTH = 80
GLOBAL_COOLDOWN = 2
global_last_send_time = 0

ip_last_send_time = LimitedDict(1000)
ip_location_cache = LimitedDict(1000)
ip_last_message = LimitedDict(1000)

online_sessions = {}
ONLINE_TIMEOUT = 30


def get_online_count():
    """获取当前在线人数，并自动清理已经掉线的死记录"""
    current_time = time.time()
    expired_accounts = [
        acc
        for acc, last_active in online_sessions.items()
        if current_time - last_active > ONLINE_TIMEOUT
    ]
    for acc in expired_accounts:
        del online_sessions[acc]
    return len(online_sessions)


def load_banned_words():
    try:
        with open("sensitive_words.txt", "r", encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]
    except Exception as e:
        print(f"Failed to load sensitive_words.txt: {e}")
        return []


BANNED_WORDS = load_banned_words()

MAX_TOTAL_ACCOUNTS = 500
MAX_ACCOUNTS_PER_IP = 1
MAX_PENDING_ACCOUNTS = 20
REGISTER_COOLDOWN = 30
MAX_REASON_LENGTH = 60
FILES_TOTAL_LIMIT = 250 * 1024 * 1024
last_register_time = 0

MAX_LOGIN_FAILURES = 5
login_failures = LimitedDict(1000, 300)

chat_lock = threading.Lock()


def get_active_account():
    """返回当前已登录且已通过审核的账号名，否则返回 None。"""
    account = session.get("nokia_account")
    user = users_db.get(account) if account else None
    if not user or user.get("status") == "pending":
        return None
    return account


def add_chat_message(title, text, pure_sender, pure_text, **extra):
    """生成一条聊天室消息：分配编号和时间，写入内存与数据库，内存里只保留最近 50 条。"""
    global current_msg_id
    with chat_lock:
        current_msg_id += 1
        now = datetime.datetime.now(TZ_UTC8).strftime("%H:%M:%S")
        new_msg = {
            "id": current_msg_id,
            "sender_title": f"{title} {now}",
            "text": text,
            "pure_sender": pure_sender,
            "pure_text": pure_text,
            **extra,
        }
        chat_history.append(new_msg)
        if len(chat_history) > 50:
            chat_history.pop(0)
    save_chat_message(new_msg)
    return new_msg


def highlight_mentions(safe_text):
    """把已转义文本里的 [@某人] 和 @某人 标成高亮。"""
    safe_text = re.sub(
        r"\[@([^\]]+)\]", r'<span class="action-reply">[@\1]</span>', safe_text
    )
    return re.sub(
        r"(?<![\w.-])@([\w一-龥·_-]+)",
        r'<span class="action-reply">@\1</span>',
        safe_text,
    )


def remember_echo(text):
    """登记机器人即将发到群里的文字，Webhook 收到同样内容时不再转回网页。"""
    recent_sent_messages.append(re.sub(r"\s+", "", text))
    if len(recent_sent_messages) > 50:
        recent_sent_messages.pop(0)


def clean_nickname(raw):
    """校验并清洗昵称，返回 (昵称, 错误提示)。方括号一律去掉，防止昵称被当成 CQ 码执行。"""
    name = re.sub(r"\[.*?\]", "", filter_emoji((raw or "").strip()))
    name = re.sub(r"[\[\]\s]+", " ", name).strip()[:12].strip()
    if not name:
        return None, "昵称不能为空或全为非法字符！"
    clean = re.sub(r"[^\w一-龥]", "", name).lower()
    if any(w.lower() in clean for w in BANNED_WORDS):
        return None, "昵称包含违规词汇！"
    return name, None


def set_nickname(account, raw):
    """修改昵称，返回 (新昵称, 错误提示)。"""
    name, error = clean_nickname(raw)
    if error:
        return None, error
    users_db[account]["nickname"] = name
    try:
        users_collection.update_one({"_id": account}, {"$set": {"nickname": name}})
    except Exception as e:
        print(f"修改昵称同步云端单用户失败: {e}")
    return name, None


def sign_url(url):
    """给服务器自己生成的外部下载地址签名，下载接口只认带正确签名的地址。"""
    return hmac.new(
        app.secret_key.encode("utf-8"), url.encode("utf-8"), "sha256"
    ).hexdigest()[:32]


def try_login(account, password):
    """校验登录并写入会话。成功返回 None，失败返回错误提示。"""
    if login_failures.get(account, 0) >= MAX_LOGIN_FAILURES:
        return "失败次数过多，请5分钟后再试！"

    if not verify_password(account, password):
        login_failures[account] = login_failures.get(account, 0) + 1
        return "账号或密码错误！"

    if users_db[account].get("status") == "pending":
        return "账号正在审核中，请等待群管理员通过！"

    login_failures.pop(account, None)
    session.permanent = True
    session["nokia_account"] = account
    return None


def hash_password(password):
    return generate_password_hash(password, method="pbkdf2:sha256")


def verify_password(account, password):
    """校验密码。旧版本明文存储的密码在首次登录成功后自动升级为哈希。"""
    user = users_db.get(account)
    if not user:
        return False
    stored = user.get("password") or ""

    if stored.startswith("pbkdf2:"):
        return check_password_hash(stored, password)

    if not stored or not hmac.compare_digest(
        stored.encode("utf-8"), password.encode("utf-8")
    ):
        return False

    new_hash = hash_password(password)
    user["password"] = new_hash
    try:
        users_collection.update_one({"_id": account}, {"$set": {"password": new_hash}})
    except Exception as e:
        print(f"升级密码哈希失败: {e}")
    return True


def bot_say(text):
    """让机器人在目标群里发一条文字，并登记回声以免被当成群消息再转回网页。"""
    remember_echo(text)
    try:
        requests.post(
            "http://127.0.0.1:3000/send_group_msg",
            json={
                "group_id": TARGET_GROUP_ID,
                "message": [{"type": "text", "data": {"text": text}}],
            },
            timeout=5,
        )
        return True
    except Exception as e:
        print(f"机器人发送群消息失败: {e}")
        return False


def register_account(account, password, reason, req):
    """注册一个待审核账号，并在 QQ 群里通知群主/管理员审批。返回 (是否成功, 提示)。"""
    global last_register_time

    reason = re.sub(r"\s+", " ", filter_emoji(reason or "")).strip()
    if not reason:
        return False, "注册请填写申请理由！"
    if len(reason) > MAX_REASON_LENGTH:
        return False, f"申请理由不能超过{MAX_REASON_LENGTH}字！"

    clean_reason = re.sub(r"[^\w一-龥]", "", reason).lower()
    if any(w.lower() in clean_reason for w in BANNED_WORDS):
        return False, "申请理由包含违规词汇！"

    if len(users_db) >= MAX_TOTAL_ACCOUNTS:
        return False, "服务器名额已满，暂停注册！"

    if account in users_db:
        return False, "账号已存在，请直接登录！"

    client_ip = get_real_ip(req)
    ip_reg_count = sum(1 for u in users_db.values() if u.get("ip") == client_ip)
    if ip_reg_count >= MAX_ACCOUNTS_PER_IP:
        return False, "您的IP已达到注册上限！"

    pending_count = sum(1 for u in users_db.values() if u.get("status") == "pending")
    if pending_count >= MAX_PENDING_ACCOUNTS:
        return False, "待审核人数过多，请稍后再试！"

    if time.time() - last_register_time < REGISTER_COOLDOWN:
        return False, "注册过于频繁，请稍后再试！"
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
        return False, "数据库暂时不可用，请稍后再试！"
    users_db[account] = user_data

    bot_say(
        f"[注册申请] 账号: {account}\n"
        f"来自: {location}\n"
        f"理由: {reason}\n"
        f"群主/管理员回复「通过 {account}」或「拒绝 {account}」"
    )
    return True, "申请已提交，请等待群管理员审核！"


ADMIN_COMMAND_RE = re.compile(
    r"^(通过|拒绝|删号|待审核|删文件)(?:\s+(.+))?$", flags=re.DOTALL
)


def is_group_manager(data):
    """消息发送者是否为目标群的群主或管理员。"""
    sender = data.get("sender", {}) or {}
    role = sender.get("role")
    if not role:
        try:
            resp = requests.post(
                "http://127.0.0.1:3000/get_group_member_info",
                json={
                    "group_id": TARGET_GROUP_ID,
                    "user_id": int(sender.get("user_id", 0)),
                    "no_cache": True,
                },
                timeout=2,
            ).json()
            role = (resp.get("data") or {}).get("role")
        except Exception as e:
            print(f"查询群成员身份失败: {e}")
    return role in ("owner", "admin")


def handle_admin_command(data):
    """处理群主/管理员在 QQ 群里发的审批指令。返回 True 表示这条消息是指令，已处理。"""
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

    # 不带参数、或参数不像账号名的，当作普通聊天，避免误触发
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
    if not re.match(r"^[A-Za-z0-9_]{3,15}$", account):
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


def cleanup_old_files(max_total=FILES_TOTAL_LIMIT):
    """网盘文件直接存在 MongoDB 里，按总容量上限从最旧的开始清理，防止撑爆免费额度。"""
    try:
        total = 0
        old_ids = []
        for doc in files_collection.find({}, {"_id": 1, "size": 1}).sort("time", -1):
            total += doc.get("size", 0) or 0
            if total > max_total:
                old_ids.append(doc["_id"])
        if old_ids:
            files_collection.delete_many({"_id": {"$in": old_ids}})
            print(f"[自动清理] 网盘超过容量上限，已清理 {len(old_ids)} 个最旧的文件。")
    except Exception as e:
        print(f"网盘自动清理失败: {e}")


def send_web_message(account, raw_text, req):
    """把网页/WML 用户的一条发言转发到 QQ 群并记入聊天室。成功返回 None，被拦截返回提示文字。"""
    global global_last_send_time

    msg_text = filter_emoji((raw_text or "").strip())
    if not msg_text:
        return None

    username = users_db[account].get("nickname", account)
    client_ip = get_real_ip(req)
    current_time = time.time()
    clean_msg = re.sub(r"[^\w一-龥]", "", msg_text).lower()

    intercept_msg = None
    if len(msg_text) > MAX_MSG_LENGTH:
        intercept_msg = f"[系统拦截]：消息过长，最多允许 {MAX_MSG_LENGTH} 个字符。"
    elif "[CQ:" in msg_text:
        intercept_msg = "[系统拦截]：禁止发送带有特殊控制符的代码！"
    elif any(w.lower() in clean_msg for w in BANNED_WORDS):
        intercept_msg = "[系统拦截]：发送失败，您的消息包含违规词汇！"
    elif msg_text == ip_last_message.get(client_ip, ""):
        intercept_msg = "[系统拦截]：请勿连续发送完全相同的内容。"
    elif current_time - ip_last_send_time.get(client_ip, 0) < COOLDOWN_SECONDS:
        intercept_msg = f"[系统拦截]：发送太快啦，请等待 {COOLDOWN_SECONDS} 秒后再试！"
    elif current_time - global_last_send_time < GLOBAL_COOLDOWN:
        intercept_msg = "[系统拦截]：当前网页端发送人数过多，通道拥挤，请稍后再试。"

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
    reply_cq_code = ""
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
                reply_cq_code = f"[CQ:reply,id={target_qq_msg_id}]"
            else:
                reply_prefix_qq = f"[回复 @{t_sender}: {t_text}]\n"

            reply_prefix_web = f'<span class="reply-text">[回复 @{html.escape(t_sender)}: {html.escape(t_text)}]</span><br/>'

    # 每个被 @ 的名字只查一次：QQ 侧换成真正的 @，回声侧换成对方的群名片
    mention_pattern = r"\[@([^\]]+)\]|(?<![\w.-])@([\w一-龥·_-]+)"
    mentions = {}
    for match in re.finditer(mention_pattern, clean_text):
        name = match.group(1) or match.group(2)
        if name not in mentions:
            mentions[name] = get_qq_by_name(name)

    def replace_at(match):
        qq_uid = mentions.get(match.group(1) or match.group(2))
        return f"[CQ:at,qq={qq_uid}] " if qq_uid else match.group(0)

    def replace_at_for_echo(match):
        qq_uid = mentions.get(match.group(1) or match.group(2))
        if not qq_uid:
            return match.group(0)
        if qq_uid == "all":
            return "[@全体成员] "
        return f"[@{get_qq_name(TARGET_GROUP_ID, qq_uid)}] "

    qq_clean_text = re.sub(mention_pattern, replace_at, clean_text)
    echo_clean_text = re.sub(mention_pattern, replace_at_for_echo, clean_text)

    location = get_ip_location(client_ip, req)
    full_message = (
        f"{reply_cq_code}{reply_prefix_qq}[{location} - {username}] {qq_clean_text}"
    )
    remember_echo(f"{reply_prefix_qq}[{location} - {username}] {echo_clean_text}")

    try:
        resp = requests.post(
            "http://127.0.0.1:3000/send_group_msg",
            json={"group_id": TARGET_GROUP_ID, "message": full_message},
            timeout=3,
        ).json()
    except Exception as e:
        print(f"发送到QQ失败: {e}")
        return "[系统异常]：连接 QQ 机器人后端失败。"

    qq_msg_id = None
    if resp and resp.get("status") in ["ok", "success"]:
        qq_msg_id = (resp.get("data") or {}).get("message_id")

    safe_text = highlight_mentions(html.escape(clean_text).replace("\n", "<br/>"))
    add_chat_message(
        "消息",
        f"{reply_prefix_web}[网页-{location}]{html.escape(username)}: {safe_text}",
        username,
        clean_text,
        qq_msg_id=qq_msg_id,
    )
    return None


LOGIN_WML = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE wml PUBLIC "-//WAPFORUM//DTD WML 1.1//EN" "http://www.wapforum.org/DTD/wml_1.1.xml">
<wml>
<head><meta http-equiv="Cache-Control" content="max-age=0"/></head>
<card id="login" title="登录"><p align="left"><b>WapChatQQ</b>{%if error%}<b>[{{error}}]</b>{%endif%}{%if success%}<b>[{{success}}]</b>{%endif%}<br/>账号:<input name="account" type="text" emptyok="false" format="*M"/><br/>密码:<input name="password" type="password" emptyok="false"/><br/>理由(注册才填):<input name="reason" type="text" emptyok="true" maxlength="60"/><br/><anchor>[登录]<go href="login" method="post"><postfield name="account" value="$(account)"/><postfield name="password" value="$(password)"/><postfield name="login_btn" value="1"/></go></anchor> <anchor>[注册]<go href="login" method="post"><postfield name="account" value="$(account)"/><postfield name="password" value="$(password)"/><postfield name="reason" value="$(reason)"/><postfield name="register_btn" value="1"/></go></anchor></p></card>
</wml>
"""

NOKIA_WML = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE wml PUBLIC "-//WAPFORUM//DTD WML 1.1//EN" "http://www.wapforum.org/DTD/wml_1.1.xml">
<wml>
    <head>
        <meta http-equiv="Cache-Control" content="max-age=0"/>
    </head>
    
    <card id="chat" title="WapChat QQ">
        <onevent type="onenterforward">
            <refresh>
                <setvar name="message" value=""/>
            </refresh>
        </onevent>
        <p>
            <b>WapChatQQ({{ online_count }}在线)</b> <a href="#options">[选项]</a><br/>
            --------<br/>
            {% if notice %}{{ notice }}<br/>{% endif %}
            发言:<input name="message" type="text" emptyok="true" format="*M"/><br/>
            <anchor>[发送]
                <go href="./" method="post">
                    <postfield name="message" value="$(message)"/>
                </go>
            </anchor>
            <anchor>[刷新]
                <go href="./" method="post">
                    <postfield name="r" value="1"/>
                </go>
            </anchor><br/>
            --------<br/>
            {% if not history %}暂无消息<br/>{% endif %}
            {% for msg in history %}
                <b>{{ msg.get('sender_title', 'QQ群') }}</b><br/>
                {{ msg.wml_text | safe }}<br/>
            {% endfor %}
        </p>
    </card>

    <card id="options" title="选项菜单">
        <p>
            <a href="#rename">改名</a><br/>
            <a href="logout">注销</a><br/>
            <a href="#chat">返回聊天</a>
        </p>
    </card>

    <card id="rename" title="修改昵称">
        <p>
            当前昵称: {{ saved_username }}<br/>
            新昵称:<input name="new_name" type="text" emptyok="false"/><br/>
            <anchor>[确认保存]
                <go href="rename_wml" method="post">
                    <postfield name="new_name" value="$(new_name)"/>
                </go>
            </anchor><br/>
            <a href="#options">返回选项</a>
        </p>
    </card>
</wml>
"""

NOKIA_HTML = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html PUBLIC "-//WAPFORUM//DTD XHTML Mobile 1.0//EN" "http://www.wapforum.org/DTD/xhtml-mobile10.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
    <meta http-equiv="Content-Type" content="application/xhtml+xml; charset=utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
    <title>WapChat QQ</title>
    <style type="text/css">
/* <![CDATA[ */
        * { -webkit-box-sizing: border-box; -moz-box-sizing: border-box; box-sizing: border-box; }
        body { margin: 0; padding: 0; background-color: #ffffff; color: #000000; font-family: sans-serif; font-size: 13px; }
        h2 { font-size: 14px; margin: 0; padding: 2px 6px; border-bottom: 2px solid #000; background-color: #f5f5f5; line-height: 18px; }
        #chat { padding: 6px; }
        .msg { margin-bottom: 4px; border-bottom: 1px dashed #cccccc; padding: 0 0 4px 0; font-size: 12px; line-height: 1.3; word-wrap: break-word; }
        .user-msg { color: #000000; font-weight: bold; }
        .ai-msg { color: #333333; }
        .input-area { padding: 6px; background-color: #f5f5f5; border-bottom: 2px solid #000; }
        #userInput { width: 100%; height: 27px; padding: 2px 5px; font-size: 13px; border: 1px solid #000; vertical-align: middle; display: block; border-radius: 0; box-sizing: border-box; -webkit-box-sizing: border-box; }
        #sendBtn { width: 100%; height: 27px; border: 1px solid #000; background-color: #dddddd; color: #000; font-weight: bold; font-size: 13px; vertical-align: middle; cursor: pointer; display: block; border-radius: 0; box-sizing: border-box; -webkit-box-sizing: border-box; -webkit-appearance: none; }
        #sendBtn:active { background-color: #999999; color: #fff; }
        #attachBtn { width: 100%; height: 27px; border: 1px solid #000; background-color: #eee; color: #000; font-weight: bold; font-size: 13px; padding: 0; margin: 0; display: block; border-radius: 0; box-sizing: border-box; -webkit-box-sizing: border-box; -webkit-appearance: none; }
        .action-at { color: blue; text-decoration: none; }
        .action-reply { color: #ff6600; text-decoration: none; }
        .reply-text { color: #ff6600; font-size: 11px; }
        .nav-links { float: right; font-size: 12px; font-weight: normal; }
        .nav-links a { color: blue; text-decoration: none; margin-left: 8px; }
    /* ]]> */
    </style>
    {% if theme == 'dark' %}
    <style type="text/css">
/* <![CDATA[ */
        body { background-color: #000000 !important; color: #aaaaaa !important; }
        h2 { border-bottom: 2px solid #555555 !important; background-color: #111111 !important; color: #dddddd !important; }
        .input-area { background-color: #111111 !important; border-bottom: 2px solid #555555 !important; }
        .msg { border-bottom: 1px dashed #333333 !important; }
        .user-msg { color: #dddddd !important; }
        .ai-msg { color: #888888 !important; }
        #userInput { background-color: #222222 !important; color: #ffffff !important; border-color: #555555 !important; }
        #sendBtn, #attachBtn { background-color: #333333 !important; color: #dddddd !important; border-color: #555555 !important; }
        #optionsMenu, #attachMenu { background: #111111 !important; border-color: #555555 !important; }
        #optionsMenu a, #attachMenu > div { border-bottom-color: #555555 !important; }
        #attachMenu { color: #dddddd !important; }
        .nav-links a { color: blue !important; }
        .action-at { color: blue !important; }
        .action-reply { color: #ff6600 !important; }
        .reply-text { color: #ff6600 !important; }
    /* ]]> */
    </style>
    {% endif %}
</head>
<body>
    <h2>
        <span class="nav-links">
            <a href="javascript:toggleMenu();">[选项▼]</a>
        </span>
        WapChat QQ 
        <a id="onlineCount" href="/online" style="font-size:12px; font-weight:normal; color:green; text-decoration:underline;">({{ online_count }}在线)</a>
    </h2>
    
    <div id="optionsMenu" style="display:none; position:absolute; top:30px; right:6px; width:50px; background:#fafafa; border:1px solid #000; padding:0; z-index:999; text-align:center;">
        <a href="/toggle_theme" style="display:block; font-size:12px; color:#888; text-decoration:none; border-bottom:1px solid #000; padding:3px 0; margin:0;">{% if theme == 'dark' %}日间{% else %}夜间{% endif %}</a>
        <a href="/history" style="display:block; font-size:12px; color:#ff6600; text-decoration:none; border-bottom:1px solid #000; padding:3px 0; margin:0;">历史</a>
        <a href="/drive" style="display:block; font-size:12px; color:green; text-decoration:none; border-bottom:1px solid #000; padding:3px 0; margin:0;">网盘</a>
        <a href="{{ ai_url }}" style="display:block; font-size:12px; color:purple; text-decoration:none; border-bottom:1px solid #000; padding:3px 0; margin:0;">智能</a>
        <a href="javascript:changeName(); toggleMenu();" style="display:block; font-size:12px; color:blue; text-decoration:none; border-bottom:1px solid #000; padding:3px 0; margin:0;">改名</a>
        <a href="/logout" style="display:block; font-size:12px; color:red; text-decoration:none; padding:3px 0; margin:0;">注销</a>
    </div>
    
    <div id="attachMenu" style="display:none; position:absolute; top:65px; left:0; width:50px; background:#fafafa; border:1px solid #000; padding:0; z-index:999; text-align:center;">
        <div style="position:relative; width:100%; border-bottom:1px solid #000; overflow:hidden;">
            <a href="javascript:goToEmoji();" style="display:block; font-size:12px; color:#ff00ff; text-decoration:none; padding:3px 0; margin:0;">颜文字</a>
        </div>
        <div style="position:relative; width:100%; border-bottom:1px solid #000; overflow:hidden;">
            <div style="display:block; font-size:12px; color:blue; padding:3px 0; margin:0;">图片</div>
            <form id="uploadForm" action="/api/upload_image" method="POST" enctype="multipart/form-data" target="uploadTarget" style="position:absolute; top:0; left:0; width:100%; height:100%; margin:0; padding:0;">
                <input type="file" name="image" accept="image/*" onchange="handleAutoUpload('uploadForm', '图片');" style="position:absolute; top:0; left:0; width:100%; height:100%; opacity:0.01; filter:alpha(opacity=1); cursor:pointer; font-size:50px; outline:none;" />
            </form>
        </div>
        <div style="position:relative; width:100%; border-bottom:1px solid #000; overflow:hidden;">
            <div style="display:block; font-size:12px; color:green; padding:3px 0; margin:0;">文件</div>
            <form id="uploadFileForm" action="/api/upload_file" method="POST" enctype="multipart/form-data" target="uploadTarget" style="position:absolute; top:0; left:0; width:100%; height:100%; margin:0; padding:0;">
                <input type="file" name="file" onchange="handleAutoUpload('uploadFileForm', '文件');" style="position:absolute; top:0; left:0; width:100%; height:100%; opacity:0.01; filter:alpha(opacity=1); cursor:pointer; font-size:50px; outline:none;" />
            </form>
        </div>
        <iframe id="uploadTarget" name="uploadTarget" style="display:none;"></iframe>
    </div>

    <div class="input-area">
        <form onsubmit="sendMessage(); return false;">
            <table style="width: 100%; table-layout: fixed;" cellspacing="0" cellpadding="0" border="0">
                <tr>
                    <td style="width: 66%; padding-right: 2px; vertical-align: middle;">
                        <input type="text" id="userInput" placeholder="在此输入..." spellcheck="false" autocorrect="off" autocapitalize="off" autocomplete="off" />
                    </td>
                    <td style="width: 10%; padding-right: 2px; vertical-align: middle;">
                        <input type="button" id="attachBtn" value="▼" onclick="toggleAttachMenu();" />
                    </td>
                    <td style="width: 24%; vertical-align: middle;">
                        <input type="button" id="sendBtn" value="发送" onclick="sendMessage();" />
                    </td>
                </tr>
            </table>
        </form>
    </div>

    <div id="chat">
        {% for msg in history|reverse %}
        <div class="msg ai-msg" id="msg_{{ msg.id }}">
            <div style="overflow:hidden; margin-bottom:2px;">
                <b style="float:left;">{{ msg.get('sender_title', 'QQ群') }}:</b>
                {% if msg.pure_sender %}
                <span style="float:right;">
                    <a href="javascript:atUser('{{ msg.pure_sender | replace("'", "") | replace('"', '') }}');" class="action-at">[@]</a>
                    <a href="javascript:replyMsg('{{ msg.id }}', '{{ msg.pure_sender | replace("'", "") | replace('"', '') }}');" class="action-reply">[回]</a>
                </span>
                {% endif %}
            </div>
            <div style="clear:both;">{{ msg.text | safe }}</div>
        </div>
        {% endfor %}
        <div class="msg ai-msg"><b>系统:</b><br/>连接就绪，正在监听 QQ 群消息...</div>
    </div>

    <a name="bottom" id="bottom"></a>

    <script type="text/javascript">
/* <![CDATA[ */
        if (typeof XMLHttpRequest === "undefined") {
            window.XMLHttpRequest = function () {
                try { return new ActiveXObject("Msxml2.XMLHTTP.6.0"); } catch (e) {}
                try { return new ActiveXObject("Msxml2.XMLHTTP.3.0"); } catch (e) {}
                try { return new ActiveXObject("Microsoft.XMLHTTP"); } catch (e) {}
                throw new Error("此浏览器不支持 AJAX");
            };
        }
        if (!window.JSON) {
            window.JSON = {
                parse: function (sJSON) { try { return eval('(' + sJSON + ')'); } catch (e) { return null; } },
                stringify: function (v) {
                    var i, s = "", len;
                    if (v === null) return "null";
                    if (typeof v === 'number' || typeof v === 'boolean') return v.toString();
                    if (typeof v === 'string') return '"' + v.replace(/"/g, '\\"') + '"';
                    if (v instanceof Array) {
                        len = v.length;
                        for (i = 0; i < len; i++) { s += (s ? "," : "") + this.stringify(v[i]); }
                        return "[" + s + "]";
                    }
                    if (typeof v === 'object') {
                        for (i in v) { if (v.hasOwnProperty(i)) { s += (s ? "," : "") + '"' + i + '":' + this.stringify(v[i]); } }
                        return "{" + s + "}";
                    }
                    return "";
                }
            };
        }

        var chatBox = document.getElementById('chat');
        var input = document.getElementById('userInput');
        var btn = document.getElementById('sendBtn');

        function atUser(name) {
            if (!name) return;
            input.value += '[@' + name + '] ';
            input.focus();
        }
        function replyMsg(id, name) {
            if (!id) return;
            var val = input.value.replace(/^\\[回复:\\d+\\]\\s*/, '');
            if (name && val.indexOf('[@' + name + ']') === -1) {
                val = '[@' + name + '] ' + val;
            }
            input.value = '[回复:' + id + '] ' + val;
            input.focus();
        }
        function toggleMenu() {
            var menu = document.getElementById('optionsMenu');
            if (menu.style.display === 'none' || menu.style.display === '') {
                menu.style.display = 'block';
            } else {
                menu.style.display = 'none';
            }
        }
        var myUsername = "{{ saved_username }}";
        function getCookie(name) {
            var arr, reg = new RegExp("(^| )" + name + "=([^;]*)(;|$)");
            if (arr = document.cookie.match(reg)) return decodeURIComponent(arr[2]);
            else return "";
        }
        function goToEmoji() {
            var currentText = document.getElementById('userInput').value;
            document.cookie = "nokia_draft=" + encodeURIComponent(currentText) + "; path=/";
            var w = window.innerWidth || document.documentElement.clientWidth || document.body.clientWidth || 240;
            var h = window.innerHeight || document.documentElement.clientHeight || document.body.clientHeight || 320;
            var cols = Math.floor(w / 80); 
            var rows = Math.floor((h - 90) / 32); 
            var limit = cols * rows;
            limit = Math.floor(limit * 0.8);
            if (limit < 8) limit = 8;
            if (limit > 500) limit = 500;
            document.cookie = "emoji_limit=" + limit + "; path=/";

            window.location.href = '/emojis';
        }
        var draftText = getCookie('nokia_draft');
        if (draftText !== "") {
            document.getElementById('userInput').value = draftText;
            document.cookie = "nokia_draft=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/;";
            document.getElementById('userInput').focus();
        }
        function getElementLeft(element) {
            var actualLeft = element.offsetLeft;
            var current = element.offsetParent;
            while (current !== null) {
                actualLeft += current.offsetLeft;
                current = current.offsetParent;
            }
            return actualLeft;
        }
        function toggleAttachMenu() {
            var menu = document.getElementById('attachMenu');
            var btn = document.getElementById('attachBtn');
            
            if (menu.style.display === 'none' || menu.style.display === '') {
                var btnWidth = btn.offsetWidth;
                var btnLeft = getElementLeft(btn);
                var finalWidth = btnWidth < 50 ? 50 : btnWidth;
                var finalLeft = Math.floor(btnLeft - ((finalWidth - btnWidth) / 2));
                menu.style.width = finalWidth + 'px';
                menu.style.left = finalLeft + 'px';
                menu.style.right = 'auto';
                
                menu.style.display = 'block';
            } else {
                menu.style.display = 'none';
            }
        }
        function handleAutoUpload(formId, typeName) {
            var form = document.getElementById(formId);
            var fileInput = form.getElementsByTagName('input')[0];
            if (!fileInput.value) return; 
            form.submit();
            toggleAttachMenu();
            addMessage('系统', typeName + '正在后台发送中，请稍候...', true);
        }
        function uploadCallback(status, msg) {
            var menu = document.getElementById('attachMenu');
            menu.style.display = 'none';
            var imgInput = document.getElementById('uploadForm').getElementsByTagName('input')[0];
            if(imgInput) imgInput.value = '';
            
            var fileInput = document.getElementById('uploadFileForm').getElementsByTagName('input')[0];
            if(fileInput) fileInput.value = '';
            
            if (status === 'ok') {
                addMessage('系统', '发送成功！', true);
                if (pollTimer) clearTimeout(pollTimer);
                currentInterval = baseInterval;
                doPoll();
            } else {
                addMessage('系统', '发送失败: ' + msg, true);
            }
        }
        function changeName() {
            var newName = prompt("请输入您的新昵称：\\n(此操作不会更改您的登录账号)", myUsername);
            if (newName !== null) {
                newName = newName.replace(/^\\s+|\\s+$/g, '');
                if (newName !== '') {
                    var xhr = new XMLHttpRequest();
                    xhr.open('POST', '/api/change_name?t=' + new Date().getTime(), true);
                    xhr.setRequestHeader('Content-Type', 'application/json');
                    xhr.onreadystatechange = function () {
                        if (xhr.readyState === 4) {
                            if (xhr.status === 200) {
                                var res = JSON.parse(xhr.responseText);
                                if (res.status === 'ok') {
                                    myUsername = res.new_name;
                                    addMessage('系统', '昵称已成功更改为: ' + myUsername, true);
                                } else {
                                    addMessage('系统', '改名失败: ' + res.msg, true);
                                }
                            } else {
                                addMessage('系统', '网络请求失败，请稍后重试', true);
                            }
                        }
                    };
                    xhr.send(JSON.stringify({ new_name: newName }));
                } else {
                    addMessage('系统', '昵称不能为空，取消修改。', true);
                }
            }
        }
        var lastMsgId = {{ last_id }}; 

        function addMessage(sender_title, text, isAi, pure_sender, msg_id) {
            var div = document.createElement('div');
            div.className = 'msg ' + (isAi ? 'ai-msg' : 'user-msg');
            if (msg_id !== undefined && msg_id !== null) {
                div.id = 'msg_' + msg_id;
            }
            
            var actionLinks = '';
            if (pure_sender && msg_id) {
                var safeName = pure_sender.replace(/['"]/g, '');
                var atLink = '<a href="javascript:atUser(' + "'" + safeName + "'" + ');" class="action-at">[@]</a>';
                var repLink = '&nbsp;<a href="javascript:replyMsg(' + "'" + msg_id + "'" + ', ' + "'" + safeName + "'" + ');" class="action-reply">[回]</a>';
                actionLinks = '<span style="float:right;">' + atLink + repLink + '</span>';
            }
            div.innerHTML = '<div style="overflow:hidden; margin-bottom:2px;"><b style="float:left;">' + sender_title + ':</b>' + actionLinks + '</div><div style="clear:both;">' + text.replace(/\\n/g, '<br/>') + '</div>';
            
            if (chatBox.firstChild) {
                chatBox.insertBefore(div, chatBox.firstChild);
            } else {
                chatBox.appendChild(div);
            }
            window.scrollTo(0, 0);

            while (chatBox.children.length > 26) { 
                chatBox.removeChild(chatBox.children[chatBox.children.length - 1]); 
            }
        }

        function sendMessage() {
            var text = input.value;
            if (text === null || text === '') return;
            
            input.value = '';
            btn.disabled = true;
            btn.value = '发送中';

            var xhr = new XMLHttpRequest();
            xhr.open('POST', '/api/sync?t=' + new Date().getTime(), true);
            xhr.setRequestHeader('Content-Type', 'application/json');

            xhr.onreadystatechange = function () {
                if (xhr.readyState === 4) {
                    btn.disabled = false;
                    btn.value = '发送';
                    if (xhr.status === 200) {
                        try {
                            var res = JSON.parse(xhr.responseText);
                            if (res.online_count !== undefined) {
                                var ocSpan = document.getElementById('onlineCount');
                                if (ocSpan) ocSpan.innerHTML = '(' + res.online_count + '在线)';
                            }
                            if (res.messages && res.messages.length > 0) {
                                for (var i = 0; i < res.messages.length; i++) {
                                    var m = res.messages[i];
                                    if (m.recall_target_id !== undefined && m.recall_target_id !== null) {
                                        var targetDiv = document.getElementById('msg_' + m.recall_target_id);
                                        if (targetDiv) {
                                            targetDiv.innerHTML = '<div style="color:#999; font-size:12px; margin-bottom:2px;"><i>[该消息已被撤回]</i></div>';
                                        }
                                    }
                                    if (m.id === undefined || m.id > lastMsgId) {
                                        var safeText = m.text.replace(/[\\uD800-\\uDBFF][\\uDC00-\\uDFFF]|[\\u2600-\\u27BF]/g, '');
                                        addMessage(m.sender_title, safeText, true, m.pure_sender, m.id);
                                        if (m.id !== undefined) {
                                            lastMsgId = m.id;
                                        }
                                    }
                                }
                            }
                            if (res.last_id !== undefined && res.last_id > lastMsgId) {
                                lastMsgId = res.last_id;
                            }
                        } catch (e) {
                            addMessage('系统', '解析数据失败', true);
                        }
                    } else {
                        addMessage('系统', '网络请求失败，状态码: ' + xhr.status, true);
                    }
                }
            };
            var requestData = JSON.stringify({ message: text, username: myUsername, last_id: lastMsgId });
            currentInterval = baseInterval;
            if (pollTimer) clearTimeout(pollTimer);
            pollTimer = setTimeout(doPoll, currentInterval);
            
            xhr.send(requestData);
        }

        var baseInterval = 5000;  
        var maxInterval = 25000;   
        var currentInterval = baseInterval;
        var pollTimer = null;
        var isPolling = false;

        function doPoll() {
            if (isPolling) return; 
            
            isPolling = true;
            var xhr = new XMLHttpRequest();
            xhr.open('POST', '/api/sync?t=' + new Date().getTime(), true);
            xhr.setRequestHeader('Content-Type', 'application/json');
            xhr.onreadystatechange = function () {
                if (xhr.readyState === 4) {
                    isPolling = false; 
                    var hasNewMessage = false; 

                    if (xhr.status === 200) {
                        try {
                            var res = JSON.parse(xhr.responseText);
                            if (res.online_count !== undefined) {
                                var ocSpan = document.getElementById('onlineCount');
                                if (ocSpan) ocSpan.innerHTML = '(' + res.online_count + '在线)';
                            }
                            if (res.messages && res.messages.length > 0) {
                                for (var i = 0; i < res.messages.length; i++) {
                                    var m = res.messages[i];
                                    if (m.recall_target_id !== undefined && m.recall_target_id !== null) {
                                        var targetDiv = document.getElementById('msg_' + m.recall_target_id);
                                        if (targetDiv) {
                                            targetDiv.innerHTML = '<div style="color:#999; font-size:12px; margin-bottom:2px;"><i>[该消息已被撤回]</i></div>';
                                        }
                                    }
                                    if (m.id === undefined || m.id > lastMsgId) {
                                        var safeText = m.text.replace(/[\\uD800-\\uDBFF][\\uDC00-\\uDFFF]|[\\u2600-\\u27BF]/g, '');
                                        addMessage(m.sender_title, safeText, true, m.pure_sender, m.id);
                                        if (m.id !== undefined) {
                                            lastMsgId = m.id;
                                            hasNewMessage = true; 
                                        }
                                    }
                                }
                            }
                            if (res.last_id !== undefined && res.last_id > lastMsgId) {
                                lastMsgId = res.last_id;
                            }
                        } catch (e) {}
                    }
                    if (hasNewMessage) {
                        currentInterval = baseInterval;
                    } else {
                        currentInterval = Math.min(currentInterval + 2000, maxInterval);
                    }
                    pollTimer = setTimeout(doPoll, currentInterval);
                }
            };
            xhr.send(JSON.stringify({ message: "", last_id: lastMsgId })); 
        }
        pollTimer = setTimeout(doPoll, currentInterval);
    /* ]]> */
</script>
</body>
</html>
"""

LOGIN_HTML = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html PUBLIC "-//WAPFORUM//DTD XHTML Mobile 1.0//EN" "http://www.wapforum.org/DTD/xhtml-mobile10.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
    <meta http-equiv="Content-Type" content="application/xhtml+xml; charset=utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
    <title>身份验证</title>
    <style type="text/css">
/* <![CDATA[ */
        
        * { -webkit-box-sizing: border-box; -moz-box-sizing: border-box; box-sizing: border-box; }
        body { margin: 0; padding: 0; background-color: #ffffff; color: #000000; font-family: sans-serif; font-size: 13px; }
        
        h2 { font-size: 14px; margin: 0; padding: 2px 6px; border-bottom: 2px solid #000; background-color: #f5f5f5; line-height: 18px; }
      
        .content { width: 95%; margin: 3px auto; }
     
        .msg { color: #d00; font-size: 12px; margin: 0 0 1px 0; font-weight: bold; line-height: 1.1; }
        
        label { font-size: 12px; font-weight: bold; display: block; margin-top: 2px; margin-bottom: 2px; }
        
        input[type="text"], input[type="password"] { width: 100%; height: 27px; border: 1px solid #000; margin: 0 0 5px 0; padding: 2px 5px; font-size: 13px; display: block; border-radius: 0; }
        input[type="submit"] { width: 100%; height: 27px; border: 1px solid #000; background-color: #dddddd; color: #000; font-weight: bold; font-size: 13px; margin-top: 8px; cursor: pointer; display: block; border-radius: 0; -webkit-appearance: none; }
        input[type="submit"]:active { background-color: #999999; color: #fff; }
    /* ]]> */
</style>
    {% if theme == 'dark' %}
    <style type="text/css">
/* <![CDATA[ */
        
        html, body { background-color: #000000 !important; color: #aaaaaa !important; }
        h2 { border-bottom: 2px solid #555555 !important; background-color: #111111 !important; color: #dddddd !important; }
       
        #chat, #history-content, #online-content { background: #111111 !important; border-color: #555555 !important; }
        
        .msg, .user-item { border-bottom: 1px dashed #333333 !important; }
        .user-msg { color: #dddddd !important; }
        .ai-msg { color: #888888 !important; }
        .nickname { color: #dddddd !important; }
        .account { color: #888888 !important; }
        
        input[type="text"], input[type="password"] { background-color: #111111 !important; color: #ffffff !important; border-color: #555555 !important; }
        input[type="button"], input[type="submit"] { background-color: #333333 !important; color: #dddddd !important; border-color: #555555 !important; }
        
        #attachBtn { background-color: #222222 !important; }
       
        #optionsMenu, #attachMenu { background: #111111 !important; border-color: #555555 !important; }
        #optionsMenu a, #attachMenu > div { border-bottom-color: #555555 !important; }
        #attachMenu { color: #dddddd !important; }
        
        .nav a { background: #333333 !important; color: blue !important; border-color: #555555 !important; }
       
        .action-at { color: blue !important; } 
      
        .action-reply { color: #ff6600 !important; } 
      
        .reply-text { color: #ff6600 !important; }
    /* ]]> */
</style>
    {% endif %}
</head>
<body>
    <h2>WapChat QQ</h2>
    <div class="content">
        {% if error %}<div class="msg">{{ error }}</div>{% endif %}
        {% if success %}<div class="msg" style="color: green;">{{ success }}</div>{% endif %}
        <form method="POST" action="/login">
            <label>登录账号(纯英数字):</label>
            <input type="text" name="account" required="required" spellcheck="false" autocapitalize="off" autocomplete="off" />
            <label>终端密码:</label>
            <input type="password" name="password" required="required" />
            <label>申请理由(仅注册时填写):</label>
            <input type="text" name="reason" maxlength="60" autocomplete="off" />
            <input type="submit" name="login_btn" value="登 录" />
            <input type="submit" name="register_btn" value="注册新账号" />
        </form>
    </div>
</body>
</html>
"""

HISTORY_HTML = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html PUBLIC "-//WAPFORUM//DTD XHTML Mobile 1.0//EN" "http://www.wapforum.org/DTD/xhtml-mobile10.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
    <meta http-equiv="Content-Type" content="application/xhtml+xml; charset=utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
    <title>聊天历史记录</title>
    <style type="text/css">
/* <![CDATA[ */
      
        * { -webkit-box-sizing: border-box; -moz-box-sizing: border-box; box-sizing: border-box; }
        body { margin: 0; padding: 0; background-color: #ffffff; color: #000000; font-family: sans-serif; font-size: 13px; }
      
        h2 { font-size: 14px; margin: 0; padding: 2px 6px; border-bottom: 2px solid #000; background-color: #f5f5f5; line-height: 18px; }
        
        #history-content { padding: 6px; }

        .search-bar input[type="text"] { background-color: #111111 !important; color: #ffffff !important; border-color: #555555 !important; }
        .search-bar input[type="submit"] { background-color: #333333 !important; color: #dddddd !important; border-color: #555555 !important; }
        .msg { margin-bottom: 4px; border-bottom: 1px dashed #cccccc; padding: 0 0 4px 0; font-size: 12px; line-height: 1.3; word-wrap: break-word; }
        .ai-msg { color: #333333; }
     
        .nav { padding: 10px; text-align: center; font-size: 13px; font-weight: bold; }
        .nav a { color: blue; text-decoration: none; padding: 0 5px; border: 1px solid #ccc; background: #eee; margin: 0 2px; display: inline-block; height: 22px; line-height: 20px; vertical-align: middle; box-sizing: border-box; -webkit-box-sizing: border-box; }
    /* ]]> */
</style>
    {% if theme == 'dark' %}
    <style type="text/css">
/* <![CDATA[ */
       
        html, body { background-color: #000000 !important; color: #aaaaaa !important; }
        h2 { border-bottom: 2px solid #555555 !important; background-color: #111111 !important; color: #dddddd !important; }
        
        #chat, #history-content, #online-content { background: #111111 !important; border-color: #555555 !important; }
        
        .msg, .user-item { border-bottom: 1px dashed #333333 !important; }
        .user-msg { color: #dddddd !important; }
        .ai-msg { color: #888888 !important; }
        .nickname { color: #dddddd !important; }
        .account { color: #888888 !important; }
    
        input[type="text"], input[type="password"] { background-color: #111111 !important; color: #ffffff !important; border-color: #555555 !important; }
        input[type="button"], input[type="submit"] { background-color: #333333 !important; color: #dddddd !important; border-color: #555555 !important; }
     
        #attachBtn { background-color: #222222 !important; }
        
   
        #optionsMenu, #attachMenu { background: #111111 !important; border-color: #555555 !important; }
        #optionsMenu a, #attachMenu > div { border-bottom-color: #555555 !important; }
        #attachMenu { color: #dddddd !important; }
        
 
        .nav a { background: #333333 !important; color: blue !important; border-color: #555555 !important; }
      
        .action-at { color: blue !important; } 
      
        .action-reply { color: #ff6600 !important; } 
       
        .reply-text { color: #ff6600 !important; }
    /* ]]> */
</style>
    {% endif %}
</head>
<body>
    <h2>历史记录
        <span style="float:right;">
            <a href="/" style="font-size:12px; font-weight:normal; color:red; text-decoration:none;">[返回聊天]</a>
        </span>
    </h2>

    <div class="search-bar" style="width: 95%; margin: 2px auto;">
        <form action="/history" method="GET" style="margin:0; padding:0;">
            <table style="width: 100%;" cellspacing="0" cellpadding="0" border="0">
                <tr>
                    <td style="width: 75%; padding-right: 2px;">
                        <input type="text" name="q" value="{{ q }}" placeholder="搜点什么..." style="width: 100%; height: 22px; border: 1px solid #000; font-size: 12px; margin: 0; border-radius: 0; -webkit-box-sizing: border-box; box-sizing: border-box;" />
                    </td>
                    <td style="width: 25%;">
                        <input type="submit" value="搜索" style="width: 100%; height: 22px; border: 1px solid #000; background-color: #eee; font-size: 12px; margin: 0; cursor: pointer; border-radius: 0; -webkit-appearance: none; -webkit-box-sizing: border-box; box-sizing: border-box;" />
                    </td>
                </tr>
            </table>
        </form>
    </div>

    <div id="history-content">
        {% if not messages %}
            <div class="msg">暂无更多历史记录...</div>
        {% endif %}
        
        {% for msg in messages %}
        <div class="msg ai-msg"><b>{{ msg.get('sender_title', 'QQ群') }}</b><br/>{{ msg.text | safe }}</div>
        {% endfor %}
    </div>

    <div class="nav">
        {% if page > 1 %}
            <a href="/history?page={{ page - 1 }}{% if q %}&amp;q={{ q }}{% endif %}">上一页</a>
        {% endif %}
        
        <form action="/history" method="GET" style="display:inline; margin:0; padding:0; vertical-align:middle;">
            {% if q %}
            <input type="hidden" name="q" value="{{ q }}" />
            {% endif %}
            <input type="text" name="page" value="{{ page }}" style="width:28px; height:22px; text-align:center; padding:0; margin:0; border:1px solid #999; font-size:12px; font-family:sans-serif; vertical-align:middle; border-radius:0; box-sizing:border-box; -webkit-box-sizing:border-box; -webkit-appearance:none;" />
            
            <span style="display:inline-block; vertical-align:middle; font-size:12px; font-family:sans-serif; font-weight:normal;"> / {{ total_pages }}</span>
            
            <input type="submit" value="跳" style="height:22px; border:1px solid #999; background:#eee; padding:0 5px; margin:0 0 0 2px; font-size:12px; vertical-align:middle; cursor:pointer; border-radius:0; box-sizing:border-box; -webkit-box-sizing:border-box; -webkit-appearance:none;" />
        </form>
        
        {% if page < total_pages %}
            <a href="/history?page={{ page + 1 }}{% if q %}&amp;q={{ q }}{% endif %}">下一页</a>
        {% endif %}
    </div>
</body>
</html>
"""

ONLINE_HTML = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html PUBLIC "-//WAPFORUM//DTD XHTML Mobile 1.0//EN" "http://www.wapforum.org/DTD/xhtml-mobile10.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
    <meta http-equiv="Content-Type" content="application/xhtml+xml; charset=utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
    <title>在线人员</title>
    <style type="text/css">
/* <![CDATA[ */
  
        * { -webkit-box-sizing: border-box; -moz-box-sizing: border-box; box-sizing: border-box; }
        body { margin: 0; padding: 0; background-color: #ffffff; color: #000000; font-family: sans-serif; font-size: 13px; }
        
   
        h2 { font-size: 14px; margin: 0; padding: 2px 6px; border-bottom: 2px solid #000; background-color: #f5f5f5; line-height: 18px; }
        

        #online-content { padding: 6px; }
        .user-item { margin-bottom: 4px; border-bottom: 1px dashed #cccccc; padding: 4px 0; font-size: 13px; line-height: 1.3; }
        .nickname { font-weight: bold; color: #000000; }
        .account { font-size: 11px; color: #666666; }
    /* ]]> */
</style>
    {% if theme == 'dark' %}
    <style type="text/css">
/* <![CDATA[ */
      
        html, body { background-color: #000000 !important; color: #aaaaaa !important; }
        h2 { border-bottom: 2px solid #555555 !important; background-color: #111111 !important; color: #dddddd !important; }
        
    
        #chat, #history-content, #online-content { background: #111111 !important; border-color: #555555 !important; }
        
   
        .msg, .user-item { border-bottom: 1px dashed #333333 !important; }
        .user-msg { color: #dddddd !important; }
        .ai-msg { color: #888888 !important; }
        .nickname { color: #dddddd !important; }
        .account { color: #888888 !important; }
        
      
        input[type="text"], input[type="password"] { background-color: #111111 !important; color: #ffffff !important; border-color: #555555 !important; }
        input[type="button"], input[type="submit"] { background-color: #333333 !important; color: #dddddd !important; border-color: #555555 !important; }
       
        #attachBtn { background-color: #222222 !important; }
        
     
        #optionsMenu, #attachMenu { background: #111111 !important; border-color: #555555 !important; }
        #optionsMenu a, #attachMenu > div { border-bottom-color: #555555 !important; }
        #attachMenu { color: #dddddd !important; }
       
        .nav a { background: #333333 !important; color: blue !important; border-color: #555555 !important; }
      
        .action-at { color: blue !important; } 
      
        .action-reply { color: #ff6600 !important; } 
       
        .reply-text { color: #ff6600 !important; }
    /* ]]> */
</style>
    {% endif %}
</head>
<body>
    <h2>在线名单 ({{ users|length }}人)
        <span style="float:right;">
            <a href="/" style="font-size:12px; font-weight:normal; color:red; text-decoration:none;">[返回聊天]</a>
        </span>
    </h2>

    <div id="online-content">
        {% if not users %}
            <div class="user-item">当前没有其他用户在线...</div>
        {% else %}
            {% for u in users %}
            <div class="user-item">
                <span style="color:green;">●</span> 
                <span class="nickname">{{ u.nickname }}</span> 
                <span class="account">({{ u.account }})</span>
                {% if u.is_me %} <span style="font-size:11px; color:blue;">[我自己]</span> {% endif %}
            </div>
            {% endfor %}
        {% endif %}
    </div>
</body>
</html>
"""

EMOJI_HTML = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html PUBLIC "-//WAPFORUM//DTD XHTML Mobile 1.0//EN" "http://www.wapforum.org/DTD/xhtml-mobile10.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
    <meta http-equiv="Content-Type" content="application/xhtml+xml; charset=utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
    <title>选择颜文字</title>
    <style type="text/css">
/* <![CDATA[ */
        * { -webkit-box-sizing: border-box; -moz-box-sizing: border-box; box-sizing: border-box; }
        body { margin: 0; padding: 0; background-color: #ffffff; color: #000000; font-family: sans-serif; font-size: 13px; }
        h2 { font-size: 14px; margin: 0; padding: 2px 6px; border-bottom: 2px solid #000; background-color: #f5f5f5; line-height: 18px; }
        #emoji-content { padding: 6px; text-align: center; }
        
        .kmj-btn { display: inline-block; padding: 3px 5px; margin: 3px; border: 1px solid #999; background: #eee; text-decoration: none; color: #000; font-size: 12px; line-height: 1.2; }
        .kmj-btn:active { background: #ccc; }
        
        .nav { padding: 10px; text-align: center; font-size: 13px; font-weight: bold; }
        .nav a { color: blue; text-decoration: none; padding: 0 5px; border: 1px solid #ccc; background: #eee; margin: 0 2px; display: inline-block; height: 22px; line-height: 22px; vertical-align: middle; }
    /* ]]> */
</style>
    {% if theme == 'dark' %}
    <style type="text/css">
/* <![CDATA[ */
        html, body { background-color: #000000 !important; color: #aaaaaa !important; }
        h2 { border-bottom: 2px solid #555555 !important; background-color: #111111 !important; color: #dddddd !important; }
        #emoji-content { background: #111111 !important; border-color: #555555 !important; }
        .kmj-btn { border: 1px solid #555; background: #333; color: #ddd; }
        .kmj-btn:active { background: #555; }
        .nav a { background: #333333 !important; color: blue !important; border-color: #555555 !important; }
    
        .nav input[type="text"] { background-color: #111111 !important; color: #ffffff !important; border-color: #555555 !important; }
        .nav input[type="submit"] { background-color: #333333 !important; color: #dddddd !important; border-color: #555555 !important; }
    /* ]]> */
</style>
    {% endif %}
</head>
<body>
    <h2>颜文字 ({{ page }}/{{ total_pages }})
        <span style="float:right;">
            <a href="/" style="font-size:12px; font-weight:normal; color:red; text-decoration:none;">[取消返回]</a>
        </span>
    </h2>

    <div id="emoji-content">
        {% for kmj in emojis %}
            <a href="javascript:selectEmoji('{{ kmj.encoded }}');" class="kmj-btn">{{ kmj.raw }}</a>
        {% endfor %}
    </div>

    <div class="nav">
        {% if page > 1 %}<a href="/emojis?page={{ page - 1 }}">上一页</a>{% endif %}
        
        <form action="/emojis" method="GET" style="display:inline; margin:0; padding:0; vertical-align:middle;">
            <input type="text" name="page" value="{{ page }}" style="width:28px; height:22px; line-height:20px; text-align:center; padding:0; margin:0; border:1px solid #999; font-size:12px; font-family:sans-serif; vertical-align:middle; border-radius:0;" />
            <span style="vertical-align:middle; font-size:12px; font-family:sans-serif; font-weight:normal;"> / {{ total_pages }}</span>
            <input type="submit" value="跳" style="height:22px; line-height:20px; border:1px solid #999; background:#eee; padding:0 5px; margin:0 0 0 2px; font-size:12px; vertical-align:middle; cursor:pointer; border-radius:0; -webkit-appearance:none;" />
        </form>
        
        {% if page < total_pages %}<a href="/emojis?page={{ page + 1 }}">下一页</a>{% endif %}
    </div>

    <script type="text/javascript">
/* <![CDATA[ */
        function getCookie(name) {
            var arr, reg = new RegExp("(^| )" + name + "=([^;]*)(;|$)");
            if (arr = document.cookie.match(reg)) return decodeURIComponent(arr[2]);
            else return "";
        }
        function selectEmoji(encodedEmoji) {
            var emoji = decodeURIComponent(encodedEmoji);
            var draft = getCookie("nokia_draft");
            draft = draft + emoji; 
            document.cookie = "nokia_draft=" + encodeURIComponent(draft) + "; path=/";
            window.location.href = "/";
        }
    /* ]]> */
</script>
</body>
</html>
"""

DRIVE_HTML = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html PUBLIC "-//WAPFORUM//DTD XHTML Mobile 1.0//EN" "http://www.wapforum.org/DTD/xhtml-mobile10.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
    <meta http-equiv="Content-Type" content="application/xhtml+xml; charset=utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
    <title>群网盘</title>
    <style type="text/css">
/* <![CDATA[ */
        * { -webkit-box-sizing: border-box; -moz-box-sizing: border-box; box-sizing: border-box; }
        body { margin: 0; padding: 0; background-color: #ffffff; color: #000000; font-family: sans-serif; font-size: 13px; }
        h2 { font-size: 14px; margin: 0; padding: 2px 6px; border-bottom: 2px solid #000; background-color: #f5f5f5; line-height: 18px; }
      
        #drive-content { padding: 6px; }
        
        .file-item { margin-bottom: 4px; border-bottom: 1px dashed #cccccc; padding: 4px 0; font-size: 13px; line-height: 1.3; word-wrap: break-word;}
        .file-name { font-weight: bold; color: #000000; }
        .file-meta { font-size: 11px; color: #666666; margin-top: 2px;}
        .download-btn { color: blue; text-decoration: underline; font-weight: bold; }

        .search-bar input[type="text"] { background-color: #ffffff; color: #000000; border-color: #000000; }
        .search-bar input[type="submit"] { background-color: #eeeeee; color: #000000; border-color: #000000; }
        .nav { padding: 10px; text-align: center; font-size: 13px; font-weight: bold; }
        .nav a { color: blue; text-decoration: none; padding: 0 5px; border: 1px solid #ccc; background: #eee; margin: 0 2px; display: inline-block; height: 22px; line-height: 20px; vertical-align: middle; box-sizing: border-box; -webkit-box-sizing: border-box; }
    /* ]]> */
</style>
    {% if theme == 'dark' %}
    <style type="text/css">
/* <![CDATA[ */
        html, body { background-color: #000000 !important; color: #aaaaaa !important; }
        h2 { border-bottom: 2px solid #555555 !important; background-color: #111111 !important; color: #dddddd !important; }
        #drive-content { background: #111111 !important; border-color: #555555 !important; }
        .file-item { border-bottom: 1px dashed #333333 !important; }
        .file-name { color: #dddddd !important; }
        .file-meta { color: #888888 !important; }
        .download-btn { color: blue !important; }
  
        .search-bar input[type="text"] { background-color: #111111 !important; color: #ffffff !important; border-color: #555555 !important; }
        .search-bar input[type="submit"] { background-color: #333333 !important; color: #dddddd !important; border-color: #555555 !important; }
        .nav a { background: #333333 !important; color: blue !important; border-color: #555555 !important; }
    /* ]]> */
</style>
    {% endif %}
</head>
<body>
    <h2>群网盘
        <span style="float:right;">
            <a href="/" style="font-size:12px; font-weight:normal; color:red; text-decoration:none;">[返回聊天]</a>
        </span>
    </h2>

    <div class="search-bar" style="width: 95%; margin: 2px auto;">
        <form action="/drive" method="GET" style="margin:0; padding:0;">
            <table style="width: 100%;" cellspacing="0" cellpadding="0" border="0">
                <tr>
                    <td style="width: 75%; padding-right: 2px;">
                        <input type="text" name="q" value="{{ q }}" placeholder="搜文件名或上传者..." style="width: 100%; height: 22px; border: 1px solid #000; font-size: 12px; margin: 0; border-radius: 0; -webkit-box-sizing: border-box; box-sizing: border-box;" />
                    </td>
                    <td style="width: 25%;">
                        <input type="submit" value="搜索" style="width: 100%; height: 22px; border: 1px solid #000; background-color: #eee; font-size: 12px; margin: 0; cursor: pointer; border-radius: 0; -webkit-appearance: none; -webkit-box-sizing: border-box; box-sizing: border-box;" />
                    </td>
                </tr>
            </table>
        </form>
    </div>

    <div id="drive-content">
        {% if not files %}
            <div class="file-item">网盘空空如也，或者没有搜到文件哦。</div>
        {% else %}
            {% for f in files %}
            <div class="file-item">
                <div class="file-name">{{ f.filename }}</div>
                <div class="file-meta">
                    上传者: {{ f.uploader }} | 大小: {{ f.size_mb }}MB <br/>
                    时间: {{ f.upload_time }}
                    <span style="float: right;">
                        <a href="/api/download_file?id={{ f._id }}" class="download-btn">[下载]</a>
                    </span>
                </div>
            </div>
            {% endfor %}
        {% endif %}
    </div>

    <div class="nav">
        {% if page > 1 %}
            <a href="/drive?page={{ page - 1 }}{% if q %}&amp;q={{ q }}{% endif %}">上一页</a>
        {% endif %}
        
        <form action="/drive" method="GET" style="display:inline; margin:0; padding:0; vertical-align:middle;">
            {% if q %}
            <input type="hidden" name="q" value="{{ q }}" />
            {% endif %}
            <input type="text" name="page" value="{{ page }}" style="width:28px; height:22px; text-align:center; padding:0; margin:0; border:1px solid #999; font-size:12px; font-family:sans-serif; vertical-align:middle; border-radius:0; box-sizing:border-box; -webkit-box-sizing:border-box; -webkit-appearance:none;" />
            
            <span style="display:inline-block; vertical-align:middle; font-size:12px; font-family:sans-serif; font-weight:normal;"> / {{ total_pages }}</span>
            
            <input type="submit" value="跳" style="height:22px; border:1px solid #999; background:#eee; padding:0 5px; margin:0 0 0 2px; font-size:12px; vertical-align:middle; cursor:pointer; border-radius:0; box-sizing:border-box; -webkit-box-sizing:border-box; -webkit-appearance:none;" />
        </form>
        
        {% if page < total_pages %}
            <a href="/drive?page={{ page + 1 }}{% if q %}&amp;q={{ q }}{% endif %}">下一页</a>
        {% endif %}
    </div>
</body>
</html>
"""


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


@app.route("/login", methods=["GET", "POST"])
def login_page():
    theme = request.cookies.get("nokia_theme", "light")
    if request.method == "GET":
        if get_active_account():
            return redirect(url_for("index"))

        return render_template_string(LOGIN_HTML, theme=theme)

    account = request.form.get("account", "").strip()
    password = request.form.get("password", "").strip()

    if not account or not password:
        return render_template_string(
            LOGIN_HTML, error="账号和密码不能为空！", theme=theme
        )

    if not re.match(r"^[A-Za-z0-9_]{3,15}$", account):
        return render_template_string(
            LOGIN_HTML, error="账号只能包含3-15位字母、数字或下划线！", theme=theme
        )

    if "register_btn" in request.form:
        ok, msg = register_account(
            account, password, request.form.get("reason", ""), request
        )
        if ok:
            return render_template_string(LOGIN_HTML, success=msg, theme=theme)
        return render_template_string(LOGIN_HTML, error=msg, theme=theme)

    error = try_login(account, password)
    if error:
        return render_template_string(LOGIN_HTML, error=error, theme=theme)
    return redirect(url_for("index"))


@app.route("/logout", methods=["GET"])
def logout():
    session.pop("nokia_account", None)
    return redirect(url_for("login_page"))


@app.route("/history", methods=["GET"])
def view_history():
    account = get_active_account()
    if not account:
        return redirect(url_for("login_page"))

    page = request.args.get("page", 1, type=int)
    q = request.args.get("q", "").strip()

    if page < 1:
        page = 1

    per_page = 25

    query = {}
    if q:
        safe_q = re.escape(q)
        query["$or"] = [
            {"pure_text": {"$regex": safe_q, "$options": "i"}},
            {"pure_sender": {"$regex": safe_q, "$options": "i"}},
        ]

    total_msgs = chat_collection.count_documents(query)
    total_pages = (total_msgs + per_page - 1) // per_page if total_msgs > 0 else 1

    if page > total_pages:
        page = total_pages

    skip_count = (page - 1) * per_page

    docs = list(
        chat_collection.find(query, {"_id": 0})
        .sort("id", -1)
        .skip(skip_count)
        .limit(per_page)
    )

    docs.reverse()

    theme = request.cookies.get("nokia_theme", "light")
    return render_template_string(
        HISTORY_HTML,
        messages=docs,
        page=page,
        total_pages=total_pages,
        theme=theme,
        q=q,
    )


@app.route("/online", methods=["GET"])
def view_online_users():
    account = get_active_account()
    if not account:
        return redirect(url_for("login_page"))

    get_online_count()

    active_users = []
    for acc in online_sessions.keys():
        nickname = users_db.get(acc, {}).get("nickname", acc)
        nickname = filter_emoji(nickname)

        active_users.append(
            {
                "account": acc,
                "nickname": nickname,
                "is_me": (acc == account),
            }
        )

    active_users.sort(key=lambda x: (not x["is_me"], x["account"]))

    theme = request.cookies.get("nokia_theme", "light")
    return render_template_string(ONLINE_HTML, users=active_users, theme=theme)


@app.route("/emojis", methods=["GET"])
def view_emojis():
    account = get_active_account()
    if not account:
        return redirect(url_for("login_page"))

    page = request.args.get("page", 1, type=int)
    if page < 1:
        page = 1

    try:
        per_page = int(request.cookies.get("emoji_limit", 40))
    except ValueError:
        per_page = 40

    total_kmj = len(KAOMOJI_LIST)
    total_pages = (total_kmj + per_page - 1) // per_page if total_kmj > 0 else 1
    if page > total_pages:
        page = total_pages

    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    current_emojis = KAOMOJI_LIST[start_idx:end_idx]

    safe_emojis = [{"raw": k, "encoded": urllib.parse.quote(k)} for k in current_emojis]

    theme = request.cookies.get("nokia_theme", "light")
    return render_template_string(
        EMOJI_HTML, emojis=safe_emojis, page=page, total_pages=total_pages, theme=theme
    )


@app.route("/toggle_theme", methods=["GET"])
def toggle_theme():
    current_theme = request.cookies.get("nokia_theme", "light")
    new_theme = "dark" if current_theme == "light" else "light"

    next_url = request.referrer or url_for("index")

    resp = make_response(redirect(next_url))
    resp.set_cookie("nokia_theme", new_theme, max_age=31536000)
    return resp


WEBHOOK_SECRET_TOKEN = os.environ.get("WEBHOOK_TOKEN", "")


@app.route("/api/change_name", methods=["POST"])
def change_name_api():
    account = session.get("nokia_account")
    if not account or account not in users_db:
        return jsonify({"status": "error", "msg": "登录已失效，请重新登录。"})

    data = request.get_json(silent=True) or {}
    new_name, error = set_nickname(account, str(data.get("new_name", "")))
    if error:
        return jsonify({"status": "error", "msg": error})
    return jsonify({"status": "ok", "new_name": new_name})


@app.route("/api/upload_image", methods=["POST"])
def upload_image():
    account = get_active_account()
    if not account:
        return '<script type="text/javascript">/* <![CDATA[ */window.parent.uploadCallback("error", "权限不足或未登录");/* ]]> */</script>'

    file = request.files.get("image")
    if not file or file.filename == "":
        return '<script type="text/javascript">/* <![CDATA[ */window.parent.uploadCallback("error", "未选择任何照片");/* ]]> */</script>'

    try:
        img = Image.open(file.stream)
        if img.mode != "RGB":
            img = img.convert("RGB")

        max_size = 800
        if img.width > max_size or img.height > max_size:
            img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)

        img_io = io.BytesIO()
        img.save(img_io, "JPEG", quality=80)

        b64_data = base64.b64encode(img_io.getvalue()).decode("utf-8")
        cq_code = f"[CQ:image,file=base64://{b64_data}]"

        username = users_db[account].get("nickname", account)
        client_ip = get_real_ip(request)
        location = get_ip_location(client_ip, request)

        full_message = f"[{location} - {username}]\n{cq_code}"

        requests.post(
            "http://127.0.0.1:3000/send_group_msg",
            json={"group_id": TARGET_GROUP_ID, "message": full_message},
            timeout=8,
        )

        img_id = hashlib.md5(b64_data.encode("utf-8")).hexdigest()[:16]
        images_collection.insert_one(
            {"_id": img_id, "data": b64_data, "time": time.time()}
        )

        cleanup_old_images(500)

        add_chat_message(
            "消息",
            f"[网页-{location}]{html.escape(username)}: [图片] <a href='/view_image?b64={img_id}' target='_blank' class='action-at'>[查看]</a>",
            username,
            "[图片]",
        )

        return '<script type="text/javascript">/* <![CDATA[ */window.parent.uploadCallback("ok", "");/* ]]> */</script>'

    except Exception as e:
        print(f"上传图片失败: {e}")
        return f'<script type="text/javascript">/* <![CDATA[ */window.parent.uploadCallback("error", "文件处理失败");/* ]]> */</script>'


@app.route("/api/upload_file", methods=["POST"])
def upload_file():
    account = get_active_account()
    if not account:
        return '<script type="text/javascript">/* <![CDATA[ */window.parent.uploadCallback("error", "权限不足或未登录");/* ]]> */</script>'

    file = request.files.get("file")
    if not file or file.filename == "":
        return '<script type="text/javascript">/* <![CDATA[ */window.parent.uploadCallback("error", "未选择任何文件");/* ]]> */</script>'

    try:
        filename = file.filename
        username = users_db[account].get("nickname", account)
        client_ip = get_real_ip(request)
        location = get_ip_location(client_ip, request)

        recent_web_uploads[filename] = username

        file_bytes = file.read()
        if not file_bytes:
            return '<script type="text/javascript">/* <![CDATA[ */window.parent.uploadCallback("error", "不能发送空文件");/* ]]> */</script>'

        file_id = str(uuid.uuid4())

        try:
            files_collection.insert_one(
                {
                    "_id": file_id,
                    "filename": filename,
                    "data": file_bytes,
                    "size": len(file_bytes),
                    "uploader": username,
                    "time": time.time(),
                }
            )

            cleanup_old_files()
        except Exception as e:
            print(f"存入网盘失败: {e}")

        qq_notification_text = f"[网页-{location}]{username}: [发送了文件: {filename}]"
        remember_echo(qq_notification_text)

        try:
            requests.post(
                "http://127.0.0.1:3000/send_group_msg",
                json={"group_id": TARGET_GROUP_ID, "message": qq_notification_text},
                timeout=3,
            )
        except Exception as e:
            print(f"发送文件前置文字提示失败: {e}")

        local_port = os.environ.get("PORT", 7860)
        download_url = f"http://127.0.0.1:{local_port}/api/internal_download/{file_id}"

        # 机器人通过内部地址回来取文件；无论成败都要释放这份内存副本
        internal_file_cache[file_id] = {"bytes": file_bytes, "name": filename}
        try:
            resp_data = requests.post(
                "http://127.0.0.1:3000/upload_group_file",
                json={
                    "group_id": TARGET_GROUP_ID,
                    "file": download_url,
                    "name": filename,
                },
                timeout=60,
            ).json()
        finally:
            internal_file_cache.pop(file_id, None)

        if resp_data.get("status") == "failed":
            raise Exception(
                f"机器人报错: {resp_data.get('wording', resp_data.get('msg', '未知拦截'))}"
            )

        safe_filename = html.escape(filename or "")
        add_chat_message(
            "消息",
            f"[网页-{location}]{html.escape(username)}: <span style='color:#008800;'>[发送了文件: {safe_filename}]</span> <a href='/api/download_file?id={file_id}' target='_blank' style='color: blue; text-decoration:underline;'>[下载]</a>",
            username,
            f"[发送了文件: {filename}]",
        )

        return '<script type="text/javascript">/* <![CDATA[ */window.parent.uploadCallback("ok", "");/* ]]> */</script>'

    except Exception as e:
        print(f"上传文件失败: {e}")
        safe_error_msg = str(e).replace('"', "").replace("'", "").replace("\n", " ")
        return f'<script type="text/javascript">/* <![CDATA[ */window.parent.uploadCallback("error", "{safe_error_msg}");/* ]]> */</script>'


@app.route("/drive", methods=["GET"])
def view_drive():
    account = get_active_account()
    if not account:
        return redirect(url_for("login_page"))

    page = request.args.get("page", 1, type=int)
    q = request.args.get("q", "").strip()

    if page < 1:
        page = 1

    per_page = 20

    # 旧版本存在 R2 上的文件(只有 r2_key 没有 data)已无法读取，不再列出
    query = {"data": {"$exists": True}}
    if q:
        safe_q = re.escape(q)
        query["$or"] = [
            {"filename": {"$regex": safe_q, "$options": "i"}},
            {"uploader": {"$regex": safe_q, "$options": "i"}},
        ]

    total_files = files_collection.count_documents(query)
    total_pages = (total_files + per_page - 1) // per_page if total_files > 0 else 1

    if page > total_pages:
        page = total_pages

    skip_count = (page - 1) * per_page

    file_docs = list(
        files_collection.find(query, {"data": 0})
        .sort("time", -1)
        .skip(skip_count)
        .limit(per_page)
    )

    display_files = []
    for f in file_docs:
        size_bytes = f.get("size", 0)
        size_mb = round(size_bytes / (1024 * 1024), 2) if size_bytes else "未知"

        dt = datetime.datetime.fromtimestamp(f.get("time", 0), tz=TZ_UTC8)

        display_files.append(
            {
                "_id": f["_id"],
                "filename": f.get("filename", "未知文件"),
                "uploader": f.get("uploader", "系统"),
                "size_mb": size_mb,
                "upload_time": dt.strftime("%Y-%m-%d %H:%M"),
            }
        )

    theme = request.cookies.get("nokia_theme", "light")

    return render_template_string(
        DRIVE_HTML,
        files=display_files,
        theme=theme,
        page=page,
        total_pages=total_pages,
        q=q,
    )


def is_mobile_device(req):
    accept_header = req.headers.get("Accept", "").lower()
    if "text/vnd.wap.wml" in accept_header:
        return True
    if req.headers.get("X-Wap-Profile") or req.headers.get("Profile"):
        return True
    ua = req.headers.get("User-Agent", "").lower()
    mobile_keywords = [
        "nokia",
        "symbian",
        "ucweb",
        "wap",
        "motorola",
        "mot-",
        "sonyericsson",
        "blackberry",
        "j2me",
        "midp",
        "cldc",
        "up.browser",
        "netfront",
        "obigo",
        "teleca",
        "openwave",
    ]
    return any(k in ua for k in mobile_keywords)


def adapt_wml_to_xhtmlmp(wml_str, target_card=None):
    if target_card:
        wml_str = re.sub(r'href="#([^"]+)"', r'href="?card=\1"', wml_str)
        match = re.search(
            f'<card id="{target_card}"[^>]*>.*?</card>', wml_str, flags=re.DOTALL
        )
        if match:
            wml_str = re.sub(r"<card.*?</card>", "", wml_str, flags=re.DOTALL)
            wml_str = wml_str.replace("</wml>", f"{match.group(0)}\n</wml>")

    html = re.sub(
        r"<\?xml.*?\?>",
        '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html PUBLIC "-//WAPFORUM//DTD XHTML Mobile 1.0//EN" "http://www.wapforum.org/DTD/xhtml-mobile10.dtd">',
        wml_str,
    )
    html = re.sub(r"<!DOCTYPE wml.*?>", "", html, flags=re.DOTALL)
    html = html.replace(
        "<wml>",
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1.0"/><title>WapChat QQ</title></head><body>',
    )
    html = html.replace("</wml>", "</body></html>")

    def replace_card(m):
        card_id = m.group(1)
        title = m.group(2)
        if card_id in ["login", "chat", "options", "rename"]:
            return f'<div id="{card_id}">'
        else:
            return f'<div id="{card_id}"><h2>{title}</h2>'

    html = re.sub(r'<card id="([^"]+)" title="([^"]+)">', replace_card, html)
    html = html.replace("</card>", "</div>")
    html = html.replace("<h2>WapChat QQ</h2>", "")
    html = re.sub(r'emptyok="[^"]+"', "", html)
    html = re.sub(r'format="[^"]+"', "", html)
    html = (
        html.replace('<p align="left">', "<div>")
        .replace("<p>", "<div>")
        .replace("</p>", "</div>")
    )

    if 'id="login"' in html:
        html = html.replace(
            "账号:<input", '<form action="/wml/login" method="post">账号:<input'
        )
        html = re.sub(
            r'<anchor>\[登录\].*?name="login_btn".*?</anchor>',
            r'<input type="submit" name="login_btn" value="登录"/>',
            html,
            flags=re.DOTALL,
        )
        html = re.sub(
            r'<anchor>\[注册\].*?name="register_btn".*?</anchor>',
            r'<input type="submit" name="register_btn" value="注册"/></form>',
            html,
            flags=re.DOTALL,
        )

    if 'id="chat"' in html:
        html = html.replace(
            "发言:<input",
            '<form action="/wml/" method="post" style="display:inline;">发言:<input',
        )
        html = re.sub(
            r"<anchor>\[发送\].*?</anchor>",
            r'<input type="submit" value="发送"/></form>',
            html,
            flags=re.DOTALL,
        )
        html = re.sub(
            r"<anchor>\[刷新\].*?</anchor>",
            r'<form action="/wml/" method="post" style="display:inline; margin-left:4px;"><input type="hidden" name="r" value="1"/><input type="submit" value="刷新"/></form>',
            html,
            flags=re.DOTALL,
        )

    if 'id="rename"' in html:
        html = html.replace(
            "新昵称:<input",
            '<form action="/wml/rename_wml" method="post" style="display:inline;">新昵称:<input',
        )
        html = re.sub(
            r"<anchor>\[确认保存\].*?</anchor>",
            r'<input type="submit" value="确认保存"/></form>',
            html,
            flags=re.DOTALL,
        )

    return html


@app.route("/wml/login", methods=["GET", "POST"])
def wml_login_page():
    def render_adaptive_login(error=None, success=None):
        raw_content = render_template_string(LOGIN_WML, error=error, success=success)
        if is_mobile_device(request):
            resp = make_response(raw_content)
            resp.headers["Content-Type"] = "text/vnd.wap.wml; charset=utf-8"
        else:
            pc_content = adapt_wml_to_xhtmlmp(raw_content)
            resp = make_response(pc_content)
            resp.headers["Content-Type"] = "text/html; charset=utf-8"
        return resp

    if request.method == "GET":
        if get_active_account():
            return redirect(url_for("wml_index"))

        return render_adaptive_login()

    account = request.form.get("account", "").strip()
    password = request.form.get("password", "").strip()

    if not account or not password:
        return render_adaptive_login(error="账号和密码不能为空！")

    if not re.match(r"^[A-Za-z0-9_]{3,15}$", account):
        return render_adaptive_login(error="账号仅限3-15位字母数字下划线！")

    if "register_btn" in request.form:
        ok, msg = register_account(
            account, password, request.form.get("reason", ""), request
        )
        if ok:
            return render_adaptive_login(success=msg)
        return render_adaptive_login(error=msg)

    error = try_login(account, password)
    if error:
        return render_adaptive_login(error=error)
    return redirect("/wml/")


@app.route("/wml/logout", methods=["GET"])
def wml_logout():
    session.pop("nokia_account", None)
    return redirect("/wml/login")


@app.route("/wml", methods=["GET", "POST"])
def wml_index_no_slash():
    return redirect("/wml/")


@app.route("/wml/", methods=["GET", "POST"])
def wml_index():
    account = get_active_account()
    if not account:
        return redirect("/wml/login")

    notice = None
    if request.method == "POST":
        notice = send_web_message(account, request.form.get("message", ""), request)

        # 电脑端用 POST-重定向-GET 避免刷新时重复提交；有拦截提示时直接渲染以便显示
        if not notice and not is_mobile_device(request):
            return redirect("/wml/")

    wml_history = []
    for m in chat_history[-20:]:
        wml_msg = dict(m)
        clean_text = re.sub(
            r'<a href=([\'"])([^\'"]+)\1[^>]*>(.*?)</a>',
            r'<a href="\2">\3</a>',
            m.get("text", ""),
        )
        wml_msg["wml_text"] = re.sub(r"</?span[^>]*>", "", clean_text)
        wml_history.append(wml_msg)

    wml_history.reverse()

    online_sessions[account] = time.time()

    raw_content = render_template_string(
        NOKIA_WML,
        history=wml_history,
        saved_username=filter_emoji(users_db[account].get("nickname", account)),
        online_count=get_online_count(),
        notice=notice,
    )
    if is_mobile_device(request):
        resp = make_response(raw_content)
        resp.headers["Content-Type"] = "text/vnd.wap.wml; charset=utf-8"
    else:
        target_card = request.args.get("card", "chat")
        pc_content = adapt_wml_to_xhtmlmp(raw_content, target_card)
        resp = make_response(pc_content)
        resp.headers["Content-Type"] = "text/html; charset=utf-8"
    return resp


@app.route("/wml/rename_wml", methods=["POST"])
def wml_rename():
    account = get_active_account()
    if not account:
        return redirect("/wml/login")

    set_nickname(account, request.form.get("new_name", ""))
    return redirect("/wml/")


@app.route("/", methods=["GET"])
def index():
    account = get_active_account()
    if not account:
        return redirect(url_for("login_page"))

    recent_history = chat_history[-25:]
    last_id = chat_history[-1]["id"] if chat_history else 0

    saved_username = users_db[account].get("nickname", account)

    saved_username = filter_emoji(saved_username)

    online_sessions[account] = time.time()
    current_online = get_online_count()

    theme = request.cookies.get("nokia_theme", "light")
    ai_url = os.environ.get("AI_URL", "#")
    return render_template_string(
        NOKIA_HTML,
        history=recent_history,
        last_id=last_id,
        saved_username=saved_username,
        online_count=current_online,
        theme=theme,
        ai_url=ai_url,
    )


@app.route("/api/sync", methods=["POST"])
def handle_nokia_ajax():
    data = request.get_json(silent=True) or {}
    try:
        client_last_id = int(data.get("last_id", 0))
    except (TypeError, ValueError):
        client_last_id = 0

    account = get_active_account()
    if not account:
        return jsonify(
            {
                "messages": [
                    {
                        "sender_title": "系统",
                        "text": "登录失效或账号正在审核中，请刷新页面重新登录！",
                    }
                ],
                "last_id": client_last_id,
            }
        )

    online_sessions[account] = time.time()

    intercept_msg = send_web_message(account, str(data.get("message", "")), request)

    new_msgs = [m for m in chat_history if m["id"] > client_last_id]
    response_msgs = []

    if intercept_msg:
        response_msgs.append({"sender_title": "系统", "text": intercept_msg})

    for m in new_msgs:
        msg_dict = {
            "id": m.get("id"),
            "sender_title": m.get("sender_title", "QQ群"),
            "text": m["text"],
            "pure_sender": m.get("pure_sender", ""),
        }
        if m.get("recall_target_id") is not None:
            msg_dict["recall_target_id"] = m["recall_target_id"]

        response_msgs.append(msg_dict)

    new_last_id = chat_history[-1]["id"] if chat_history else client_last_id

    current_online = get_online_count()

    return jsonify(
        {
            "messages": response_msgs,
            "last_id": new_last_id,
            "online_count": current_online,
        }
    )


@app.route("/webhook", methods=["POST"])
def receive_qq_msg():
    client_ip = request.remote_addr or ""

    if WEBHOOK_SECRET_TOKEN:
        # 容器内由 start.sh 每次启动随机生成，并写进 NapCat 的上报地址里
        authorized = hmac.compare_digest(
            request.args.get("token", "").encode("utf-8"),
            WEBHOOK_SECRET_TOKEN.encode("utf-8"),
        )
    else:
        # 没有令牌时(本地调试)只接受本机直连，带转发头的一律视为外网请求
        authorized = (
            client_ip in ("127.0.0.1", "::1")
            and "X-Forwarded-For" not in request.headers
        )

    if not authorized:
        print(f"[安全拦截] 拦截未授权的 Webhook 请求！来源IP: {client_ip}")
        return jsonify({"status": "failed", "msg": "Unauthorized"}), 403

    data = request.get_json(silent=True) or {}

    if data.get("message_type") == "group" and data.get("group_id") == TARGET_GROUP_ID:
        if handle_admin_command(data):
            return jsonify({"status": "admin_command"})

        sender_name = data.get("sender", {}).get("nickname", "某人")

        sender_name = filter_emoji(sender_name)
        sender_name = re.sub(r"\[.*?\]", "", sender_name).strip()
        if not sender_name:
            sender_name = "无名氏"

        sender_qq = str(data.get("sender", {}).get("user_id", ""))
        if sender_qq and sender_name:
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
                                img = Image.open(io.BytesIO(resp.content))
                                if img.mode != "RGB":
                                    img = img.convert("RGB")
                                max_width = 240
                                if img.width > max_width:
                                    ratio = max_width / img.width
                                    new_height = int(img.height * ratio)
                                    img = img.resize(
                                        (max_width, new_height),
                                        Image.Resampling.LANCZOS,
                                    )

                                img_io = io.BytesIO()
                                img.save(img_io, "JPEG", quality=70)
                                b64_data = base64.b64encode(img_io.getvalue()).decode(
                                    "utf-8"
                                )

                                images_collection.insert_one(
                                    {
                                        "_id": img_id,
                                        "data": b64_data,
                                        "time": time.time(),
                                    }
                                )

                                cleanup_old_images(500)

                        except Exception as e:
                            print(f"图床持久化或清理失败: {e}")

                    raw_msg += f"[图片] {{IMG:{img_id}}}"
                else:
                    raw_msg += "[发送了一张图片]"
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
                raw_msg += "[发送了一条语音]"
            elif seg_type == "video":
                raw_msg += "[发送了一段视频]"

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
                raw_msg += "[发送了一条图文卡片]"
            elif seg_type in ["forward", "node"]:
                raw_msg += "[发送了合并转发聊天记录]"
            elif seg_type == "share":
                title = segment.get("data", {}).get("title", "链接")
                raw_msg += f"[分享了链接: {title}]"
            elif seg_type == "music":
                title = segment.get("data", {}).get("title", "一首歌")
                raw_msg += f"[分享了音乐: {title}]"
            elif seg_type == "location":
                title = segment.get("data", {}).get("title", "某个位置")
                raw_msg += f"[分享了位置: {title}]"
            elif seg_type == "contact":
                raw_msg += "[推荐了一个联系人名片]"
            elif seg_type == "poke":
                target_qq = str(segment.get("data", {}).get("qq", ""))
                if target_qq and target_qq != "all":
                    target_name = get_qq_name(TARGET_GROUP_ID, target_qq)
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
                        target_name = get_qq_name(TARGET_GROUP_ID, target_qq)
                    raw_msg += f"[@{target_name}] "
            elif seg_type == "reply":
                msg_id = segment.get("data", {}).get("id")
                reply_text = ""
                reply_sender = "某人"

                if msg_id:
                    try:
                        resp = requests.post(
                            "http://127.0.0.1:3000/get_msg",
                            json={"message_id": msg_id},
                            timeout=1.5,
                        ).json()
                        if resp and resp.get("status") in ["ok", "success"]:
                            orig_data = resp.get("data", {})

                            orig_qq = orig_data.get("sender", {}).get("user_id")
                            if orig_qq:
                                reply_sender = get_qq_name(TARGET_GROUP_ID, orig_qq)

                            orig_chain = orig_data.get("message", [])
                            if isinstance(orig_chain, list):
                                for orig_seg in orig_chain:
                                    if orig_seg.get("type") == "text":
                                        reply_text += orig_seg.get("data", {}).get(
                                            "text", ""
                                        )
                                    elif orig_seg.get("type") == "image":
                                        reply_text += "[图片]"
                                    elif orig_seg.get("type") == "face":
                                        reply_text += "[表情]"
                            elif isinstance(orig_chain, str):
                                reply_text = orig_chain

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

        raw_msg_no_space = re.sub(r"\s+", "", raw_msg)

        if raw_msg.strip():
            if raw_msg_no_space in recent_sent_messages:
                recent_sent_messages.remove(raw_msg_no_space)
                return jsonify({"status": "ignored_echo"})

            safe_text = html.escape(raw_msg).replace("\n", "<br/>")

            safe_text = re.sub(
                r"\{IMG:([A-Za-z0-9_-]+)\}",
                r'<a href="/view_image?b64=\1" target="_blank" class="action-at">[查看]</a>',
                safe_text,
            )
            safe_text = highlight_mentions(safe_text)

            safe_sender = html.escape(sender_name)

            print(f"💬 [QQ群收到]: {safe_sender} - {raw_msg}")

            # 机器人自己发的消息(网页用户的发言)按 QQ 号识别，不能按昵称，否则改个昵称就能冒充
            bot_ids = {str(data.get("self_id", "")), os.environ.get("BOT_QQ", "")}
            if sender_qq in bot_ids:
                final_pure_sender = ""

                transformed_text = re.sub(
                    r"\[([^\]]+?)\s*-\s*([^\]]+?)\](?:\s|<br/>)*",
                    r"[网页-\1]\2: ",
                    safe_text,
                    count=1,
                )

                final_display_text = f"{qq_reply_html}{transformed_text}"
            else:
                final_display_text = f"{qq_reply_html}{safe_sender}: {safe_text}"
                final_pure_sender = sender_name

            add_chat_message(
                "群聊",
                final_display_text,
                final_pure_sender,
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
        recalled_qq_msg_id = None
        recalled_local_id = None

        if notice_type == "notify" and sub_type == "poke":
            sender_name = get_qq_name(TARGET_GROUP_ID, user_qq)
            target_qq = str(data.get("target_id", ""))
            target_name = get_qq_name(TARGET_GROUP_ID, target_qq)
            sys_msg_text = f"{sender_name} 戳了戳 {target_name}"

        elif notice_type == "group_increase":
            user_name = get_qq_name(TARGET_GROUP_ID, user_qq)
            sys_msg_text = f"欢迎 {user_name} 加入了群聊！"
        elif notice_type == "group_decrease":
            user_name = get_qq_name(TARGET_GROUP_ID, user_qq)
            if sub_type == "leave":
                sys_msg_text = f"{user_name} 默默离开了群聊"
            elif sub_type in ["kick", "kick_me"]:
                operator_name = get_qq_name(TARGET_GROUP_ID, operator_qq)
                sys_msg_text = f"{user_name} 被 {operator_name} 请出了群聊"

        elif notice_type == "group_recall":
            user_name = get_qq_name(TARGET_GROUP_ID, user_qq)
            sys_msg_text = ""

            recalled_qq_msg_id = data.get("message_id")

            if recalled_qq_msg_id:
                recalled_str = str(recalled_qq_msg_id)

                if recalled_str not in processed_notices_cache:
                    processed_notices_cache[recalled_str] = True

                    original_msg = chat_collection.find_one(
                        {"qq_msg_id": {"$in": [recalled_qq_msg_id, recalled_str]}}
                    )

                    if original_msg:
                        if original_msg.get("pure_text") != "[此消息已被撤回]":
                            sys_msg_text = f"{user_name} 撤回了一条消息"

                            for m in chat_history:
                                if str(m.get("qq_msg_id")) == recalled_str:
                                    recalled_local_id = m["id"]
                                    m["text"] = (
                                        f'<span style="color:#999; font-size:12px;"><i>[该消息已被 {html.escape(user_name)} 撤回]</i></span>'
                                    )
                                    m["pure_text"] = "[此消息已被撤回]"
                                    break

                            try:
                                chat_collection.update_many(
                                    {
                                        "qq_msg_id": {
                                            "$in": [recalled_qq_msg_id, recalled_str]
                                        }
                                    },
                                    {
                                        "$set": {
                                            "text": f'<span style="color:#999; font-size:12px;"><i>[该消息已被 {html.escape(user_name)} 撤回]</i></span>',
                                            "pure_text": "[此消息已被撤回]",
                                        }
                                    },
                                )
                            except Exception as e:
                                print(f"云端更新撤回状态失败: {e}")

        elif notice_type == "group_upload":
            user_name = get_qq_name(TARGET_GROUP_ID, user_qq)
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
                        resp = requests.post(
                            "http://127.0.0.1:3000/get_group_file_url",
                            json={
                                "group_id": TARGET_GROUP_ID,
                                "file_id": file_id,
                                "busid": file_busid,
                            },
                            timeout=2,
                        ).json()
                        if resp and resp.get("status") in ["ok", "success"]:
                            file_url = resp.get("data", {}).get("url", "")
                    except Exception:
                        pass

                safe_sender = html.escape(display_name)
                safe_filename = html.escape(file_name)

                if file_url:
                    encoded_url = urllib.parse.quote(file_url, safe="")
                    encoded_name = urllib.parse.quote(file_name)
                    proxy_url = f"/api/download_qq_file?name={encoded_name}&url={encoded_url}&sig={sign_url(file_url)}"
                    file_html = f"<span style='color:#008800;'>[发送了文件: {safe_filename}]</span> <a href='{proxy_url}' target='_blank' style='color: blue; text-decoration:underline;'>[下载]</a>"
                else:
                    file_html = f"<span style='color:#008800;'>[发送了文件: {safe_filename}]</span> <span style='color:#999; font-size:11px;'>(暂无法获取下载链接)</span>"

                add_chat_message(
                    "群聊",
                    f"{safe_sender}: {file_html}",
                    display_name,
                    f"[发送了文件: {file_name}]",
                    qq_msg_id=None,
                )

        elif notice_type == "group_ban":
            user_name = get_qq_name(TARGET_GROUP_ID, user_qq)
            if sub_type == "ban":
                duration = data.get("duration", 0) // 60
                sys_msg_text = f"{user_name} 被管理员禁言了 {duration} 分钟"
            elif sub_type == "lift_ban":
                sys_msg_text = f"{user_name} 被解除了禁言"
        elif notice_type == "group_admin":
            user_name = get_qq_name(TARGET_GROUP_ID, user_qq)
            if sub_type == "set":
                sys_msg_text = f"{user_name} 荣升为群管理员"
            elif sub_type == "unset":
                sys_msg_text = f"{user_name} 被取消了管理员"

        if sys_msg_text:
            print(f"[系统事件]: {sys_msg_text}")

            extra = {}
            if notice_type == "group_recall" and recalled_local_id is not None:
                extra["recall_target_id"] = recalled_local_id
            if notice_type == "group_recall" and recalled_qq_msg_id:
                extra["recalled_qq_msg_id"] = str(recalled_qq_msg_id)

            add_chat_message(
                "系统提示",
                f'<span style="color:#ff6600; font-size:12px;">[通知] {html.escape(sys_msg_text)}</span>',
                "系统",
                sys_msg_text,
                **extra,
            )

    return jsonify({"status": "ok"})


@app.route("/view_image")
def view_image():
    account = get_active_account()
    if not account:
        return "请先登录", 403

    img_id = request.args.get("b64")
    if not img_id:
        return "缺少图片参数", 400

    try:
        img_doc = images_collection.find_one({"_id": img_id})
        if not img_doc:
            return "图片已被清理或不存在", 404

        img_bytes = base64.b64decode(img_doc["data"])
        return send_file(io.BytesIO(img_bytes), mimetype="image/jpeg")

    except Exception as e:
        print(f"图片处理异常: {e}")
        return "图片格式不受支持或源图片已失效", 500


@app.route("/api/download_file")
def download_web_file():
    account = get_active_account()
    if not account:
        return "请先登录", 403

    file_id = request.args.get("id")
    if not file_id:
        return "缺少文件参数", 400

    try:
        file_doc = files_collection.find_one({"_id": file_id})

        if file_doc:
            filename = file_doc.get("filename", "未知文件")
            from urllib.parse import quote

            encoded_filename = quote(filename)

            if "data" in file_doc:
                file_bytes = file_doc["data"]
                file_io = io.BytesIO(file_bytes)
                response = make_response(
                    send_file(file_io, as_attachment=True, download_name=filename)
                )
                response.headers["Content-Disposition"] = (
                    f"attachment; filename*=UTF-8''{encoded_filename}"
                )
                return response
            else:
                return "该文件存于旧版网盘，已无法下载", 410
        else:
            return "文件已被清理或不存在", 404

    except Exception as e:
        print(f"文件下载异常: {e}")
        return "文件处理异常", 500


@app.route("/api/download_qq_file")
def download_qq_file():
    account = get_active_account()
    if not account:
        return "请先登录", 403

    file_url = request.args.get("url")
    file_name = request.args.get("name", "未知文件")

    if not file_url:
        return "缺少文件下载链接", 400

    # 只代下载服务器自己生成并签过名的地址，防止被当成任意网址的代理
    if not hmac.compare_digest(
        request.args.get("sig", "").encode("utf-8"), sign_url(file_url).encode("utf-8")
    ):
        return "下载链接无效或已过期", 403

    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        req = requests.get(file_url, headers=headers, stream=True, timeout=10)

        if req.status_code != 200:
            return f"下载失败，腾讯服务器返回状态码: {req.status_code}", 500

        encoded_filename = urllib.parse.quote(file_name)

        response_headers = {
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}",
            "Content-Type": req.headers.get("Content-Type", "application/octet-stream"),
        }

        if "Content-Length" in req.headers:
            response_headers["Content-Length"] = req.headers["Content-Length"]

        return Response(
            stream_with_context(req.iter_content(chunk_size=1024 * 512)),
            headers=response_headers,
        )
    except Exception as e:
        print(f"中转QQ文件下载异常: {e}")
        return "文件处理异常或链接已失效", 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    print(f"🚀 QQ 中转后端已启动！监听 {port} 端口...")
    app.run(host="0.0.0.0", port=port)
