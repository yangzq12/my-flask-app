"""SQLite 数据层：用户、商品、账号、设置、匹配日志。

线程安全说明：每次操作开一个短连接（check_same_thread=False + 立即关闭），
Web 线程与后台引擎线程都可安全调用。
"""
import os
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
                name TEXT NOT NULL,               -- 商品名（用于关键词匹配）
                link TEXT NOT NULL DEFAULT '',    -- 商品链接
                shop TEXT NOT NULL DEFAULT '',    -- 店铺名
                image_path TEXT NOT NULL DEFAULT '',  -- 商品图片相对路径
                image_hash TEXT NOT NULL DEFAULT '',  -- 感知哈希（dhash:phash）
                image_enabled INTEGER NOT NULL DEFAULT 0,  -- 是否启用图片上传/识别
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                token TEXT NOT NULL DEFAULT '',
                next_available_time REAL NOT NULL DEFAULT 0,
                last_used_time TEXT NOT NULL DEFAULT '从未使用',
                created_at TEXT NOT NULL
            )""")
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

        # 默认设置
        for k, v in DEFAULT_SETTINGS.items():
            c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (k, v))

        # 默认管理员
        c.execute("SELECT COUNT(*) AS n FROM users")
        if c.fetchone()["n"] == 0:
            c.execute(
                "INSERT INTO users(username, password_hash, created_at) VALUES(?, ?, ?)",
                ("admin", generate_password_hash("admin123"), _now()),
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
    sql = "SELECT * FROM products"
    args = ()
    if keyword:
        sql += " WHERE code LIKE ? OR name LIKE ? OR link LIKE ? OR shop LIKE ?"
        like = f"%{keyword}%"
        args = (like, like, like, like)
    sql += " ORDER BY updated_at DESC"
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
        conn.execute(
            """INSERT INTO products(code, name, link, shop, image_path, image_hash,
                                    image_enabled, enabled, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (code, name, link, shop, image_path, image_hash,
             int(image_enabled), int(enabled), now, now),
        )
    export_product_maps()


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
        conn.execute("DELETE FROM products WHERE id=?", (pid,))
    export_product_maps()


def products_with_image_hash():
    """返回所有启用了图片识别且有哈希的商品（供引擎做图片匹配）。"""
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM products WHERE enabled=1 AND image_enabled=1 AND image_hash != ''"
        ).fetchall()


def enabled_products():
    with get_conn() as conn:
        return conn.execute("SELECT * FROM products WHERE enabled=1").fetchall()


# ---------------- 账号 ----------------
def list_accounts():
    with get_conn() as conn:
        return conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()


def add_account(name, token):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO accounts(name, token, next_available_time, last_used_time, created_at) "
            "VALUES(?,?,0,'从未使用',?)",
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


def counts():
    with get_conn() as conn:
        p = conn.execute("SELECT COUNT(*) AS n FROM products").fetchone()["n"]
        a = conn.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()["n"]
        img = conn.execute(
            "SELECT COUNT(*) AS n FROM products WHERE image_enabled=1 AND image_hash != ''"
        ).fetchone()["n"]
    return {"products": p, "accounts": a, "image_products": img}


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
