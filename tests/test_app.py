from conftest import BOT, GROUP, admin, group_text, login, make_user, png_bytes, register, webhook


def test_register_needs_approval(client, bot):
    r = register(client, "alice")
    assert "等待群管理员审核" in r.get_data(as_text=True)
    assert any("注册申请" in m and "alice" in m for m in bot.said())

    r = login(client, "alice")
    assert "审核中" in r.get_data(as_text=True)

    admin(client, "通过 alice")
    assert any("alice 已通过" in m for m in bot.said())
    r = login(client, "alice")
    assert r.status_code == 302


def test_member_cannot_use_admin_commands(client, app_env, bot):
    register(client, "bob")
    group_text(client, "通过 bob", role="member")
    assert app_env.users_db["bob"]["status"] == "pending"


def test_admin_commands_not_forwarded_to_web(client, app_env):
    admin(client, "待审核")
    assert not any("待审核" in m.get("pure_text", "") for m in app_env.chat_history)


def test_reject_and_delete(client, app_env, bot):
    register(client, "carol")
    admin(client, "拒绝 carol")
    assert "carol" not in app_env.users_db
    make_user(client, "dave", ip="2.2.2.2")
    admin(client, "删号 dave")
    assert "dave" not in app_env.users_db


def test_send_message_reaches_qq_and_web(client, app_env, bot):
    make_user(client, "erin")
    login(client, "erin")
    r = client.post("/api/sync", json={"last_id": 0, "message": "你好"})
    texts = [m["text"] for m in r.get_json()["messages"]]
    assert any("你好" in t for t in texts)
    assert any("你好" in m for m in bot.said())


def last_sent_segments(bot):
    return [p["message"] for a, p in bot.calls if a == "send_group_msg"][-1]


def test_cq_code_in_message_sent_as_plain_text(client, bot):
    make_user(client, "frank")
    login(client, "frank")
    client.post("/api/sync", json={"last_id": 0, "message": "[CQ:at,qq=all]"})
    segs = last_sent_segments(bot)
    assert all(s["type"] == "text" for s in segs)
    assert "[CQ:at,qq=all]" in "".join(s["data"]["text"] for s in segs)


def test_mention_becomes_at_segment(client, app_env, bot):
    import qq

    make_user(client, "olga")
    login(client, "olga")
    qq.name_to_qq_cache["小明"] = "555"
    client.post("/api/sync", json={"last_id": 0, "message": "你好 @小明 吃了吗"})
    segs = last_sent_segments(bot)
    assert {"type": "at", "data": {"qq": "555"}} in segs
    text = "".join(s["data"].get("text", "") for s in segs if s["type"] == "text")
    assert "你好 " in text and "吃了吗" in text and "@小明" not in text


def test_reply_to_qq_message_uses_reply_segment(client, app_env, bot):
    make_user(client, "pete")
    login(client, "pete")
    group_text(client, "原消息")
    local_id = app_env.chat_history[-1]["id"]
    client.post("/api/sync", json={"last_id": 0, "message": f"[回复:{local_id}] 收到"})
    segs = last_sent_segments(bot)
    assert segs[0] == {"type": "reply", "data": {"id": "777"}}


def test_group_message_shows_on_web(client, app_env):
    group_text(client, "<b>hi</b>")
    last = app_env.chat_history[-1]
    assert "&lt;b&gt;hi" in last["text"]


def test_echo_of_own_message_ignored(client, app_env, bot):
    make_user(client, "gina")
    login(client, "gina")
    client.post("/api/sync", json={"last_id": 0, "message": "回声测试"})
    before = len(app_env.chat_history)
    sent = bot.said()[-1]
    group_text(client, sent, user_id=BOT)
    assert len(app_env.chat_history) == before


def test_webhook_requires_token(client):
    r = client.post("/webhook?token=wrong", json={})
    assert r.status_code == 403


def test_upload_image(client, app_env, bot):
    make_user(client, "hank")
    login(client, "hank")
    r = client.post("/api/upload_image", data={"image": (png_bytes(), "a.png")}, content_type="multipart/form-data")
    assert "ok" in r.get_data(as_text=True)
    assert app_env.images_collection.count_documents({}) == 1


def test_upload_filename_cannot_inject_cq(client, app_env, bot):
    import io

    make_user(client, "ivan")
    login(client, "ivan")
    client.post(
        "/api/upload_file",
        data={"file": (io.BytesIO(b"x"), "[CQ:at,qq=all].txt")},
        content_type="multipart/form-data",
    )
    for action, payload in bot.calls:
        if action == "send_group_msg":
            msg = payload["message"]
            assert isinstance(msg, list) or "[CQ:at" not in msg


def test_register_does_not_overwrite_existing_db_user(client, app_env):
    from werkzeug.security import generate_password_hash

    import common

    common.users_collection.insert_one({"_id": "judy", "password": generate_password_hash("old"), "status": "active"})
    register(client, "judy", password="hacked")
    doc = common.users_collection.find_one({"_id": "judy"})
    assert doc["status"] == "active"


def test_client_ip_comes_from_proxy_forwarded_for(client, app_env):
    import accounts

    client.post(
        "/login",
        data={"account": "kim1", "password": "pw123456", "reason": "r", "register_btn": "1"},
        headers={"X-Forwarded-For": "8.8.8.8, 2a06:98c0:3600::103", "CF-Connecting-IP": "5.5.5.5", "X-Real-IP-Custom": "5.5.5.5"},
    )
    accounts.last_register_time = 0
    client.post(
        "/login",
        data={"account": "kim2", "password": "pw123456", "reason": "r", "register_btn": "1"},
        headers={"X-Forwarded-For": "8.8.8.8, 2a06:98c0:3600::103", "CF-Connecting-IP": "6.6.6.6", "X-Real-IP-Custom": "6.6.6.6"},
    )
    assert app_env.users_db["kim1"]["ip"] == "8.8.8.8"
    assert "kim2" not in app_env.users_db


def test_proxy_location_headers_are_sanitized(client, app_env, bot):
    import accounts

    accounts.ip_location_cache.clear()
    make_user(client, "uma", ip="8.8.4.4")
    login(client, "uma")
    client.post(
        "/api/sync",
        json={"last_id": 0, "message": "看看地址"},
        headers={"X-Forwarded-For": "8.8.4.4", "X-Real-Country": "CN", "X-Real-Region": "<script>Guangdong"},
    )
    text = app_env.chat_history[-1]["text"]
    assert "<script" not in text
    assert "[网页-中国-scriptGuangdong]" in text


def test_wml_chat(client, app_env):
    make_user(client, "leo")
    login(client, "leo")
    group_text(client, "wml测试")
    r = client.get("/wml/", headers={"User-Agent": "Nokia6300"})
    assert r.headers["Content-Type"].startswith("text/vnd.wap.wml")
    assert "wml测试" in r.get_data(as_text=True)


def test_recall(client, app_env, monkeypatch):
    import webhook as webhook_module

    monkeypatch.setattr(webhook_module, "APP_START_TIME", 0)
    group_text(client, "要撤回")
    webhook(
        client,
        {
            "post_type": "notice",
            "notice_type": "group_recall",
            "group_id": GROUP,
            "user_id": 123,
            "message_id": 777,
            "time": 9999999999,
        },
    )
    assert app_env.chat_history[0]["pure_text"] == "[消息已撤回]"


def test_deleted_user_logged_out(client, app_env):
    make_user(client, "mia")
    login(client, "mia")
    admin(client, "删号 mia")
    r = client.get("/")
    assert r.status_code == 302


def test_history_and_drive_pages(client, app_env):
    make_user(client, "ned")
    login(client, "ned")
    group_text(client, "历史")
    assert client.get("/history").status_code == 200
    assert client.get("/history?q=历").status_code == 200
    assert client.get("/drive").status_code == 200
    assert client.get("/online").status_code == 200
    assert client.get("/emojis").status_code == 200


def test_help_and_status(client, bot):
    admin(client, "帮助")
    assert "封禁 账号" in bot.said()[-1]
    admin(client, "状态")
    assert bot.said()[-1].startswith("[状态]")


def test_user_list_paginates(client, app_env, bot):
    from werkzeug.security import generate_password_hash

    for i in range(35):
        app_env.users_db[f"user{i:02d}"] = {"password": generate_password_hash("x"), "status": "active", "nickname": f"user{i:02d}"}
    admin(client, "用户列表")
    assert "第 1/2 页" in bot.said()[-1]
    admin(client, "用户列表 2")
    assert "user34" in bot.said()[-1]


def test_ban_blocks_login_and_kicks_session(client, app_env, bot):
    make_user(client, "quinn")
    login(client, "quinn")
    admin(client, "封禁 quinn")
    assert client.get("/").status_code == 302
    assert "封禁" in login(client, "quinn").get_data(as_text=True)
    assert "quinn" in app_env.users_db
    admin(client, "解封 quinn")
    assert login(client, "quinn").status_code == 302


def test_reset_password_sends_private_message(client, app_env, bot):
    make_user(client, "rosa")
    admin(client, "重置密码 rosa")
    private = [p for a, p in bot.calls if a == "send_private_msg"]
    assert private and private[-1]["user_id"] == 1
    new_password = private[-1]["message"][0]["data"]["text"].rsplit(": ", 1)[1]
    assert login(client, "rosa", password=new_password).status_code == 302
    assert "密码已重置" in bot.said()[-1]


def test_reset_password_keeps_old_if_private_fails(client, app_env, bot, monkeypatch):
    make_user(client, "sam")
    original = bot.post

    def failing(url, json=None, timeout=None):
        if url.endswith("send_private_msg"):
            raise ConnectionError("not friend")
        return original(url, json=json, timeout=timeout)

    import requests

    monkeypatch.setattr(requests, "post", failing)
    admin(client, "重置密码 sam")
    assert login(client, "sam").status_code == 302


def test_member_status_command_is_normal_chat(client, app_env):
    group_text(client, "状态", role="member")
    assert app_env.chat_history[-1]["pure_text"] == "状态"


def test_admin_recalling_web_message_in_qq_recalls_on_web(client, app_env, bot, monkeypatch):
    import webhook as webhook_module

    monkeypatch.setattr(webhook_module, "APP_START_TIME", 0)
    make_user(client, "tina")
    login(client, "tina")
    client.post("/api/sync", json={"last_id": 0, "message": "说错话了"})
    web_msg = app_env.chat_history[-1]
    webhook(
        client,
        {
            "post_type": "notice",
            "notice_type": "group_recall",
            "group_id": GROUP,
            "user_id": int(BOT),
            "operator_id": 1,
            "message_id": web_msg["qq_msg_id"],
            "time": 9999999999,
        },
    )
    assert web_msg["pure_text"] == "[消息已撤回]"
    assert app_env.chat_history[-1]["pure_text"] == "n1 撤回了一条消息"

