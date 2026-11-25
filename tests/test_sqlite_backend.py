"""SQLite 后端专项测试：建表、方言 UPSERT、事务、命名锁与并发去重。

运行方式：设置环境变量 DB_TYPE=sqlite 后执行
    python -m unittest tests.test_sqlite_backend -v
未使用 SQLite 后端时全部用例明确跳过。
"""
import threading
import unittest
from datetime import datetime, timedelta, timezone

import app.auth as auth
import app.database as db
import app.models as models
from tests.sqlite_test_utils import (
    clean_tables, create_test_database, drop_test_database, require_sqlite,
)


def _now_iso(offset_seconds=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat(
        timespec="seconds"
    )


class SqliteCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_sqlite()
        cls.tmpdir = create_test_database()

    @classmethod
    def tearDownClass(cls):
        drop_test_database(cls.tmpdir)

    def setUp(self):
        clean_tables()

    def _insert_image(self, status="pending", ip="1.1.1.1", like_count=0,
                      created_at=None):
        conn = db.get_db()
        cursor = conn.execute(
            "INSERT INTO images (filename, thumb_filename, original_size,"
            " compressed_size, width, height, description, title, author,"
            " content_md5, ip, ua, ua_hash, province, city, region, status,"
            " created_at, reviewed_at, views, like_count, is_duplicate)"
            " VALUES (?,?,1,1,10,10,'','t','a',?,?,'','uh','p','c','',?,?,?,0,?,0)",
            (f"f{created_at or 'x'}.webp", "t.webp", "md5-" + ip if ip else "md5",
             ip, status, created_at or _now_iso(),
             _now_iso() if status == "approved" else None, like_count),
        )
        conn.commit()
        return cursor.lastrowid


class TestSchema(SqliteCase):
    def test_expected_tables_created(self):
        names = {
            r["name"] for r in db.get_db().execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        self.assertTrue(set(db.EXPECTED_TABLES).issubset(names))

    def test_init_db_is_idempotent_and_keeps_data(self):
        image_id = self._insert_image(status="approved")
        db.init_db()
        db.init_db()
        self.assertIsNotNone(models.get_image_by_id(image_id))
        versions = [r["version"] for r in db.get_db().execute(
            "SELECT version FROM schema_migrations").fetchall()]
        self.assertEqual(len(versions), len(set(versions)))
        self.assertIn(db.SCHEMA_VERSION, versions)

    def test_unique_view_session_index_enforced(self):
        image_id = self._insert_image(status="approved")
        conn = db.get_db()
        conn.execute(
            "INSERT INTO view_sessions (image_id, ip, viewed_at) VALUES (?,?,?)",
            (image_id, "5.5.5.5", _now_iso()),
        )
        with self.assertRaises(db.IntegrityError):
            conn.execute(
                "INSERT INTO view_sessions (image_id, ip, viewed_at) VALUES (?,?,?)",
                (image_id, "5.5.5.5", _now_iso()),
            )


class TestDialectUpsert(SqliteCase):
    def test_settings_upsert(self):
        models.set_setting("site_name", "第一次")
        models.set_setting("site_name", "第二次")
        self.assertEqual(models.get_setting("site_name"), "第二次")

    def test_daily_stats_upsert(self):
        models.ensure_daily_stats("2026-01-01")
        models.ensure_daily_stats("2026-01-01")
        models.increment_daily_stat("2026-01-01", "like_count")
        models.increment_daily_stat("2026-01-01", "like_count")
        row = models.get_daily_stats("2026-01-01")
        self.assertEqual(row["like_count"], 2)

    def test_view_dedup_true_then_false(self):
        image_id = self._insert_image(status="approved")
        self.assertTrue(models.should_count_view(image_id, "9.9.9.1"))
        self.assertFalse(models.should_count_view(image_id, "9.9.9.1"))

    def test_view_dedup_allows_new_hit_after_window(self):
        image_id = self._insert_image(status="approved")
        self.assertTrue(models.should_count_view(image_id, "9.9.9.2"))
        db.get_db().execute(
            "UPDATE view_sessions SET viewed_at=? WHERE image_id=? AND ip=?",
            (_now_iso(-models.VIEW_DEDUP_WINDOW - 60), image_id, "9.9.9.2"),
        )
        db.get_db().commit()
        self.assertTrue(models.should_count_view(image_id, "9.9.9.2"))

    def test_cooling_multiplier_capped(self):
        for _ in range(10):
            models.increment_reject_cooldown("2.2.2.2")
        self.assertEqual(models.get_cooldown_multiplier("2.2.2.2"), 16)
        row = db.get_db().execute(
            "SELECT reject_count FROM cooling WHERE ip=?", ("2.2.2.2",)
        ).fetchone()
        self.assertEqual(row["reject_count"], 10)

    def test_login_rate_limit_accumulates_and_resets(self):
        for expected in range(1, 4):
            auth.record_login_failure("3.3.3.3")
            row = db.get_db().execute(
                "SELECT attempts FROM login_rate_limits WHERE `key` LIKE '%3.3.3.3%'"
            ).fetchone()
            self.assertEqual(row["attempts"], expected)
        auth.reset_login_ip_limit("3.3.3.3")
        row = db.get_db().execute(
            "SELECT attempts FROM login_rate_limits WHERE `key` LIKE '%3.3.3.3%'"
        ).fetchone()
        self.assertIsNone(row)


class TestTransactions(SqliteCase):
    def test_commit_on_success(self):
        with db.transaction() as conn:
            conn.execute("INSERT INTO settings (`key`, value) VALUES ('tx_ok', '1')")
        row = db.get_db().execute(
            "SELECT value FROM settings WHERE `key`='tx_ok'").fetchone()
        self.assertEqual(row["value"], "1")

    def test_rollback_on_exception(self):
        with self.assertRaises(RuntimeError):
            with db.transaction() as conn:
                conn.execute("INSERT INTO settings (`key`, value) VALUES ('tx_bad', '1')")
                raise RuntimeError("boom")
        self.assertIsNone(db.get_db().execute(
            "SELECT value FROM settings WHERE `key`='tx_bad'").fetchone())

    def test_nested_savepoint_rollback_keeps_outer(self):
        with db.transaction() as conn:
            conn.execute("INSERT INTO settings (`key`, value) VALUES ('outer', '1')")
            with self.assertRaises(RuntimeError):
                with db.transaction() as inner:
                    inner.execute("INSERT INTO settings (`key`, value) VALUES ('inner', '1')")
                    raise RuntimeError("inner boom")
        rows = {
            r["key"] for r in db.get_db().execute(
                "SELECT `key` FROM settings WHERE `key` IN ('outer', 'inner')").fetchall()
        }
        self.assertEqual(rows, {"outer"})


class TestNamedLock(SqliteCase):
    def test_lock_is_mutually_exclusive_across_threads(self):
        conn = db.get_db()
        self.assertTrue(db.acquire_named_lock(conn, "amig_upload_quota", 1))
        got = {}

        def worker():
            other = db.get_db()
            got["locked"] = db.acquire_named_lock(other, "amig_upload_quota", 0)
            if got["locked"]:
                db.release_named_lock(other, "amig_upload_quota")
            db.close_db()

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=10)
        self.assertFalse(got.get("locked"), "命名锁未互斥")
        db.release_named_lock(conn, "amig_upload_quota")
        self.assertTrue(db.acquire_named_lock(conn, "amig_upload_quota", 1))
        db.release_named_lock(conn, "amig_upload_quota")


class TestConcurrency(SqliteCase):
    def test_concurrent_likes_are_deduplicated(self):
        image_id = self._insert_image(status="approved")
        results = []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            results.append(models.insert_like(image_id, "8.8.8.8", "UA", "省", "市"))
            db.close_db()

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(results.count(True), 1, f"重复点赞未被去重: {results}")
        self.assertEqual(
            db.get_db().execute(
                "SELECT COUNT(*) FROM likes WHERE image_id=?", (image_id,)
            ).fetchone()[0],
            1,
        )

    def test_connection_is_thread_local(self):
        main_conn = db.get_db()
        captured = {}

        def worker():
            captured["same"] = db.get_db() is main_conn
            db.close_db()

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        self.assertFalse(captured["same"])


if __name__ == "__main__":
    unittest.main()
