"""P1-1 回归：数据库切换期间的全量业务写入守卫（SQLite / MySQL 双后端）。

覆盖：
- 点赞、浏览量、审核、封禁、设置、操作日志、日统计、冷却等写入全部被拦截；
- 读操作不受影响；
- 「切换成功、等待重启」状态同样拦截写入；
- 状态文件过期自愈、负缓存只作用于「未切换」结果；
- HTTP 层点赞返回 503 而页面读取仍为 200；
- 8 线程并发写入与切换同时发生时不产生部分修改；
- 切换锁互斥与释放；
- 切换失败（目标库非空 / 写 .env 异常）不留下切换状态，旧库继续可写；
- 切换成功后旧库写入被拒、目标库数据与切换审计日志完整。

任何写入断言都检查数据库最终状态，而不只是异常/状态码。
"""
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

import app.config as config
import app.database as db
import app.db_migration as migration
import app.models as models

if db.IS_SQLITE:
    from tests.sqlite_test_utils import (
        clean_tables, create_test_database, drop_test_database,
    )
else:
    from tests.mysql_test_utils import (
        clean_tables, create_test_database, drop_test_database,
    )


class DbSwitchWriteGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 独立临时测试库（SQLite 临时目录 / MySQL amig_test_ 前缀库）
        cls.db_handle = create_test_database()
        # 备份、.env、迁移目标等文件一律放在文件系统临时目录，
        # 与数据库句柄分开（MySQL 模式下句柄是库名，不是路径）
        cls.tmpdir = tempfile.mkdtemp(prefix="amig_switch_guard_")
        cls.backup_dir = os.path.join(cls.tmpdir, "backups")
        os.makedirs(cls.backup_dir, exist_ok=True)
        cls._old_backup_dir = config.BACKUP_DIR
        config.BACKUP_DIR = cls.backup_dir
        # 测试中不做跨进程传播等待（生产为 1.1 秒，见 db_migration）
        cls._old_propagation = migration._GUARD_PROPAGATION_SECONDS
        migration._GUARD_PROPAGATION_SECONDS = 0

    @classmethod
    def tearDownClass(cls):
        migration.clear_pending_restart()
        migration.release_switch_lock()
        config.BACKUP_DIR = cls._old_backup_dir
        migration._GUARD_PROPAGATION_SECONDS = cls._old_propagation
        drop_test_database(cls.db_handle)
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        # 先清切换状态再清表：守卫生效时 TRUNCATE/DELETE 会被拒绝
        migration.clear_pending_restart()
        migration.release_switch_lock()
        migration.reset_write_guard_cache()
        clean_tables()
        self._init_settings()
        self.image_id = self._approved_image("guard")

    def tearDown(self):
        migration.clear_pending_restart()
        migration.release_switch_lock()
        migration.reset_write_guard_cache()

    # ---------- 辅助 ----------

    def _init_settings(self):
        models.set_setting("admin_domain", "admin.test")
        models.set_setting("admin_domain_initialized", "1")
        models.set_setting("enable_captcha", "0")
        models.set_setting("enable_dedup", "0")

    def _approved_image(self, tag):
        suffix = f"{tag}-{time.time_ns()}"
        image_id = models.insert_image(
            f"{suffix}.webp", f"{suffix}_thumb.webp", 1000, 500, 100, 50,
            "守卫测试描述", "", f"md5-{suffix}", "1.2.3.4", f"ua-{suffix}",
            f"uah-{suffix}", "", "",
        )
        models.try_approve_image(image_id)
        return image_id

    def _scalar(self, sql, params=()):
        row = db.get_db().execute(sql, params).fetchone()
        return row[0] if row is not None else None

    def _snapshot(self):
        return {
            "likes": self._scalar("SELECT COUNT(*) FROM likes"),
            "views": self._scalar("SELECT views FROM images WHERE id=?", (self.image_id,)),
            "like_count": self._scalar(
                "SELECT like_count FROM images WHERE id=?", (self.image_id,)
            ),
            "status": self._scalar("SELECT status FROM images WHERE id=?", (self.image_id,)),
            "bans": self._scalar("SELECT COUNT(*) FROM ban_list"),
            "admin_logs": self._scalar("SELECT COUNT(*) FROM admin_logs"),
            "cooling": self._scalar("SELECT COUNT(*) FROM cooling"),
            "site_name": self._scalar("SELECT value FROM settings WHERE `key`='site_name'"),
        }

    def _business_writes(self):
        """(说明, 可调用对象)：覆盖前台与后台的全部典型写入口。"""
        return [
            ("点赞", lambda: models.add_like_atomic(
                self.image_id, "9.9.9.9", "ua-guard", "省", "市")),
            ("浏览量", lambda: models.increment_view(self.image_id)),
            ("审核", lambda: models.try_approve_image(self.image_id, 1, "admin")),
            ("封禁", lambda: models.add_ban("8.8.8.8", "ip", "测试封禁", 1)),
            ("设置", lambda: models.set_setting("site_name", "切换期写入")),
            ("操作日志", lambda: models.insert_admin_log(1, "admin", "guard_probe", "x")),
            ("日统计", lambda: models.increment_daily_stat("2026-01-01", "view_count")),
            ("拒绝冷却", lambda: models.increment_reject_cooldown("7.7.7.7")),
        ]

    def _switch_state_files(self):
        return (migration.switch_lock_path(), migration.switch_pending_restart_path())

    # ---------- 1. 切换进行中：全部业务写入被拒且无部分修改 ----------

    def test_switch_running_blocks_every_business_write(self):
        self.assertTrue(migration.acquire_switch_lock())
        before = self._snapshot()
        for name, call in self._business_writes():
            with self.subTest(write=name):
                with self.assertRaises(db.DatabaseSwitchInProgressError):
                    call()
        self.assertEqual(self._snapshot(), before, "被拒绝的写入不得产生任何修改")

        migration.release_switch_lock()
        for name, call in self._business_writes():
            with self.subTest(write=name, phase="released"):
                call()
        after = self._snapshot()
        self.assertEqual(after["likes"], before["likes"] + 1)
        self.assertEqual(after["views"], before["views"] + 1)
        self.assertEqual(after["bans"], before["bans"] + 1)
        self.assertEqual(after["admin_logs"], before["admin_logs"] + 1)
        self.assertEqual(after["cooling"], before["cooling"] + 1)
        self.assertEqual(after["site_name"], "切换期写入")

    # ---------- 2. 读操作不受影响 ----------

    def test_reads_allowed_while_switch_running(self):
        self.assertTrue(migration.acquire_switch_lock())
        self.assertEqual(self._scalar("SELECT 1"), 1)
        self.assertEqual(models.get_setting("admin_domain"), "admin.test")
        self.assertIsNotNone(models.get_approved_image_by_id(self.image_id))

    # ---------- 3. pending_restart 语义 ----------

    def test_pending_restart_blocks_writes_until_cleared(self):
        migration.mark_pending_restart()
        self.assertTrue(migration.switch_blocking_writes())
        before = self._snapshot()
        with self.assertRaises(db.DatabaseSwitchInProgressError):
            models.set_setting("site_name", "pending")
        self.assertEqual(self._snapshot(), before)

        migration.clear_pending_restart()
        self.assertFalse(migration.switch_blocking_writes())
        models.set_setting("site_name", "after-pending")
        self.assertEqual(self._scalar("SELECT value FROM settings WHERE `key`='site_name'"),
                         "after-pending")

    # ---------- 4. 状态文件过期自愈 ----------

    def test_stale_state_files_self_heal(self):
        self.assertTrue(migration.acquire_switch_lock())
        migration.mark_pending_restart()
        stale = time.time() - migration._SWITCH_LOCK_STALE_SECONDS - 60
        for path in self._switch_state_files():
            self.assertTrue(os.path.exists(path))
            os.utime(path, (stale, stale))
        migration.reset_write_guard_cache()

        self.assertFalse(migration.switch_blocking_writes())
        for path in self._switch_state_files():
            self.assertFalse(os.path.exists(path), f"过期文件应被清理：{path}")
        models.set_setting("site_name", "stale-ok")
        self.assertEqual(self._scalar("SELECT value FROM settings WHERE `key`='site_name'"),
                         "stale-ok")

    # ---------- 5. 负缓存只作用于「未切换」 ----------

    def test_guard_cache_short_circuits_only_when_free(self):
        migration.reset_write_guard_cache()
        probes = {"n": 0}
        real_exists = os.path.exists

        def counting(path):
            probes["n"] += 1
            return real_exists(path)

        with mock.patch.object(os.path, "exists", side_effect=counting):
            self.assertFalse(migration.switch_blocking_writes())
            first = probes["n"]
            self.assertGreaterEqual(first, 1)
            self.assertFalse(migration.switch_blocking_writes())
            self.assertEqual(probes["n"], first, "1 秒内重复调用应命中负缓存")

        # 创建锁后即使缓存刚命中负结果，也不会一直放行（最多 1 秒窗口）
        migration.reset_write_guard_cache()
        self.assertTrue(migration.acquire_switch_lock())
        self.assertTrue(migration.switch_blocking_writes())

    # ---------- 6. HTTP 层：写 503、读 200 ----------

    def test_read_paths_tolerate_write_guard(self):
        """图片浏览（浏览计数写库）与后台仪表盘（日统计初始化）不得因守卫 503。"""
        from fastapi.testclient import TestClient
        from app.main import app as fastapi_app
        from app import stats

        client = TestClient(fastapi_app, base_url="https://admin.test",
                            raise_server_exceptions=False)
        before = self._snapshot()
        self.assertTrue(migration.acquire_switch_lock())
        try:
            resp = client.get(f"/image/{self.image_id}")
            self.assertEqual(resp.status_code, 200, resp.text[:300])
            self.assertEqual(self._snapshot(), before, "切换期间不得写入浏览计数")
            dashboard = stats.get_dashboard_data()
            self.assertIn("total_images", dashboard)
        finally:
            migration.release_switch_lock()

    def test_http_like_returns_503_while_switch_running(self):
        from fastapi.testclient import TestClient
        from app.main import app as fastapi_app

        client = TestClient(fastapi_app, base_url="https://admin.test",
                            raise_server_exceptions=False)
        self.assertEqual(client.get(f"/image/{self.image_id}").status_code, 200)

        self.assertTrue(migration.acquire_switch_lock())
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM likes"), 0)
        resp = client.post(f"/api/like/{self.image_id}")
        self.assertEqual(resp.status_code, 503, resp.text[:300])
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM likes"), 0,
                         "503 响应不得留下任何点赞数据")
        self.assertEqual(client.get(f"/image/{self.image_id}").status_code, 200)

        migration.release_switch_lock()
        resp = client.post(f"/api/like/{self.image_id}")
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM likes"), 1)

    # ---------- 7. 并发写入与切换同时发生 ----------

    def test_concurrent_writes_rejected_without_partial_change(self):
        self.assertTrue(migration.acquire_switch_lock())
        errors = []
        lock = threading.Lock()

        def worker(index):
            try:
                models.add_like_atomic(
                    self.image_id, f"10.0.0.{index}", f"ua-{index}", "省", "市")
            except Exception as exc:  # noqa: BLE001 - 记录后统一断言
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(len(errors), 8)
        for exc in errors:
            self.assertIsInstance(exc, db.DatabaseSwitchInProgressError)
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM likes"), 0)
        self.assertEqual(self._scalar("SELECT like_count FROM images WHERE id=?",
                                      (self.image_id,)), 0)

        # 释放后并发点赞全部成功，计数准确
        migration.release_switch_lock()
        for i in range(8):
            models.add_like_atomic(
                self.image_id, f"10.0.0.{i}", f"ua-{i}", "省", "市")
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM likes"), 8)
        self.assertEqual(self._scalar("SELECT like_count FROM images WHERE id=?",
                                      (self.image_id,)), 8)

    # ---------- 8. 切换锁互斥与释放 ----------

    def test_switch_lock_is_exclusive_and_released(self):
        self.assertTrue(migration.acquire_switch_lock())
        self.assertFalse(migration.acquire_switch_lock(), "并发切换必须被拒绝")
        with self.assertRaises(db.DatabaseSwitchInProgressError):
            models.set_setting("site_name", "locked")
        migration.release_switch_lock()
        self.assertFalse(migration.switch_in_progress())
        models.set_setting("site_name", "unlocked")
        self.assertEqual(self._scalar("SELECT value FROM settings WHERE `key`='site_name'"),
                         "unlocked")

    # ---------- 9/10/11. 切换流程 ----------

    def _source_cfg(self):
        """当前测试库配置（避免误用 config 中的真实数据库配置）。"""
        if db.IS_SQLITE:
            return {"db_type": "sqlite", "database_path": db.DATABASE_PATH}
        return {
            "db_type": "mysql",
            "host": db.MYSQL_HOST,
            "port": db.MYSQL_PORT,
            "database": db.MYSQL_DATABASE,
            "user": db.MYSQL_USER,
            "password": db.MYSQL_PASSWORD,
            "charset": db.MYSQL_CHARSET,
            "collation": db.MYSQL_COLLATION,
        }

    def _write_env(self, path):
        updates = migration.env_updates_for_target(self._source_cfg())
        with open(path, "w", encoding="utf-8") as fh:
            for key, value in updates.items():
                fh.write(f"{key}={value}\n")

    def test_switch_failure_keeps_old_db_writable(self):
        """目标库非空导致迁移失败：不可残留切换状态，旧库继续可写。"""
        target_path = os.path.join(self.tmpdir, "non_empty_target.db")
        conn = sqlite3.connect(target_path)
        try:
            conn.execute("CREATE TABLE images (id INTEGER PRIMARY KEY)")
            conn.execute("INSERT INTO images (id) VALUES (1)")
            conn.commit()
        finally:
            conn.close()

        env_path = os.path.join(self.tmpdir, "failed.env")
        self._write_env(env_path)
        with open(env_path, "rb") as fh:
            env_before = fh.read()

        with self.assertRaises(migration.MigrationError):
            migration.switch_database(
                {"db_type": "sqlite", "database_path": target_path},
                env_path=env_path,
            )

        self.assertFalse(migration.switch_pending_restart())
        self.assertFalse(migration.switch_blocking_writes())
        with open(env_path, "rb") as fh:
            self.assertEqual(fh.read(), env_before, "切换失败不得修改 .env")
        models.set_setting("site_name", "after-failed-switch")
        self.assertEqual(self._scalar("SELECT value FROM settings WHERE `key`='site_name'"),
                         "after-failed-switch")

    def test_switch_exception_in_env_write_clears_pending(self):
        """切换最后一步写 .env 异常：必须清除 pending 标记并恢复写入。"""
        target_path = os.path.join(self.tmpdir, "env_fail_target.db")
        env_path = os.path.join(self.tmpdir, "env_fail.env")
        self._write_env(env_path)
        with open(env_path, "rb") as fh:
            env_before = fh.read()

        with mock.patch("installer.env_io.update_env_file", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                migration.switch_database(
                    {"db_type": "sqlite", "database_path": target_path},
                    env_path=env_path,
                )

        self.assertFalse(migration.switch_pending_restart())
        self.assertFalse(migration.switch_blocking_writes())
        with open(env_path, "rb") as fh:
            self.assertEqual(fh.read(), env_before)
        models.set_setting("site_name", "after-env-failure")
        self.assertEqual(self._scalar("SELECT value FROM settings WHERE `key`='site_name'"),
                         "after-env-failure")

    def test_switch_success_blocks_old_db_and_logs_to_target(self):
        """切换成功后：旧库写入被拒；新库数据与切换审计日志完整。"""
        target_path = os.path.join(self.tmpdir, "success_target.db")
        env_path = os.path.join(self.tmpdir, "success.env")
        self._write_env(env_path)
        source = self._source_cfg()

        with mock.patch.object(migration, "current_source", return_value=source):
            report = migration.switch_database(
                {"db_type": "sqlite", "database_path": target_path},
                env_path=env_path,
            )

        self.assertTrue(report["verified"])
        self.assertTrue(migration.switch_pending_restart(), "成功后须进入等待重启状态")

        before = self._snapshot()
        with self.assertRaises(db.DatabaseSwitchInProgressError):
            models.set_setting("site_name", "after-switch-old-db")
        self.assertEqual(self._snapshot(), before, "切换后旧库不得再接受写入")

        # 切换审计日志写入目标库
        migration.insert_admin_log_into_target(
            {"db_type": "sqlite", "database_path": target_path},
            1, "admin", "database_switch", "守卫测试",
        )

        conn = sqlite3.connect(target_path)
        try:
            images = conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
            logs = conn.execute(
                "SELECT COUNT(*) FROM admin_logs WHERE action='database_switch'"
            ).fetchone()[0]
            site_name = conn.execute(
                "SELECT value FROM settings WHERE `key`='admin_domain'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertGreaterEqual(images, 1, "切换前已确认的数据必须完整进入新库")
        self.assertGreaterEqual(logs, 1, "切换审计日志必须写入新库")
        self.assertEqual(site_name, "admin.test", "切换前已有设置必须完整进入新库")

        # 模拟重启：新进程启动后清除标记，恢复写入
        migration.clear_pending_restart()
        self.assertFalse(migration.switch_blocking_writes())


if __name__ == "__main__":
    unittest.main()
