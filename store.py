"""SQLite 数据层：用户、商品、账号、设置、匹配日志。

线程安全说明：每次操作开一个短连接（check_same_thread=False + 立即关闭），
Web 线程与后台引擎线程都可安全调用。
"""
import os
import json
import sqlite3
import time
from contextlib import contextmanager

import yaml
from werkzeug.security import generate_password_hash, check_password_hash

from config import DB_PATH, PRODUCT_MAP_FILE, DEFAULT_SETTINGS


def _ensure_dirs():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


@contextmanager
def get_conn():
    _ensure_dirs()
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """建表 + 写入默认设置 + 初始化默认管理员账号。"""
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,        -- 商品唯一编码
                name TEXT NOT NULL,               -- 商品名（用于相似匹配）
                link TEXT NOT NULL DEFAULT '',    -- 商品链接
                shop TEXT NOT NULL DEFAULT '',    -- 店铺名
                image_path TEXT NOT NULL DEFAULT '',  -- 兼容旧版本：第一张商品图片相对路径
                image_hash TEXT NOT NULL DEFAULT '',  -- 兼容旧版本：第一张商品图片哈希
                image_enabled INTEGER NOT NULL DEFAULT 0,  -- 是否启用图片上传/识别
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS product_images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                image_path TEXT NOT NULL DEFAULT '',
                image_hash TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE
            )""")
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_product_images_product_id
            ON product_images(product_id)
            """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                token TEXT NOT NULL DEFAULT '',
                next_available_time REAL NOT NULL DEFAULT 0,
                last_used_time TEXT NOT NULL DEFAULT '从未使用',
                status TEXT NOT NULL DEFAULT 'active',
                invalid_reason TEXT NOT NULL DEFAULT '',
                invalid_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )""")
        account_cols = {r["name"] for r in c.execute("PRAGMA table_info(accounts)").fetchall()}
        if "status" not in account_cols:
            c.execute("ALTER TABLE accounts ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
        if "invalid_reason" not in account_cols:
            c.execute("ALTER TABLE accounts ADD COLUMN invalid_reason TEXT NOT NULL DEFAULT ''")
        if "invalid_at" not in account_cols:
            c.execute("ALTER TABLE accounts ADD COLUMN invalid_at TEXT NOT NULL DEFAULT ''")
        c.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS match_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,            -- discord / web-test
                query_text TEXT NOT NULL DEFAULT '',
                had_image INTEGER NOT NULL DEFAULT 0,
                match_type TEXT NOT NULL DEFAULT '',  -- keyword / image / shop / none
                matched_code TEXT NOT NULL DEFAULT '',
                matched_link TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS replied_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL DEFAULT 'discord',
                channel_id TEXT NOT NULL DEFAULT '',
                message_id TEXT NOT NULL DEFAULT '',
                author_id TEXT NOT NULL DEFAULT '',
                username TEXT NOT NULL DEFAULT '',
                user_content TEXT NOT NULL DEFAULT '',
                had_image INTEGER NOT NULL DEFAULT 0,
                image_urls TEXT NOT NULL DEFAULT '',
                reply_content TEXT NOT NULL DEFAULT '',
                reply_mode TEXT NOT NULL DEFAULT '',
                reply_channel_id TEXT NOT NULL DEFAULT '',
                account_name TEXT NOT NULL DEFAULT '',
                match_type TEXT NOT NULL DEFAULT '',
                matched_code TEXT NOT NULL DEFAULT '',
                matched_link TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(channel_id, message_id)
            )""")
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_replied_messages_message
            ON replied_messages(channel_id, message_id)
            """)
        replied_cols = {r["name"] for r in c.execute("PRAGMA table_info(replied_messages)").fetchall()}
        if "image_urls" not in replied_cols:
            c.execute("ALTER TABLE replied_messages ADD COLUMN image_urls TEXT NOT NULL DEFAULT ''")

        # 默认设置
        for k, v in DEFAULT_SETTINGS.items():
            c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (k, v))
        c.execute(
            "UPDATE settings SET value=? WHERE key='CUSTOM_REPLY' AND value=?",
            (DEFAULT_SETTINGS["CUSTOM_REPLY"], "欢迎访问我们的店铺查看更多商品~"),
        )

        # 默认管理员
        c.execute("SELECT COUNT(*) AS n FROM users")
        if c.fetchone()["n"] == 0:
            c.execute(
                "INSERT INTO users(username, password_hash, created_at) VALUES(?, ?, ?)",
                ("admin", generate_password_hash("admin123"), _now()),
            )

        # 兼容旧数据：把 products 表里原来的单张商品图迁移到多图表。
        rows = c.execute(
            """SELECT id, image_path, image_hash, created_at FROM products
               WHERE image_hash != '' AND image_path != ''"""
        ).fetchall()
        for p in rows:
            exists = c.execute(
                """SELECT 1 FROM product_images
                   WHERE product_id=? AND image_hash=? LIMIT 1""",
                (p["id"], p["image_hash"]),
            ).fetchone()
            if not exists:
                c.execute(
                    """INSERT INTO product_images(product_id, image_path, image_hash, created_at)
                       VALUES(?,?,?,?)""",
                    (p["id"], p["image_path"], p["image_hash"], p["created_at"] or _now()),
                )


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---------------- 用户 / 登录 ----------------
def verify_user(username, password):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    return bool(row) and check_password_hash(row["password_hash"], password)


def get_user(username):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()


def change_credentials(old_username, new_username, new_password):
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET username=?, password_hash=? WHERE username=?",
            (new_username, generate_password_hash(new_password), old_username),
        )


# ---------------- 设置 ----------------
def get_settings():
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    data = {r["key"]: r["value"] for r in rows}
    # 补齐缺省项
    for k, v in DEFAULT_SETTINGS.items():
        data.setdefault(k, v)
    return data


def get_setting(key, default=None):
    return get_settings().get(key, default)


def update_settings(items):
    with get_conn() as conn:
        for k, v in items.items():
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (k, str(v)),
            )


# ---------------- 商品 ----------------
def list_products(keyword=None):
    sql = """
        SELECT p.*,
               COALESCE(
                   (SELECT pi.image_path FROM product_images pi
                    WHERE pi.product_id=p.id
                    ORDER BY pi.id LIMIT 1),
                   p.image_path
               ) AS first_image_path,
               (SELECT COUNT(*) FROM product_images pi
                WHERE pi.product_id=p.id AND pi.image_hash != '') AS image_count
        FROM products p
    """
    args = ()
    if keyword:
        sql += " WHERE p.code LIKE ? OR p.name LIKE ? OR p.link LIKE ? OR p.shop LIKE ?"
        like = f"%{keyword}%"
        args = (like, like, like, like)
    sql += " ORDER BY p.updated_at DESC"
    with get_conn() as conn:
        return conn.execute(sql, args).fetchall()


def get_product(pid):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()


def get_product_by_code(code):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM products WHERE code=?", (code,)).fetchone()


def add_product(code, name, link, shop, image_path, image_hash, image_enabled, enabled=1):
    now = _now()
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO products(code, name, link, shop, image_path, image_hash,
                                    image_enabled, enabled, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (code, name, link, shop, image_path, image_hash,
             int(image_enabled), int(enabled), now, now),
        )
        product_id = cur.lastrowid
    export_product_maps()
    return product_id


def update_product(pid, **fields):
    if not fields:
        return
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k}=?" for k in fields)
    args = list(fields.values()) + [pid]
    with get_conn() as conn:
        conn.execute(f"UPDATE products SET {cols} WHERE id=?", args)
    export_product_maps()


def delete_product(pid):
    with get_conn() as conn:
        conn.execute("DELETE FROM product_images WHERE product_id=?", (pid,))
        conn.execute("DELETE FROM products WHERE id=?", (pid,))
    export_product_maps()


def product_images(pid):
    """返回某个商品的全部图片。"""
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM product_images WHERE product_id=? ORDER BY id", (pid,)
        ).fetchall()


def add_product_image(product_id, image_path, image_hash):
    """给商品追加一张图片，并在旧字段为空时同步第一张图以保持兼容。"""
    now = _now()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO product_images(product_id, image_path, image_hash, created_at)
               VALUES(?,?,?,?)""",
            (product_id, image_path, image_hash, now),
        )
        p = conn.execute("SELECT image_path, image_hash FROM products WHERE id=?", (product_id,)).fetchone()
        if p and (not p["image_path"] or not p["image_hash"]):
            conn.execute(
                "UPDATE products SET image_path=?, image_hash=?, updated_at=? WHERE id=?",
                (image_path, image_hash, now, product_id),
            )


def replace_product_images(product_id, images, product_image_hash=None):
    """替换某个商品的全部图片；images 为 [(image_path, image_hash), ...]。"""
    now = _now()
    first_path = images[0][0] if images else ""
    first_hash = product_image_hash if product_image_hash is not None else (images[0][1] if images else "")
    with get_conn() as conn:
        conn.execute("DELETE FROM product_images WHERE product_id=?", (product_id,))
        conn.execute(
            "UPDATE products SET image_path=?, image_hash=?, updated_at=? WHERE id=?",
            (first_path, first_hash, now, product_id),
        )
        for image_path, image_hash in images:
            conn.execute(
                """INSERT INTO product_images(product_id, image_path, image_hash, created_at)
                   VALUES(?,?,?,?)""",
                (product_id, image_path, image_hash, now),
            )


def products_with_image_hash():
    """返回所有启用了图片识别且有图片哈希的商品图片（供引擎做多图匹配）。"""
    with get_conn() as conn:
        return conn.execute(
            """SELECT p.*,
                      pi.id AS product_image_id,
                      pi.image_path AS product_image_path,
                      pi.image_hash AS product_image_hash
               FROM products p
               JOIN product_images pi ON pi.product_id=p.id
               WHERE p.enabled=1
                 AND p.image_enabled=1
                 AND pi.image_hash != ''
               ORDER BY p.id, pi.id"""
        ).fetchall()


def enabled_products():
    with get_conn() as conn:
        return conn.execute("SELECT * FROM products WHERE enabled=1").fetchall()


# ---------------- 账号 ----------------
def list_accounts():
    with get_conn() as conn:
        return conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()


def active_accounts():
    with get_conn() as conn:
        return conn.execute("SELECT * FROM accounts WHERE status='active' ORDER BY id").fetchall()


def add_account(name, token):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO accounts(name, token, next_available_time, last_used_time,
                                    status, invalid_reason, invalid_at, created_at)
               VALUES(?,?,0,'从未使用','active','','',?)""",
            (name, token, _now()),
        )


def delete_account(name):
    with get_conn() as conn:
        conn.execute("DELETE FROM accounts WHERE name=?", (name,))


def update_account_usage(name, next_available_time, last_used_time):
    with get_conn() as conn:
        conn.execute(
            "UPDATE accounts SET next_available_time=?, last_used_time=? WHERE name=?",
            (next_available_time, last_used_time, name),
        )


def mark_account_invalid(name, reason):
    with get_conn() as conn:
        conn.execute(
            """UPDATE accounts
               SET status='invalid',
                   invalid_reason=?,
                   invalid_at=?,
                   next_available_time=0
               WHERE name=?""",
            (reason, _now(), name),
        )


# ---------------- 匹配日志 ----------------
def log_match(source, query_text, had_image, match_type, matched_code, matched_link):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO match_logs(source, query_text, had_image, match_type,
                                      matched_code, matched_link, created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (source, query_text[:200], int(had_image), match_type,
             matched_code, matched_link, _now()),
        )


def recent_logs(limit=50):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM match_logs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


# ---------------- 已回复消息记录 ----------------
def has_replied_message(channel_id, message_id):
    with get_conn() as conn:
        row = conn.execute(
            """SELECT 1 FROM replied_messages
               WHERE channel_id=? AND message_id=? LIMIT 1""",
            (str(channel_id or ""), str(message_id or "")),
        ).fetchone()
    return bool(row)


def log_replied_message(
    channel_id, message_id, author_id, username, user_content, had_image,
    image_urls, reply_content, reply_mode, reply_channel_id, account_name,
    match_type, matched_code, matched_link,
):
    with get_conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO replied_messages(
                   source, channel_id, message_id, author_id, username, user_content,
                   had_image, image_urls, reply_content, reply_mode, reply_channel_id, account_name,
                   match_type, matched_code, matched_link, created_at
               )
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "discord",
                str(channel_id or ""),
                str(message_id or ""),
                str(author_id or ""),
                username or "",
                (user_content or "")[:1000],
                int(had_image),
                json.dumps(list(image_urls or []), ensure_ascii=False),
                reply_content or "",
                reply_mode or "",
                str(reply_channel_id or ""),
                account_name or "",
                match_type or "",
                matched_code or "",
                matched_link or "",
                _now(),
            ),
        )


def recent_replied_messages(limit=50):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM replied_messages ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


def count_replied_messages():
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM replied_messages").fetchone()["n"]


def clear_replied_messages():
    with get_conn() as conn:
        conn.execute("DELETE FROM replied_messages")


def counts():
    with get_conn() as conn:
        p = conn.execute("SELECT COUNT(*) AS n FROM products").fetchone()["n"]
        a = conn.execute("SELECT COUNT(*) AS n FROM accounts WHERE status='active'").fetchone()["n"]
        img = conn.execute(
            """SELECT COUNT(DISTINCT p.id) AS n
               FROM products p
               JOIN product_images pi ON pi.product_id=p.id
               WHERE p.image_enabled=1 AND pi.image_hash != ''"""
        ).fetchone()["n"]
        replies = conn.execute("SELECT COUNT(*) AS n FROM replied_messages").fetchone()["n"]
    return {"products": p, "accounts": a, "image_products": img, "replies": replies}


# ---------------- 兼容老 reply.py：导出 product_maps.yaml ----------------
def export_product_maps():
    """把商品名→链接导出为 reply.py 可热重载的 product_maps.yaml。"""
    try:
        text_maps = {}
        for p in enabled_products():
            if p["name"] and p["link"]:
                text_maps[p["name"]] = p["link"]
        os.makedirs(os.path.dirname(PRODUCT_MAP_FILE), exist_ok=True)
        with open(PRODUCT_MAP_FILE, "w", encoding="utf-8") as f:
            yaml.dump({"text_maps": text_maps}, f, allow_unicode=True,
                      indent=2, sort_keys=False)
    except Exception as e:  # 导出失败不影响主流程
        print(f"[store] 导出 product_maps.yaml 失败: {e}")
