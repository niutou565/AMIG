"""第四阶段：SQLite 连接管理、WAL、事务与并发一致性测试。

这些测试全部使用临时数据库，不接触 data/app.db。
"""
import multiprocessing
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone

import app.database as db
import app.models as models


def _multiprocess_worker(db_path, image_id, index, queue):
    """独立进程模拟一个 uvicorn worker：各自建连、并发写同一 SQLite 库。"""
    import app.database as database
    import app.models as models_module

    database.DATABASE_PATH = db_path
    try:
        database.close_all_db()
        database.init_db(max_wait_seconds=60)
        views_new = 0
        conn = database.get_db()
        for i in range(20):
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)",
                (f"mp_{index}_{i}", "1"),
            )
            conn.commit()
            if models_module.should_count_view(image_id, "10.9.9.9"):
                views_new += 1
                models_module.increment_view(image_id)
        like_ok = models_module.add_like_atomic(
            image_id, "10.9.9.8", "UA-MP", "省", "市"
        )
        queue.put({"index": index, "views_new": views_new,
                   "like_ok": like_ok, "error": None})
    except Exception as exc:  # 把异常带回父进程断言
        queue.put({"index": index, "views_new": 0, "like_ok": None,
                   "error": f"{type(exc).__name__}: {exc}"})
    finally:
        database.close_all_db()


def _now_iso(offset_seconds=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat(
        timespec="seconds"
    )


class TempDbCase(unittest.TestCase):
    """每个测试类一份独立临时库。"""

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.mkdtemp(prefix="amig_stage4_")
        cls._orig_path = db.DATABASE_PATH
        cls._orig_backup = db.BACKUP_DIR
        # 先关闭其他测试遗留的连接，避免连接仍指向旧路径而绕过临时库
        db.close_all_db()
        db.DATABASE_PATH = os.path.join(cls._tmpdir, "stage4.db")
        db.BACKUP_DIR = os.path.join(cls._tmpdir, "backups")
        db.init_db()

    @classmethod
    def tearDownClass(cls):
        db.close_all_db()
        db.DATABASE_PATH = cls._orig_path
        db.BACKUP_DIR = cls._orig_backup
        shutil.rmtree(cls._tmpdir, ignore_errors=True)

    def _insert_image(self, status="pending", ip="1.1.1.1", like_count=0,
                      created_at=None, md5=None):
        conn = db.get_db()
        conn.execute(
            "INSERT INTO images (filename, thumb_filename, original_size,"
            " compressed_size, width, height, description, title, author,"
            " content_md5, ip, ua, ua_hash, province, city, region, status,"
            " reject_reason, admin_edited, created_at, reviewed_at, views,"
            " like_count, is_duplicate)"
            " VALUES (?,?,1,1,10,10,'','t','a',?,?,'','uh','p','c','',?,NULL,0,?,?,0,?,0)",
            (f"f{os.urandom(4).hex()}.webp", f"t{os.urandom(4).hex()}.webp",
             md5 or os.urandom(8).hex(), ip, status,
             created_at or _now_iso(), _now_iso() if status == "approved" else None,
             like_count),
        )
        conn.commit()
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


class TestConnectionManagement(TempDbCase):
    def test_connection_is_thread_local(self):
        """不同线程必须拿到不同连接，绝不能跨线程共享。"""
        main_conn = db.get_db()
        captured = {}

        def worker():
            captured["conn"] = db.get_db()
            captured["same"] = captured["conn"] is main_conn
            db.close_db()

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        self.assertFalse(captured["same"], "子线程复用了主线程的连接")
        self.assertIs(db.get_db(), main_conn, "同线程内应复用同一连接")

    def test_close_all_db_closes_pooled_connections(self):
        """close_all_db 必须关闭线程池里所有连接（否则 Windows 下无法替换数据库文件）。"""
        collected = []

        def worker():
            conn = db.get_db()
            conn.execute("SELECT 1")
            collected.append(conn)

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(collected), 3)

        db.close_all_db()
        for conn in collected:
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")

    def test_get_db_recovers_after_close_all(self):
        """连接被统一关闭后，get_db 应自动重建而不是抛错。"""
        db.get_db().execute("SELECT 1")
        db.close_all_db()
        conn = db.get_db()
        self.assertEqual(conn.execute("SELECT 1").fetchone()[0], 1)

    def test_pragmas_applied(self):
        """WAL / busy_timeout / foreign_keys 等关键 PRAGMA 必须生效。"""
        status = db.get_pragma_status()
        self.assertEqual(str(status["journal_mode"]).lower(), "wal")
        self.assertEqual(status["foreign_keys"], 1)
        self.assertGreaterEqual(status["busy_timeout"], 1000)
        # synchronous=FULL 为 2；不允许为了写入速度静默降级
        self.assertEqual(status["synchronous"], 2)


class TestWalAndCheckpoint(TempDbCase):
    def test_wal_file_created_and_checkpoint_truncates(self):
        """提交后应产生 -wal 文件；TRUNCATE checkpoint 应把它清空。"""
        self._insert_image()
        wal_path = str(db.DATABASE_PATH) + "-wal"
        self.assertTrue(os.path.exists(wal_path), "未生成 WAL 文件")
        self.assertGreater(os.path.getsize(wal_path), 0)

        result = db.checkpoint_wal("TRUNCATE")
        self.assertEqual(result["busy"], 0)
        self.assertEqual(os.path.getsize(wal_path), 0, "TRUNCATE 后 WAL 未清空")

    def test_checkpoint_rejects_unknown_mode(self):
        for mode in ("BOGUS", "truncate; DROP TABLE images", ""):
            with self.assertRaises(ValueError):
                db.checkpoint_wal(mode)

    def test_readers_not_blocked_by_writer(self):
        """WAL 模式下写事务不应阻塞另一个连接的读取。"""
        image_id = self._insert_image(status="approved")
        reader = sqlite3.connect(str(db.DATABASE_PATH), timeout=2)
        try:
            writer = db.get_db()
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("UPDATE images SET views=views+1 WHERE id=?", (image_id,))
            # 写锁持有期间，另一连接仍应能读到旧值而不是等待
            rows = reader.execute("SELECT COUNT(*) FROM images").fetchone()
            self.assertGreaterEqual(rows[0], 1)
            writer.commit()
        finally:
            reader.close()


class TestTransactionContext(TempDbCase):
    def test_commit_on_success(self):
        with db.transaction() as conn:
            conn.execute("INSERT INTO settings (key, value) VALUES ('tx_ok', '1')")
        row = db.get_db().execute(
            "SELECT value FROM settings WHERE key='tx_ok'").fetchone()
        self.assertEqual(row["value"], "1")

    def test_rollback_on_exception(self):
        with self.assertRaises(RuntimeError):
            with db.transaction() as conn:
                conn.execute("INSERT INTO settings (key, value) VALUES ('tx_bad', '1')")
                raise RuntimeError("boom")
        row = db.get_db().execute(
            "SELECT value FROM settings WHERE key='tx_bad'").fetchone()
        self.assertIsNone(row, "异常后事务未回滚")

    def test_nested_savepoint_inner_rollback_keeps_outer(self):
        """内层 SAVEPOINT 回滚不应影响外层已完成的修改。"""
        with db.transaction() as conn:
            conn.execute("INSERT INTO settings (key, value) VALUES ('outer', '1')")
            with self.assertRaises(RuntimeError):
                with db.transaction() as inner:
                    inner.execute("INSERT INTO settings (key, value) VALUES ('inner', '1')")
                    raise RuntimeError("inner boom")

        rows = {
            r["key"] for r in db.get_db().execute(
                "SELECT key FROM settings WHERE key IN ('outer', 'inner')").fetchall()
        }
        self.assertEqual(rows, {"outer"})


class TestViewDedup(TempDbCase):
    def test_should_count_view_returns_true_then_false(self):
        image_id = self._insert_image(status="approved")
        self.assertTrue(models.should_count_view(image_id, "9.9.9.1"))
        self.assertFalse(models.should_count_view(image_id, "9.9.9.1"),
                         "去重窗口内重复浏览被重复计数")

    def test_view_dedup_allows_new_hit_after_window(self):
        image_id = self._insert_image(status="approved")
        self.assertTrue(models.should_count_view(image_id, "9.9.9.2"))
        conn = db.get_db()
        # 把记录时间推到去重窗口之外
        conn.execute(
            "UPDATE view_sessions SET viewed_at=? WHERE image_id=? AND ip=?",
            (_now_iso(-models.VIEW_DEDUP_WINDOW - 60), image_id, "9.9.9.2"),
        )
        conn.commit()
        self.assertTrue(models.should_count_view(image_id, "9.9.9.2"))

    def test_unique_index_exists(self):
        names = {
            r["name"] for r in db.get_db().execute(
                "SELECT name FROM sqlite_master WHERE type='index'").fetchall()
        }
        self.assertIn("idx_view_sessions_unique", names)


class TestLikeConcurrency(TempDbCase):
    def test_concurrent_likes_are_deduplicated(self):
        """同一 IP + UA 的并发点赞只能成功一次。"""
        image_id = self._insert_image(status="approved")
        results = []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            ok = models.insert_like(image_id, "8.8.8.8", "Mozilla/5.0", "省", "市")
            results.append(ok)
            db.close_db()

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(results.count(True), 1, f"重复点赞未被去重: {results}")
        count = db.get_db().execute(
            "SELECT COUNT(*) FROM likes WHERE image_id=?", (image_id,)).fetchone()[0]
        self.assertEqual(count, 1)

    def test_multi_process_writes_have_no_lock_errors(self):
        """4 个独立进程同时读写：无锁错误、无丢失更新、计数与记录一致。"""
        image_id = self._insert_image(status="approved")
        ctx = multiprocessing.get_context("spawn")
        queue = ctx.Queue()
        procs = [
            ctx.Process(
                target=_multiprocess_worker,
                args=(str(db.DATABASE_PATH), image_id, index, queue),
            )
            for index in range(4)
        ]
        for proc in procs:
            proc.start()
        results = []
        try:
            for _ in procs:
                results.append(queue.get(timeout=120))
        finally:
            for proc in procs:
                proc.join(timeout=120)

        errors = [r["error"] for r in results if r["error"]]
        self.assertEqual(errors, [], f"多进程写入出现错误: {errors}")
        self.assertEqual(
            sum(1 for r in results if r["like_ok"]), 1,
            f"跨进程点赞未被去重: {results}",
        )
        self.assertEqual(
            sum(r["views_new"] for r in results), 1,
            "跨进程浏览去重失效，同一 IP 被重复计数",
        )

        conn = db.get_db()
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM settings WHERE key LIKE 'mp_%'"
            ).fetchone()[0],
            80,
            "并发提交的写入出现丢失",
        )
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM likes WHERE image_id=?", (image_id,)
            ).fetchone()[0],
            1,
        )
        row = conn.execute(
            "SELECT like_count, views FROM images WHERE id=?", (image_id,)
        ).fetchone()
        self.assertEqual((row["like_count"], row["views"]), (1, 1))
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_failed_like_does_not_leave_write_lock(self):
        """唯一索引冲突后必须回滚，否则线程连接长期持有写锁。"""
        image_id = self._insert_image(status="approved")
        self.assertTrue(models.insert_like(image_id, "7.7.7.7", "UA", "省", "市"))
        self.assertFalse(models.insert_like(image_id, "7.7.7.7", "UA", "省", "市"))

        conn = db.get_db()
        self.assertFalse(conn.in_transaction,
                         "点赞去重失败后连接仍处于未结束事务，会长期持有写锁")
        # 另一连接应能立即写入，证明写锁已释放
        other = sqlite3.connect(str(db.DATABASE_PATH), timeout=2)
        try:
            other.execute("INSERT INTO settings (key, value) VALUES ('lock_free', '1')")
            other.commit()
        finally:
            other.close()

    def test_batch_like_delete_updates_count_in_one_transaction(self):
        image_id = self._insert_image(status="approved", like_count=2)
        conn = db.get_db()
        ids = []
        for i in range(2):
            cursor = conn.execute(
                "INSERT INTO likes (image_id, ip, ua_hash, province, city, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (image_id, f"6.6.6.{i}", f"ua{i}", "省", "市", _now_iso()),
            )
            ids.append(cursor.lastrowid)
        conn.commit()

        models.delete_likes_by_ids(ids)
        row = conn.execute(
            "SELECT like_count FROM images WHERE id=?", (image_id,)
        ).fetchone()
        self.assertEqual(row["like_count"], 0)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM likes WHERE image_id=?",
                         (image_id,)).fetchone()[0],
            0,
        )
        # 重复删除同一批 ID 不应产生负计数或异常
        models.delete_likes_by_ids(ids)
        row = conn.execute(
            "SELECT like_count FROM images WHERE id=?", (image_id,)
        ).fetchone()
        self.assertEqual(row["like_count"], 0)


class TestPaginationClamp(TempDbCase):
    def test_negative_page_offset_is_clamped(self):
        """page=0/-5 不得产生负 OFFSET 或异常。"""
        for _ in range(3):
            self._insert_image(status="approved")
        rows = models.get_images_by_status("approved", limit=2, offset=-10)
        self.assertEqual(len(rows), 2)

    def test_limit_is_clamped_to_maximum(self):
        for _ in range(3):
            self._insert_image(status="approved")
        rows = models.get_images_by_status("approved", limit=100000, offset=0)
        # 夹取到上限后仍是合法查询，不会一次拉全表以外的内容
        self.assertLessEqual(len(rows), models._MAX_PAGE_LIMIT)

    def test_invalid_pagination_values_fall_back(self):
        for _ in range(2):
            self._insert_image(status="approved")
        rows = models.get_images_by_status("approved", limit="abc", offset=None)
        self.assertIsInstance(rows, list)


class TestSchemaMigration(TempDbCase):
    def test_schema_version_recorded(self):
        versions = {
            r["version"] for r in db.get_db().execute(
                "SELECT version FROM schema_migrations").fetchall()
        }
        self.assertIn(db.SCHEMA_VERSION, versions)

    def test_init_db_is_idempotent(self):
        db.init_db()
        db.init_db()
        versions = [r["version"] for r in db.get_db().execute(
            "SELECT version FROM schema_migrations").fetchall()]
        self.assertEqual(len(versions), len(set(versions)), "版本表出现重复记录")

    def test_stage4_indexes_exist(self):
        names = {
            r["name"] for r in db.get_db().execute(
                "SELECT name FROM sqlite_master WHERE type='index'").fetchall()
        }
        for idx in ("idx_images_status_like", "idx_admin_logs_action_created",
                    "idx_view_sessions_unique"):
            self.assertIn(idx, names, f"缺少第四阶段索引 {idx}")

    def test_migration_from_v4_database(self):
        """模拟第三阶段的旧库（无 v5/v6 结构），验证迁移可正常补齐。"""
        legacy_dir = tempfile.mkdtemp(prefix="amig_legacy_")
        legacy_path = os.path.join(legacy_dir, "legacy.db")
        orig = db.DATABASE_PATH
        try:
            db.close_all_db()
            db.DATABASE_PATH = legacy_path
            db.init_db()
            conn = db.get_db()
            # 退化到"旧库"：去掉新索引与版本记录，并写入重复浏览记录
            conn.execute("DROP INDEX IF EXISTS idx_images_status_like")
            conn.execute("DROP INDEX IF EXISTS idx_admin_logs_action_created")
            conn.execute("DROP INDEX IF EXISTS idx_view_sessions_unique")
            conn.execute("DELETE FROM schema_migrations WHERE version >= 5")
            conn.execute(
                "INSERT INTO view_sessions (image_id, ip, viewed_at) VALUES "
                "(1, '1.1.1.1', ?), (1, '1.1.1.1', ?)", (_now_iso(-100), _now_iso()))
            conn.commit()

            db.close_all_db()
            db.init_db()

            conn = db.get_db()
            names = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
            self.assertIn("idx_images_status_like", names)
            self.assertIn("idx_admin_logs_action_created", names)
            self.assertIn("idx_view_sessions_unique", names)
            dup = conn.execute(
                "SELECT COUNT(*) FROM view_sessions WHERE image_id=1 AND ip='1.1.1.1'"
            ).fetchone()[0]
            self.assertEqual(dup, 1, "迁移未清理 view_sessions 重复行")
        finally:
            db.close_all_db()
            db.DATABASE_PATH = orig
            shutil.rmtree(legacy_dir, ignore_errors=True)


class TestDeleteSemantics(TempDbCase):
    def test_delete_image_returns_row_then_none(self):
        image_id = self._insert_image(status="pending")
        image = models.get_image_by_id(image_id)
        first = models.delete_image(image_id)
        self.assertIsNotNone(first, "首次删除应返回被删除的文件名信息")
        self.assertEqual(first["filename"], image["filename"])
        self.assertEqual(first["thumb_filename"], image["thumb_filename"])
        self.assertIsNone(models.delete_image(image_id),
                          "重复删除应返回 None 供并发分支判断")

    def test_try_approve_is_conditional(self):
        """并发审核只能有一个成功，重复通过不产生第二条日志。"""
        image_id = self._insert_image(status="pending")
        self.assertTrue(models.try_approve_image(image_id, admin_id=1, admin_name="a"))
        self.assertFalse(models.try_approve_image(image_id, admin_id=1, admin_name="a"))
        logs = db.get_db().execute(
            "SELECT COUNT(*) FROM admin_logs WHERE action='approve'").fetchone()[0]
        self.assertEqual(logs, 1, "重复审核写入了多条日志")


if __name__ == "__main__":
    unittest.main()
