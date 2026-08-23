import os
import re
import shutil
import tempfile
import unittest
from io import BytesIO
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

import app.database as database
import app.image_utils as image_utils
import app.models as models
import app.routes.front as front
from app.auth import hash_password
from app.main import app


class FeatureIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="amig-test-")
        cls.db_path = os.path.join(cls.tmpdir, "app.db")
        cls.upload_dir = os.path.join(cls.tmpdir, "uploads")
        cls.original_db_path = database.DATABASE_PATH
        cls.original_upload_dir = image_utils.UPLOADS_DIR
        database.close_db()
        database.DATABASE_PATH = cls.db_path
        image_utils.UPLOADS_DIR = cls.upload_dir
        database.init_db()
        models.set_setting("admin_domain", "admin.test")
        models.set_setting("enable_captcha", "0")
        models.set_setting("enable_dedup", "0")
        database.get_db().execute(
            "INSERT INTO admins (username, password_hash, created_at) VALUES (?,?,?)",
            ("tester", hash_password("test-password"), models.now_iso()),
        )
        database.get_db().commit()

    @classmethod
    def tearDownClass(cls):
        database.close_db()
        database.DATABASE_PATH = cls.original_db_path
        image_utils.UPLOADS_DIR = cls.original_upload_dir
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def client(self, host="admin.test"):
        return TestClient(app, base_url=f"https://{host}", raise_server_exceptions=False)

    def csrf(self, response):
        match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
        self.assertIsNotNone(match, "页面未提供 CSRF 表单令牌")
        return match.group(1)

    def login(self, client):
        page = client.get("/admin/login")
        self.assertEqual(page.status_code, 200)
        token = self.csrf(page)
        result = client.post(
            "/admin/login",
            data={"username": "tester", "password": "test-password", "csrf_token": token},
            follow_redirects=False,
        )
        self.assertEqual(result.status_code, 302, result.text)
        self.assertIn("admin_session", result.cookies)
        return token

    @staticmethod
    def image_bytes(fmt="PNG", size=(32, 24)):
        stream = BytesIO()
        Image.new("RGB", size, (40, 120, 220)).save(stream, format=fmt)
        return stream.getvalue()

    def test_upload_png_saves_original_thumbnail_and_database_row(self):
        client = self.client("upload.test")
        with patch.object(front, "get_client_ip", return_value="10.0.0.11"), \
             patch.object(front, "get_ip_location", return_value=("测试省", "测试市")), \
             patch.object(front, "get_ip_region", return_value="测试地区"):
            page = client.get("/upload")
            token = self.csrf(page)
            response = client.post(
                "/upload",
                data={"csrf_token": token, "title": "测试图片", "description": "描述", "agree": "1"},
                files={"image": ("sample.png", self.image_bytes(), "image/png")},
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("已提交", response.text)
        row = database.get_db().execute("SELECT * FROM images").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["title"], "测试图片")
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["province"], "测试省")
        original_path = os.path.join(self.upload_dir, row["filename"])
        thumb_path = os.path.join(self.upload_dir, row["thumb_filename"])
        self.assertTrue(os.path.isfile(original_path))
        self.assertTrue(os.path.isfile(thumb_path))
        for path in (original_path, thumb_path):
            with Image.open(path) as saved:
                pixels = list(saved.convert("RGB").getdata())
                self.assertGreater(len(set(pixels)), 1, "水印未写入输出图片")
        stat = database.get_db().execute("SELECT upload_count FROM daily_stats").fetchone()
        self.assertEqual(stat["upload_count"], 1)

    def test_upload_supports_jpeg_webp_and_rejects_oversize(self):
        for index, fmt in enumerate(("JPEG", "WEBP"), start=40):
            client = self.client("upload.test")
            with patch.object(front, "get_client_ip", return_value=f"10.0.0.{index}"), \
                 patch.object(front, "get_ip_location", return_value=("", "")), \
                 patch.object(front, "get_ip_region", return_value=""):
                page = client.get("/upload")
                ext = fmt.lower()
                response = client.post(
                    "/upload",
                    data={"csrf_token": self.csrf(page), "title": fmt, "agree": "1"},
                    files={"image": (f"sample.{ext}", self.image_bytes(fmt), f"image/{ext}")},
                )
            self.assertEqual(response.status_code, 200)
            self.assertIn("已提交", response.text)

        client = self.client("upload.test")
        with patch.object(front, "get_client_ip", return_value="10.0.0.42"):
            page = client.get("/upload")
            response = client.post(
                "/upload",
                data={"csrf_token": self.csrf(page), "title": "超大文件", "agree": "1"},
                files={"image": ("large.png", b"x" * (10 * 1024 * 1024 + 1), "image/png")},
            )
        self.assertIn("大小超过限制", response.text)

    def test_upload_rejects_invalid_format_empty_file_and_missing_agreement(self):
        cases = (
            ("bad.txt", b"not image", "不支持的图片格式"),
            ("fake.png", b"not image", "格式与文件内容不匹配"),
        )
        for index, (name, payload, message) in enumerate(cases, start=20):
            client = self.client("upload.test")
            with patch.object(front, "get_client_ip", return_value=f"10.0.0.{index}"):
                page = client.get("/upload")
                response = client.post(
                    "/upload",
                    data={"csrf_token": self.csrf(page), "title": "测试", "agree": "1"},
                    files={"image": (name, payload, "image/png")},
                )
            self.assertEqual(response.status_code, 200)
            self.assertIn(message, response.text)

        client = self.client("upload.test")
        with patch.object(front, "get_client_ip", return_value="10.0.0.30"):
            page = client.get("/upload")
            response = client.post(
                "/upload",
                data={"csrf_token": self.csrf(page), "title": "测试"},
                files={"image": ("empty.png", b"", "image/png")},
            )
        self.assertIn("同意站规", response.text)

    def test_upload_requires_csrf_and_turnstile_when_enabled(self):
        client = self.client("upload.test")
        with patch.object(front, "get_client_ip", return_value="10.0.0.31"):
            page = client.get("/upload")
            response = client.post(
                "/upload",
                data={"title": "测试", "agree": "1"},
                files={"image": ("sample.png", self.image_bytes(), "image/png")},
            )
        self.assertIn("请求无效", response.text)
        models.set_setting("enable_captcha", "1")
        try:
            client = self.client("upload.test")
            with patch.object(front, "get_client_ip", return_value="10.0.0.32"):
                page = client.get("/upload")
                response = client.post(
                    "/upload",
                    data={"csrf_token": self.csrf(page), "title": "测试", "agree": "1"},
                    files={"image": ("sample.png", self.image_bytes(), "image/png")},
                )
            self.assertIn("请完成人机验证", response.text)
        finally:
            models.set_setting("enable_captcha", "0")

    def test_watermark_setting_and_upload_progress_ui(self):
        client = self.client()
        self.login(client)
        settings_page = client.get("/admin/settings")
        token = self.csrf(settings_page)
        saved = client.post(
            "/admin/settings",
            data={"csrf_token": token, "admin_domain": "admin.test", "watermark_text": "测试水印", "allow_author": "on"},
            follow_redirects=False,
        )
        self.assertEqual(saved.status_code, 302)
        self.assertEqual(models.get_setting("watermark_text"), "测试水印")
        upload_page = self.client("upload.test").get("/upload")
        self.assertIn("XMLHttpRequest", upload_page.text)
        self.assertIn("upload.addEventListener('progress'", upload_page.text)
        self.assertIn("uploadProgressBar", upload_page.text)

    def test_page_create_public_navigation_and_delete(self):
        client = self.client()
        self.assertEqual(client.get("/admin/pages", follow_redirects=False).status_code, 302)
        self.login(client)
        page_list = client.get("/admin/pages")
        token = self.csrf(page_list)
        created = client.post(
            "/admin/pages/save",
            data={"csrf_token": token, "slug": "test-page", "title": "测试页面", "content": "页面内容", "sort_order": "1", "is_visible": "1"},
            follow_redirects=False,
        )
        self.assertEqual(created.status_code, 302)
        row = models.get_page_by_slug("test-page")
        self.assertIsNotNone(row)
        public = client.get("/page/test-page")
        self.assertEqual(public.status_code, 200)
        self.assertIn("页面内容", public.text)
        home = client.get("/")
        self.assertIn("测试页面", home.text)

        deleted = client.post(
            f"/admin/pages/delete/{row['id']}",
            data={"csrf_token": self.csrf(client.get("/admin/pages"))},
            follow_redirects=False,
        )
        self.assertEqual(deleted.status_code, 302)
        self.assertIsNone(models.get_page_by_id(row["id"]))
        self.assertEqual(client.get("/page/test-page").status_code, 404)
        self.assertNotIn("测试页面", client.get("/").text)

    def test_page_write_requires_session_and_csrf(self):
        client = self.client()
        response = client.post("/admin/pages/save", data={"slug": "blocked", "title": "blocked"}, follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/admin/login")
        self.login(client)
        response = client.post("/admin/pages/save", data={"slug": "blocked", "title": "blocked"}, follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/admin/pages")
        self.assertIsNone(models.get_page_by_slug("blocked"))

    def test_database_constraints_foreign_keys_and_allowed_stats(self):
        conn = database.get_db()
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
        models.upsert_page(0, "unique-page", "标题", "内容", 1, 1)
        with self.assertRaises(Exception):
            models.upsert_page(0, "unique-page", "重复", "", 2, 1)
        with self.assertRaises(ValueError):
            models.increment_daily_stat(models.today_str(), "not_a_column")
        image_id = models.insert_image("a.webp", "a_thumb.webp", 1, 1, 1, 1, "", "", "md5-db", "1.1.1.1", "ua", "uah", "", "")
        self.assertTrue(models.insert_like(image_id, "1.1.1.1", "ua-db", "", ""))
        conn.execute("DELETE FROM images WHERE id=?", (image_id,))
        conn.commit()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM likes WHERE image_id=?", (image_id,)).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
