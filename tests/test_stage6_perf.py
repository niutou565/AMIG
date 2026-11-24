# -*- coding: utf-8 -*-
"""第六阶段：前台查询精简、静态缓存与浏览去重写入优化测试。"""
import os
import re
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.routing import Mount

import app.database as db
import app.models as models
import app.stats as stats
from app.main import CachedStaticFiles
from tests.mysql_test_utils import create_test_database, drop_test_database


class TempDbCase(unittest.TestCase):
    def setUp(self):
        self._db_name = create_test_database()

    def tearDown(self):
        drop_test_database(self._db_name)

    def add_image(self):
        conn = db.get_db()
        cursor = conn.execute(
            "INSERT INTO images (filename, thumb_filename, original_size,"
            " compressed_size, width, height, description, title, author,"
            " content_md5, ip, ua, ua_hash, province, city, region, status,"
            " reviewed_at, created_at) VALUES (?,?,1000,800,800,600,'','t','a',"
            "'md5','1.1.1.1','UA','uh','省','市','','approved',"
            "'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')",
            (f"{os.urandom(4).hex()}.webp", f"{os.urandom(4).hex()}_t.webp"),
        )
        conn.commit()
        return cursor.lastrowid


class GetDbProbeTest(TempDbCase):
    def test_repeated_get_db_does_not_execute_probe_sql(self):
        conn = db.get_db()
        # MySQL 连接没有 set_trace_callback：直接拦截 execute，任何探测 SQL 都会被记录
        with patch.object(db.ConnectionWrapper, "execute") as mocked:
            for _ in range(5):
                self.assertIs(db.get_db(), conn)
        mocked.assert_not_called()

    def test_connection_recreated_after_close_all(self):
        old = db.get_db()
        db.close_all_db()
        new = db.get_db()
        self.assertIsNot(new, old)
        self.assertEqual(new.execute("SELECT 1").fetchone()[0], 1)


class ViewDedupWriteTest(TempDbCase):
    def test_repeat_view_within_window_does_not_write(self):
        image_id = self.add_image()
        self.assertTrue(models.should_count_view(image_id, "8.8.8.8"))

        statements = []
        original_execute = db.ConnectionWrapper.execute

        def recording_execute(conn_self, sql, params=()):
            statements.append(sql)
            return original_execute(conn_self, sql, params)

        # MySQL 连接没有 set_trace_callback：包装 execute 记录 SQL，并拦截 begin/commit
        with patch.object(db.ConnectionWrapper, "execute", recording_execute), \
                patch.object(db.ConnectionWrapper, "begin") as begin_mock, \
                patch.object(db.ConnectionWrapper, "commit") as commit_mock:
            self.assertFalse(models.should_count_view(image_id, "8.8.8.8"))
        begin_mock.assert_not_called()
        commit_mock.assert_not_called()

        writes = [
            sql for sql in statements
            if re.search(r"\b(INSERT|UPDATE|DELETE|REPLACE)\b", sql, re.IGNORECASE)
        ]
        self.assertEqual(writes, [], f"重复浏览仍执行了写操作: {writes}")

    def test_concurrent_first_views_still_only_count_once(self):
        image_id = self.add_image()
        results = []
        for _ in range(5):
            results.append(models.should_count_view(image_id, "9.9.9.9"))
        self.assertEqual(results.count(True), 1)


class TopCacheTest(unittest.TestCase):
    def setUp(self):
        stats.invalidate_top_cache()

    def tearDown(self):
        stats.invalidate_top_cache()

    def test_preloaded_settings_avoid_setting_queries(self):
        with patch.object(stats, "get_top_images", return_value=["x"]) as mocked, \
                patch.object(stats, "get_setting_int", side_effect=AssertionError("不应查库")):
            result = stats.get_top_cached(3, window_days=7, show_count=5)
        self.assertEqual(result, ["x"])
        mocked.assert_called_once_with(7, 5, limit=3)

    def test_cache_key_includes_limit(self):
        with patch.object(stats, "get_top_images", side_effect=[["a"], ["b"]]) as mocked:
            self.assertEqual(stats.get_top_cached(3, window_days=7, show_count=5), ["a"])
            self.assertEqual(stats.get_top_cached(10, window_days=7, show_count=5), ["b"])
        self.assertEqual(mocked.call_count, 2, "不同 limit 错误复用了缓存")


class StaticCacheTest(unittest.TestCase):
    def test_static_cache_control_and_304(self):
        app = FastAPI()
        app.mount(
            "/static",
            CachedStaticFiles(directory=os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "app", "static")),
        )
        with TestClient(app) as client:
            first = client.get("/static/css/dark.css")
            self.assertEqual(first.status_code, 200)
            self.assertIn("public", first.headers.get("cache-control", ""))
            etag = first.headers.get("etag")
            self.assertTrue(etag)
            second = client.get("/static/css/dark.css", headers={"If-None-Match": etag})
            self.assertEqual(second.status_code, 304)
            self.assertIn("public", second.headers.get("cache-control", ""))


if __name__ == "__main__":
    unittest.main()
