"""老版本（1.1.x）SQLite 库直接升级到 1.2.5 的兼容性测试。

目标（用户明确要求）：老用户不运行安装脚本，直接用新版本启动应用即完成升级：
- 继续使用原 SQLite 库（.env 里没有 DB_TYPE 时默认 sqlite）；
- 自动补齐缺失的表、列与索引；
- 原有数据、管理员、配置原样保留；
- 升级后业务流程（写图片、点赞、配置读写）正常工作。
"""
import os
import sqlite3
import tempfile
import unittest

import app.database as db
import app.models as models
from tests.sqlite_test_utils import require_sqlite

# 1.1.x 早期版本的建表语句（缺少后来新增的列/表，用于模拟老库）
_OLD_SCHEMA = """
CREATE TABLE images (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    filename        TEXT NOT NULL,
    thumb_filename  TEXT NOT NULL,
    original_size   INTEGER NOT NULL,
    compressed_size INTEGER NOT NULL,
    width           INTEGER NOT NULL,
    height          INTEGER NOT NULL,
    description     TEXT DEFAULT '',
    author          TEXT DEFAULT NULL,
    created_at      TEXT NOT NULL
);
CREATE TABLE likes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    image_id    INTEGER NOT NULL,
    ip          TEXT NOT NULL,
    ua_hash     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE ban_list (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    target      TEXT NOT NULL,
    type        TEXT NOT NULL,
    reason      TEXT NOT NULL,
    admin_id    INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    is_active   INTEGER DEFAULT 1
);
CREATE TABLE admins (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    login_attempts  INTEGER DEFAULT 0,
    locked_until    TEXT DEFAULT NULL,
    created_at      TEXT NOT NULL
);
CREATE TABLE sessions (
    session_id  TEXT PRIMARY KEY,
    admin_id    INTEGER NOT NULL,
    username    TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);
CREATE TABLE login_rate_limits (
    key          TEXT PRIMARY KEY,
    attempts     INTEGER NOT NULL DEFAULT 0,
    window_start TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE TABLE settings (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);
CREATE TABLE pages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT NOT NULL UNIQUE,
    title       TEXT NOT NULL,
    content     TEXT DEFAULT '',
    sort_order  INTEGER DEFAULT 0,
    is_visible  INTEGER DEFAULT 1,
    updated_at  TEXT NOT NULL
);
CREATE TABLE friend_links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    url         TEXT NOT NULL,
    sort_order  INTEGER DEFAULT 0,
    is_visible  INTEGER DEFAULT 1,
    created_at  TEXT NOT NULL
);
CREATE TABLE daily_stats (
    date            TEXT PRIMARY KEY,
    upload_count    INTEGER DEFAULT 0,
    approved_count  INTEGER DEFAULT 0,
    rejected_count  INTEGER DEFAULT 0,
    like_count      INTEGER DEFAULT 0,
    view_count      INTEGER DEFAULT 0
);
CREATE TABLE admin_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id    INTEGER NOT NULL,
    admin_name  TEXT NOT NULL,
    action      TEXT NOT NULL,
    detail      TEXT DEFAULT '',
    target      TEXT DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE TABLE view_sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    image_id    INTEGER NOT NULL,
    ip          TEXT NOT NULL,
    viewed_at   TEXT NOT NULL
);
CREATE TABLE schema_migrations (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
);
"""


class OldVersionUpgradeCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_sqlite()
        cls.tmpdir = tempfile.mkdtemp(prefix="amig_test_sqlite_upgrade_")
        cls.db_path = os.path.join(cls.tmpdir, "app.db")

        conn = sqlite3.connect(cls.db_path)
        try:
            conn.executescript(_OLD_SCHEMA)
            conn.execute(
                "INSERT INTO admins (username, password_hash, created_at)"
                " VALUES ('admin', 'old-hash', '2025-01-01T00:00:00+00:00')"
            )
            conn.execute(
                "INSERT INTO images (filename, thumb_filename, original_size,"
                " compressed_size, width, height, description, author, created_at)"
                " VALUES ('old.webp', 'old_thumb.webp', 1024, 900, 100, 80,"
                " '老数据', '老作者', '2025-01-01T00:00:00+00:00')"
            )
            conn.execute(
                "INSERT INTO settings (`key`, value) VALUES ('site_name', '老站点')"
            )
            conn.execute(
                "INSERT INTO sessions (session_id, admin_id, username, created_at, expires_at)"
                " VALUES ('s-old', 1, 'admin', '2025-01-01T00:00:00+00:00',"
                " '2099-01-01T00:00:00+00:00')"
            )
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at)"
                " VALUES (4, '2025-01-01T00:00:00+00:00')"
            )
            conn.commit()
        finally:
            conn.close()

        # 模拟「老用户直接启动应用」：不做任何手工迁移，只指向原库并初始化
        db.DATABASE_PATH = cls.db_path
        db._reset_pool()
        db.init_db(max_wait_seconds=10)

    @classmethod
    def tearDownClass(cls):
        db._reset_pool()
        import shutil
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_old_data_preserved(self):
        image = db.get_db().execute(
            "SELECT filename, description, author FROM images WHERE filename='old.webp'"
        ).fetchone()
        self.assertIsNotNone(image, "升级后老图片丢失")
        self.assertEqual(image["description"], "老数据")
        self.assertEqual(image["author"], "老作者")

        admin = db.get_db().execute(
            "SELECT username, password_hash FROM admins WHERE username='admin'"
        ).fetchone()
        self.assertEqual(admin["password_hash"], "old-hash", "升级后管理员被改写")

        self.assertEqual(models.get_setting("site_name"), "老站点")

    def test_missing_columns_and_tables_added(self):
        image_cols = {
            r["name"] for r in db.get_db().execute(
                "PRAGMA table_info(images)").fetchall()
        }
        for col in ("title", "region", "status", "content_md5", "ua_hash",
                    "like_count", "is_duplicate", "reviewed_at"):
            self.assertIn(col, image_cols, f"老库缺少列 {col}")

        session_cols = {
            r["name"] for r in db.get_db().execute(
                "PRAGMA table_info(sessions)").fetchall()
        }
        self.assertIn("csrf_token", session_cols)
        self.assertIn("last_seen_at", session_cols)
        last_seen = db.get_db().execute(
            "SELECT last_seen_at FROM sessions WHERE session_id='s-old'"
        ).fetchone()["last_seen_at"]
        self.assertEqual(last_seen, "2025-01-01T00:00:00+00:00",
                         "老会话的最近活动时间未回填")

        tables = {
            r["name"] for r in db.get_db().execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        for table in ("upload_reservations", "cooling", "view_sessions"):
            self.assertIn(table, tables, f"老库缺少表 {table}")

    def test_schema_version_upgraded(self):
        versions = {
            r["version"] for r in db.get_db().execute(
                "SELECT version FROM schema_migrations").fetchall()
        }
        self.assertIn(db.SCHEMA_VERSION, versions)

    def test_zz_business_flows_work_after_upgrade(self):
        # 老图片可正常读取
        row = models.get_image_by_id(1)
        self.assertEqual(row["filename"], "old.webp")
        # 新增图片（依赖补列）可正常写入
        new_id = models.insert_image(
            "new.webp", "new_thumb.webp", 100, 90, 10, 10, "新图片", "作者",
            "md5-new", "1.1.1.1", "UA", models.ua_hash("UA"), "省", "市",
        )
        self.assertIsNotNone(new_id)
        # 点赞（唯一索引 + 日统计 UPSERT）可正常工作
        self.assertTrue(models.add_like_atomic(new_id, "1.1.1.1", "UA", "省", "市"))
        self.assertFalse(models.add_like_atomic(new_id, "1.1.1.1", "UA", "省", "市"))
        # 配置读写
        models.set_setting("site_name", "新站点")
        self.assertEqual(models.get_setting("site_name"), "新站点")


if __name__ == "__main__":
    unittest.main()
