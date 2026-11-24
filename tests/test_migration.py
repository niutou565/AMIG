"""SQLite → MySQL 迁移工具（tools/migrate_sqlite_to_mysql.py）测试。

构造一个旧版 AMIG SQLite 库（含图片/点赞关联、管理员、设置、日志、统计、
页面与结构版本），调用真实迁移工具导入独立 MySQL 测试库，验证：
- 行数一致、主键 ID 保留、中文/emoji/NULL 内容一致；
- 点赞关联关系保留、自增序列已恢复；
- 目标库非空时拒绝重复迁移，且数据不被改动；
- 迁移失败（字段超长）时目标库无数据、源 SQLite 文件哈希不变。

SQLite 仅用于构造“旧库”测试数据；对 MySQL 的检查全部通过 information_schema
或业务查询完成。
"""
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import app.database as database
import app.models as models
from app.config import MYSQL_HOST, MYSQL_PASSWORD, MYSQL_PORT, MYSQL_USER
from tests.mysql_test_utils import create_test_database, drop_test_database

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MIGRATOR = PROJECT_ROOT / "tools" / "migrate_sqlite_to_mysql.py"

# 源库中会创建的表（schema_migrations 单独校验）
SOURCE_TABLES = (
    "images", "likes", "admins", "settings", "admin_logs",
    "daily_stats", "pages", "schema_migrations",
)
# 迁移工具视为“业务表”的全部表（用于断言失败时目标库无数据）
MIGRATION_TABLES = (
    "images", "likes", "ban_list", "admins", "sessions", "login_rate_limits",
    "settings", "upload_reservations", "pages", "friend_links", "daily_stats",
    "admin_logs", "view_sessions", "cooling",
)

_SOURCE_SCHEMA = """
CREATE TABLE images (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    filename        TEXT NOT NULL,
    thumb_filename  TEXT NOT NULL,
    original_size   INTEGER NOT NULL,
    compressed_size INTEGER NOT NULL,
    width           INTEGER NOT NULL,
    height          INTEGER NOT NULL,
    description     TEXT,
    title           TEXT NOT NULL DEFAULT '',
    author          TEXT,
    content_md5     TEXT NOT NULL,
    ip              TEXT NOT NULL DEFAULT '',
    ua              TEXT,
    ua_hash         TEXT NOT NULL DEFAULT '',
    province        TEXT NOT NULL DEFAULT '',
    city            TEXT NOT NULL DEFAULT '',
    region          TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending',
    reject_reason   TEXT,
    admin_edited    INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    reviewed_at     TEXT,
    views           INTEGER NOT NULL DEFAULT 0,
    like_count      INTEGER NOT NULL DEFAULT 0,
    is_duplicate    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE likes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    image_id    INTEGER NOT NULL,
    ip          TEXT NOT NULL,
    ua_hash     TEXT NOT NULL,
    province    TEXT NOT NULL DEFAULT '',
    city        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE TABLE admins (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    login_attempts  INTEGER NOT NULL DEFAULT 0,
    locked_until    TEXT,
    created_at      TEXT NOT NULL
);
CREATE TABLE settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
CREATE TABLE admin_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id    INTEGER NOT NULL,
    admin_name  TEXT NOT NULL,
    action      TEXT NOT NULL,
    detail      TEXT,
    target      TEXT NOT NULL DEFAULT '',
    ip          TEXT NOT NULL DEFAULT '',
    location    TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE TABLE daily_stats (
    date            TEXT PRIMARY KEY,
    upload_count    INTEGER NOT NULL DEFAULT 0,
    approved_count  INTEGER NOT NULL DEFAULT 0,
    rejected_count  INTEGER NOT NULL DEFAULT 0,
    like_count      INTEGER NOT NULL DEFAULT 0,
    view_count      INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE pages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT NOT NULL UNIQUE,
    title       TEXT NOT NULL,
    content     TEXT,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    is_visible  INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT NOT NULL
);
CREATE TABLE schema_migrations (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
);
"""


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class MigrationToolTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="amig_migrate_")
        self._sqlite_path = os.path.join(self._tmpdir, "legacy.db")
        # 工具会自行 init_db 并拒绝非空目标库；这里给一个干净的全新测试库
        self.db_name = create_test_database()

    def tearDown(self):
        drop_test_database(self.db_name)
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    # ---------- 工具 ----------

    def _build_source(self, long_ip=False):
        """构造旧版 SQLite 库并插入样例数据，返回各表行数。"""
        ip_for_long_check = "9" * 100 if long_ip else "2.2.2.2"
        conn = sqlite3.connect(self._sqlite_path)
        try:
            conn.executescript(_SOURCE_SCHEMA)
            conn.executemany(
                "INSERT INTO images (id, filename, thumb_filename, original_size,"
                " compressed_size, width, height, description, title, author,"
                " content_md5, ip, ua, ua_hash, province, city, region, status,"
                " reject_reason, admin_edited, created_at, reviewed_at, views,"
                " like_count, is_duplicate) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                "?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        1, "美丽图片.webp", "美丽图片_t.webp", 2000, 1500, 800, 600,
                        "海边日落 🌅", "美丽风景 🌄", None, "a" * 32, "1.2.3.4",
                        "Mozilla/5.0 测试浏览器", "uh1", "广西", "柳州市", "cn",
                        "approved", None, 0, "2024-01-01T00:00:00+00:00",
                        "2024-01-02T00:00:00+00:00", 5, 2, 0,
                    ),
                    (
                        2, "b.webp", "b_t.webp", 1000, 800, 400, 300, None, "",
                        "张三", "b" * 32, ip_for_long_check, None, "uh2", "", "",
                        "", "pending", "内容重复", 1,
                        "2024-01-05T00:00:00+00:00", None, 0, 0, 1,
                    ),
                ],
            )
            conn.executemany(
                "INSERT INTO likes (id, image_id, ip, ua_hash, province, city,"
                " created_at) VALUES (?,?,?,?,?,?,?)",
                [
                    (10, 1, "1.2.3.4", "uh1", "广西", "柳州市",
                     "2024-01-03T12:00:00+00:00"),
                    (11, 1, "1.2.3.5", "uh1b", "广西", "柳州市",
                     "2024-01-04T08:30:00+00:00"),
                ],
            )
            conn.execute(
                "INSERT INTO admins (id, username, password_hash, login_attempts,"
                " locked_until, created_at) VALUES (?,?,?,?,?,?)",
                (1, "管理员", "hash_abc123", 0, None,
                 "2023-12-01T00:00:00+00:00"),
            )
            conn.executemany(
                "INSERT INTO settings (key, value) VALUES (?,?)",
                [
                    ("site_name", "测试站 🚀"),
                    ("admin_domain", "admin.test"),
                    ("sensitive_words", ""),
                ],
            )
            conn.execute(
                "INSERT INTO admin_logs (id, admin_id, admin_name, action, detail,"
                " target, ip, location, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (100, 1, "管理员", "login", "登录成功 🎉", "", "1.2.3.4",
                 "广西 柳州市", "2024-01-06T09:00:00+00:00"),
            )
            conn.executemany(
                "INSERT INTO daily_stats (date, upload_count, approved_count,"
                " rejected_count, like_count, view_count) VALUES (?,?,?,?,?,?)",
                [
                    ("2024-01-01", 3, 1, 0, 2, 5),
                    ("2024-01-02", 1, 1, 0, 0, 2),
                ],
            )
            conn.execute(
                "INSERT INTO pages (id, slug, title, content, sort_order,"
                " is_visible, updated_at) VALUES (?,?,?,?,?,?,?)",
                (5, "about", "关于我们", "内容 🎨", 1, 1,
                 "2024-01-01T00:00:00+00:00"),
            )
            conn.executemany(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?,?)",
                [
                    (1, "2023-01-01T00:00:00"),
                    (2, "2023-02-01T00:00:00"),
                    (3, "2023-03-01T00:00:00"),
                ],
            )
            conn.commit()
            return {
                table: conn.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                for table in SOURCE_TABLES
            }
        finally:
            conn.close()

    def _run_migrator(self):
        env = os.environ.copy()
        env.update({
            "MYSQL_HOST": MYSQL_HOST,
            "MYSQL_PORT": str(MYSQL_PORT),
            "MYSQL_USER": MYSQL_USER,
            "MYSQL_PASSWORD": MYSQL_PASSWORD,
            "MYSQL_DATABASE": self.db_name,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        })
        return subprocess.run(
            [sys.executable, str(MIGRATOR), "--source", self._sqlite_path, "--yes"],
            cwd=str(PROJECT_ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )

    def _refresh_db(self):
        """让本进程重新连接测试库，读取子进程迁移后的数据。"""
        database._reset_pool()

    def _count(self, table):
        return database.get_db().execute(
            f"SELECT COUNT(*) AS c FROM `{table}`"
        ).fetchone()["c"]

    def _business_snapshot(self):
        """迁移工具关注的业务表内容快照（不含 schema_migrations）。"""
        conn = database.get_db()
        snapshot = {}
        for table in MIGRATION_TABLES:
            rows = conn.execute(f"SELECT * FROM `{table}`").fetchall()
            snapshot[table] = sorted((tuple(r) for r in rows), key=repr)
        return snapshot

    # ---------- 用例 ----------

    def test_migrates_rows_ids_content_and_autoincrement(self):
        counts = self._build_source()
        digest_before = sha256_of(self._sqlite_path)

        result = self._run_migrator()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        # 源 SQLite 文件未被修改
        self.assertEqual(sha256_of(self._sqlite_path), digest_before)

        self._refresh_db()
        conn = database.get_db()

        # 行数一致
        for table, expected in counts.items():
            self.assertEqual(self._count(table), expected, table)

        # 主键 ID 保留
        image = conn.execute("SELECT * FROM images WHERE id=1").fetchone()
        self.assertIsNotNone(image)
        self.assertEqual(image["filename"], "美丽图片.webp")
        self.assertEqual(image["title"], "美丽风景 🌄")
        self.assertEqual(image["description"], "海边日落 🌅")
        self.assertIsNone(image["author"])
        self.assertEqual(image["reviewed_at"], "2024-01-02T00:00:00+00:00")
        self.assertEqual(image["province"], "广西")
        self.assertEqual(image["city"], "柳州市")
        self.assertEqual(image["status"], "approved")

        second = conn.execute("SELECT * FROM images WHERE id=2").fetchone()
        self.assertIsNotNone(second)
        self.assertIsNone(second["description"])
        self.assertEqual(second["author"], "张三")
        self.assertIsNone(second["reviewed_at"])
        self.assertEqual(second["is_duplicate"], 1)
        self.assertEqual(second["admin_edited"], 1)

        # 中文/emoji 内容（含 NULL）完整保留
        settings = {
            row["key"]: row["value"]
            for row in conn.execute("SELECT * FROM settings").fetchall()
        }
        self.assertEqual(settings["site_name"], "测试站 🚀")
        self.assertEqual(settings["admin_domain"], "admin.test")
        self.assertEqual(settings["sensitive_words"], "")

        admin_log = conn.execute("SELECT * FROM admin_logs WHERE id=100").fetchone()
        self.assertIsNotNone(admin_log)
        self.assertEqual(admin_log["detail"], "登录成功 🎉")
        self.assertEqual(admin_log["location"], "广西 柳州市")

        admin = conn.execute("SELECT * FROM admins WHERE id=1").fetchone()
        self.assertEqual(admin["username"], "管理员")
        self.assertIsNone(admin["locked_until"])

        page = conn.execute("SELECT * FROM pages WHERE id=5").fetchone()
        self.assertEqual(page["content"], "内容 🎨")

        stat = conn.execute(
            "SELECT * FROM daily_stats WHERE `date`='2024-01-01'"
        ).fetchone()
        self.assertEqual(
            (stat["upload_count"], stat["approved_count"],
             stat["like_count"], stat["view_count"]),
            (3, 1, 2, 5),
        )

        # 点赞关联关系保留
        likes = conn.execute(
            "SELECT id, image_id FROM likes ORDER BY id"
        ).fetchall()
        self.assertEqual([(r["id"], r["image_id"]) for r in likes], [(10, 1), (11, 1)])
        joined = conn.execute(
            "SELECT COUNT(*) FROM likes l INNER JOIN images i ON i.id = l.image_id"
        ).fetchone()[0]
        self.assertEqual(joined, 2)

        # 结构版本历史完整迁移
        versions = [
            r["version"] for r in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        self.assertEqual(versions, [1, 2, 3])

        # 自增序列已恢复：迁移后新插入记录获得更大的 ID
        new_id = models.insert_image(
            "new.webp", "new_t.webp", 100, 80, 10, 10, "描述", "李四",
            "c" * 32, "3.3.3.3", "ua", "uh-new", "广东", "广州",
            0, "新图片", "cn",
        )
        self.assertGreater(new_id, 2)

    def test_rejects_non_empty_target_without_changing_data(self):
        self._build_source()
        digest_before = sha256_of(self._sqlite_path)

        first = self._run_migrator()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)

        self._refresh_db()
        snapshot = self._business_snapshot()

        # 再次迁移：目标库已有业务数据，必须被拒绝
        second = self._run_migrator()
        self.assertNotEqual(second.returncode, 0, second.stdout)
        self.assertIn("存在业务数据", second.stdout)

        self._refresh_db()
        self.assertEqual(self._business_snapshot(), snapshot, "被拒绝的迁移改动了目标库数据")
        self.assertEqual(sha256_of(self._sqlite_path), digest_before)

    def test_failure_does_not_damage_source_or_target(self):
        # 源库 images.ip 长度为 100，超过目标 VARCHAR(64)：长度预检应中止迁移
        self._build_source(long_ip=True)
        digest_before = sha256_of(self._sqlite_path)

        result = self._run_migrator()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("超出目标字段长度", result.stdout)

        self._refresh_db()
        for table in MIGRATION_TABLES:
            self.assertEqual(
                self._count(table), 0,
                f"迁移失败后目标表 {table} 仍被写入了数据",
            )
        self.assertEqual(sha256_of(self._sqlite_path), digest_before)


if __name__ == "__main__":
    unittest.main()
