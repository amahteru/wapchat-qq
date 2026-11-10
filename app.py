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
    render_template,
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
WEBHOOK_SECRET_TOKEN = os.environ.get("WEBHOOK_TOKEN", "")
ONEBOT_URL = "http://127.0.0.1:3000"
ACCOUNT_RE = re.compile(r"^[A-Za-z0-9_]{3,15}$")

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


BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def read_data_lines(filename):
    """读取随代码一起发布的数据文件，返回去掉空行后的各行。"""
    with open(os.path.join(BASE_DIR, filename), encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def load_qq_faces():
    """QQ 原生小黄脸：编号 -> 名称(如 14 -> 微笑)。"""
    with open(os.path.join(BASE_DIR, "face_config.json"), encoding="utf-8") as f:
        faces = json.load(f).get("sysface", [])
    return {
        int(face["QSid"]): face["QDes"].lstrip("/")
        for face in faces
        if face.get("QSid") and face.get("QDes")
    }


QQ_FACES = load_qq_faces()
# 颜文字库每行是"颜文字<TAB>其他列"，只取第一列并去重
KAOMOJI_LIST = list(
    dict.fromkeys(
        line.split("\t")[0] for line in read_data_lines("all_output_result_kmj.txt")
    )
)
BANNED_WORDS = read_data_lines("sensitive_words.txt")
print(f"✅ 已加载 {len(QQ_FACES)} 个 QQ 表情、{len(KAOMOJI_LIST)} 个颜文字、{len(BANNED_WORDS)} 个敏感词")


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


def to_jpeg_b64(source, max_width, max_height, quality):
    """把图片等比缩小到指定尺寸以内并转成 JPEG，返回 base64 文本。"""
    img = Image.open(source)
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)

    img_io = io.BytesIO()
    img.save(img_io, "JPEG", quality=quality)
    return base64.b64encode(img_io.getvalue()).decode("utf-8")


def save_image(img_id, b64_data):
    """把图片存进图床；同一张图重复保存只刷新时间。超出上限时清理最旧的。"""
    images_collection.replace_one(
        {"_id": img_id}, {"data": b64_data, "time": time.time()}, upsert=True
    )
    cleanup_old_images()


chat_history, current_msg_id = load_chat_history()

TARGET_GROUP_ID = int(os.environ.get("TARGET_GROUP_ID", "0"))

qq_name_cache = LimitedDict(1000)


def get_qq_name(group_id, qq):
    qq_str = str(qq)
    if qq_str in qq_name_cache:
        return qq_name_cache[qq_str]
    try:
        resp = onebot("get_group_member_info", {"group_id": group_id, "user_id": int(qq)}, timeout=1.5)

        if resp and resp.get("status") in ["ok", "success"]:
            data = resp.get("data", {})
            name = clean_qq_name(data.get("card") or data.get("nickname")) or qq_str

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
        resp = onebot("get_group_member_list", {"group_id": TARGET_GROUP_ID}, timeout=2)

        if resp and resp.get("status") in ["ok", "success"]:
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


@app.context_processor
def inject_theme():
    """所有页面模板都能直接用 theme，不必每个视图各传一遍。"""
    return {"theme": request.cookies.get("nokia_theme", "light")}


def onebot(action, payload, timeout=3):
    """调用本机 NapCat 的 OneBot 接口，返回解析后的 JSON。"""
    return requests.post(f"{ONEBOT_URL}/{action}", json=payload, timeout=timeout).json()


def clean_qq_name(name):
    """QQ 昵称/群名片转成老手机能显示的纯文字：emoji 和方括号内容都去掉。"""
    return re.sub(r"\[.*?\]", "", filter_emoji(name or "")).strip()


def paginate(total, per_page):
    """按请求里的 page 参数算出 (当前页, 总页数, 要跳过的条数)。"""
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(max(request.args.get("page", 1, type=int), 1), total_pages)
    return page, total_pages, (page - 1) * per_page


def search_query(q, *fields):
    """生成"任一字段包含 q"的 MongoDB 查询条件；q 按普通文字处理，不当正则。"""
    if not q:
        return {}
    pattern = {"$regex": re.escape(q), "$options": "i"}
    return {"$or": [{field: pattern} for field in fields]}


def upload_result(status, msg=""):
    """上传接口的返回页：在隐藏 iframe 里回调父页面的 uploadCallback。"""
    safe_msg = json.dumps(str(msg)).replace("<", "&lt;")
    return f'<script type="text/javascript">/* <![CDATA[ */window.parent.uploadCallback("{status}", {safe_msg});/* ]]> */</script>'


def wml_response(raw_content, target_card=None):
    """老手机直接返回 WML；电脑和现代手机看不了 WML，转成 XHTML 再返回。"""
    if is_mobile_device(request):
        resp = make_response(raw_content)
        resp.headers["Content-Type"] = "text/vnd.wap.wml; charset=utf-8"
    else:
        resp = make_response(adapt_wml_to_xhtmlmp(raw_content, target_card))
        resp.headers["Content-Type"] = "text/html; charset=utf-8"
    return resp


def process_login_form():
    """处理登录/注册表单(网页版和 WML 版共用)。返回 (是否已登录, 错误提示, 成功提示)。"""
    account = request.form.get("account", "").strip()
    password = request.form.get("password", "").strip()

    if not account or not password:
        return False, "账号和密码不能为空！", None

    if not ACCOUNT_RE.match(account):
        return False, "账号只能包含3-15位字母、数字或下划线！", None

    if "register_btn" in request.form:
        ok, msg = register_account(
            account, password, request.form.get("reason", ""), request
        )
        return (False, None, msg) if ok else (False, msg, None)

    error = try_login(account, password)
    return error is None, error, None


def has_banned_word(text):
    """文字里是否含敏感词。先去掉标点和空格再比，防止用符号隔开来绕过。"""
    clean = re.sub(r"[^\w\u4e00-\u9fa5]", "", text).lower()
    return any(word.lower() in clean for word in BANNED_WORDS)


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
        r"(?<![\w.-])@([\w\u4e00-\u9fa5·_-]+)",
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
    if has_banned_word(name):
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
        onebot("send_group_msg", {"group_id": TARGET_GROUP_ID, "message": [{"type": "text", "data": {"text": text}}]}, timeout=5)
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

    if has_banned_word(reason):
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
            resp = onebot("get_group_member_info", {"group_id": TARGET_GROUP_ID, "user_id": int(sender.get("user_id", 0)), "no_cache": True}, timeout=2)
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

    intercept_msg = None
    if len(msg_text) > MAX_MSG_LENGTH:
        intercept_msg = f"[系统拦截]：消息过长，最多允许 {MAX_MSG_LENGTH} 个字符。"
    elif "[CQ:" in msg_text:
        intercept_msg = "[系统拦截]：禁止发送带有特殊控制符的代码！"
    elif has_banned_word(msg_text):
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
    mention_pattern = r"\[@([^\]]+)\]|(?<![\w.-])@([\w\u4e00-\u9fa5·_-]+)"
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
        resp = onebot("send_group_msg", {"group_id": TARGET_GROUP_ID, "message": full_message}, timeout=3)
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
    error = success = None
    if request.method == "POST":
        logged_in, error, success = process_login_form()
        if logged_in:
            return redirect(url_for("index"))
    elif get_active_account():
        return redirect(url_for("index"))

    return render_template("login.html", error=error, success=success)


@app.route("/logout", methods=["GET"])
def logout():
    session.pop("nokia_account", None)
    return redirect(url_for("login_page"))


@app.route("/history", methods=["GET"])
def view_history():
    if not get_active_account():
        return redirect(url_for("login_page"))

    per_page = 25
    q = request.args.get("q", "").strip()
    query = search_query(q, "pure_text", "pure_sender")
    page, total_pages, skip = paginate(chat_collection.count_documents(query), per_page)

    docs = list(
        chat_collection.find(query, {"_id": 0}).sort("id", -1).skip(skip).limit(per_page)
    )
    docs.reverse()

    return render_template(
        "history.html", messages=docs, page=page, total_pages=total_pages, q=q
    )


@app.route("/online", methods=["GET"])
def view_online_users():
    account = get_active_account()
    if not account:
        return redirect(url_for("login_page"))

    get_online_count()

    active_users = [
        {
            "account": acc,
            "nickname": filter_emoji(users_db.get(acc, {}).get("nickname", acc)),
            "is_me": acc == account,
        }
        for acc in online_sessions
    ]
    active_users.sort(key=lambda x: (not x["is_me"], x["account"]))

    return render_template("online.html", users=active_users)


@app.route("/emojis", methods=["GET"])
def view_emojis():
    if not get_active_account():
        return redirect(url_for("login_page"))

    # 每页个数由聊天页按屏幕大小算好放在 cookie 里
    try:
        per_page = min(max(int(request.cookies.get("emoji_limit", 40)), 1), 500)
    except ValueError:
        per_page = 40

    page, total_pages, skip = paginate(len(KAOMOJI_LIST), per_page)
    emojis = [
        {"raw": k, "encoded": urllib.parse.quote(k)}
        for k in KAOMOJI_LIST[skip : skip + per_page]
    ]

    return render_template(
        "emojis.html", emojis=emojis, page=page, total_pages=total_pages
    )


@app.route("/toggle_theme", methods=["GET"])
def toggle_theme():
    current_theme = request.cookies.get("nokia_theme", "light")
    new_theme = "dark" if current_theme == "light" else "light"

    next_url = request.referrer or url_for("index")

    resp = make_response(redirect(next_url))
    resp.set_cookie("nokia_theme", new_theme, max_age=31536000)
    return resp


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
        return upload_result("error", "权限不足或未登录")

    file = request.files.get("image")
    if not file or file.filename == "":
        return upload_result("error", "未选择任何照片")

    try:
        b64_data = to_jpeg_b64(file.stream, 800, 800, 80)
        cq_code = f"[CQ:image,file=base64://{b64_data}]"

        username = users_db[account].get("nickname", account)
        client_ip = get_real_ip(request)
        location = get_ip_location(client_ip, request)

        full_message = f"[{location} - {username}]\n{cq_code}"

        onebot("send_group_msg", {"group_id": TARGET_GROUP_ID, "message": full_message}, timeout=8)

        img_id = hashlib.md5(b64_data.encode("utf-8")).hexdigest()[:16]
        save_image(img_id, b64_data)

        add_chat_message(
            "消息",
            f"[网页-{location}]{html.escape(username)}: [图片] <a href='/view_image?b64={img_id}' target='_blank' class='action-at'>[查看]</a>",
            username,
            "[图片]",
        )

        return upload_result("ok")

    except Exception as e:
        print(f"上传图片失败: {e}")
        return upload_result("error", "文件处理失败")


@app.route("/api/upload_file", methods=["POST"])
def upload_file():
    account = get_active_account()
    if not account:
        return upload_result("error", "权限不足或未登录")

    file = request.files.get("file")
    if not file or file.filename == "":
        return upload_result("error", "未选择任何文件")

    try:
        filename = file.filename
        username = users_db[account].get("nickname", account)
        client_ip = get_real_ip(request)
        location = get_ip_location(client_ip, request)

        recent_web_uploads[filename] = username

        file_bytes = file.read()
        if not file_bytes:
            return upload_result("error", "不能发送空文件")

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
            onebot("send_group_msg", {"group_id": TARGET_GROUP_ID, "message": qq_notification_text}, timeout=3)
        except Exception as e:
            print(f"发送文件前置文字提示失败: {e}")

        local_port = os.environ.get("PORT", 7860)
        download_url = f"http://127.0.0.1:{local_port}/api/internal_download/{file_id}"

        # 机器人通过内部地址回来取文件；无论成败都要释放这份内存副本
        internal_file_cache[file_id] = {"bytes": file_bytes, "name": filename}
        try:
            resp_data = onebot("upload_group_file", {"group_id": TARGET_GROUP_ID, "file": download_url, "name": filename}, timeout=60)
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

        return upload_result("ok")

    except Exception as e:
        print(f"上传文件失败: {e}")
        return upload_result("error", e)


@app.route("/drive", methods=["GET"])
def view_drive():
    if not get_active_account():
        return redirect(url_for("login_page"))

    per_page = 20
    q = request.args.get("q", "").strip()

    # 旧版本存在 R2 上的文件(只有 r2_key 没有 data)已无法读取，不再列出
    query = {"data": {"$exists": True}, **search_query(q, "filename", "uploader")}
    page, total_pages, skip = paginate(files_collection.count_documents(query), per_page)

    display_files = []
    for f in (
        files_collection.find(query, {"data": 0}).sort("time", -1).skip(skip).limit(per_page)
    ):
        size_bytes = f.get("size", 0)
        dt = datetime.datetime.fromtimestamp(f.get("time", 0), tz=TZ_UTC8)
        display_files.append(
            {
                "_id": f["_id"],
                "filename": f.get("filename", "未知文件"),
                "uploader": f.get("uploader", "系统"),
                "size_mb": round(size_bytes / (1024 * 1024), 2) if size_bytes else "未知",
                "upload_time": dt.strftime("%Y-%m-%d %H:%M"),
            }
        )

    return render_template(
        "drive.html", files=display_files, page=page, total_pages=total_pages, q=q
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
    error = success = None
    if request.method == "POST":
        logged_in, error, success = process_login_form()
        if logged_in:
            return redirect("/wml/")
    elif get_active_account():
        return redirect("/wml/")

    return wml_response(render_template("wml_login.xml", error=error, success=success))


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

    raw_content = render_template(
        "wml_chat.xml",
        history=wml_history,
        saved_username=filter_emoji(users_db[account].get("nickname", account)),
        online_count=get_online_count(),
        notice=notice,
    )
    return wml_response(raw_content, request.args.get("card", "chat"))


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

    online_sessions[account] = time.time()

    return render_template(
        "chat.html",
        history=chat_history[-25:],
        last_id=chat_history[-1]["id"] if chat_history else 0,
        saved_username=filter_emoji(users_db[account].get("nickname", account)),
        online_count=get_online_count(),
        ai_url=os.environ.get("AI_URL", "#"),
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

    response_msgs = []
    if intercept_msg:
        response_msgs.append({"sender_title": "系统", "text": intercept_msg})

    for m in chat_history:
        if m["id"] <= client_last_id:
            continue
        msg_dict = {
            "id": m["id"],
            "sender_title": m.get("sender_title", "QQ群"),
            "text": m["text"],
            "pure_sender": m.get("pure_sender", ""),
        }
        if m.get("recall_target_id") is not None:
            msg_dict["recall_target_id"] = m["recall_target_id"]

        response_msgs.append(msg_dict)

    return jsonify(
        {
            "messages": response_msgs,
            "last_id": chat_history[-1]["id"] if chat_history else client_last_id,
            "online_count": get_online_count(),
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

        sender = data.get("sender", {})
        sender_name = clean_qq_name(sender.get("nickname", "某人")) or "无名氏"

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
                                # 老手机屏幕窄，群里的图只按 240 宽存一份缩略图
                                save_image(
                                    img_id,
                                    to_jpeg_b64(io.BytesIO(resp.content), 240, 100000, 70),
                                )
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
                        resp = onebot("get_msg", {"message_id": msg_id}, timeout=1.5)
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
                        resp = onebot("get_group_file_url", {"group_id": TARGET_GROUP_ID, "file_id": file_id, "busid": file_busid}, timeout=2)
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
