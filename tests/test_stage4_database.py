"""第四阶段：MySQL 连接管理、事务、并发一致性与删除语义测试。

这些测试使用独立的随机 MySQL 测试库（amig_test_*），不接触开发库。
"""
import multiprocessing
import os
import threading
import unittest
from datetime import datetime, timedelta, timezone

import app.database as db
import app.models as models
from tests.mysql_test_utils import clean_tables, create_test_database, drop_test_database


def _multiprocess_worker(db_name, image_id, index, queue):
    """独立进程模拟一个 uvicorn worker：各自建连、并发写同一 MySQL 库。

    测试库名通过环境变量 MYSQL_DATABASE 传递（spawn 子进程会重新导入配置）。
    """
    os.environ["MYSQL_DATABASE"] = db_name
    import app.database as database
    import app.models as models_module

    try:
        database.init_db(max_wait_seconds=60)
        views_new = 0
        conn = database.get_db()
        for i in range(20):
            conn.execute(
                "INSERT INTO settings (`key`, value) VALUES (?, ?)",
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
    """每个测试类一份独立 MySQL 测试库。"""

    @classmethod
    def setUpClass(cls):
        cls.db_name = create_test_database()
        # 供 multiprocessing/spawn 子进程使用
        cls._orig_env_db = os.environ.get("MYSQL_DATABASE")
        os.environ["MYSQL_DATABASE"] = cls.db_name

    @classmethod
    def tearDownClass(cls):
        if cls._orig_env_db is None:
            os.environ.pop("MYSQL_DATABASE", None)
        else:
            os.environ["MYSQL_DATABASE"] = cls._orig_env_db
        drop_test_database(cls.db_name)

    def setUp(self):
        # 用例间相互独立，避免前一用例的数据影响计数类断言
        clean_tables()

    def _insert_image(self, status="pending", ip="1.1.1.1", like_count=0,
                      created_at=None, md5=None):
        conn = db.get_db()
        cursor = conn.execute(
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
        return cursor.lastrowid


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
        """close_all_db 必须关闭所有引用到的连接。"""
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
            with self.assertRaises(db.DatabaseError):
                conn.execute("SELECT 1")
        # 计数正确归零，后续仍能正常签出
        self.assertEqual(db._POOL._created, 0)
        self.assertEqual(db.get_db().execute("SELECT 1").fetchone()[0], 1)

    def test_get_db_recovers_after_close_all(self):
        """连接被统一关闭后，get_db 应自动重建而不是抛错。"""
        db.get_db().execute("SELECT 1")
        db.close_all_db()
        conn = db.get_db()
        self.assertEqual(conn.execute("SELECT 1").fetchone()[0], 1)

    def test_mysql_session_settings(self):
        """连接字符集、排序规则、自动提交与表引擎必须符合设计。"""
        status = db.get_db_status()
        self.assertEqual(status["charset"].lower(), "utf8mb4")
        self.assertTrue(status["collation"].lower().startswith("utf8mb4"))
        self.assertGreaterEqual(int(status["server_version"].split(".")[0]), 8)
        row = db.get_db().execute(
            "SELECT @@autocommit AS ac").fetchone()
        self.assertEqual(int(row["ac"]), 1, "连接必须处于自动提交模式")
        engine = db.get_db().execute(
            "SELECT ENGINE AS engine, TABLE_COLLATION AS coll FROM information_schema.tables "
            "WHERE table_schema=? AND table_name='images'",
            (db.MYSQL_DATABASE,),
        ).fetchone()
        self.assertEqual(engine["engine"], "InnoDB")
        self.assertTrue(engine["coll"].lower().startswith("utf8mb4"))


class TestConcurrencyIsolation(TempDbCase):
    def test_reader_not_blocked_by_writer_transaction(self):
        """写事务未提交时，另一连接仍应能读到旧值而不是等待锁。"""
        image_id = self._insert_image(status="approved")
        captured = {}

        def reader():
            conn = db.get_db()
            row = conn.execute(
                "SELECT views FROM images WHERE id=?", (image_id,)
            ).fetchone()
            captured["views"] = row["views"]
            db.close_db()

        writer = db.get_db()
        writer.execute("SELECT 1")
        with db.transaction() as conn:
            conn.execute("UPDATE images SET views=views+1 WHERE id=?", (image_id,))
            thread = threading.Thread(target=reader)
            thread.start()
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive(), "读请求被写事务阻塞")
        self.assertEqual(captured["views"], 0, "读到了未提交的数据（隔离级别异常）")

    def test_pool_exhaustion_raises_clear_error(self):
        """连接池上限耗尽时给出明确错误，而不是无限等待或崩溃。"""
        original_size = db.MYSQL_POOL_SIZE
        original_overflow = db.MYSQL_POOL_MAX_OVERFLOW
        original_timeout = db.MYSQL_POOL_TIMEOUT
        try:
            db._reset_pool()
            db.MYSQL_POOL_SIZE = 0
            db.MYSQL_POOL_MAX_OVERFLOW = 0
            db.MYSQL_POOL_TIMEOUT = 1
            error = {}

            def worker():
                try:
                    db.get_db()
                except db.DatabaseError as exc:
                    error["msg"] = str(exc)
                finally:
                    db.close_db()

            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(timeout=10)
            self.assertIn("连接池已耗尽", error.get("msg", ""))
        finally:
            db._reset_pool()
            db.MYSQL_POOL_SIZE = original_size
            db.MYSQL_POOL_MAX_OVERFLOW = original_overflow
            db.MYSQL_POOL_TIMEOUT = original_timeout
            db._reset_pool()


class TestTransactionContext(TempDbCase):
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
        row = db.get_db().execute(
            "SELECT value FROM settings WHERE `key`='tx_bad'").fetchone()
        self.assertIsNone(row, "异常后事务未回滚")

    def test_nested_savepoint_inner_rollback_keeps_outer(self):
        """内层 SAVEPOINT 回滚不应影响外层已完成的修改。"""
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
            r["index_name"] for r in db.get_db().execute(
                "SELECT DISTINCT INDEX_NAME AS index_name FROM information_schema.statistics "
                "WHERE table_schema=? AND table_name='view_sessions'",
                (db.MYSQL_DATABASE,),
            ).fetchall()
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
                args=(self.db_name, image_id, index, queue),
            )
            for index in range(4)
        ]
        for proc in procs:
            proc.start()
        results = []
        try:
            for _ in procs:
                results.append(queue.get(timeout=180))
        finally:
            for proc in procs:
                proc.join(timeout=180)

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
                "SELECT COUNT(*) FROM settings WHERE `key` LIKE 'mp!_%' ESCAPE '!'"
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
        # 数据一致性：该图片的点赞计数与明细一致、无孤儿点赞、表结构正常
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM images i WHERE i.id=? AND i.like_count <> "
            "(SELECT COUNT(*) FROM likes l WHERE l.image_id=i.id)", (image_id,)
        ).fetchone()[0], 0)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM likes l LEFT JOIN images i ON l.image_id=i.id "
            "WHERE i.id IS NULL"
        ).fetchone()[0], 0)
        check = conn.execute("CHECK TABLE images").fetchone()
        self.assertEqual(check.get("Msg_type"), "status")

    def test_failed_like_does_not_leave_write_lock(self):
        """唯一索引冲突后必须回滚，否则连接会长期持有行锁。"""
        image_id = self._insert_image(status="approved")
        self.assertTrue(models.insert_like(image_id, "7.7.7.7", "UA", "省", "市"))
        self.assertFalse(models.insert_like(image_id, "7.7.7.7", "UA", "省", "市"))

        # 另一连接应能立即写入同一行，证明锁已释放
        captured = {}

        def other_writer():
            conn = db.get_db()
            try:
                conn.execute(
                    "UPDATE images SET views=views+1 WHERE id=?", (image_id,)
                )
                captured["ok"] = True
            except Exception as exc:  # 记录错误供断言
                captured["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                db.close_db()

        thread = threading.Thread(target=other_writer)
        thread.start()
        thread.join(timeout=10)
        self.assertNotIn("error", captured, f"点赞失败后仍持有锁: {captured.get('error')}")

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
            r["index_name"] for r in db.get_db().execute(
                "SELECT DISTINCT INDEX_NAME AS index_name FROM information_schema.statistics "
                "WHERE table_schema=?", (db.MYSQL_DATABASE,),
            ).fetchall()
        }
        for idx in ("idx_images_status_like", "idx_admin_logs_action_created",
                    "idx_view_sessions_unique"):
            self.assertIn(idx, names, f"缺少第四阶段索引 {idx}")

    def test_reinit_on_existing_schema_keeps_data(self):
        """对已有数据的库重复初始化：不报错、不丢数据、唯一约束仍生效。"""
        image_id = self._insert_image(status="approved", ip="5.5.5.5")
        db.init_db()
        self.assertIsNotNone(models.get_image_by_id(image_id))

        # 唯一约束（image_id, ip）必须仍然生效，从数据库层面阻止重复浏览记录
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
