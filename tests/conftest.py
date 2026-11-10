import io
import os
import sys

import mongomock
import pymongo
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

GROUP = 10001
BOT = "99999"
TOKEN = "testtoken"

os.environ.update(
    MONGO_URI="mongodb://localhost",
    TARGET_GROUP_ID=str(GROUP),
    BOT_QQ=BOT,
    WEBHOOK_TOKEN=TOKEN,
)
pymongo.MongoClient = mongomock.MongoClient


class FakeResp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class FakeBot:
    def __init__(self):
        self.calls = []
        self.roles = {}
        self.next_msg_id = 5000

    def post(self, url, json=None, timeout=None):
        action = url.rsplit("/", 1)[-1]
        self.calls.append((action, json))
        if action == "send_group_msg":
            self.next_msg_id += 1
            return FakeResp({"status": "ok", "data": {"message_id": self.next_msg_id}})
        if action == "get_group_member_info":
            uid = str(json.get("user_id"))
            return FakeResp({"status": "ok", "data": {"role": self.roles.get(uid, "member"), "nickname": f"n{uid}", "card": ""}})
        if action == "get_group_member_list":
            return FakeResp({"status": "ok", "data": []})
        if action == "upload_group_file":
            return FakeResp({"status": "ok"})
        return FakeResp({"status": "ok", "data": {}})

    def said(self):
        out = []
        for action, payload in self.calls:
            if action != "send_group_msg":
                continue
            msg = payload["message"]
            if isinstance(msg, list):
                msg = "".join(seg["data"].get("text", "") for seg in msg if seg["type"] == "text")
            out.append(msg)
        return out


@pytest.fixture
def bot(monkeypatch):
    import requests

    fake = FakeBot()
    monkeypatch.setattr(requests, "post", fake.post)

    def no_network(*args, **kwargs):
        raise requests.ConnectionError("network disabled in tests")

    monkeypatch.setattr(requests, "get", no_network)
    return fake


@pytest.fixture
def app_env(bot):
    import app as app_module
    import accounts
    import common
    import qq
    import sending

    for coll in (common.users_collection, common.chat_collection, common.images_collection, common.files_collection):
        coll.delete_many({})
    accounts.users_db.clear()
    accounts.login_failures.clear()
    accounts.last_register_time = 0
    common.chat_history.clear()
    common.online_sessions.clear()
    qq.recent_sent_messages.clear()
    qq.qq_name_cache.clear()
    qq.name_to_qq_cache.clear()
    sending.ip_last_send_time.clear()
    sending.ip_last_message.clear()
    sending.global_last_send_time = 0
    if hasattr(accounts, "ip_register_time"):
        accounts.ip_register_time.clear()

    app_module.app.config["TESTING"] = True
    return app_module


@pytest.fixture
def client(app_env):
    return app_env.app.test_client()


def webhook(client, data):
    return client.post(f"/webhook?token={TOKEN}", json=data)


def group_text(client, text, user_id="123", role="member", nickname="群友"):
    return webhook(
        client,
        {
            "post_type": "message",
            "message_type": "group",
            "group_id": GROUP,
            "self_id": int(BOT),
            "message_id": 777,
            "sender": {"user_id": int(user_id), "nickname": nickname, "role": role},
            "message": [{"type": "text", "data": {"text": text}}],
        },
    )


def admin(client, text):
    return group_text(client, text, user_id="1", role="owner", nickname="群主")


def register(client, account, password="pw123456", reason="想聊天", ip="1.1.1.1"):
    return client.post(
        "/login",
        data={"account": account, "password": password, "reason": reason, "register_btn": "注册"},
        headers={"X-Forwarded-For": ip},
    )


def login(client, account, password="pw123456"):
    return client.post("/login", data={"account": account, "password": password, "login_btn": "登录"})


def make_user(client, account, ip="1.1.1.1", password="pw123456"):
    import accounts

    register(client, account, password=password, ip=ip)
    accounts.last_register_time = 0
    if hasattr(accounts, "ip_register_time"):
        accounts.ip_register_time.clear()
    admin(client, f"通过 {account}")


def png_bytes():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (10, 10), "red").save(buf, "PNG")
    buf.seek(0)
    return buf
