import sqlite3
import threading
from app.config import DATABASE_PATH

_local = threading.local()

def get_db() -> sqlite3.Connection:
    """获取当前线程的数据库连接，自动开启 WAL 模式"""
    if not hasattr(_local, "conn") or _local.conn is None:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        _local.conn = conn
    return _local.conn


def close_db():
    """关闭当前线程的数据库连接"""
    if hasattr(_local, "conn") and _local.conn is not None:
        _local.conn.close()
        _local.conn = None


def _add_col_safe(conn, table, col_def):
    """安全地为表添加列，忽略列已存在的错误。

    col_def 形如 "ip TEXT NOT NULL DEFAULT ''"，即 ADD COLUMN 之后的完整定义。
    """
    col_name = col_def.split()[0]
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")
    except sqlite3.OperationalError:
        # 多 worker 并发启动或列已存在时均忽略
        pass


def init_db():
    """初始化数据库表结构

    注意：为兼容老版本数据库（缺少 ip/ua_hash 等列），执行顺序必须严格为
    「建表 → 补列迁移 → 建索引」，否则 CREATE INDEX 会因列不存在而报错。
    """
    conn = get_db()

    # ── 第一阶段：仅建表（不含索引） ──────────────────────────────────
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS images (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            filename        TEXT NOT NULL,
            thumb_filename  TEXT NOT NULL,
            original_size   INTEGER NOT NULL,
            compressed_size INTEGER NOT NULL,
            width           INTEGER NOT NULL,
            height          INTEGER NOT NULL,
            description     TEXT DEFAULT '',
            title           TEXT DEFAULT '',
            author          TEXT DEFAULT NULL,
            content_md5     TEXT NOT NULL,
            ip              TEXT NOT NULL,
            ua              TEXT DEFAULT '',
            ua_hash         TEXT NOT NULL,
            province        TEXT DEFAULT '',
            city            TEXT DEFAULT '',
            region          TEXT DEFAULT '',
            status          TEXT DEFAULT 'pending',
            reject_reason   TEXT DEFAULT NULL,
            admin_edited    INTEGER DEFAULT 0,
            created_at      TEXT NOT NULL,
            reviewed_at     TEXT DEFAULT NULL,
            views           INTEGER DEFAULT 0,
            like_count      INTEGER DEFAULT 0,
            is_duplicate    INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS likes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            image_id    INTEGER NOT NULL,
            ip          TEXT NOT NULL,
            ua_hash     TEXT NOT NULL,
            province    TEXT DEFAULT '',
            city        TEXT DEFAULT '',
            created_at  TEXT NOT NULL,
            FOREIGN KEY (image_id) REFERENCES images(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS ban_list (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            target      TEXT NOT NULL,
            type        TEXT NOT NULL,
            reason      TEXT NOT NULL,
            admin_id    INTEGER NOT NULL,
            created_at  TEXT NOT NULL,
            is_active   INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS admins (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            username        TEXT NOT NULL UNIQUE,
            password_hash   TEXT NOT NULL,
            login_attempts  INTEGER DEFAULT 0,
            locked_until    TEXT DEFAULT NULL,
            created_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sessions (
            session_id  TEXT PRIMARY KEY,
            admin_id    INTEGER NOT NULL,
            username    TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            expires_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settings (
            key     TEXT PRIMARY KEY,
            value   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            slug        TEXT NOT NULL UNIQUE,
            title       TEXT NOT NULL,
            content     TEXT DEFAULT '',
            sort_order  INTEGER DEFAULT 0,
            is_visible  INTEGER DEFAULT 1,
            updated_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS friend_links (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            url         TEXT NOT NULL,
            sort_order  INTEGER DEFAULT 0,
            is_visible  INTEGER DEFAULT 1,
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS daily_stats (
            date            TEXT PRIMARY KEY,
            upload_count    INTEGER DEFAULT 0,
            approved_count  INTEGER DEFAULT 0,
            rejected_count  INTEGER DEFAULT 0,
            like_count      INTEGER DEFAULT 0,
            view_count      INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS admin_logs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id    INTEGER NOT NULL,
            admin_name  TEXT NOT NULL,
            action      TEXT NOT NULL,
            detail      TEXT DEFAULT '',
            target      TEXT DEFAULT '',
            ip          TEXT DEFAULT '',
            location    TEXT DEFAULT '',
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS view_sessions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            image_id    INTEGER NOT NULL,
            ip          TEXT NOT NULL,
            viewed_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS cooling (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ip          TEXT NOT NULL UNIQUE,
            reject_count INTEGER DEFAULT 0,
            cooldown_multiplier INTEGER DEFAULT 1,
            updated_at  TEXT NOT NULL
        );
    """)

    # ── 第二阶段：补列迁移（必须在建索引之前完成） ──────────────────────

    # images 表：兼容更早期版本中缺失的基础字段
    img_cols = {r["name"] for r in conn.execute("PRAGMA table_info(images)").fetchall()}
    img_missing = [
        # (列名, ADD COLUMN 定义)
        ("content_md5", "content_md5 TEXT NOT NULL DEFAULT ''"),
        ("ip",          "ip TEXT NOT NULL DEFAULT ''"),
        ("ua",          "ua TEXT DEFAULT ''"),
        ("ua_hash",     "ua_hash TEXT NOT NULL DEFAULT ''"),
        ("province",    "province TEXT DEFAULT ''"),
        ("city",        "city TEXT DEFAULT ''"),
        ("title",       "title TEXT DEFAULT ''"),
        ("region",      "region TEXT DEFAULT ''"),
        ("status",      "status TEXT DEFAULT 'pending'"),
        ("reject_reason", "reject_reason TEXT DEFAULT NULL"),
        ("admin_edited",  "admin_edited INTEGER DEFAULT 0"),
        ("reviewed_at",   "reviewed_at TEXT DEFAULT NULL"),
        ("views",         "views INTEGER DEFAULT 0"),
        ("like_count",    "like_count INTEGER DEFAULT 0"),
        ("is_duplicate",  "is_duplicate INTEGER DEFAULT 0"),
    ]
    for name, defn in img_missing:
        if name not in img_cols:
            _add_col_safe(conn, "images", defn)

    # admin_logs 表
    log_cols = {r["name"] for r in conn.execute("PRAGMA table_info(admin_logs)").fetchall()}
    for name, defn in [
        ("ip",       "ip TEXT DEFAULT ''"),
        ("location", "location TEXT DEFAULT ''"),
    ]:
        if name not in log_cols:
            _add_col_safe(conn, "admin_logs", defn)

    conn.commit()

    # ── 第三阶段：建索引（此时所有列均已存在） ──────────────────────────
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_images_status ON images(status);
        CREATE INDEX IF NOT EXISTS idx_images_reviewed_at ON images(reviewed_at);
        CREATE INDEX IF NOT EXISTS idx_images_like_count ON images(like_count);
        CREATE INDEX IF NOT EXISTS idx_images_content_md5 ON images(content_md5);
        CREATE INDEX IF NOT EXISTS idx_images_created_at ON images(created_at);
        CREATE INDEX IF NOT EXISTS idx_images_ip ON images(ip);
        CREATE INDEX IF NOT EXISTS idx_images_ua_hash ON images(ua_hash);
        CREATE INDEX IF NOT EXISTS idx_images_status_reviewed ON images(status, reviewed_at);
        CREATE INDEX IF NOT EXISTS idx_images_status_created ON images(status, created_at);

        CREATE UNIQUE INDEX IF NOT EXISTS idx_likes_fingerprint ON likes(image_id, ip, ua_hash);
        CREATE INDEX IF NOT EXISTS idx_likes_created_at ON likes(created_at);
        CREATE INDEX IF NOT EXISTS idx_likes_image_id ON likes(image_id);
        CREATE INDEX IF NOT EXISTS idx_likes_ip ON likes(ip);
        CREATE INDEX IF NOT EXISTS idx_likes_province ON likes(province);
        CREATE INDEX IF NOT EXISTS idx_likes_image_created ON likes(image_id, created_at);

        CREATE INDEX IF NOT EXISTS idx_ban_list_target ON ban_list(target);
        CREATE INDEX IF NOT EXISTS idx_ban_list_type ON ban_list(type);

        CREATE INDEX IF NOT EXISTS idx_admin_logs_created_at ON admin_logs(created_at);
        CREATE INDEX IF NOT EXISTS idx_admin_logs_ip ON admin_logs(ip);
        CREATE INDEX IF NOT EXISTS idx_admin_logs_action ON admin_logs(action);

        CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at);

        CREATE INDEX IF NOT EXISTS idx_view_sessions_lookup ON view_sessions(image_id, ip, viewed_at);
    """)

    conn.commit()