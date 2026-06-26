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

from config import ADMIN_PASSWORD, ADMIN_USERNAME, DB_PATH, PRODUCT_MAP_FILE, DEFAULT_SETTINGS

_DEFAULT_USER_ID = None
_PRODUCT_CACHE_VERSION = {}
_BLOCKED_KEYWORDS_CACHE = {}


def _normalize_user_id(user_id):
    if user_id is None:
        return default_user_id()
    return int(user_id)


def product_cache_version(user_id=None):
    return _PRODUCT_CACHE_VERSION.get(_normalize_user_id(user_id), 0)


def _bump_product_cache_version(user_id=None):
    user_id = _normalize_user_id(user_id)
    _PRODUCT_CACHE_VERSION[user_id] = _PRODUCT_CACHE_VERSION.get(user_id, 0) + 1


def _bump_blocked_keywords_cache(user_id=None):
    _BLOCKED_KEYWORDS_CACHE.pop(_normalize_user_id(user_id), None)


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


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _table_exists(conn, table):
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone())


def _columns(conn, table):
    if not _table_exists(conn, table):
        return set()
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _create_users_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )""")
    cols = _columns(conn, "users")
    if "is_admin" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")


def _ensure_admin_user(conn):
    row = conn.execute("SELECT * FROM users WHERE username=?", (ADMIN_USERNAME,)).fetchone()
    password_hash = generate_password_hash(ADMIN_PASSWORD)
    if row:
        conn.execute(
            "UPDATE users SET password_hash=?, is_admin=1 WHERE id=?",
            (password_hash, row["id"]),
        )
        return row["id"]
    cur = conn.execute(
        "INSERT INTO users(username, password_hash, is_admin, created_at) VALUES(?,?,1,?)",
        (ADMIN_USERNAME, password_hash, _now()),
    )
    return cur.lastrowid


def _ensure_default_settings(conn, user_id):
    for k, v in DEFAULT_SETTINGS.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings(user_id, key, value) VALUES(?,?,?)",
            (user_id, k, v),
        )
    conn.execute(
        """UPDATE settings SET value=?
           WHERE user_id=? AND key='CUSTOM_REPLY' AND value=?""",
        (DEFAULT_SETTINGS["CUSTOM_REPLY"], user_id, "欢迎访问我们的店铺查看更多商品~"),
    )
    _normalize_image_threshold_setting(conn, user_id)
    conn.execute(
        """DELETE FROM settings
           WHERE user_id=? AND key IN ('MESSAGE_RECORD_EXPIRE_DAYS', 'MESSAGE_RECORD_MAX_ROWS')""",
        (user_id,),
    )


def _format_similarity(value):
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _normalize_image_threshold_setting(conn, user_id):
    row = conn.execute(
        "SELECT value FROM settings WHERE user_id=? AND key='IMAGE_MATCH_THRESHOLD'",
        (user_id,),
    ).fetchone()
    if not row:
        return
    try:
        raw = float(row["value"])
    except (TypeError, ValueError):
        raw = float(DEFAULT_SETTINGS["IMAGE_MATCH_THRESHOLD"])
    if raw > 1:
        raw = 1 - max(0, min(128, raw)) / 128
    raw = max(0.0, min(1.0, raw))
    normalized = _format_similarity(raw)
    if normalized != row["value"]:
        conn.execute(
            "UPDATE settings SET value=? WHERE user_id=? AND key='IMAGE_MATCH_THRESHOLD'",
            (normalized, user_id),
        )


def _create_products_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            code TEXT NOT NULL,
            name TEXT NOT NULL,
            link TEXT NOT NULL DEFAULT '',
            shop TEXT NOT NULL DEFAULT '',
            image_path TEXT NOT NULL DEFAULT '',
            image_hash TEXT NOT NULL DEFAULT '',
            image_enabled INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(user_id, code)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_user ON products(user_id)")


def _create_product_images_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS product_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            image_path TEXT NOT NULL DEFAULT '',
            image_hash TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE
        )""")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_product_images_product_id
        ON product_images(product_id)
        """)


def _create_accounts_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            token TEXT NOT NULL DEFAULT '',
            next_available_time REAL NOT NULL DEFAULT 0,
            last_used_time TEXT NOT NULL DEFAULT '从未使用',
            status TEXT NOT NULL DEFAULT 'active',
            invalid_reason TEXT NOT NULL DEFAULT '',
            invalid_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(user_id, name)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_user ON accounts(user_id)")


def _create_settings_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            user_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            PRIMARY KEY(user_id, key)
        )""")


def _create_blocked_keywords_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS blocked_keywords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            keyword TEXT NOT NULL,
            keyword_norm TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(user_id, keyword_norm)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_blocked_keywords_user ON blocked_keywords(user_id)")


def _create_match_logs_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS match_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            query_text TEXT NOT NULL DEFAULT '',
            had_image INTEGER NOT NULL DEFAULT 0,
            match_type TEXT NOT NULL DEFAULT '',
            matched_code TEXT NOT NULL DEFAULT '',
            matched_link TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_match_logs_user ON match_logs(user_id)")


def _create_replied_messages_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS replied_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
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
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(user_id, channel_id, message_id)
        )""")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_replied_messages_message
        ON replied_messages(user_id, channel_id, message_id)
        """)


def _create_message_records_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS message_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            source TEXT NOT NULL DEFAULT 'discord',
            channel_id TEXT NOT NULL DEFAULT '',
            message_id TEXT NOT NULL DEFAULT '',
            author_id TEXT NOT NULL DEFAULT '',
            username TEXT NOT NULL DEFAULT '',
            user_content TEXT NOT NULL DEFAULT '',
            had_image INTEGER NOT NULL DEFAULT 0,
            image_urls TEXT NOT NULL DEFAULT '',
            match_type TEXT NOT NULL DEFAULT '',
            matched_code TEXT NOT NULL DEFAULT '',
            matched_link TEXT NOT NULL DEFAULT '',
            reply_content TEXT NOT NULL DEFAULT '',
            reply_status TEXT NOT NULL DEFAULT '',
            reply_mode TEXT NOT NULL DEFAULT '',
            reply_channel_id TEXT NOT NULL DEFAULT '',
            account_name TEXT NOT NULL DEFAULT '',
            skip_reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(user_id, channel_id, message_id)
        )""")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_message_records_message
        ON message_records(user_id, channel_id, message_id)
        """)


def _rebuild_table(conn, table, create_fn, copy_sql):
    if not _table_exists(conn, table):
        create_fn(conn)
        return
    backup = f"_{table}_legacy_{int(time.time() * 1000)}"
    conn.execute(f"ALTER TABLE {table} RENAME TO {backup}")
    create_fn(conn)
    conn.execute(copy_sql.format(old=backup))
    conn.execute(f"DROP TABLE {backup}")
    create_fn(conn)


def _migrate_user_scoped_tables(conn, admin_id):
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF;")

    if not _table_exists(conn, "products") or "user_id" in _columns(conn, "products"):
        _create_products_table(conn)
    else:
        _rebuild_table(conn, "products", _create_products_table, f"""
            INSERT INTO products(
                id, user_id, code, name, link, shop, image_path, image_hash,
                image_enabled, enabled, created_at, updated_at
            )
            SELECT id, {admin_id}, code, name, link, shop, image_path, image_hash,
                   image_enabled, enabled, created_at, updated_at
            FROM {{old}}
        """)

    if not _table_exists(conn, "product_images"):
        _create_product_images_table(conn)
    elif "product_id" in _columns(conn, "product_images"):
        _rebuild_table(conn, "product_images", _create_product_images_table, """
            INSERT INTO product_images(id, product_id, image_path, image_hash, created_at)
            SELECT id, product_id, image_path, image_hash, created_at FROM {old}
        """)
    else:
        _create_product_images_table(conn)

    if not _table_exists(conn, "accounts") or "user_id" in _columns(conn, "accounts"):
        _create_accounts_table(conn)
        cols = _columns(conn, "accounts")
        if "status" not in cols:
            conn.execute("ALTER TABLE accounts ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
        if "invalid_reason" not in cols:
            conn.execute("ALTER TABLE accounts ADD COLUMN invalid_reason TEXT NOT NULL DEFAULT ''")
        if "invalid_at" not in cols:
            conn.execute("ALTER TABLE accounts ADD COLUMN invalid_at TEXT NOT NULL DEFAULT ''")
    else:
        cols = _columns(conn, "accounts")
        status_expr = "status" if "status" in cols else "'active'"
        invalid_reason_expr = "invalid_reason" if "invalid_reason" in cols else "''"
        invalid_at_expr = "invalid_at" if "invalid_at" in cols else "''"
        _rebuild_table(conn, "accounts", _create_accounts_table, f"""
            INSERT INTO accounts(
                id, user_id, name, token, next_available_time, last_used_time,
                status, invalid_reason, invalid_at, created_at
            )
            SELECT id, {admin_id}, name, token, next_available_time, last_used_time,
                   COALESCE({status_expr}, 'active'), COALESCE({invalid_reason_expr}, ''),
                   COALESCE({invalid_at_expr}, ''), created_at
            FROM {{old}}
        """)

    if not _table_exists(conn, "settings") or "user_id" in _columns(conn, "settings"):
        _create_settings_table(conn)
    else:
        _rebuild_table(conn, "settings", _create_settings_table, f"""
            INSERT INTO settings(user_id, key, value)
            SELECT {admin_id}, key, value FROM {{old}}
        """)

    if not _table_exists(conn, "blocked_keywords") or "user_id" in _columns(conn, "blocked_keywords"):
        _create_blocked_keywords_table(conn)
    else:
        _rebuild_table(conn, "blocked_keywords", _create_blocked_keywords_table, f"""
            INSERT INTO blocked_keywords(id, user_id, keyword, keyword_norm, created_at)
            SELECT id, {admin_id}, keyword, keyword_norm, created_at FROM {{old}}
        """)

    if not _table_exists(conn, "match_logs") or "user_id" in _columns(conn, "match_logs"):
        _create_match_logs_table(conn)
    else:
        _rebuild_table(conn, "match_logs", _create_match_logs_table, f"""
            INSERT INTO match_logs(
                id, user_id, source, query_text, had_image, match_type,
                matched_code, matched_link, created_at
            )
            SELECT id, {admin_id}, source, query_text, had_image, match_type,
                   matched_code, matched_link, created_at
            FROM {{old}}
        """)

    if not _table_exists(conn, "replied_messages") or "user_id" in _columns(conn, "replied_messages"):
        _create_replied_messages_table(conn)
    else:
        _rebuild_table(conn, "replied_messages", _create_replied_messages_table, f"""
            INSERT OR IGNORE INTO replied_messages(
                id, user_id, source, channel_id, message_id, author_id, username,
                user_content, had_image, image_urls, reply_content, reply_mode,
                reply_channel_id, account_name, match_type, matched_code,
                matched_link, created_at
            )
            SELECT id, {admin_id}, source, channel_id, message_id, author_id, username,
                   user_content, had_image, image_urls, reply_content, reply_mode,
                   reply_channel_id, account_name, match_type, matched_code,
                   matched_link, created_at
            FROM {{old}}
        """)

    if not _table_exists(conn, "message_records") or "user_id" in _columns(conn, "message_records"):
        _create_message_records_table(conn)
    else:
        _rebuild_table(conn, "message_records", _create_message_records_table, f"""
            INSERT OR IGNORE INTO message_records(
                id, user_id, source, channel_id, message_id, author_id, username,
                user_content, had_image, image_urls, match_type, matched_code,
                matched_link, reply_content, reply_status, reply_mode, reply_channel_id,
                account_name, skip_reason, created_at, updated_at
            )
            SELECT id, {admin_id}, source, channel_id, message_id, author_id, username,
                   user_content, had_image, image_urls, match_type, matched_code,
                   matched_link, reply_content, reply_status, reply_mode, reply_channel_id,
                   account_name, skip_reason, created_at, updated_at
            FROM {{old}}
        """)

    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON;")


def init_db():
    """建表 + 写入默认设置 + 初始化固定管理员账号。"""
    global _DEFAULT_USER_ID
    with get_conn() as conn:
        _create_users_table(conn)
        admin_id = _ensure_admin_user(conn)
        _DEFAULT_USER_ID = admin_id
        _migrate_user_scoped_tables(conn, admin_id)
        _ensure_default_settings(conn, admin_id)

        # 兼容旧数据：把 products 表里原来的单张商品图迁移到多图表。
        rows = conn.execute(
            """SELECT id, image_path, image_hash, created_at FROM products
               WHERE user_id=? AND image_hash != '' AND image_path != ''""",
            (admin_id,),
        ).fetchall()
        for p in rows:
            exists = conn.execute(
                """SELECT 1 FROM product_images
                   WHERE product_id=? AND image_hash=? LIMIT 1""",
                (p["id"], p["image_hash"]),
            ).fetchone()
            if not exists:
                conn.execute(
                    """INSERT INTO product_images(product_id, image_path, image_hash, created_at)
                       VALUES(?,?,?,?)""",
                    (p["id"], p["image_path"], p["image_hash"], p["created_at"] or _now()),
                )

        conn.execute("""
            INSERT OR IGNORE INTO message_records(
                user_id, source, channel_id, message_id, author_id, username, user_content,
                had_image, image_urls, match_type, matched_code, matched_link,
                reply_content, reply_status, reply_mode, reply_channel_id, account_name,
                skip_reason, created_at, updated_at
            )
            SELECT user_id, source, channel_id, message_id, author_id, username, user_content,
                   had_image, image_urls, match_type, matched_code, matched_link,
                   reply_content, 'sent', reply_mode, reply_channel_id, account_name,
                   '', created_at, created_at
            FROM replied_messages
            """)


def default_user_id():
    global _DEFAULT_USER_ID
    if _DEFAULT_USER_ID is not None:
        return _DEFAULT_USER_ID
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM users WHERE username=?", (ADMIN_USERNAME,)).fetchone()
    if not row:
        init_db()
        return _DEFAULT_USER_ID
    _DEFAULT_USER_ID = row["id"]
    return _DEFAULT_USER_ID


# ---------------- 用户 / 登录 ----------------
def authenticate_user(username, password):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if row and check_password_hash(row["password_hash"], password):
        return row
    return None


def verify_user(username, password):
    return bool(authenticate_user(username, password))


def get_user(username):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()


def get_user_by_id(user_id):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE id=?", (_normalize_user_id(user_id),)).fetchone()


def list_users():
    with get_conn() as conn:
        return conn.execute(
            """SELECT u.*,
                      (SELECT COUNT(*) FROM products p WHERE p.user_id=u.id) AS product_count,
                      (SELECT COUNT(*) FROM accounts a WHERE a.user_id=u.id) AS account_count
               FROM users u
               ORDER BY u.is_admin DESC, u.id"""
        ).fetchall()


def create_user(username, password):
    username = (username or "").strip()
    password = password or ""
    if not username:
        return False, "用户名不能为空"
    if username == ADMIN_USERNAME:
        return False, "该用户名为固定管理员账号"
    if len(username) > 80:
        return False, "用户名不能超过 80 个字符"
    if len(password) < 6:
        return False, "密码至少需要 6 位"
    try:
        with get_conn() as conn:
            cur = conn.execute(
                """INSERT INTO users(username, password_hash, is_admin, created_at)
                   VALUES(?,?,0,?)""",
                (username, generate_password_hash(password), _now()),
            )
            _ensure_default_settings(conn, cur.lastrowid)
        return True, "用户已创建"
    except sqlite3.IntegrityError:
        return False, "该用户名已存在"


def delete_user(user_id):
    user_id = _normalize_user_id(user_id)
    user = get_user_by_id(user_id)
    if not user:
        return False, "用户不存在"
    if user["is_admin"]:
        return False, "不能删除管理员用户"
    with get_conn() as conn:
        product_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM products WHERE user_id=?", (user_id,)
        ).fetchall()]
        if product_ids:
            placeholders = ",".join("?" for _ in product_ids)
            conn.execute(f"DELETE FROM product_images WHERE product_id IN ({placeholders})", product_ids)
        for table in (
            "message_records", "replied_messages", "match_logs", "blocked_keywords",
            "accounts", "settings", "products",
        ):
            conn.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    _PRODUCT_CACHE_VERSION.pop(user_id, None)
    _BLOCKED_KEYWORDS_CACHE.pop(user_id, None)
    return True, "用户已删除"


def reset_user_password(user_id, password):
    user_id = _normalize_user_id(user_id)
    if len(password or "") < 6:
        return False, "密码至少需要 6 位"
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            return False, "用户不存在"
        if row["is_admin"]:
            return False, "管理员账号密码由固定配置控制"
        conn.execute(
            "UPDATE users SET password_hash=? WHERE id=?",
            (generate_password_hash(password), user_id),
        )
    return True, "密码已重置"


def change_credentials(user_id, new_username, new_password):
    user_id = _normalize_user_id(user_id)
    new_username = (new_username or "").strip()
    if not new_username:
        return False, "登录名不能为空"
    if len(new_password or "") < 6:
        return False, "密码至少需要 6 位"
    user = get_user_by_id(user_id)
    if not user:
        return False, "用户不存在"
    if user["is_admin"]:
        return False, "管理员账号由固定配置控制，不能在这里修改"
    try:
        with get_conn() as conn:
            conn.execute(
                "UPDATE users SET username=?, password_hash=? WHERE id=?",
                (new_username, generate_password_hash(new_password), user_id),
            )
        return True, "登录凭据已更新"
    except sqlite3.IntegrityError:
        return False, "该登录名已存在"


# ---------------- 设置 ----------------
def get_settings(user_id=None):
    user_id = _normalize_user_id(user_id)
    with get_conn() as conn:
        _ensure_default_settings(conn, user_id)
        rows = conn.execute("SELECT key, value FROM settings WHERE user_id=?", (user_id,)).fetchall()
    data = {r["key"]: r["value"] for r in rows}
    for k, v in DEFAULT_SETTINGS.items():
        data.setdefault(k, v)
    return data


def get_setting(key, default=None, user_id=None):
    return get_settings(user_id).get(key, default)


def update_settings(user_id, items):
    user_id = _normalize_user_id(user_id)
    with get_conn() as conn:
        for k, v in items.items():
            conn.execute(
                """INSERT INTO settings(user_id, key, value) VALUES(?, ?, ?)
                   ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value""",
                (user_id, k, str(v)),
            )


# ---------------- 屏蔽关键字 ----------------
def _keyword_norm(keyword):
    return (keyword or "").strip().casefold()


def list_blocked_keywords(user_id=None):
    user_id = _normalize_user_id(user_id)
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM blocked_keywords WHERE user_id=? ORDER BY id DESC", (user_id,)
        ).fetchall()


def add_blocked_keyword(user_id, keyword):
    user_id = _normalize_user_id(user_id)
    keyword = (keyword or "").strip()
    norm = _keyword_norm(keyword)
    if not keyword:
        return False, "屏蔽关键字不能为空"
    if len(keyword) > 200:
        return False, "屏蔽关键字不能超过 200 个字符"
    try:
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO blocked_keywords(user_id, keyword, keyword_norm, created_at)
                   VALUES(?,?,?,?)""",
                (user_id, keyword, norm, _now()),
            )
        _bump_blocked_keywords_cache(user_id)
        return True, "屏蔽关键字已添加"
    except sqlite3.IntegrityError:
        return False, "该屏蔽关键字已存在"


def delete_blocked_keyword(user_id, keyword_id):
    user_id = _normalize_user_id(user_id)
    with get_conn() as conn:
        conn.execute("DELETE FROM blocked_keywords WHERE user_id=? AND id=?", (user_id, keyword_id))
    _bump_blocked_keywords_cache(user_id)


def find_blocked_keyword(user_id, text):
    user_id = _normalize_user_id(user_id)
    text_norm = _keyword_norm(text)
    if not text_norm:
        return None
    if user_id not in _BLOCKED_KEYWORDS_CACHE:
        _BLOCKED_KEYWORDS_CACHE[user_id] = [
            (row["keyword"], row["keyword_norm"]) for row in list_blocked_keywords(user_id)
        ]
    for keyword, keyword_norm in _BLOCKED_KEYWORDS_CACHE[user_id]:
        if keyword_norm and keyword_norm in text_norm:
            return keyword
    return None


# ---------------- 商品 ----------------
def list_products(user_id=None, keyword=None):
    user_id = _normalize_user_id(user_id)
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
        WHERE p.user_id=?
    """
    args = [user_id]
    if keyword:
        sql += " AND (p.code LIKE ? OR p.name LIKE ? OR p.link LIKE ? OR p.shop LIKE ?)"
        like = f"%{keyword}%"
        args.extend([like, like, like, like])
    sql += " ORDER BY p.updated_at DESC"
    with get_conn() as conn:
        return conn.execute(sql, args).fetchall()


def get_product(user_id, pid):
    user_id = _normalize_user_id(user_id)
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM products WHERE user_id=? AND id=?", (user_id, pid)
        ).fetchone()


def get_product_by_code(user_id, code):
    user_id = _normalize_user_id(user_id)
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM products WHERE user_id=? AND code=?", (user_id, code)
        ).fetchone()


def add_product(user_id, code, name, link, shop, image_path, image_hash, image_enabled, enabled=1):
    user_id = _normalize_user_id(user_id)
    now = _now()
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO products(user_id, code, name, link, shop, image_path, image_hash,
                                    image_enabled, enabled, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (user_id, code, name, link, shop, image_path, image_hash,
             int(image_enabled), int(enabled), now, now),
        )
        product_id = cur.lastrowid
    _bump_product_cache_version(user_id)
    export_product_maps(user_id)
    return product_id


def update_product(user_id, pid, **fields):
    user_id = _normalize_user_id(user_id)
    if not fields:
        return
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k}=?" for k in fields)
    args = list(fields.values()) + [user_id, pid]
    with get_conn() as conn:
        conn.execute(f"UPDATE products SET {cols} WHERE user_id=? AND id=?", args)
    _bump_product_cache_version(user_id)
    export_product_maps(user_id)


def delete_product(user_id, pid):
    user_id = _normalize_user_id(user_id)
    with get_conn() as conn:
        product = conn.execute(
            "SELECT id FROM products WHERE user_id=? AND id=?", (user_id, pid)
        ).fetchone()
        if not product:
            return
        conn.execute("DELETE FROM product_images WHERE product_id=?", (pid,))
        conn.execute("DELETE FROM products WHERE user_id=? AND id=?", (user_id, pid))
    _bump_product_cache_version(user_id)
    export_product_maps(user_id)


def product_images(user_id, pid):
    user_id = _normalize_user_id(user_id)
    with get_conn() as conn:
        product = conn.execute(
            "SELECT id FROM products WHERE user_id=? AND id=?", (user_id, pid)
        ).fetchone()
        if not product:
            return []
        return conn.execute(
            "SELECT * FROM product_images WHERE product_id=? ORDER BY id", (pid,)
        ).fetchall()


def add_product_image(user_id, product_id, image_path, image_hash):
    user_id = _normalize_user_id(user_id)
    now = _now()
    with get_conn() as conn:
        p = conn.execute(
            "SELECT image_path, image_hash FROM products WHERE user_id=? AND id=?",
            (user_id, product_id),
        ).fetchone()
        if not p:
            return
        conn.execute(
            """INSERT INTO product_images(product_id, image_path, image_hash, created_at)
               VALUES(?,?,?,?)""",
            (product_id, image_path, image_hash, now),
        )
        if not p["image_path"] or not p["image_hash"]:
            conn.execute(
                "UPDATE products SET image_path=?, image_hash=?, updated_at=? WHERE user_id=? AND id=?",
                (image_path, image_hash, now, user_id, product_id),
            )
        else:
            conn.execute(
                "UPDATE products SET updated_at=? WHERE user_id=? AND id=?",
                (now, user_id, product_id),
            )
    _bump_product_cache_version(user_id)


def replace_product_images(user_id, product_id, images, product_image_hash=None):
    user_id = _normalize_user_id(user_id)
    now = _now()
    first_path = images[0][0] if images else ""
    first_hash = product_image_hash if product_image_hash is not None else (images[0][1] if images else "")
    with get_conn() as conn:
        product = conn.execute(
            "SELECT id FROM products WHERE user_id=? AND id=?", (user_id, product_id)
        ).fetchone()
        if not product:
            return
        conn.execute("DELETE FROM product_images WHERE product_id=?", (product_id,))
        conn.execute(
            "UPDATE products SET image_path=?, image_hash=?, updated_at=? WHERE user_id=? AND id=?",
            (first_path, first_hash, now, user_id, product_id),
        )
        for image_path, image_hash in images:
            conn.execute(
                """INSERT INTO product_images(product_id, image_path, image_hash, created_at)
                   VALUES(?,?,?,?)""",
                (product_id, image_path, image_hash, now),
            )
    _bump_product_cache_version(user_id)


def products_with_image_hash(user_id=None):
    user_id = _normalize_user_id(user_id)
    with get_conn() as conn:
        return conn.execute(
            """SELECT p.*,
                      pi.id AS product_image_id,
                      pi.image_path AS product_image_path,
                      pi.image_hash AS product_image_hash
               FROM products p
               JOIN product_images pi ON pi.product_id=p.id
               WHERE p.user_id=?
                 AND p.enabled=1
                 AND p.image_enabled=1
                 AND pi.image_hash != ''
               ORDER BY p.id, pi.id""",
            (user_id,),
        ).fetchall()


def enabled_products(user_id=None):
    user_id = _normalize_user_id(user_id)
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM products WHERE user_id=? AND enabled=1", (user_id,)
        ).fetchall()


def owns_upload(user_id, image_path):
    user_id = _normalize_user_id(user_id)
    image_path = image_path or ""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT 1
               FROM products p
               LEFT JOIN product_images pi ON pi.product_id=p.id
               WHERE p.user_id=?
                 AND (p.image_path=? OR pi.image_path=?)
               LIMIT 1""",
            (user_id, image_path, image_path),
        ).fetchone()
    return bool(row)


# ---------------- 账号 ----------------
def list_accounts(user_id=None):
    user_id = _normalize_user_id(user_id)
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM accounts WHERE user_id=? ORDER BY id", (user_id,)
        ).fetchall()


def active_accounts(user_id=None):
    user_id = _normalize_user_id(user_id)
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM accounts WHERE user_id=? AND status='active' ORDER BY id",
            (user_id,),
        ).fetchall()


def add_account(user_id, name, token):
    user_id = _normalize_user_id(user_id)
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO accounts(user_id, name, token, next_available_time, last_used_time,
                                    status, invalid_reason, invalid_at, created_at)
               VALUES(?,?,?,0,'从未使用','active','','',?)""",
            (user_id, name, token, _now()),
        )


def delete_account(user_id, name):
    user_id = _normalize_user_id(user_id)
    with get_conn() as conn:
        conn.execute("DELETE FROM accounts WHERE user_id=? AND name=?", (user_id, name))


def update_account_usage(user_id, name, next_available_time, last_used_time):
    user_id = _normalize_user_id(user_id)
    with get_conn() as conn:
        conn.execute(
            """UPDATE accounts
               SET next_available_time=?, last_used_time=?
               WHERE user_id=? AND name=?""",
            (next_available_time, last_used_time, user_id, name),
        )


def mark_account_invalid(user_id, name, reason):
    user_id = _normalize_user_id(user_id)
    with get_conn() as conn:
        conn.execute(
            """UPDATE accounts
               SET status='invalid',
                   invalid_reason=?,
                   invalid_at=?,
                   next_available_time=0
               WHERE user_id=? AND name=?""",
            (reason, _now(), user_id, name),
        )


# ---------------- 匹配日志 ----------------
def log_match(user_id, source, query_text, had_image, match_type, matched_code, matched_link):
    user_id = _normalize_user_id(user_id)
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO match_logs(user_id, source, query_text, had_image, match_type,
                                      matched_code, matched_link, created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (user_id, source, query_text[:200], int(had_image), match_type,
             matched_code, matched_link, _now()),
        )


def recent_logs(user_id=None, limit=50):
    user_id = _normalize_user_id(user_id)
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM match_logs WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()


# ---------------- 已回复消息记录 ----------------
def has_replied_message(user_id, channel_id, message_id):
    user_id = _normalize_user_id(user_id)
    with get_conn() as conn:
        row = conn.execute(
            """SELECT 1 FROM message_records
               WHERE user_id=? AND channel_id=? AND message_id=? AND reply_status='sent'
               LIMIT 1""",
            (user_id, str(channel_id or ""), str(message_id or "")),
        ).fetchone()
    return bool(row)


def log_message_record(
    user_id, channel_id, message_id, author_id, username, user_content, had_image,
    image_urls, reply_content, reply_mode, reply_channel_id, account_name,
    match_type, matched_code, matched_link, reply_status, skip_reason="",
):
    user_id = _normalize_user_id(user_id)
    now = _now()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO message_records(
                   user_id, source, channel_id, message_id, author_id, username, user_content,
                   had_image, image_urls, match_type, matched_code, matched_link,
                   reply_content, reply_status, reply_mode, reply_channel_id, account_name,
                   skip_reason, created_at, updated_at
               )
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(user_id, channel_id, message_id) DO UPDATE SET
                   source=excluded.source,
                   author_id=excluded.author_id,
                   username=excluded.username,
                   user_content=excluded.user_content,
                   had_image=excluded.had_image,
                   image_urls=excluded.image_urls,
                   match_type=excluded.match_type,
                   matched_code=excluded.matched_code,
                   matched_link=excluded.matched_link,
                   reply_content=excluded.reply_content,
                   reply_status=excluded.reply_status,
                   reply_mode=excluded.reply_mode,
                   reply_channel_id=excluded.reply_channel_id,
                   account_name=excluded.account_name,
                   skip_reason=excluded.skip_reason,
                   updated_at=excluded.updated_at""",
            (
                user_id,
                "discord",
                str(channel_id or ""),
                str(message_id or ""),
                str(author_id or ""),
                username or "",
                (user_content or "")[:1000],
                int(had_image),
                json.dumps(list(image_urls or []), ensure_ascii=False),
                match_type or "",
                matched_code or "",
                matched_link or "",
                reply_content or "",
                reply_status or "",
                reply_mode or "",
                str(reply_channel_id or ""),
                account_name or "",
                skip_reason or "",
                now,
                now,
            ),
        )


def log_replied_message(
    user_id, channel_id, message_id, author_id, username, user_content, had_image,
    image_urls, reply_content, reply_mode, reply_channel_id, account_name,
    match_type, matched_code, matched_link,
):
    log_message_record(
        user_id, channel_id, message_id, author_id, username, user_content, had_image,
        image_urls, reply_content, reply_mode, reply_channel_id, account_name,
        match_type, matched_code, matched_link, "sent",
    )


def recent_message_records(user_id=None, limit=50):
    user_id = _normalize_user_id(user_id)
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM message_records WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()


def count_message_records(user_id=None):
    user_id = _normalize_user_id(user_id)
    with get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM message_records WHERE user_id=?", (user_id,)
        ).fetchone()["n"]


def prune_message_records(user_id=None, max_rows=3000):
    user_id = _normalize_user_id(user_id)

    def positive_int(value, default):
        try:
            n = int(value)
            return n if n >= 0 else default
        except (TypeError, ValueError):
            return default

    max_rows = positive_int(max_rows, 3000)
    deleted = 0
    with get_conn() as conn:
        if max_rows > 0:
            cur = conn.execute(
                """DELETE FROM message_records
                   WHERE user_id=?
                     AND id NOT IN (
                       SELECT id FROM message_records
                       WHERE user_id=?
                       ORDER BY id DESC LIMIT ?
                   )""",
                (user_id, user_id, max_rows),
            )
            deleted += max(cur.rowcount or 0, 0)
    return deleted


def delete_message_record(user_id, record_id):
    user_id = _normalize_user_id(user_id)
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM message_records WHERE user_id=? AND id=?", (user_id, record_id)
        )


def delete_message_records(user_id, record_ids):
    user_id = _normalize_user_id(user_id)
    ids = [int(record_id) for record_id in record_ids if str(record_id).strip().isdigit()]
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    with get_conn() as conn:
        cur = conn.execute(
            f"DELETE FROM message_records WHERE user_id=? AND id IN ({placeholders})",
            [user_id] + ids,
        )
        return cur.rowcount


def clear_message_records(user_id=None):
    user_id = _normalize_user_id(user_id)
    with get_conn() as conn:
        conn.execute("DELETE FROM message_records WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM replied_messages WHERE user_id=?", (user_id,))


def recent_replied_messages(user_id=None, limit=50):
    return recent_message_records(user_id, limit)


def count_replied_messages(user_id=None):
    return count_message_records(user_id)


def clear_replied_messages(user_id=None):
    clear_message_records(user_id)


def counts(user_id=None):
    user_id = _normalize_user_id(user_id)
    with get_conn() as conn:
        p = conn.execute(
            "SELECT COUNT(*) AS n FROM products WHERE user_id=?", (user_id,)
        ).fetchone()["n"]
        a = conn.execute(
            "SELECT COUNT(*) AS n FROM accounts WHERE user_id=? AND status='active'", (user_id,)
        ).fetchone()["n"]
        img = conn.execute(
            """SELECT COUNT(DISTINCT p.id) AS n
               FROM products p
               JOIN product_images pi ON pi.product_id=p.id
               WHERE p.user_id=? AND p.image_enabled=1 AND pi.image_hash != ''""",
            (user_id,),
        ).fetchone()["n"]
        messages = conn.execute(
            "SELECT COUNT(*) AS n FROM message_records WHERE user_id=?", (user_id,)
        ).fetchone()["n"]
    return {"products": p, "accounts": a, "image_products": img, "messages": messages}


# ---------------- 兼容老 reply.py：导出 product_maps.yaml ----------------
def export_product_maps(user_id=None):
    """把当前用户的商品名→链接导出为 yaml。网页版匹配直接读 SQLite。"""
    user_id = _normalize_user_id(user_id)
    try:
        text_maps = {}
        for p in enabled_products(user_id):
            if p["name"] and p["link"]:
                text_maps[p["name"]] = p["link"]
        os.makedirs(os.path.dirname(PRODUCT_MAP_FILE), exist_ok=True)
        base, ext = os.path.splitext(PRODUCT_MAP_FILE)
        path = f"{base}_{user_id}{ext or '.yaml'}"
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump({"text_maps": text_maps}, f, allow_unicode=True,
                      indent=2, sort_keys=False)
        if user_id == default_user_id():
            with open(PRODUCT_MAP_FILE, "w", encoding="utf-8") as f:
                yaml.dump({"text_maps": text_maps}, f, allow_unicode=True,
                          indent=2, sort_keys=False)
    except Exception as e:
        print(f"[store] 导出 product_maps.yaml 失败: {e}")
