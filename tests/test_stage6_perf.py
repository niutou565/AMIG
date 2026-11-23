# -*- coding: utf-8 -*-
"""第六阶段：前台查询精简、静态缓存与浏览去重写入优化测试。"""
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.routing import Mount

import app.database as db
import app.models as models
import app.stats as stats
from app.main import CachedStaticFiles


class TempDbCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="amig_stage6_")
        self._orig_path = db.DATABASE_PATH
        self._orig_backup = db.BACKUP_DIR
        db.close_all_db()
        db.DATABASE_PATH = os.path.join(self.tmpdir, "app.db")
        db.BACKUP_DIR = os.path.join(self.tmpdir, "backups")
        db.init_db()

    def tearDown(self):
        db.close_all_db()
        db.DATABASE_PATH = self._orig_path
        db.BACKUP_DIR = self._orig_backup
        shutil.rmtree(self.tmpdir, ignore_errors=True)

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
        statements = []
        conn.set_trace_callback(statements.append)
        try:
            for _ in range(5):
                self.assertIs(db.get_db(), conn)
        finally:
            conn.set_trace_callback(None)
        self.assertEqual(statements, [], "get_db 仍在校验连接时执行 SQL")

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

        conn = db.get_db()
        statements = []
        conn.set_trace_callback(statements.append)
        try:
            self.assertFalse(models.should_count_view(image_id, "8.8.8.8"))
        finally:
            conn.set_trace_callback(None)

        sql = " ".join(statements).upper()
        self.assertNotIn("INSERT", sql, "重复浏览仍执行了写操作")
        self.assertNotIn("BEGIN", sql, "重复浏览仍开启了写事务")

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
