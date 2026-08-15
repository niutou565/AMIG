"""测试数据库迁移逻辑：模拟老旧库缺少字段时的兼容升级"""
import os
import shutil
import sqlite3
import tempfile
import unittest

import app.database as db


class TestLegacyMigration(unittest.TestCase):
    """模拟老版本数据库（缺少 ip 等基础字段），验证 init_db 能正确补列并建索引"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig = db.DATABASE_PATH
        self._db_path = os.path.join(self._tmpdir, "legacy.db")
        db.DATABASE_PATH = self._db_path
        # 重置线程本地连接，确保指向新库
        db.close_db()

    def tearDown(self):
        db.close_db()
        db.DATABASE_PATH = self._orig
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _create_legacy_images_table(self, conn):
        """模拟一个极早期版本的 images 表：缺少 ip/ua_hash/province/city 等字段"""
        conn.execute("DROP TABLE IF EXISTS images")
        conn.execute("""
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
                content_md5     TEXT NOT NULL,
                created_at      TEXT NOT NULL
            )
        """)
        # 插入一条老数据
        conn.execute(
            "INSERT INTO images (filename, thumb_filename, original_size, compressed_size,"
            " width, height, description, author, content_md5, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("a.webp", "a_t.webp", 2000, 1500, 800, 600, "老照片", "老王",
             "deadbeef", "2024-01-01T00:00:00"),
        )
        conn.commit()

    def test_migrate_legacy_images_table(self):
        conn = db.get_db()
        self._create_legacy_images_table(conn)
        # 关闭旧连接后再调用 init_db，避免线程本地 conn 被复用导致 PRAGMA 结果缓存
        db.close_db()

        # 执行初始化（应该会补列再建索引）
        db.init_db()

        conn = db.get_db()
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(images)").fetchall()}
        # 验证所有新增字段都已补齐
        for name in ("ip", "ua", "ua_hash", "province", "city",
                     "title", "region", "status", "views", "like_count",
                     "is_duplicate", "reviewed_at", "reject_reason", "admin_edited"):
            self.assertIn(name, cols, f"缺少字段: {name}")

        # 验证索引创建成功（之前报错的根源：idx_images_ip）
        indexes = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='images'"
        )}
        for idx in ("idx_images_ip", "idx_images_ua_hash",
                    "idx_images_status", "idx_images_like_count"):
            self.assertIn(idx, indexes, f"缺少索引: {idx}")

        # 验证老数据依然存在，新增字段默认值正确
        row = conn.execute("SELECT * FROM images WHERE id=1").fetchone()
        self.assertEqual(row["filename"], "a.webp")
        self.assertEqual(row["ip"], "")
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["title"], "")
        self.assertEqual(row["views"], 0)

    def test_migrate_legacy_admin_logs_table(self):
        conn = db.get_db()
        # 模拟旧版 admin_logs（缺少 ip / location）
        conn.execute("DROP TABLE IF EXISTS admin_logs")
        conn.execute("""
            CREATE TABLE admin_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id    INTEGER NOT NULL,
                admin_name  TEXT NOT NULL,
                action      TEXT NOT NULL,
                detail      TEXT DEFAULT '',
                target      TEXT DEFAULT '',
                created_at  TEXT NOT NULL
            )
        """)
        conn.execute(
            "INSERT INTO admin_logs (admin_id, admin_name, action, detail, target, created_at)"
            " VALUES (1, 'admin', 'login', 'ok', '', '2024-06-01T12:00:00')",
        )
        conn.commit()
        db.close_db()

        db.init_db()
        conn = db.get_db()
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(admin_logs)").fetchall()}
        self.assertIn("ip", cols)
        self.assertIn("location", cols)
        row = conn.execute("SELECT * FROM admin_logs WHERE id=1").fetchone()
        self.assertEqual(row["ip"], "")
        self.assertEqual(row["location"], "")


if __name__ == "__main__":
    unittest.main()
