import base64
import functools
import datetime
import hashlib
import hmac
import mimetypes
import os
import html
import re
import time
import urllib.parse
import uuid

from flask import (
    Flask,
    request,
    flash,
    get_flashed_messages,
    render_template,
    redirect,
    url_for,
    make_response,
    session,
    Response,
    stream_with_context,
)
from flask_compress import Compress
import requests

from common import (
    KAOMOJI_LIST,
    SECRET_KEY,
    TARGET_GROUP_ID,
    TZ_UTC8,
    add_chat_message,
    chat_collection,
    chat_history,
    cleanup_old_files,
    files_collection,
    format_size,
    get_online_count,
    images_collection,
    online_sessions,
    paginate,
    save_image,
    search_query,
    sign_url,
    to_jpeg_b64,
)
from qq import onebot, send_group, text_seg
from location import get_ip_location
from accounts import (
    nickname,
    get_active_account,
    get_real_ip,
    process_login_form,
    set_nickname,
    users_db,
)
from sending import send_web_message
from webhook import receive_qq_msg


app = Flask(__name__)


app.config["MAX_CONTENT_LENGTH"] = 15 * 1024 * 1024


app.config["COMPRESS_ALGORITHM"] = "gzip"


app.config["COMPRESS_MIN_SIZE"] = 100


app.config["COMPRESS_MIMETYPES"] = ["text/html"]


Compress(app)


app.permanent_session_lifetime = datetime.timedelta(days=30)


app.config["SESSION_COOKIE_SAMESITE"] = "Lax"


app.secret_key = SECRET_KEY


MESSAGES_PER_PAGE = 10


@app.after_request
def no_cache(response):
    if response.mimetype in ("text/html", "text/vnd.wap.wml"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "Thu, 01 Jan 1970 00:00:00 GMT"
    return response


@app.context_processor
def inject_theme():
    return {"theme": request.cookies.get("nokia_theme", "light")}


def login_required(view_or_url=None):
    login_url = view_or_url if isinstance(view_or_url, str) else "/login"

    def decorate(view):
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            account = get_active_account()
            if not account:
                return redirect(login_url)
            return view(account, *args, **kwargs)

        return wrapper

    return decorate if isinstance(view_or_url, str) else decorate(view_or_url)


def take_notice():
    return next(iter(get_flashed_messages()), None)


def send_and_redirect(account, message):
    session.pop("draft", None)
    notice = send_web_message(account, message, request)
    if notice:
        flash(notice)
        session["draft"] = message
    return redirect(url_for("index"))


def wml_response(raw_content):
    resp = make_response(raw_content)
    resp.headers["Content-Type"] = "text/vnd.wap.wml; charset=utf-8"
    return resp


@app.route("/login", methods=["GET", "POST"])
def login_page():
    return login_view(
        url_for("login_page"),
        url_for("index"),
        lambda **notices: render_template("login.html", **notices),
    )


def login_view(self_url, home_url, render):
    if request.method == "POST":
        logged_in, error, success = process_login_form()
        if logged_in:
            return redirect(home_url)
        flash(error or success, "error" if error else "success")
        return redirect(self_url)
    if get_active_account():
        return redirect(home_url)
    notices = dict(get_flashed_messages(with_categories=True))
    return render(error=notices.get("error"), success=notices.get("success"))


@app.route("/logout", methods=["GET"])
def logout():
    session.pop("nokia_account", None)
    return redirect(url_for("login_page"))


@app.route("/history", methods=["GET"])
@login_required
def view_history(account):

    per_page = MESSAGES_PER_PAGE
    q = request.args.get("q", "").strip()
    query = search_query(q, "pure_text", "pure_sender")
    page, total_pages, skip = paginate(chat_collection.count_documents(query), per_page)

    docs = list(
        chat_collection.find(query, {"_id": 0}).sort("id", -1).skip(skip).limit(per_page)
    )

    return render_template(
        "history.html", messages=docs, page=page, total_pages=total_pages, q=q
    )


@app.route("/online", methods=["GET"])
@login_required
def view_online_users(account):

    get_online_count()

    active_users = [
        {
            "account": acc,
            "nickname": users_db[acc]["nickname"],
            "is_me": acc == account,
        }
        for acc in list(online_sessions)
        if acc in users_db
    ]
    active_users.sort(key=lambda x: (not x["is_me"], x["account"]))

    return render_template("online.html", users=active_users)


EMOJIS_PER_PAGE = 30


@app.route("/emojis", methods=["GET", "POST"])
@login_required
def view_emojis(account):

    if request.method == "POST":
        message = request.form.get("message", "")
        if "add" in request.form:
            session["draft"] = message + request.form["add"]
            return redirect(url_for("view_emojis", page=request.args.get("page", 1, type=int)))
        return send_and_redirect(account, message)

    page, total_pages, skip = paginate(len(KAOMOJI_LIST), EMOJIS_PER_PAGE)
    return render_template(
        "emojis.html",
        emojis=KAOMOJI_LIST[skip : skip + EMOJIS_PER_PAGE],
        page=page,
        total_pages=total_pages,
        draft=session.get("draft", ""),
        notice=take_notice(),
    )


@app.route("/toggle_theme", methods=["GET"])
def toggle_theme():
    current_theme = request.cookies.get("nokia_theme", "light")
    new_theme = "dark" if current_theme == "light" else "light"

    referrer = urllib.parse.urlsplit(request.referrer or "")
    next_url = url_for("index")
    if referrer.netloc == request.host and referrer.path.startswith("/"):
        next_url = referrer.path + (f"?{referrer.query}" if referrer.query else "")

    resp = make_response(redirect(next_url))
    resp.set_cookie("nokia_theme", new_theme, max_age=31536000)
    return resp


@app.route("/menu")
@login_required
def menu_page(account):
    return render_template("menu.html")


@app.route("/rename", methods=["GET", "POST"])
@login_required
def rename_page(account):

    if request.method == "POST":
        new_name, error = set_nickname(account, request.form.get("new_name", ""))
        if error:
            flash(error)
            return redirect(url_for("rename_page"))
        flash(f"昵称已改为 {new_name}。")
        return redirect(url_for("index"))

    return render_template("rename.html", saved_username=nickname(account), notice=take_notice())


@app.route("/upload")
@login_required
def upload_page(account):
    return render_template("upload.html", notice=take_notice())


def back_to_upload(message):
    flash(message)
    return redirect(url_for("upload_page"))


@app.route("/upload/image", methods=["POST"])
@login_required
def upload_image(account):

    file = request.files.get("image")
    if not file or file.filename == "":
        return back_to_upload("没有选择图片。")

    try:
        image_bytes = file.read()
        qq_image = to_jpeg_b64(image_bytes, 800, 800, 80)
        web_image = to_jpeg_b64(image_bytes, 240, 100000, 70)

        username = nickname(account)
        location = get_ip_location(get_real_ip(request))

        caption = f"[{location} - {username}]\n"
        qq_msg_id = send_group(
            [text_seg(caption), {"type": "image", "data": {"file": f"base64://{qq_image}"}}],
            timeout=8,
        )

        img_id = hashlib.md5(web_image.encode("utf-8")).hexdigest()[:16]
        save_image(img_id, web_image)

        add_chat_message(
            "网页",
            f"[网页-{html.escape(location)}]{html.escape(username)}: [图片] <a href='/view_image?b64={img_id}'>[查看]</a>",
            username,
            "[图片]",
            qq_msg_id=qq_msg_id,
        )
    except Exception as e:
        print(f"上传图片失败: {e}")
        return back_to_upload("图片发送失败，请稍后再试。")

    flash("图片已发送。")
    return redirect(url_for("index"))


@app.route("/upload/file", methods=["POST"])
@login_required
def upload_file(account):

    file = request.files.get("file")
    if not file or file.filename == "":
        return back_to_upload("没有选择文件。")

    filename = file.filename
    file_bytes = file.read()
    if not file_bytes:
        return back_to_upload("不能发送空文件。")

    try:
        username = nickname(account)
        location = get_ip_location(get_real_ip(request))
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

        resp_data = onebot(
            "upload_group_file",
            {"group_id": TARGET_GROUP_ID, "file": "base64://" + base64.b64encode(file_bytes).decode(), "name": filename},
            timeout=60,
        )
        if resp_data.get("status") != "ok":
            raise RuntimeError(resp_data.get("wording") or resp_data.get("message") or "机器人返回失败")

        qq_msg_id = send_group([text_seg(f"[{location} - {username}] [文件: {filename}]")], timeout=3)

        add_chat_message(
            "网页",
            f"[网页-{html.escape(location)}]{html.escape(username)}: <span style='color:#009900;'>[文件: {html.escape(filename)}]</span> <a href='{file_link(file_id, filename)}'>[下载]</a>",
            username,
            f"[文件: {filename}]",
            qq_msg_id=qq_msg_id,
        )
    except Exception as e:
        print(f"上传文件失败: {e}")
        return back_to_upload("文件发送失败，请稍后再试。")

    flash("文件已发送。")
    return redirect(url_for("index"))


@app.route("/drive", methods=["GET"])
@login_required
def view_drive(account):

    per_page = 20
    q = request.args.get("q", "").strip()

    query = search_query(q, "filename", "uploader")
    page, total_pages, skip = paginate(files_collection.count_documents(query), per_page)

    display_files = []
    for f in (
        files_collection.find(query, {"data": 0}).sort("time", -1).skip(skip).limit(per_page)
    ):
        dt = datetime.datetime.fromtimestamp(f["time"], tz=TZ_UTC8)
        display_files.append(
            {
                "link": file_link(f["_id"], f["filename"]),
                "filename": f["filename"],
                "uploader": f["uploader"],
                "size": format_size(f["size"]),
                "upload_time": dt.strftime("%Y-%m-%d %H:%M"),
            }
        )

    return render_template(
        "drive.html", files=display_files, page=page, total_pages=total_pages, q=q
    )


@app.route("/wml/login", methods=["GET", "POST"])
def wml_login_page():
    return login_view(
        "/wml/login",
        "/wml/",
        lambda **notices: wml_response(render_template("wml_login.xml", **notices)),
    )


@app.route("/wml/logout", methods=["GET"])
def wml_logout():
    session.pop("nokia_account", None)
    return redirect("/wml/login")


@app.route("/wml", methods=["GET", "POST"])
def wml_index_no_slash():
    return redirect("/wml/")


@app.route("/wml/", methods=["GET", "POST"])
@login_required("/wml/login")
def wml_index(account):

    notice = take_notice()
    if request.method == "POST":
        notice = send_web_message(account, request.form.get("message", ""), request)

    wml_history = []
    for m in chat_history[-MESSAGES_PER_PAGE:]:
        wml_msg = dict(m)
        clean_text = re.sub(
            r'<a href=([\'"])([^\'"]+)\1[^>]*>(.*?)</a>',
            r'<a href="\2">\3</a>',
            m["text"],
        )
        wml_msg["wml_text"] = re.sub(r"</?span[^>]*>", "", clean_text)
        wml_history.append(wml_msg)

    wml_history.reverse()

    online_sessions[account] = time.time()

    raw_content = render_template(
        "wml_chat.xml",
        history=wml_history,
        saved_username=nickname(account),
        online_count=get_online_count(),
        notice=notice,
    )
    return wml_response(raw_content)


@app.route("/wml/rename_wml", methods=["POST"])
@login_required("/wml/login")
def wml_rename(account):

    _, error = set_nickname(account, request.form.get("new_name", ""))
    if error:
        flash(error)
    return redirect("/wml/")


@app.route("/", methods=["GET", "POST"])
@login_required
def index(account):

    if request.method == "POST":
        return send_and_redirect(account, request.form.get("message", ""))

    online_sessions[account] = time.time()

    draft = session.pop("draft", "")
    reply_id = request.args.get("reply", type=int)
    target = next((m for m in chat_history if m["id"] == reply_id), None)
    if target:
        draft = f"[回复:{target['id']}] [@{target['pure_sender']}] "
    elif request.args.get("at"):
        draft = f"[@{request.args['at']}] "

    return render_template(
        "chat.html",
        history=chat_history[-MESSAGES_PER_PAGE:],
        online_count=get_online_count(),
        draft=draft,
        notice=take_notice(),
    )


app.add_url_rule("/webhook", view_func=receive_qq_msg, methods=["POST"])


OLD_PHONE_TYPES = {
    ".jar": "application/java-archive",
    ".jad": "text/vnd.sun.j2me.app-descriptor",
    ".sis": "application/vnd.symbian.install",
    ".sisx": "x-epoc/x-sisx-app",
    ".amr": "audio/amr",
    ".3gp": "video/3gpp",
    ".mid": "audio/midi",
    ".mp3": "audio/mpeg",
    ".txt": "text/plain",
}


def file_type(name):
    ext = os.path.splitext(name)[1].lower()
    return OLD_PHONE_TYPES.get(ext) or mimetypes.guess_type(name)[0] or "application/octet-stream"


def download_headers(name):
    ascii_name = name.encode("ascii", "replace").decode().replace("?", "_").replace('"', "_")
    return {
        "Content-Type": file_type(name),
        "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{urllib.parse.quote(name)}",
    }


def file_link(file_id, name):
    return f"/download/{file_id}/{urllib.parse.quote(name, safe='')}"


@app.route("/view_image")
@login_required
def view_image(account):

    img_doc = images_collection.find_one({"_id": request.args.get("b64")})
    if not img_doc:
        return "图片已被清理或不存在。", 404
    return Response(base64.b64decode(img_doc["data"]), mimetype="image/jpeg")


@app.route("/download/<file_id>/<path:filename>")
@login_required
def download_web_file(account, file_id, filename):

    file_doc = files_collection.find_one({"_id": file_id})
    if not file_doc:
        return "文件已被清理或不存在。", 404
    return Response(file_doc["data"], headers=download_headers(file_doc["filename"]))


@app.route("/qqfile/<path:file_name>")
@login_required
def download_qq_file(account, file_name):

    file_url = request.args.get("url")
    if not file_url:
        return "下载链接无效。", 400

    if not hmac.compare_digest(
        request.args.get("sig", "").encode("utf-8"), sign_url(file_url).encode("utf-8")
    ):
        return "下载链接无效或已过期。", 403

    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        req = requests.get(file_url, headers=headers, stream=True, timeout=10)

        if req.status_code != 200:
            return "下载失败，文件可能已过期。", 500

        response_headers = download_headers(file_name)
        if "Content-Length" in req.headers:
            response_headers["Content-Length"] = req.headers["Content-Length"]

        return Response(
            stream_with_context(req.iter_content(chunk_size=1024 * 512)),
            headers=response_headers,
        )
    except Exception as e:
        print(f"中转QQ文件下载异常: {e}")
        return "下载失败，链接可能已失效。", 500

