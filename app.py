"""Fuck Discord 管理后台 —— reply.py 的网页版。

功能：
  - 登录鉴权（登录名 + 密码）
  - 商品 CRUD：新增 / 修改 / 删除 / 查询（与截图一致），支持启用图片上传与识别
  - 账号管理：Discord token 增删
  - 系统配置：与 reply.py 完全一致的全部配置项
  - 引擎控制：启动/停止 Discord 监听，查看实时日志
  - 测试匹配：输入文字或上传图片，实时查看会回复什么（演示图片识别）
"""
import os
import functools
import uuid

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, jsonify, send_from_directory, abort,
)
from werkzeug.utils import secure_filename

import config
import store
import matcher
from imagehash_util import compute_hash
from bot_engine import engine

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16MB 上传上限

os.makedirs(config.UPLOAD_DIR, exist_ok=True)
store.init_db()


# ---------------- 鉴权 ----------------
def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


@app.context_processor
def inject_globals():
    return {"current_user": session.get("user"), "engine_running": engine.running}


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if store.verify_user(username, password):
            session["user"] = username
            nxt = request.args.get("next") or url_for("dashboard")
            return redirect(nxt)
        flash("登录名或密码错误", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------- 仪表盘 ----------------
@app.route("/")
@login_required
def dashboard():
    return render_template(
        "dashboard.html",
        stats=store.counts(),
        logs=store.recent_logs(15),
        engine_status=engine.status(),
    )


# ---------------- 商品：查询（列表）----------------
@app.route("/products")
@login_required
def products():
    q = request.args.get("q", "").strip()
    rows = store.list_products(q or None)
    return render_template("products.html", products=rows, q=q)


# ---------------- 商品：新增 ----------------
@app.route("/products/add", methods=["GET", "POST"])
@login_required
def product_add():
    if request.method == "POST":
        ok, msg = _save_product(None)
        if ok:
            flash("商品添加成功", "success")
            return redirect(url_for("products"))
        flash(msg, "error")
    return render_template("product_form.html", product=None, mode="add")


# ---------------- 商品：修改 ----------------
@app.route("/products/<int:pid>/edit", methods=["GET", "POST"])
@login_required
def product_edit(pid):
    product = store.get_product(pid)
    if not product:
        abort(404)
    if request.method == "POST":
        ok, msg = _save_product(pid)
        if ok:
            flash("商品已更新", "success")
            return redirect(url_for("products"))
        flash(msg, "error")
        product = store.get_product(pid)
    return render_template("product_form.html", product=product, mode="edit")


# ---------------- 商品：删除 ----------------
@app.route("/products/<int:pid>/delete", methods=["POST"])
@login_required
def product_delete(pid):
    product = store.get_product(pid)
    if product:
        # 删除关联图片文件
        if product["image_path"]:
            fp = os.path.join(config.BASE_DIR, product["image_path"])
            if os.path.exists(fp):
                try:
                    os.remove(fp)
                except OSError:
                    pass
        store.delete_product(pid)
        flash("商品已删除", "success")
    return redirect(url_for("products"))


def _save_product(pid):
    """新增/修改共用：解析表单、处理图片上传与哈希。返回 (ok, msg)。"""
    code = request.form.get("code", "").strip()
    name = request.form.get("name", "").strip()
    link = request.form.get("link", "").strip()
    shop = request.form.get("shop", "").strip()
    image_enabled = 1 if request.form.get("image_enabled") in ("on", "1", "true") else 0
    enabled = 1 if request.form.get("enabled", "on") in ("on", "1", "true") else 0

    if not code:
        return False, "商品唯一编码为必填项"
    if not name:
        return False, "商品名为必填项（用于关键词匹配）"

    # 唯一编码冲突校验
    existing = store.get_product_by_code(code)
    if existing and (pid is None or existing["id"] != pid):
        return False, f"商品唯一编码「{code}」已存在"

    # 处理图片上传
    image_path = ""
    image_hash = ""
    if pid is not None:
        cur = store.get_product(pid)
        image_path = cur["image_path"]
        image_hash = cur["image_hash"]

    file = request.files.get("image")
    if image_enabled and file and file.filename:
        ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
        if ext not in config.ALLOWED_IMAGE_EXT:
            return False, f"不支持的图片格式：.{ext}"
        data = file.read()
        try:
            image_hash = compute_hash(data)
        except Exception as e:
            return False, f"图片解析失败：{e}"
        fname = f"{uuid.uuid4().hex}.{ext}"
        rel = os.path.join("uploads", fname)
        with open(os.path.join(config.BASE_DIR, rel), "wb") as f:
            f.write(data)
        image_path = rel

    if not image_enabled:
        # 关闭图片识别则清空哈希（保留文件路径以便重新开启时仍能展示）
        image_hash = ""

    if pid is None:
        store.add_product(code, name, link, shop, image_path, image_hash, image_enabled, enabled)
    else:
        store.update_product(
            pid, code=code, name=name, link=link, shop=shop,
            image_path=image_path, image_hash=image_hash,
            image_enabled=image_enabled, enabled=enabled,
        )
    return True, "ok"


# ---------------- 上传图片访问 ----------------
@app.route("/uploads/<path:filename>")
@login_required
def uploaded_file(filename):
    return send_from_directory(config.UPLOAD_DIR, filename)


# ---------------- 账号管理 ----------------
@app.route("/accounts", methods=["GET", "POST"])
@login_required
def accounts():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            name = request.form.get("name", "").strip()
            token = request.form.get("token", "").strip()
            if not name or not token:
                flash("账号名与 token 均不能为空", "error")
            else:
                try:
                    store.add_account(name, token)
                    flash("账号已添加", "success")
                except Exception:
                    flash(f"账号名「{name}」已存在", "error")
        elif action == "delete":
            store.delete_account(request.form.get("name", ""))
            flash("账号已删除", "success")
        return redirect(url_for("accounts"))
    return render_template("accounts.html", accounts=store.list_accounts())


# ---------------- 系统配置（与 reply.py 等价的全部配置）----------------
@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        # 更新配置项
        items = {}
        for key in config.DEFAULT_SETTINGS:
            if key in request.form:
                items[key] = request.form.get(key, "").strip()
        if items:
            store.update_settings(items)
            flash("配置已保存", "success")

        # 修改登录凭据（可选）
        new_user = request.form.get("new_username", "").strip()
        new_pass = request.form.get("new_password", "")
        if new_user and new_pass:
            store.change_credentials(session["user"], new_user, new_pass)
            session["user"] = new_user
            flash("登录凭据已更新", "success")
        return redirect(url_for("settings"))

    return render_template(
        "settings.html",
        settings=store.get_settings(),
        labels=config.SETTING_LABELS,
        keys=list(config.DEFAULT_SETTINGS.keys()),
    )


# ---------------- 引擎控制 ----------------
@app.route("/engine")
@login_required
def engine_page():
    return render_template("engine.html", status=engine.status(), logs=engine.get_logs())


@app.route("/engine/start", methods=["POST"])
@login_required
def engine_start():
    ok, msg = engine.start()
    flash(msg, "success" if ok else "error")
    return redirect(url_for("engine_page"))


@app.route("/engine/stop", methods=["POST"])
@login_required
def engine_stop():
    ok, msg = engine.stop()
    flash(msg, "success" if ok else "error")
    return redirect(url_for("engine_page"))


@app.route("/engine/logs")
@login_required
def engine_logs():
    return jsonify({"status": engine.status(), "logs": engine.get_logs()})


# ---------------- 测试匹配（演示关键词 + 图片识别）----------------
@app.route("/test", methods=["GET", "POST"])
@login_required
def test_match():
    result = None
    if request.method == "POST":
        text = request.form.get("text", "").strip()
        image_bytes = None
        file = request.files.get("image")
        if file and file.filename:
            image_bytes = file.read()
        result = matcher.match(content=text, image_bytes=image_bytes, source="web-test")
    return render_template("test.html", result=result)


# ---------------- 对外匹配 API（可被其它系统调用）----------------
@app.route("/api/match", methods=["POST"])
def api_match():
    if request.is_json:
        text = (request.json or {}).get("text", "")
    else:
        text = request.form.get("text", "")
    image_bytes = None
    file = request.files.get("image")
    if file and file.filename:
        image_bytes = file.read()
    result = matcher.match(content=text, image_bytes=image_bytes, source="api")
    return jsonify({
        "type": result["type"],
        "link": result["link"],
        "code": result["product"]["code"] if result["product"] else None,
        "name": result["product"]["name"] if result["product"] else None,
        "distance": result["distance"],
    })


if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    app.run(host=host, port=port, debug=False)
