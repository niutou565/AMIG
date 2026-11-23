# -*- coding: utf-8 -*-
"""第一阶段（1.1.0 升级）审计修复的回归测试。

覆盖：启动加载敏感词、后台分页搜索参数、设置数值校验、页面/友链表单容错、
空描述模板渲染、随机图全范围取样、点赞异常传播、IP 缓存上限、临时文件清理、
登录限流复位与登录流程冒烟。
"""

import asyncio
import os
import re
import shutil
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient

import app.auth as auth
import app.database as database
import app.image_utils as image_utils
import app.ip_geo as ip_geo
import app.main as main_module
import app.middleware as middleware
import app.models as models
from app.main import app

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


class Stage1AuditTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="amig-stage1-")
        cls.original_db_path = database.DATABASE_PATH
        cls.original_upload_dir = image_utils.UPLOADS_DIR
        database.close_db()
        database.DATABASE_PATH = os.path.join(cls.tmpdir, "app.db")
        image_utils.UPLOADS_DIR = os.path.join(cls.tmpdir, "uploads")
        os.makedirs(image_utils.UPLOADS_DIR, exist_ok=True)
        database.init_db()

        models.set_setting("admin_domain", "admin.test")
        models.set_setting("admin_domain_initialized", "1")
        models.set_setting("enable_captcha", "0")
        models.set_setting("enable_dedup", "0")

        db = database.get_db()
        db.execute(
            "INSERT INTO admins (username, password_hash, created_at) VALUES (?,?,?)",
            ("admin", auth.hash_password("admin123"), models.now_iso()),
        )
        db.commit()

    @classmethod
    def tearDownClass(cls):
        database.close_db()
        database.DATABASE_PATH = cls.original_db_path
        image_utils.UPLOADS_DIR = cls.original_upload_dir
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    # ---------- 辅助方法 ----------

    def _client(self):
        # 使用 https：CSRF/会话 Cookie 在非回环域名下带 Secure 标记，http 下测试客户端不会回传
        return TestClient(app, base_url="https://admin.test", raise_server_exceptions=False)

    def _csrf(self, client, path):
        response = client.get(path)
        self.assertEqual(response.status_code, 200, response.text[:300])
        match = CSRF_RE.search(response.text)
        self.assertIsNotNone(match, f"{path} 页面缺少 csrf_token")
        return match.group(1)

    def _login(self, client):
        token = self._csrf(client, "/admin/login")
        response = client.post(
            "/admin/login",
            data={"username": "admin", "password": "admin123", "csrf_token": token},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302, response.text[:300])

    def _insert_pending(self, index, title=None):
        suffix = f"{index}-{datetime.now(timezone.utc).timestamp()}"
        return models.insert_image(
            f"stage1_{suffix}.webp",
            f"stage1_{suffix}_thumb.webp",
            1000,
            500,
            100,
            50,
            "审计测试描述",
            "",
            f"md5-{suffix}",
            "1.2.3.4",
            f"ua-{suffix}",
            f"uah-{suffix}",
            "",
            "",
            title=title if title is not None else f"分页测试{index}",
        )

    # ---------- 启动与核心逻辑 ----------

    def test_01_startup_loads_sensitive_words(self):
        middleware.SENSITIVE_WORDS.clear()
        models.set_setting("sensitive_words", "违禁词,敏感词测试")
        asyncio.run(main_module.startup())
        self.assertIn("违禁词", middleware.SENSITIVE_WORDS)
        self.assertTrue(middleware.check_sensitive("这里包含违禁词"))
        middleware.SENSITIVE_WORDS.clear()
        models.set_setting("sensitive_words", "")

    def test_02_random_images_uses_full_range(self):
        for i in range(40):
            image_id = self._insert_pending(f"rand-{i}", title=f"随机图{i}")
            database.get_db().execute(
                "UPDATE images SET status='approved', reviewed_at=? WHERE id=?",
                (models.now_iso(), image_id),
            )
        database.get_db().commit()
        total = models.count_images_by_status("approved")
        self.assertGreater(total, 12)
        with patch("random.randint", return_value=0) as mocked:
            models.get_random_approved_images(12)
        self.assertEqual(mocked.call_args[0], (0, total - 12))

    def test_03_insert_like_reports_real_errors(self):
        image_id = self._insert_pending("like")
        self.assertTrue(models.insert_like(image_id, "1.2.3.4", "like-ua", "", ""))
        self.assertFalse(models.insert_like(image_id, "1.2.3.4", "like-ua", "", ""))

        with patch("app.models.get_db") as mocked_db:
            mocked_db.return_value.execute.side_effect = RuntimeError("db unavailable")
            with self.assertRaises(RuntimeError):
                models.insert_like(image_id, "5.6.7.8", "other-ua", "", "")

    def test_04_verify_password_and_legacy_lock_datetime(self):
        self.assertFalse(auth.verify_password("any", "not-a-bcrypt-hash"))

        # 第二阶段已取消全局账户锁定：旧的 locked_until 不再阻止正确密码登录，
        # 避免多 IP 攻击者把管理员锁死；登录成功后应清除旧锁定状态。
        db = database.get_db()
        naive_future = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(tzinfo=None).isoformat()
        db.execute("UPDATE admins SET locked_until=? WHERE username='admin'", (naive_future,))
        db.commit()
        success, _, _ = auth.login("admin", "admin123")
        self.assertTrue(success)
        row = db.execute("SELECT locked_until, login_attempts FROM admins WHERE username='admin'").fetchone()
        self.assertIsNone(row["locked_until"])
        self.assertEqual(row["login_attempts"], 0)

    # ---------- 后台页面 ----------

    def test_05_admin_review_list_keeps_search_query_on_page_two(self):
        for i in range(30):
            self._insert_pending(f"page-{i}", title=f"分页测试{i}")
        client = self._client()
        self._login(client)

        page_one = client.get("/admin/review", params={"query": "分页测试"})
        self.assertEqual(page_one.status_code, 200, page_one.text[:300])
        self.assertIn("query=", page_one.text)
        self.assertNotIn("?q=", page_one.text)

        page_two = client.get("/admin/review", params={"query": "分页测试", "page": 2})
        self.assertEqual(page_two.status_code, 200, page_two.text[:300])
        self.assertIn("分页测试", page_two.text)

    def test_06_admin_settings_rejects_invalid_numbers(self):
        client = self._client()
        self._login(client)
        token = self._csrf(client, "/admin/settings")
        models.set_setting("upload_limit_per_ip", "10")
        models.set_setting("site_name", "原始站名")
        models.set_setting("allow_author", "1")

        response = client.post(
            "/admin/settings",
            data={"csrf_token": token, "upload_limit_per_ip": "-5"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("数值设置不合法", response.text)
        self.assertEqual(models.get_setting("upload_limit_per_ip"), "10")

        response = client.post(
            "/admin/settings",
            data={"csrf_token": token, "upload_limit_per_ip": "15", "site_name": "新站名"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(models.get_setting("upload_limit_per_ip"), "15")
        self.assertEqual(models.get_setting("site_name"), "新站名")
        # 复选框未勾选时不会随表单提交，必须被显式关闭
        self.assertEqual(models.get_setting("allow_author"), "0")

        models.set_setting("upload_limit_per_ip", "10")
        models.set_setting("site_name", "AMIG")
        models.set_setting("allow_author", "0")

    def test_07_admin_page_form_duplicate_slug_and_bad_sort(self):
        client = self._client()
        self._login(client)
        models.upsert_page(0, "dup-slug", "已有页面", "content", 0, 1)

        token = self._csrf(client, "/admin/pages")
        response = client.post(
            "/admin/pages/save",
            data={"csrf_token": token, "slug": "dup-slug", "title": "重复页面", "content": "x"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Slug 已被其他页面使用", response.text)
        matches = [p for p in models.get_all_pages() if p["slug"] == "dup-slug"]
        self.assertEqual(len(matches), 1)

        response = client.post(
            "/admin/pages/save",
            data={
                "csrf_token": token,
                "slug": "sort-test",
                "title": "排序测试",
                "content": "x",
                "sort_order": "not-a-number",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        page = models.get_page_by_slug_any("sort-test")
        self.assertIsNotNone(page)
        self.assertEqual(page["sort_order"], 0)

    def test_08_admin_link_form_bad_sort(self):
        client = self._client()
        self._login(client)
        token = self._csrf(client, "/admin/links")
        response = client.post(
            "/admin/links/save",
            data={
                "csrf_token": token,
                "name": "示例友链",
                "url": "https://example.com",
                "sort_order": "abc",
                "is_visible": "1",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        links = [item for item in models.get_all_links() if item["name"] == "示例友链"]
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["sort_order"], 0)

    def test_09_null_description_renders(self):
        db = database.get_db()
        now = models.now_iso()
        cursor = db.execute(
            """
            INSERT INTO images
                (filename, thumb_filename, original_size, compressed_size, width, height,
                 description, title, author, content_md5, ip, ua, ua_hash, province, city,
                 status, created_at, reviewed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            ("null.webp", "null_thumb.webp", 1, 1, 1, 1, None, "", None,
             "md5-null", "1.2.3.4", "ua-null", "uah-null", "", "",
             "approved", now, now),
        )
        db.commit()
        image_id = cursor.lastrowid

        detail = self._client().get(f"/image/{image_id}")
        self.assertEqual(detail.status_code, 200, detail.text[:300])

        admin_client = self._client()
        self._login(admin_client)
        listing = admin_client.get("/admin/images")
        self.assertEqual(listing.status_code, 200, listing.text[:300])
        review = admin_client.get("/admin/review")
        self.assertEqual(review.status_code, 200, review.text[:300])

    # ---------- 基础设施 ----------

    def test_10_ip_geo_cache_is_bounded(self):
        original_max = ip_geo._CACHE_MAX_SIZE
        ip_geo._CACHE_MAX_SIZE = 3
        try:
            ip_geo._cache.clear()
            for i in range(3):
                ip_geo._cache[f"8.8.8.{i}"] = (0.0, ("", "", ""))
            with patch("urllib.request.urlopen", side_effect=RuntimeError("network down")):
                ip_geo._lookup("1.1.1.1")
            self.assertLessEqual(len(ip_geo._cache), 3)
        finally:
            ip_geo._CACHE_MAX_SIZE = original_max
            ip_geo._cache.clear()

    def test_11_atomic_write_cleans_tmp_on_failure(self):
        target = os.path.join(self.tmpdir, "atomic.webp")
        with patch("app.image_utils.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                image_utils._atomic_write(target, b"data")
        self.assertFalse(os.path.exists(target + ".tmp"))
        self.assertFalse(os.path.exists(target))

    def test_12_login_limit_resets_after_success(self):
        auth.clear_login_rate_limits()
        auth.record_login_failure("testclient")
        for _ in range(4):
            client = self._client()
            self._login(client)
        row = database.get_db().execute(
            "SELECT 1 FROM login_rate_limits WHERE key='ip:testclient'"
        ).fetchone()
        self.assertIsNone(row, "登录成功后应清除该 IP 的失败计数")

    def test_13_public_and_admin_pages_smoke(self):
        image_id = self._insert_pending("smoke")
        database.get_db().execute(
            "UPDATE images SET status='approved', reviewed_at=? WHERE id=?",
            (models.now_iso(), image_id),
        )
        database.get_db().commit()
        if not models.get_page_by_slug_any("smoke-page"):
            models.upsert_page(0, "smoke-page", "冒烟测试页面", "内容", 0, 1)

        client = self._client()
        for path in ["/", "/gallery", "/gallery?q=审计&sort=views", "/top", "/upload",
                     "/page/smoke-page", f"/image/{image_id}", "/api/random-image"]:
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 200, f"{path}: {response.text[:200]}")

        self.assertEqual(client.get("/no-such-page").status_code, 404)
        self.assertEqual(client.get("/static/css/dark.css").status_code, 200)
        self.assertEqual(client.get("/static/js/main.js").status_code, 200)

        self._login(client)
        admin_paths = [
            "/admin/dashboard", "/admin/images", "/admin/images?status=approved",
            "/admin/review", "/admin/users", "/admin/links", "/admin/pages",
            "/admin/pages/edit", "/admin/settings", "/admin/logs", "/admin/change-password",
        ]
        for path in admin_paths:
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 200, f"{path}: {response.text[:200]}")

        first_page = models.get_page_by_slug_any("smoke-page")
        edit = client.get(f"/admin/pages/edit/{first_page['id']}")
        self.assertEqual(edit.status_code, 200, edit.text[:200])

        like = client.post(f"/api/like/{image_id}")
        self.assertEqual(like.status_code, 200, like.text[:200])
