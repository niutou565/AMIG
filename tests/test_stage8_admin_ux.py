# -*- coding: utf-8 -*-
"""第八阶段：后台管理体验（操作反馈、空状态、批量操作与交互一致性）测试。"""
import os
import re
import shutil
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

import app.auth as auth
import app.database as database
import app.image_utils as image_utils
import app.models as models
import app.routes.admin as admin_routes
from app.main import app

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


class AdminUxTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="amig-stage8-")
        cls._orig_db = database.DATABASE_PATH
        cls._orig_uploads = image_utils.UPLOADS_DIR
        cls._orig_private = image_utils.PRIVATE_UPLOADS_DIR
        cls._orig_admin_uploads = admin_routes.UPLOADS_DIR
        database.close_all_db()
        database.DATABASE_PATH = os.path.join(cls.tmpdir, "app.db")
        image_utils.UPLOADS_DIR = os.path.join(cls.tmpdir, "uploads")
        image_utils.PRIVATE_UPLOADS_DIR = os.path.join(cls.tmpdir, "private_uploads")
        admin_routes.UPLOADS_DIR = image_utils.UPLOADS_DIR
        os.makedirs(image_utils.UPLOADS_DIR, exist_ok=True)
        os.makedirs(image_utils.PRIVATE_UPLOADS_DIR, exist_ok=True)
        database.init_db()
        models.set_setting("admin_domain", "admin.test")
        models.set_setting("admin_domain_initialized", "1")
        models.set_setting("enable_captcha", "0")
        models.set_setting("enable_dedup", "0")
        cls.admin_password = "Admin-Password-123"
        conn = database.get_db()
        conn.execute(
            "INSERT INTO admins (username, password_hash, created_at) VALUES (?,?,?)",
            ("admin", auth.hash_password(cls.admin_password), models.now_iso()),
        )
        conn.commit()

    @classmethod
    def tearDownClass(cls):
        database.close_all_db()
        database.DATABASE_PATH = cls._orig_db
        image_utils.UPLOADS_DIR = cls._orig_uploads
        image_utils.PRIVATE_UPLOADS_DIR = cls._orig_private
        admin_routes.UPLOADS_DIR = cls._orig_admin_uploads
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    # ---------- 辅助方法 ----------

    def client(self):
        return TestClient(app, base_url="https://admin.test",
                          raise_server_exceptions=False)

    def csrf(self, client, path="/admin/review"):
        response = client.get(path)
        self.assertEqual(response.status_code, 200, response.text[:300])
        match = CSRF_RE.search(response.text)
        self.assertIsNotNone(match, f"{path} 页面缺少 csrf_token")
        return match.group(1)

    def login(self, client):
        auth.clear_login_rate_limits()
        token = self.csrf(client, "/admin/login")
        response = client.post(
            "/admin/login",
            data={"username": "admin", "password": self.admin_password,
                  "csrf_token": token},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302, response.text[:300])
        return client

    def admin(self):
        return self.login(self.client())

    def insert_image(self, status="pending", title="后台体验测试"):
        unique = f"{time.time_ns()}-{status}"
        relative = f"stage8/{unique}.webp"
        thumb = f"stage8/{unique}_thumb.webp"
        base = (image_utils.UPLOADS_DIR if status == "approved"
                else image_utils.PRIVATE_UPLOADS_DIR)
        for name in (relative, thumb):
            path = os.path.join(base, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(b"RIFF\x00\x00\x00\x00WEBPVP8 ")
        image_id = models.insert_image(
            relative, thumb, 100, 50, 10, 5, "描述", "",
            f"md5-{unique}", "1.2.3.4", "ua", f"uah-{unique}", "", "",
            title=title,
        )
        conn = database.get_db()
        conn.execute(
            "UPDATE images SET status=?, reviewed_at=? WHERE id=?",
            (status, models.now_iso() if status == "approved" else None, image_id),
        )
        conn.commit()
        return image_id

    # ---------- 页面可达性 ----------

    def test_01_main_admin_pages_open(self):
        client = self.admin()
        for path in ("/admin/dashboard", "/admin/review", "/admin/images",
                     "/admin/users", "/admin/links", "/admin/pages",
                     "/admin/settings", "/admin/logs", "/admin/change-password"):
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 200, response.text[:200])

    def test_02_static_assets_include_submit_lock(self):
        client = self.client()
        js = client.get("/static/js/admin.js?v=4")
        self.assertEqual(js.status_code, 200)
        self.assertIn("处理中", js.text)
        self.assertIn("dataset.submitting", js.text)
        css = client.get("/static/css/admin.css?v=4")
        self.assertEqual(css.status_code, 200)
        self.assertIn("is-loading", css.text)

    # ---------- 审核与批量操作 ----------

    def test_03_single_approve_shows_success(self):
        image_id = self.insert_image("pending")
        client = self.admin()
        token = self.csrf(client, "/admin/review")
        response = client.post(
            f"/admin/review/{image_id}",
            data={"csrf_token": token, "action": "approve"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("msg=approved", response.headers["location"])
        page = client.get(response.headers["location"])
        self.assertIn("图片已通过审核并发布", page.text)
        self.assertEqual(
            database.get_db().execute(
                "SELECT status FROM images WHERE id=?", (image_id,)
            ).fetchone()["status"],
            "approved",
        )

    def test_04_single_reject_shows_success(self):
        image_id = self.insert_image("pending")
        client = self.admin()
        token = self.csrf(client, "/admin/review")
        response = client.post(
            f"/admin/review/{image_id}",
            data={"csrf_token": token, "action": "reject"},
            follow_redirects=False,
        )
        self.assertIn("msg=rejected", response.headers["location"])
        self.assertIn("图片已拒绝并删除", client.get(response.headers["location"]).text)
        self.assertIsNone(models.get_image_by_id(image_id))

    def test_05_batch_approve_reports_count(self):
        first = self.insert_image("pending")
        second = self.insert_image("pending")
        client = self.admin()
        token = self.csrf(client, "/admin/review")
        response = client.post(
            "/admin/review/batch",
            data={"csrf_token": token, "ids": f"{first},{second}",
                  "action": "approved"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        location = response.headers["location"]
        self.assertIn("msg=batch_approved", location)
        self.assertIn("n=2", location)
        self.assertIn("已批量通过 2 张图片", client.get(location).text)

    def test_06_batch_delete_reports_count(self):
        first = self.insert_image("pending")
        second = self.insert_image("pending")
        client = self.admin()
        token = self.csrf(client, "/admin/review")
        response = client.post(
            "/admin/review/batch",
            data={"csrf_token": token, "ids": f"{first},{second}",
                  "action": "rejected"},
            follow_redirects=False,
        )
        location = response.headers["location"]
        self.assertIn("msg=batch_rejected", location)
        self.assertIn("已批量删除 2 张图片", client.get(location).text)
        self.assertIsNone(models.get_image_by_id(first))

    def test_07_batch_buttons_disabled_until_selection(self):
        self.insert_image("pending")
        page = self.admin().get("/admin/review")
        self.assertEqual(page.status_code, 200)
        self.assertIn('id="batchCount"', page.text)
        self.assertIn('data-batch-action="approved" disabled', page.text)
        self.assertIn('data-batch-action="rejected" disabled', page.text)

    # ---------- 空状态与筛选提示 ----------

    def test_08_search_empty_states_are_explicit(self):
        client = self.admin()
        cases = [
            ("/admin/images", {"query": "不存在的关键词"}, "没有找到匹配的图片"),
            ("/admin/review", {"query": "不存在的关键词"}, "没有找到匹配的待审核图片"),
            ("/admin/logs", {"q": "不存在的关键词"}, "没有找到匹配的日志"),
        ]
        for path, params, text in cases:
            with self.subTest(path=path):
                response = client.get(path, params=params)
                self.assertEqual(response.status_code, 200)
                self.assertIn(text, response.text)

    def test_09_list_pages_show_total(self):
        client = self.admin()
        self.assertIn("张图片", client.get("/admin/images").text)
        self.assertIn("张待审核图片", client.get("/admin/review").text)

    # ---------- 友情链接 / 页面 / 用户 ----------

    def test_10_link_save_and_delete_feedback(self):
        client = self.admin()
        token = self.csrf(client, "/admin/links")
        saved = client.post(
            "/admin/links/save",
            data={"csrf_token": token, "name": "体验测试友链",
                  "url": "https://example.com", "sort_order": "0",
                  "is_visible": "1"},
            follow_redirects=False,
        )
        self.assertIn("msg=link_saved", saved.headers["location"])
        self.assertIn("友情链接已保存", client.get(saved.headers["location"]).text)

        link = [x for x in models.get_all_links() if x["name"] == "体验测试友链"][0]
        deleted = client.post(
            f"/admin/links/delete/{link['id']}",
            data={"csrf_token": self.csrf(client, "/admin/links")},
            follow_redirects=False,
        )
        self.assertIn("msg=link_deleted", deleted.headers["location"])
        self.assertIn("友情链接已删除", client.get(deleted.headers["location"]).text)

    def test_11_link_invalid_url_shows_error(self):
        client = self.admin()
        token = self.csrf(client, "/admin/links")
        response = client.post(
            "/admin/links/save",
            data={"csrf_token": token, "name": "非法链接",
                  "url": "javascript:alert(1)", "sort_order": "0",
                  "is_visible": "1"},
            follow_redirects=False,
        )
        self.assertIn("error=url", response.headers["location"])
        page = client.get(response.headers["location"])
        self.assertIn("链接地址必须以 http:// 或 https:// 开头", page.text)
        self.assertFalse([x for x in models.get_all_links() if x["name"] == "非法链接"])

    def test_12_page_save_and_delete_feedback(self):
        client = self.admin()
        token = self.csrf(client, "/admin/pages")
        saved = client.post(
            "/admin/pages/save",
            data={"csrf_token": token, "slug": "stage8-page", "title": "体验页面",
                  "content": "内容", "sort_order": "0", "is_visible": "1"},
            follow_redirects=False,
        )
        self.assertIn("msg=page_saved", saved.headers["location"])
        self.assertIn("页面已保存", client.get(saved.headers["location"]).text)

        page = models.get_page_by_slug_any("stage8-page")
        deleted = client.post(
            f"/admin/pages/delete/{page['id']}",
            data={"csrf_token": self.csrf(client, "/admin/pages")},
            follow_redirects=False,
        )
        self.assertIn("msg=page_deleted", deleted.headers["location"])
        self.assertIn("页面已删除", client.get(deleted.headers["location"]).text)

    def test_13_ban_feedback_and_missing_fields(self):
        client = self.admin()
        token = self.csrf(client, "/admin/users")
        missing = client.post(
            "/admin/users/ban",
            data={"csrf_token": token, "target": "", "ban_type": "", "reason": ""},
            follow_redirects=False,
        )
        self.assertIn("error=fields", missing.headers["location"])
        self.assertIn("请填写完整的必填项", client.get(missing.headers["location"]).text)

        banned = client.post(
            "/admin/users/ban",
            data={"csrf_token": token, "target": "203.0.113.77",
                  "ban_type": "single", "reason": "体验测试"},
            follow_redirects=False,
        )
        self.assertIn("msg=banned", banned.headers["location"])
        self.assertIn("封禁已生效", client.get(banned.headers["location"]).text)

        ban = [x for x in models.get_ban_list() if x["target"] == "203.0.113.77"][0]
        unbanned = client.post(
            f"/admin/users/unban/{ban['id']}",
            data={"csrf_token": self.csrf(client, "/admin/users")},
            follow_redirects=False,
        )
        self.assertIn("msg=unbanned", unbanned.headers["location"])
        self.assertIn("已解封该目标", client.get(unbanned.headers["location"]).text)

    # ---------- 设置与图片编辑 ----------

    def test_14_settings_save_feedback_and_field_hint(self):
        client = self.admin()
        token = self.csrf(client, "/admin/settings")
        saved = client.post(
            "/admin/settings",
            data={"csrf_token": token, "site_name": "体验站点"},
            follow_redirects=False,
        )
        self.assertIn("msg=settings_saved", saved.headers["location"])
        page = client.get(saved.headers["location"])
        self.assertIn("设置已保存", page.text)
        self.assertIn("体验站点", page.text)

        invalid = client.post(
            "/admin/settings",
            data={"csrf_token": token, "upload_limit_per_ip": "-5"},
            follow_redirects=False,
        )
        self.assertEqual(invalid.status_code, 200)
        self.assertIn("数值设置不合法", invalid.text)
        self.assertIn("每 IP 每日上限 不能小于 1", invalid.text)

    def test_15_image_edit_success_feedback(self):
        image_id = self.insert_image("approved")
        client = self.admin()
        token = self.csrf(client, f"/admin/images/{image_id}")
        response = client.post(
            f"/admin/images/{image_id}/edit",
            data={"csrf_token": token, "title": "修改后的标题",
                  "description": "修改后的简介"},
            follow_redirects=False,
        )
        self.assertIn("msg=image_saved", response.headers["location"])
        page = client.get(response.headers["location"])
        self.assertIn("标题 / 简介已更新", page.text)
        self.assertEqual(models.get_image_by_id(image_id)["title"], "修改后的标题")

    # ---------- 提示安全与权限 ----------

    def test_16_unknown_flash_keys_are_ignored(self):
        client = self.admin()
        page = client.get("/admin/images", params={"msg": "unknown-key", "n": "5"})
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("alert-success", page.text)

        payload = "<script>alert(1)</script>"
        page = client.get("/admin/images", params={"error": payload})
        self.assertNotIn("alert-error", page.text)
        self.assertNotIn(payload, page.text)

    def test_17_admin_pages_require_login(self):
        client = self.client()
        for path in ("/admin/dashboard", "/admin/review", "/admin/images",
                     "/admin/users", "/admin/links", "/admin/pages",
                     "/admin/settings", "/admin/logs"):
            with self.subTest(path=path):
                response = client.get(path, follow_redirects=False)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/admin/login", response.headers["location"])

    def test_18_batch_without_csrf_is_rejected(self):
        image_id = self.insert_image("pending")
        client = self.admin()
        response = client.post(
            "/admin/review/batch",
            data={"ids": str(image_id), "action": "approved"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(models.get_image_by_id(image_id)["status"], "pending")

    # ---------- 未解决问题回归：失败明细 / 字段错误 / 快捷入口 / 提交锁 ----------

    def test_19_batch_approve_failure_links_are_rendered(self):
        image_id = self.insert_image("pending")
        # 删除缩略图让批量通过失败，图片应保持待审核
        thumb_path = os.path.join(
            image_utils.PRIVATE_UPLOADS_DIR,
            database.get_db().execute(
                "SELECT thumb_filename FROM images WHERE id=?", (image_id,)
            ).fetchone()[0],
        )
        os.remove(thumb_path)

        client = self.admin()
        token = self.csrf(client, "/admin/review")
        response = client.post(
            "/admin/review/batch",
            data={"csrf_token": token, "ids": str(image_id), "action": "approved"},
            follow_redirects=False,
        )
        location = response.headers["location"]
        self.assertIn("failed=1", location)
        self.assertIn(f"failed_ids={image_id}", location)
        page = client.get(location)
        self.assertIn(f'href="/admin/review/{image_id}"', page.text)
        self.assertIn("失败图片", page.text)
        self.assertEqual(models.get_image_by_id(image_id)["status"], "pending")

    def test_20_failed_ids_param_is_limited_and_validated(self):
        client = self.admin()
        response = client.get(
            "/admin/review",
            params={"msg": "batch_approved", "n": "1", "failed": "1",
                    "failed_ids": "abc,-1,999999999999999999999"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("失败图片", response.text)
        self.assertNotIn("abc", response.text)

        # 非批量通过提示不接受 failed_ids，避免伪造跳转
        response = client.get(
            "/admin/review",
            params={"msg": "approved", "failed_ids": "123"},
        )
        self.assertNotIn("失败图片", response.text)

    def test_21_image_edit_marks_invalid_field(self):
        image_id = self.insert_image("approved")
        client = self.admin()
        token = self.csrf(client, f"/admin/images/{image_id}")
        response = client.post(
            f"/admin/images/{image_id}/edit",
            data={"csrf_token": token, "title": "", "description": "简介"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn('aria-invalid="true"', response.text)
        self.assertIn('class="input-error"', response.text)
        self.assertIn('id="editTitleError"', response.text)
        self.assertIn("标题不能为空", response.text)
        self.assertEqual(models.get_image_by_id(image_id)["title"], "后台体验测试")

    def test_22_dashboard_has_quick_actions(self):
        page = self.admin().get("/admin/dashboard")
        self.assertEqual(page.status_code, 200)
        self.assertIn("快捷操作", page.text)
        self.assertIn('href="/admin/review"', page.text)
        self.assertIn('href="/admin/images"', page.text)
        self.assertIn('href="/admin/settings"', page.text)

    def test_23_submit_lock_has_timeout_recovery(self):
        js = self.client().get("/static/js/admin.js?v=4").text
        self.assertIn("SUBMIT_LOCK_TIMEOUT_MS", js)
        self.assertIn("clearTimeout", js)
        self.assertIn("clearSubmitState", js)


if __name__ == "__main__":
    unittest.main()
