import base64
import datetime
import hmac
import io
import json
import os
import re
import secrets
import sys
import threading
import time
from collections import OrderedDict
from datetime import timezone, timedelta

import emoji
from flask import request
from PIL import Image
from pymongo import MongoClient, ReturnDocument


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


TZ_UTC8 = timezone(timedelta(hours=8))


MONGO_URI = os.environ.get("MONGO_URI", "")


client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)


db = client["nokia_qq_db"]


users_collection = db["users"]


chat_collection = db["chat_history"]


images_collection = db["images"]


files_collection = db["files"]


settings_collection = db["settings"]


def load_secret_key():
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


SECRET_KEY = load_secret_key()


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
    with open(os.path.join(BASE_DIR, filename), encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def load_qq_faces():
    with open(os.path.join(BASE_DIR, "face_config.json"), encoding="utf-8") as f:
        faces = json.load(f).get("sysface", [])
    return {
        int(face["QSid"]): face["QDes"].lstrip("/")
        for face in faces
        if face.get("QSid") and face.get("QDes")
    }


QQ_FACES = load_qq_faces()


KAOMOJI_LIST = list(
    dict.fromkeys(
        line.split("\t")[0] for line in read_data_lines("all_output_result_kmj.txt")
    )
)


BANNED_WORDS = read_data_lines("sensitive_words.txt")


print(f"✅ 已加载 {len(QQ_FACES)} 个 QQ 表情、{len(KAOMOJI_LIST)} 个颜文字、{len(BANNED_WORDS)} 个敏感词")


def load_chat_history():
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
    try:
        chat_collection.insert_one(msg_dict.copy())
        if msg_dict["id"] > 5000:
            chat_collection.delete_many({"id": {"$lte": msg_dict["id"] - 5000}})
    except Exception as e:
        print(f"写入云端聊天记录失败: {e}")


def cleanup_old_images(max_limit=500):
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
    img = Image.open(source)
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)

    img_io = io.BytesIO()
    img.save(img_io, "JPEG", quality=quality)
    return base64.b64encode(img_io.getvalue()).decode("utf-8")


def save_image(img_id, b64_data):
    images_collection.replace_one(
        {"_id": img_id}, {"data": b64_data, "time": time.time()}, upsert=True
    )
    cleanup_old_images()


chat_history, current_msg_id = load_chat_history()


TARGET_GROUP_ID = int(os.environ.get("TARGET_GROUP_ID", "0"))


processed_notices_cache = LimitedDict(1000, 3600)


APP_START_TIME = int(time.time())


internal_file_cache = LimitedDict(100, 300)


recent_web_uploads = LimitedDict(100, 300)


online_sessions = {}


ONLINE_TIMEOUT = 30


def get_online_count():
    current_time = time.time()
    expired_accounts = [
        acc
        for acc, last_active in online_sessions.items()
        if current_time - last_active > ONLINE_TIMEOUT
    ]
    for acc in expired_accounts:
        del online_sessions[acc]
    return len(online_sessions)


FILES_TOTAL_LIMIT = 250 * 1024 * 1024


chat_lock = threading.Lock()


def paginate(total, per_page):
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(max(request.args.get("page", 1, type=int), 1), total_pages)
    return page, total_pages, (page - 1) * per_page


def search_query(q, *fields):
    if not q:
        return {}
    pattern = {"$regex": re.escape(q), "$options": "i"}
    return {"$or": [{field: pattern} for field in fields]}


def has_banned_word(text):
    clean = re.sub(r"[^\w\u4e00-\u9fa5]", "", text).lower()
    return any(word.lower() in clean for word in BANNED_WORDS)


def add_chat_message(title, text, pure_sender, pure_text, **extra):
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


def sign_url(url):
    return hmac.new(
        SECRET_KEY.encode("utf-8"), url.encode("utf-8"), "sha256"
    ).hexdigest()[:32]


def cleanup_old_files(max_total=FILES_TOTAL_LIMIT):
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
