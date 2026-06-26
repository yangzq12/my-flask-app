"""Fuck Discord 管理后台 —— reply.py 的网页版。

功能：
  - 登录鉴权（登录名 + 密码）
  - 商品 CRUD：新增 / 修改 / 删除 / 查询（与截图一致），支持启用图片上传与识别
  - 账号管理：Discord token 增删
  - 系统配置：与 reply.py 完全一致的全部配置项
  - 引擎控制：启动/停止 Discord 监听，查看永久回复记录
  - 测试匹配：输入文字或上传图片，实时查看会回复什么（演示图片识别）
"""
import functools
import io
import json
import os
import time
import uuid
import zipfile

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, jsonify, send_from_directory, send_file, abort,
)

import config
import store
import matcher
from imagehash_util import compute_hash
from bot_engine import engine

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = 128 * 1024 * 1024  # 支持商品备份包上传

os.makedirs(config.UPLOAD_DIR, exist_ok=True)
store.init_db()


# ---------------- 鉴权 ----------------
def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user") or not session.get("user_id"):
            session.clear()
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user") or not session.get("user_id"):
            session.clear()
            return redirect(url_for("login", next=request.path))
        if not session.get("is_admin"):
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def current_user_id():
    return int(session["user_id"])


def current_engine():
    return engine.for_user(current_user_id())


def upload_path(image_path):
    image_path = image_path or ""
    prefix = "uploads/"
    if image_path.startswith(prefix):
        return image_path[len(prefix):]
    return image_path.split("/")[-1]


@app.context_processor
def inject_globals():
    engine_running = False
    if session.get("user_id"):
        engine_running = engine.running(session["user_id"])
    return {
        "current_user": session.get("user"),
        "current_user_id": session.get("user_id"),
        "is_admin": bool(session.get("is_admin")),
        "engine_running": engine_running,
        "upload_path": upload_path,
    }


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = store.authenticate_user(username, password)
        if user:
            session["user"] = user["username"]
            session["user_id"] = user["id"]
            session["is_admin"] = bool(user["is_admin"])
            nxt = request.args.get("next") or url_for("dashboard")
            if user["is_admin"] and not request.args.get("next"):
                nxt = url_for("users")
            return redirect(nxt)
        flash("登录名或密码错误", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------- 管理员：用户管理 ----------------
@app.route("/users", methods=["GET", "POST"])
@admin_required
def users():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            ok, msg = store.create_user(
                request.form.get("username", ""),
                request.form.get("password", ""),
            )
            flash(msg, "success" if ok else "error")
        elif action == "delete":
            target_id = request.form.get("user_id", "")
            if str(target_id) == str(session.get("user_id")):
                flash("不能删除当前登录的管理员", "error")
            else:
                engine.stop_user(target_id)
                ok, msg = store.delete_user(target_id)
                flash(msg, "success" if ok else "error")
        elif action == "reset_password":
            ok, msg = store.reset_user_password(
                request.form.get("user_id", ""),
                request.form.get("password", ""),
            )
            flash(msg, "success" if ok else "error")
        return redirect(url_for("users"))

    return render_template("users.html", users=store.list_users())


# ---------------- 仪表盘 ----------------
@app.route("/")
@login_required
def dashboard():
    uid = current_user_id()
    return render_template(
        "dashboard.html",
        stats=store.counts(uid),
        message_records=store.recent_message_records(uid, 50),
        engine_status=current_engine().status(),
    )


@app.route("/message-records/<int:record_id>/delete", methods=["POST"])
@login_required
def message_record_delete(record_id):
    store.delete_message_record(current_user_id(), record_id)
    flash("消息处理记录已删除", "success")
    return redirect(url_for("dashboard"))


@app.route("/message-records/delete-selected", methods=["POST"])
@login_required
def message_records_delete_selected():
    deleted = store.delete_message_records(current_user_id(), request.form.getlist("record_ids"))
    if deleted:
        flash(f"已删除 {deleted} 条消息处理记录", "success")
    else:
        flash("请先选择要删除的消息处理记录", "error")
    return redirect(url_for("dashboard"))


@app.route("/message-records/clear", methods=["POST"])
@login_required
def message_records_clear():
    uid = current_user_id()
    store.clear_message_records(uid)
    engine.clear_state(uid)
    flash("已清空全部消息处理记录", "success")
    return redirect(url_for("dashboard"))


# ---------------- 商品：查询（列表）----------------
@app.route("/products")
@login_required
def products():
    q = request.args.get("q", "").strip()
    rows = store.list_products(current_user_id(), q or None)
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
    return render_template("product_form.html", product=None, images=[], mode="add")


# ---------------- 商品：修改 ----------------
@app.route("/products/<int:pid>/edit", methods=["GET", "POST"])
@login_required
def product_edit(pid):
    product = store.get_product(current_user_id(), pid)
    if not product:
        abort(404)
    if request.method == "POST":
        ok, msg = _save_product(pid)
        if ok:
            flash("商品已更新", "success")
            return redirect(url_for("products"))
        flash(msg, "error")
        product = store.get_product(current_user_id(), pid)
    return render_template(
        "product_form.html",
        product=product,
        images=store.product_images(current_user_id(), pid),
        mode="edit",
    )


# ---------------- 商品：删除 ----------------
@app.route("/products/<int:pid>/delete", methods=["POST"])
@login_required
def product_delete(pid):
    uid = current_user_id()
    product = store.get_product(uid, pid)
    if product:
        # 删除关联图片文件
        image_paths = {img["image_path"] for img in store.product_images(uid, pid) if img["image_path"]}
        if product["image_path"]:
            image_paths.add(product["image_path"])
        for image_path in image_paths:
            fp = os.path.join(config.BASE_DIR, image_path)
            if os.path.exists(fp):
                try:
                    os.remove(fp)
                except OSError:
                    pass
        store.delete_product(uid, pid)
        flash("商品已删除", "success")
    return redirect(url_for("products"))


@app.route("/products/export")
@login_required
def products_export():
    """导出商品信息和商品图片为 zip 备份包。"""
    backup = {
        "version": 1,
        "type": "products-backup",
        "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "products": [],
    }
    image_files = []

    uid = current_user_id()
    for row in store.list_products(uid):
        p = dict(row)
        product = {
            "code": p["code"],
            "name": p["name"],
            "link": p["link"],
            "shop": p["shop"],
            "image_enabled": int(p["image_enabled"]),
            "enabled": int(p["enabled"]),
            "images": [],
        }
        seen_paths = set()
        images = list(store.product_images(uid, p["id"]))
        if p.get("image_path"):
            images.append({"image_path": p["image_path"], "image_hash": p.get("image_hash", "")})

        for img in images:
            image_path = img["image_path"]
            if not image_path or image_path in seen_paths:
                continue
            seen_paths.add(image_path)
            full_path = os.path.join(config.BASE_DIR, image_path)
            if not os.path.isfile(full_path):
                continue
            ext = os.path.splitext(image_path)[1].lower() or ".jpg"
            archive_path = f"images/{uuid.uuid4().hex}{ext}"
            product["images"].append({
                "archive_path": archive_path,
                "filename": os.path.basename(image_path),
                "image_hash": img["image_hash"],
            })
            image_files.append((full_path, archive_path))

        backup["products"].append(product)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("products.json", json.dumps(backup, ensure_ascii=False, indent=2))
        for full_path, archive_path in image_files:
            zf.write(full_path, archive_path)
    buffer.seek(0)

    filename = f"products_backup_{time.strftime('%Y%m%d_%H%M%S')}.zip"
    return send_file(buffer, mimetype="application/zip", as_attachment=True, download_name=filename)


@app.route("/products/import", methods=["POST"])
@login_required
def products_import():
    """从商品备份 zip 恢复商品。相同商品编码会被覆盖。"""
    file = request.files.get("backup")
    if not file or not file.filename:
        flash("请选择商品备份 zip 文件", "error")
        return redirect(url_for("products"))
    if not file.filename.lower().endswith(".zip"):
        flash("只支持上传 zip 格式的商品备份包", "error")
        return redirect(url_for("products"))

    try:
        added = updated = image_count = 0
        with zipfile.ZipFile(io.BytesIO(file.read())) as zf:
            names = set(zf.namelist())
            if "products.json" not in names:
                flash("备份包缺少 products.json", "error")
                return redirect(url_for("products"))

            payload = json.loads(zf.read("products.json").decode("utf-8"))
            products_data = payload.get("products")
            if not isinstance(products_data, list):
                flash("备份包格式错误：products 必须是列表", "error")
                return redirect(url_for("products"))

            for item in products_data:
                if not isinstance(item, dict):
                    continue
                code = str(item.get("code", "")).strip()
                name = str(item.get("name", "")).strip()
                if not code or not name:
                    continue

                image_enabled = 1 if str(item.get("image_enabled", "0")) in ("1", "true", "True") else 0
                enabled = 1 if str(item.get("enabled", "1")) in ("1", "true", "True") else 0
                images_to_add = []
                written_files = []

                for img in item.get("images", []) or []:
                    if not isinstance(img, dict):
                        continue
                    archive_path = _clean_zip_path(img.get("archive_path", ""))
                    if not archive_path or archive_path not in names:
                        continue
                    filename = img.get("filename") or archive_path
                    ext = _image_ext(filename)
                    if ext not in config.ALLOWED_IMAGE_EXT:
                        continue
                    data = zf.read(archive_path)
                    try:
                        img_hash = compute_hash(data)
                    except Exception:
                        continue
                    rel = os.path.join("uploads", str(current_user_id()), f"{uuid.uuid4().hex}.{ext}")
                    os.makedirs(os.path.dirname(os.path.join(config.BASE_DIR, rel)), exist_ok=True)
                    with open(os.path.join(config.BASE_DIR, rel), "wb") as out:
                        out.write(data)
                    written_files.append(rel)
                    images_to_add.append((rel, img_hash))

                try:
                    uid = current_user_id()
                    existing = store.get_product_by_code(uid, code)
                    first_path = images_to_add[0][0] if images_to_add else ""
                    first_hash = images_to_add[0][1] if images_to_add and image_enabled else ""
                    if existing:
                        old_paths = _product_image_paths(uid, existing["id"])
                        store.update_product(
                            uid,
                            existing["id"],
                            code=code,
                            name=name,
                            link=str(item.get("link", "") or ""),
                            shop=str(item.get("shop", "") or ""),
                            image_path=first_path,
                            image_hash=first_hash,
                            image_enabled=image_enabled,
                            enabled=enabled,
                        )
                        store.replace_product_images(uid, existing["id"], images_to_add, product_image_hash=first_hash)
                        _delete_upload_files(old_paths - set(written_files))
                        updated += 1
                    else:
                        product_id = store.add_product(
                            uid,
                            code,
                            name,
                            str(item.get("link", "") or ""),
                            str(item.get("shop", "") or ""),
                            first_path,
                            first_hash,
                            image_enabled,
                            enabled,
                        )
                        store.replace_product_images(uid, product_id, images_to_add, product_image_hash=first_hash)
                        added += 1
                    image_count += len(images_to_add)
                except Exception:
                    _delete_upload_files(set(written_files))
                    raise

        flash(f"商品备份导入完成：新增 {added} 个，更新 {updated} 个，图片 {image_count} 张", "success")
    except (zipfile.BadZipFile, json.JSONDecodeError):
        flash("备份包无法解析，请确认上传的是系统导出的 zip 文件", "error")
    except Exception as e:
        flash(f"导入失败：{e}", "error")
    return redirect(url_for("products"))


def _clean_zip_path(path):
    path = str(path or "").replace("\\", "/").lstrip("/")
    parts = [part for part in path.split("/") if part]
    if not parts or any(part in (".", "..") for part in parts):
        return ""
    return "/".join(parts)


def _image_ext(filename):
    filename = os.path.basename(str(filename or ""))
    if "." not in filename:
        return ""
    return filename.rsplit(".", 1)[-1].lower()


def _product_image_paths(user_id, product_id):
    paths = {img["image_path"] for img in store.product_images(user_id, product_id) if img["image_path"]}
    product = store.get_product(user_id, product_id)
    if product and product["image_path"]:
        paths.add(product["image_path"])
    return paths


def _delete_upload_files(paths):
    for rel in paths:
        if not rel:
            continue
        full_path = os.path.abspath(os.path.join(config.BASE_DIR, rel))
        upload_root = os.path.abspath(config.UPLOAD_DIR)
        if not full_path.startswith(upload_root + os.sep):
            continue
        try:
            if os.path.isfile(full_path):
                os.remove(full_path)
        except OSError:
            pass


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
        return False, "商品名为必填项（用于商品名相似匹配）"

    # 唯一编码冲突校验
    uid = current_user_id()
    existing = store.get_product_by_code(uid, code)
    if existing and (pid is None or existing["id"] != pid):
        return False, f"商品唯一编码「{code}」已存在"

    # 处理图片上传。商品可追加多张图，匹配时每张图都会参与比较。
    image_path = ""
    image_hash = ""
    if pid is not None:
        cur = store.get_product(uid, pid)
        image_path = cur["image_path"]
        image_hash = cur["image_hash"]

    image_files = [
        file for file in request.files.getlist("image")
        if file and file.filename
    ]
    images_to_add = []
    if image_enabled and image_files:
        for file in image_files:
            filename = file.filename or ""
            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            if ext not in config.ALLOWED_IMAGE_EXT:
                return False, f"不支持的图片格式：.{ext}"
            data = file.read()
            try:
                img_hash = compute_hash(data)
            except Exception as e:
                return False, f"图片解析失败：{e}"
            fname = f"{uuid.uuid4().hex}.{ext}"
            rel = os.path.join("uploads", str(uid), fname)
            images_to_add.append((rel, img_hash, data))

        if images_to_add and not image_path:
            image_path = images_to_add[0][0]
            image_hash = images_to_add[0][1]

    if not image_enabled:
        # 关闭图片识别则清空旧兼容字段的哈希；多图文件仍保留，重新开启后继续参与识别。
        image_hash = ""

    if pid is None:
        product_id = store.add_product(uid, code, name, link, shop, image_path, image_hash, image_enabled, enabled)
    else:
        product_id = pid
        store.update_product(
            uid, pid, code=code, name=name, link=link, shop=shop,
            image_path=image_path, image_hash=image_hash,
            image_enabled=image_enabled, enabled=enabled,
        )

    for rel, img_hash, data in images_to_add:
        os.makedirs(os.path.dirname(os.path.join(config.BASE_DIR, rel)), exist_ok=True)
        with open(os.path.join(config.BASE_DIR, rel), "wb") as f:
            f.write(data)
        store.add_product_image(uid, product_id, rel, img_hash)

    return True, "ok"


# ---------------- 上传图片访问 ----------------
@app.route("/uploads/<path:filename>")
@login_required
def uploaded_file(filename):
    rel = os.path.normpath(os.path.join("uploads", filename)).replace("\\", "/")
    if rel.startswith("../") or rel == ".." or not rel.startswith("uploads/"):
        abort(404)
    if not store.owns_upload(current_user_id(), rel):
        abort(404)
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
                    store.add_account(current_user_id(), name, token)
                    flash("账号已添加", "success")
                except Exception:
                    flash(f"账号名「{name}」已存在", "error")
        elif action == "delete":
            store.delete_account(current_user_id(), request.form.get("name", ""))
            flash("账号已删除", "success")
        return redirect(url_for("accounts"))
    return render_template("accounts.html", accounts=store.list_accounts(current_user_id()))


# ---------------- 屏蔽关键字 ----------------
@app.route("/blocked-keywords", methods=["GET", "POST"])
@login_required
def blocked_keywords():
    if request.method == "POST":
        keyword = request.form.get("keyword", "")
        ok, msg = store.add_blocked_keyword(current_user_id(), keyword)
        flash(msg, "success" if ok else "error")
        return redirect(url_for("blocked_keywords"))
    return render_template("blocked_keywords.html", keywords=store.list_blocked_keywords(current_user_id()))


@app.route("/blocked-keywords/<int:keyword_id>/delete", methods=["POST"])
@login_required
def blocked_keyword_delete(keyword_id):
    store.delete_blocked_keyword(current_user_id(), keyword_id)
    flash("屏蔽关键字已删除", "success")
    return redirect(url_for("blocked_keywords"))


# ---------------- 系统配置（与 reply.py 等价的全部配置）----------------
@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    uid = current_user_id()
    if request.method == "POST":
        # 更新配置项
        items = {}
        for key in config.DEFAULT_SETTINGS:
            if key in request.form:
                items[key] = request.form.get(key, "").strip()
        if items:
            store.update_settings(uid, items)
            flash("配置已保存", "success")

        # 修改登录凭据（可选）
        new_user = request.form.get("new_username", "").strip()
        new_pass = request.form.get("new_password", "")
        if new_user and new_pass:
            ok, msg = store.change_credentials(uid, new_user, new_pass)
            flash(msg, "success" if ok else "error")
            if ok:
                session["user"] = new_user
        return redirect(url_for("settings"))

    return render_template(
        "settings.html",
        settings=store.get_settings(uid),
        labels=config.SETTING_LABELS,
        keys=list(config.DEFAULT_SETTINGS.keys()),
    )


# ---------------- 引擎控制 ----------------
@app.route("/engine")
@login_required
def engine_page():
    return render_template(
        "engine.html",
        status=current_engine().status(),
    )


@app.route("/engine/start", methods=["POST"])
@login_required
def engine_start():
    ok, msg = current_engine().start()
    flash(msg, "success" if ok else "error")
    return redirect(url_for("engine_page"))


@app.route("/engine/stop", methods=["POST"])
@login_required
def engine_stop():
    ok, msg = current_engine().stop()
    flash(msg, "success" if ok else "error")
    return redirect(url_for("engine_page"))


@app.route("/engine/replied/clear", methods=["POST"])
@login_required
def engine_replied_clear():
    return redirect(url_for("dashboard"))


# ---------------- 测试匹配（演示商品名相似匹配 + 图片识别）----------------
@app.route("/test", methods=["GET", "POST"])
@login_required
def test_match():
    result = None
    if request.method == "POST":
        text = request.form.get("text", "").strip()
        image_bytes = [
            file.read() for file in request.files.getlist("image")
            if file and file.filename
        ]
        uid = current_user_id()
        blocked_keyword = store.find_blocked_keyword(uid, text)
        if blocked_keyword:
            result = {
                "type": "blocked",
                "product": None,
                "link": "",
                "distance": None,
                "similarity": None,
                "blocked_keyword": blocked_keyword,
            }
        else:
            result = matcher.match(content=text, image_bytes=image_bytes, source="web-test", user_id=uid)
    return render_template("test.html", result=result)


# ---------------- 对外匹配 API（可被其它系统调用）----------------
@app.route("/api/match", methods=["POST"])
@login_required
def api_match():
    uid = current_user_id()
    if request.is_json:
        text = (request.json or {}).get("text", "")
    else:
        text = request.form.get("text", "")
    blocked_keyword = store.find_blocked_keyword(uid, text)
    if blocked_keyword:
        return jsonify({
            "type": "blocked",
            "link": "",
            "code": None,
            "name": None,
            "distance": None,
            "similarity": None,
            "blocked_keyword": blocked_keyword,
        })
    image_bytes = [
        file.read() for file in request.files.getlist("image")
        if file and file.filename
    ]
    result = matcher.match(content=text, image_bytes=image_bytes, source="api", user_id=uid)
    return jsonify({
        "type": result["type"],
        "link": result["link"],
        "code": result["product"]["code"] if result["product"] else None,
        "name": result["product"]["name"] if result["product"] else None,
        "distance": result["distance"],
        "similarity": result.get("similarity"),
    })


if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    app.run(host=host, port=port, debug=False)
