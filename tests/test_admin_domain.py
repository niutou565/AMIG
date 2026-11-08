"""管理域名统一配置验收测试"""
import os
import re
import shutil
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import app.admin_domains as admin_domains
import app.auth as auth
import app.database as database
import app.models as models
from app.auth import hash_password
from app.main import app


class AdminDomainConfigTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="amig-domain-test-")
        cls.db_path = os.path.join(cls.tmpdir, "app.db")
        cls.original_db_path = database.DATABASE_PATH
        database.close_db()
        database.DATABASE_PATH = cls.db_path
        database.init_db()
        database.get_db().execute(
            "INSERT INTO admins (username, password_hash, created_at) VALUES (?,?,?)",
            ("tester", hash_password("test-password"), models.now_iso()),
        )
        database.get_db().commit()

    @classmethod
    def tearDownClass(cls):
        database.close_db()
        database.DATABASE_PATH = cls.original_db_path
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        models.set_setting("admin_domain", "admin.test")
        models.set_setting("admin_domain_initialized", "1")
        models.set_setting("enable_captcha", "0")
        models.set_setting("enable_dedup", "0")
        auth._login_ip_attempts.clear()

    # ---------- 工具 ----------

    def client(self, host="admin.test", client_ip="testclient"):
        return TestClient(
            app,
            base_url=f"https://{host}",
            raise_server_exceptions=False,
            client=(client_ip, 50000),
        )

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

    def save_settings(self, client, admin_domain, follow_redirects=False):
        page = client.get("/admin/settings")
        self.assertEqual(page.status_code, 200)
        token = self.csrf(page)
        data = {
            "csrf_token": token,
            "admin_domain": admin_domain,
            "site_name": "测试站",
            "site_description": "",
            "footer_icp": "",
            "footer_copyright": "",
            "theme_accent_color": "#6c5ce7",
            "upload_limit_per_ip": "10",
            "upload_limit_per_ua": "10",
            "upload_limit_daily_total": "500",
            "upload_interval_seconds": "30",
            "like_limit_per_minute": "10",
            "top_window_days": "30",
            "top_show_count": "12",
            "watermark_text": "",
            "sensitive_words": "",
            "turnstile_site_key": "",
        }
        return client.post("/admin/settings", data=data, follow_redirects=follow_redirects)

    def delete_setting(self, key):
        database.get_db().execute("DELETE FROM settings WHERE key=?", (key,))
        database.get_db().commit()

    # ---------- 归一化 ----------

    def test_normalize_handles_case_ports_slashes_and_invalid_input(self):
        domains, invalid = admin_domains.normalize_admin_domains(
            " HTTPS://ADMIN.Example.COM:8443/ , admin2.example.com.\n"
            "https://third.example.com/path, *.evil.com, bad domain, 999.1.1.1, [::1]:8000 "
        )
        self.assertEqual(
            domains, ["admin.example.com", "admin2.example.com", "::1"]
        )
        self.assertEqual(len(invalid), 4, invalid)

    def test_normalize_deduplicates_and_strips_port(self):
        domains, invalid = admin_domains.normalize_admin_domains(
            "admin.example.com, ADMIN.EXAMPLE.COM:443, admin.example.com"
        )
        self.assertEqual(domains, ["admin.example.com"])
        self.assertEqual(invalid, [])

    def test_normalized_domain_is_stored(self):
        self.assertEqual(models.get_setting("admin_domain"), "admin.test")
        self.assertEqual(admin_domains.get_admin_domains(), {"admin.test"})

    # ---------- .env 初始化与不覆盖 ----------

    def test_env_initializes_database_when_unconfigured(self):
        models.set_setting("admin_domain", "")
        self.delete_setting("admin_domain_initialized")
        with patch.object(admin_domains, "ADMIN_DOMAIN", "https://Init.Example.COM:443/"):
            admin_domains.ensure_admin_domain_initialized()
        self.assertEqual(models.get_setting("admin_domain"), "init.example.com")
        self.assertEqual(models.get_setting("admin_domain_initialized"), "1")

    def test_env_does_not_override_saved_domain(self):
        models.set_setting("admin_domain", "new.example.com")
        with patch.object(admin_domains, "ADMIN_DOMAIN", "old.example.com"):
            admin_domains.ensure_admin_domain_initialized()
        self.assertEqual(models.get_setting("admin_domain"), "new.example.com")

    def test_env_initializes_legacy_empty_database_once(self):
        models.set_setting("admin_domain", "")
        self.delete_setting("admin_domain_initialized")
        with patch.object(admin_domains, "ADMIN_DOMAIN", "legacy.example.com"):
            admin_domains.ensure_admin_domain_initialized()
        self.assertEqual(models.get_setting("admin_domain"), "legacy.example.com")

        # 再次执行不会被 .env 覆盖
        models.set_setting("admin_domain", "current.example.com")
        with patch.object(admin_domains, "ADMIN_DOMAIN", "legacy.example.com"):
            admin_domains.ensure_admin_domain_initialized()
        self.assertEqual(models.get_setting("admin_domain"), "current.example.com")

    # ---------- 修改立即生效 ----------

    def test_change_domain_takes_effect_immediately(self):
        models.set_setting("admin_domain", "admin.test")
        client = self.client("admin.test")
        self.login(client)

        response = self.save_settings(client, "New.Example.COM:8443/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("管理域名已更新", response.text)
        self.assertEqual(models.get_setting("admin_domain"), "new.example.com")

        # 旧域名立即失效
        self.assertEqual(client.get("/admin/settings").status_code, 404)
        # 新域名可正常访问并登录
        new_client = self.client("new.example.com")
        self.assertEqual(new_client.get("/admin/login").status_code, 200)
        self.login(new_client)
        self.assertEqual(new_client.get("/admin/dashboard").status_code, 200)

    def test_restart_keeps_new_domain_and_env_old_domain(self):
        models.set_setting("admin_domain", "new.example.com")
        models.set_setting("admin_domain_initialized", "1")
        with patch.object(admin_domains, "ADMIN_DOMAIN", "admin.test"):
            admin_domains.ensure_admin_domain_initialized()
        self.assertEqual(models.get_setting("admin_domain"), "new.example.com")
        self.assertEqual(self.client("new.example.com").get("/admin/login").status_code, 200)
        self.assertEqual(self.client("admin.test").get("/admin/login").status_code, 404)

    def test_remove_whitelist_domain_takes_effect_immediately(self):
        models.set_setting("admin_domain", "admin.test,alt.test")
        self.assertEqual(self.client("alt.test").get("/admin/login").status_code, 200)

        client = self.client("admin.test")
        self.login(client)
        response = self.save_settings(client, "admin.test", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(models.get_setting("admin_domain"), "admin.test")
        self.assertEqual(self.client("alt.test").get("/admin/login").status_code, 404)

    # ---------- 非法输入与伪造 Host ----------

    def test_invalid_domain_rejected_without_saving(self):
        client = self.client("admin.test")
        self.login(client)
        before = models.get_setting("admin_domain")

        response = self.save_settings(client, "https://evil.example.com/path")
        self.assertEqual(response.status_code, 200)
        self.assertIn("格式无效", response.text)
        self.assertEqual(models.get_setting("admin_domain"), before)

    def test_empty_whitelist_blocked_from_non_loopback(self):
        client = self.client("admin.test")
        self.login(client)
        response = self.save_settings(client, "")
        self.assertEqual(response.status_code, 200)
        self.assertIn("不能清空管理域名", response.text)
        self.assertEqual(models.get_setting("admin_domain"), "admin.test")

    def test_forged_host_cannot_access_admin(self):
        models.set_setting("admin_domain", "admin.test")
        self.assertEqual(self.client("evil.test").get("/admin/login").status_code, 404)
        self.assertEqual(self.client("admin.test.evil.com").get("/admin/login").status_code, 404)
        # 伪造 Host: localhost 且客户端不是本机地址，同样拒绝
        forged = self.client("localhost", client_ip="203.0.113.9")
        self.assertEqual(forged.get("/admin/login").status_code, 404)

    def test_loopback_client_keeps_local_development_access(self):
        models.set_setting("admin_domain", "admin.test")
        local = self.client("127.0.0.1", client_ip="127.0.0.1")
        self.assertEqual(local.get("/admin/login").status_code, 200)

    def test_host_with_port_matches_whitelist(self):
        models.set_setting("admin_domain", "admin.test")
        client = TestClient(
            app,
            base_url="https://admin.test:8443",
            raise_server_exceptions=False,
            client=("testclient", 50000),
        )
        self.assertEqual(client.get("/admin/login").status_code, 200)

    # ---------- 操作日志 ----------

    def test_settings_page_has_confirmation_prompt(self):
        client = self.client("admin.test")
        self.login(client)
        page = client.get("/admin/settings")
        self.assertEqual(page.status_code, 200)
        self.assertIn("确认将后台管理域名白名单修改为", page.text)
        self.assertIn("保存后立即生效", page.text)

    def test_domain_change_is_logged(self):
        client = self.client("admin.test")
        self.login(client)
        self.save_settings(client, "logged.example.com")
        row = database.get_db().execute(
            "SELECT detail FROM admin_logs WHERE action='config' AND detail LIKE '%管理域名%' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIn("admin.test", row["detail"])
        self.assertIn("logged.example.com", row["detail"])
