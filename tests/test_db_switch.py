"""数据库切换测试：迁移执行器、切换锁、.env 原子写入与后台三次确认流程。

运行方式：DB_TYPE=sqlite 时执行
    python -m unittest tests.test_db_switch -v
非 SQLite 后端时全部跳过（避免在 MySQL 模式下改动真实配置）。
"""
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

import app.config as config
import app.database as db
import app.db_migration as migration
import app.models as models
from installer.env_io import read_env_file, update_env_file
from tests.sqlite_test_utils import create_test_database, drop_test_database, require_sqlite


class EnvFileTest(unittest.TestCase):
    def test_update_env_preserves_comments_and_backup(self):
        tmpdir = tempfile.mkdtemp(prefix="amig_env_test_")
        path = os.path.join(tmpdir, ".env")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# 注释保留\nDB_TYPE=sqlite\nPORT=8000\n")
        update_env_file(path, {"PORT": "9000", "WORKERS": "1"})
        text = Path(path).read_text(encoding="utf-8")
        self.assertIn("# 注释保留", text)
        self.assertIn("PORT=9000", text)
        self.assertIn("WORKERS=1", text)
        self.assertTrue(os.path.exists(path + ".bak"))
        env = read_env_file(path)
        self.assertEqual(env["PORT"], "9000")
        self.assertEqual(env["DB_TYPE"], "sqlite")

    def test_update_env_quotes_special_values(self):
        tmpdir = tempfile.mkdtemp(prefix="amig_env_test_")
        path = os.path.join(tmpdir, ".env")
        update_env_file(path, {"MYSQL_PASSWORD": 'p@ss "word" #1'})
        self.assertEqual(read_env_file(path)["MYSQL_PASSWORD"], 'p@ss "word" #1')


class MigrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_sqlite()
        cls.tmpdir = create_test_database()
        cls.backup_dir = os.path.join(cls.tmpdir, "backups")
        os.makedirs(cls.backup_dir, exist_ok=True)
        cls._old_backup_dir = config.BACKUP_DIR
        config.BACKUP_DIR = cls.backup_dir

    @classmethod
    def tearDownClass(cls):
        config.BACKUP_DIR = cls._old_backup_dir
        drop_test_database(cls.tmpdir)

    def setUp(self):
        db.get_db().execute("PRAGMA foreign_keys=OFF")
        for table in migration.TABLE_ORDER:
            if table == "schema_migrations":
                continue
            db.get_db().execute(f"DELETE FROM `{table}`")
        db.get_db().execute("PRAGMA foreign_keys=ON")
        db.get_db().commit()

    def _seed(self):
        image_id = models.insert_image(
            "f.webp", "t.webp", 10, 9, 5, 5, "描述", "作者", "md5",
            "1.1.1.1", "UA", models.ua_hash("UA"), "省", "市",
        )
        models.add_like_atomic(image_id, "2.2.2.2", "UA", "省", "市")
        models.set_setting("site_name", "迁移测试")
        return image_id

    def test_sqlite_to_sqlite_roundtrip(self):
        image_id = self._seed()
        src = {"db_type": "sqlite", "database_path": db.DATABASE_PATH}
        target_path = os.path.join(self.tmpdir, "target.db")
        report = migration.migrate(src, {"db_type": "sqlite", "database_path": target_path})
        self.assertTrue(report["verified"])
        self.assertEqual(report["tables"]["images"], 1)
        self.assertEqual(report["tables"]["likes"], 1)
        conn = sqlite3.connect(target_path)
        try:
            row = conn.execute(
                "SELECT description, like_count FROM images WHERE id=?", (image_id,)
            ).fetchone()
            self.assertEqual(row[0], "描述")
            self.assertEqual(row[1], 1)
            value = conn.execute(
                "SELECT value FROM settings WHERE `key`='site_name'"
            ).fetchone()[0]
            self.assertEqual(value, "迁移测试")
        finally:
            conn.close()

    def test_refuses_non_empty_target(self):
        self._seed()
        src = {"db_type": "sqlite", "database_path": db.DATABASE_PATH}
        with self.assertRaises(migration.MigrationError):
            migration.migrate(src, {"db_type": "sqlite", "database_path": db.DATABASE_PATH})

    def test_backup_and_lock(self):
        backup = migration.backup_source(
            {"db_type": "sqlite", "database_path": db.DATABASE_PATH}
        )
        self.assertTrue(os.path.exists(backup))
        self.assertTrue(migration.acquire_switch_lock())
        self.assertTrue(migration.switch_in_progress())
        self.assertFalse(migration.acquire_switch_lock())
        migration.release_switch_lock()
        self.assertFalse(migration.switch_in_progress())

    def test_env_updates_for_target(self):
        updates = migration.env_updates_for_target(
            {"db_type": "sqlite", "database_path": os.path.join(self.tmpdir, "x.db")}
        )
        self.assertEqual(updates["DB_TYPE"], "sqlite")
        self.assertIn("DATABASE_PATH", updates)


class AdminSwitchRoutesTest(unittest.TestCase):
    """后台「切换数据库」路由：鉴权、CSRF、三次确认与成功流程。"""

    @classmethod
    def setUpClass(cls):
        require_sqlite()
        cls.tmpdir = create_test_database()
        cls.app_env = os.path.join(cls.tmpdir, ".env")
        cls._old_backup_dir = config.BACKUP_DIR
        config.BACKUP_DIR = os.path.join(cls.tmpdir, "backups")
        os.makedirs(config.BACKUP_DIR, exist_ok=True)

        models.set_setting("admin_domain", "testserver")
        admin_id = db.get_db().execute(
            "INSERT INTO admins (username, password_hash, created_at) VALUES (?,?,?)",
            ("admin", "hash", models.now_iso()),
        ).lastrowid
        db.get_db().commit()
        cls.admin_id = admin_id

        import app.auth as auth
        cls.session_id = auth.create_session(admin_id, "admin")
        cls.session = auth.get_session(cls.session_id)

        from fastapi.testclient import TestClient
        from app.main import app as fastapi_app
        cls.client = TestClient(fastapi_app)
        cls.client.cookies.set("admin_session", cls.session_id)

    @classmethod
    def tearDownClass(cls):
        config.BACKUP_DIR = cls._old_backup_dir
        drop_test_database(cls.tmpdir)

    def _post(self, **overrides):
        data = {
            "csrf_token": self.session["csrf_token"],
            "target_type": "sqlite",
            "sqlite_path": os.path.join(self.tmpdir, "switch_target.db"),
            "confirm_read": "on",
            "confirm_backup": "on",
            "confirm_word": "切换",
        }
        data.update(overrides)
        return self.client.post(
            "/admin/settings/database/switch", data=data, follow_redirects=False
        )

    def test_requires_admin_login(self):
        from fastapi.testclient import TestClient
        from app.main import app as fastapi_app
        anon = TestClient(fastapi_app)
        for path in ("/admin/settings/database", "/admin/settings/database/progress"):
            resp = anon.get(path, follow_redirects=False)
            self.assertEqual(resp.status_code, 302, path)

    def test_rejects_missing_csrf(self):
        resp = self._post(csrf_token="wrong-token")
        self.assertEqual(resp.status_code, 302)

    def test_requires_all_three_confirmations(self):
        for overrides in (
            {"confirm_read": ""},
            {"confirm_backup": ""},
            {"confirm_word": "错误确认词"},
        ):
            resp = self._post(**overrides)
            self.assertEqual(resp.status_code, 200)
            self.assertIn("确认", resp.text)
        from app.routes import admin as admin_module
        self.assertNotEqual(admin_module._switch_snapshot()["status"], "running")

    def test_switch_success_updates_env(self):
        # 让切换流程写到临时目录，避免污染项目 .env
        from app.routes import admin as admin_module
        old_base = admin_module.BASE_DIR
        admin_module.BASE_DIR = Path(self.tmpdir)
        # 模拟当前配置：位于临时目录的 .env
        update_env_file(self.app_env, {
            "DB_TYPE": "sqlite",
            "DATABASE_PATH": db.DATABASE_PATH,
            "PORT": "18082",
        })
        try:
            resp = self._post()
            self.assertEqual(resp.status_code, 302)
            deadline = time.monotonic() + 60
            snap = admin_module._switch_snapshot()
            while snap["status"] == "running" and time.monotonic() < deadline:
                time.sleep(0.3)
                snap = admin_module._switch_snapshot()
            self.assertEqual(snap["status"], "success", snap.get("error"))
            env = read_env_file(self.app_env)
            self.assertEqual(env["DB_TYPE"], "sqlite")
            self.assertEqual(
                os.path.abspath(env["DATABASE_PATH"]),
                os.path.abspath(os.path.join(self.tmpdir, "switch_target.db")),
            )
            self.assertIn("backup_path", snap["report"])
        finally:
            admin_module.BASE_DIR = old_base


if __name__ == "__main__":
    unittest.main()
