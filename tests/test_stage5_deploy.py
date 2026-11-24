# -*- coding: utf-8 -*-
"""第五阶段：部署配置、可信代理与管理域名初始化测试。"""
import unittest
from unittest.mock import patch

import app.admin_domains as admin_domains
import app.database as db
import app.ip_geo as ip_geo
import app.models as models
from app.config import _parse_trusted_proxies
from app.middleware import validate_same_origin
from tests.mysql_test_utils import create_test_database, drop_test_database


class FakeClient:
    def __init__(self, host):
        self.host = host


class FakeRequest:
    def __init__(self, client_host=None, headers=None):
        self.client = FakeClient(client_host) if client_host else None
        self.headers = headers or {}


class TempDbCase(unittest.TestCase):
    def setUp(self):
        self._db_name = create_test_database()

    def tearDown(self):
        drop_test_database(self._db_name)

    def clear_admin_domain(self):
        conn = db.get_db()
        conn.execute(
            "DELETE FROM settings WHERE `key` IN ('admin_domain', 'admin_domain_initialized')"
        )
        conn.commit()


class TrustedProxyConfigTest(unittest.TestCase):
    def test_parser_accepts_ip_and_cidr_only(self):
        proxies = _parse_trusted_proxies(
            "127.0.0.1, ::1, 10.0.0.0/8, 172.16.0.0/12"
        )
        self.assertEqual(
            proxies, {"127.0.0.1", "::1", "10.0.0.0/8", "172.16.0.0/12"}
        )

    def test_parser_rejects_wildcard_and_hostname(self):
        with self.assertLogs("app.config", level="WARNING"):
            proxies = _parse_trusted_proxies(
                "0.0.0.0/0, ::/0, localhost, nginx, not-an-ip"
            )
        self.assertEqual(proxies, set())

    def test_empty_config_is_empty(self):
        self.assertEqual(_parse_trusted_proxies(""), set())
        self.assertEqual(_parse_trusted_proxies(None), set())


class ClientIpTest(unittest.TestCase):
    def test_untrusted_client_cannot_forge_real_ip(self):
        request = FakeRequest("203.0.113.9", {"X-Real-IP": "1.2.3.4"})
        with patch.object(ip_geo, "TRUSTED_PROXIES", {"127.0.0.1"}):
            self.assertEqual(ip_geo.get_client_ip(request), "203.0.113.9")

    def test_trusted_proxy_real_ip_is_used(self):
        request = FakeRequest("127.0.0.1", {"X-Real-IP": "1.2.3.4"})
        with patch.object(ip_geo, "TRUSTED_PROXIES", {"127.0.0.1"}):
            self.assertEqual(ip_geo.get_client_ip(request), "1.2.3.4")

    def test_trusted_proxy_invalid_header_falls_back(self):
        request = FakeRequest("127.0.0.1", {"X-Real-IP": "not-an-ip"})
        with patch.object(ip_geo, "TRUSTED_PROXIES", {"127.0.0.1"}):
            self.assertEqual(ip_geo.get_client_ip(request), "127.0.0.1")

    def test_cidr_proxy_is_supported(self):
        request = FakeRequest("10.1.2.3", {"X-Real-IP": "8.8.8.8"})
        with patch.object(ip_geo, "TRUSTED_PROXIES", {"10.0.0.0/8"}):
            self.assertEqual(ip_geo.get_client_ip(request), "8.8.8.8")

    def test_missing_client_does_not_trust_forwarded_headers(self):
        request = FakeRequest(None, {"X-Real-IP": "1.2.3.4"})
        with patch.object(ip_geo, "TRUSTED_PROXIES", {"127.0.0.1"}):
            self.assertEqual(ip_geo.get_client_ip(request), "")


class SameOriginTest(unittest.TestCase):
    def test_matching_host_and_origin(self):
        request = FakeRequest(headers={
            "host": "admin.example.com",
            "origin": "https://admin.example.com",
        })
        self.assertTrue(validate_same_origin(request))

    def test_default_port_is_ignored(self):
        request = FakeRequest(headers={
            "host": "admin.example.com:443",
            "origin": "https://admin.example.com",
        })
        self.assertTrue(validate_same_origin(request))

    def test_cross_origin_is_rejected(self):
        request = FakeRequest(headers={
            "host": "admin.example.com",
            "origin": "https://evil.example.com",
        })
        self.assertFalse(validate_same_origin(request))

    def test_referer_is_used_when_origin_missing(self):
        request = FakeRequest(headers={
            "host": "admin.example.com",
            "referer": "https://admin.example.com/admin/login",
        })
        self.assertTrue(validate_same_origin(request))

    def test_forwarded_host_cannot_bypass_origin_check(self):
        request = FakeRequest(headers={
            "host": "admin.example.com",
            "origin": "https://evil.example.com",
            "x-forwarded-host": "admin.example.com",
        })
        self.assertFalse(validate_same_origin(request))


class AdminDomainInitTest(TempDbCase):
    def test_empty_env_does_not_mark_initialized(self):
        self.clear_admin_domain()
        with patch.object(admin_domains, "ADMIN_DOMAIN", ""), \
                self.assertLogs("app.admin_domains", level="WARNING") as logs:
            admin_domains.ensure_admin_domain_initialized()

        self.assertIsNone(models.get_setting("admin_domain_initialized"))
        self.assertIsNone(models.get_setting("admin_domain"))
        self.assertTrue(any("未配置管理域名" in line for line in logs.output))

    def test_later_env_still_applies_after_empty_first_start(self):
        self.clear_admin_domain()
        with patch.object(admin_domains, "ADMIN_DOMAIN", ""):
            admin_domains.ensure_admin_domain_initialized()

        with patch.object(admin_domains, "ADMIN_DOMAIN", "new.example.com"):
            admin_domains.ensure_admin_domain_initialized()

        self.assertEqual(models.get_setting("admin_domain"), "new.example.com")
        self.assertEqual(models.get_setting("admin_domain_initialized"), "1")

    def test_invalid_env_does_not_mark_initialized(self):
        self.clear_admin_domain()
        with patch.object(admin_domains, "ADMIN_DOMAIN", "*.bad.com, bad domain"), \
                self.assertLogs("app.admin_domains", level="WARNING"):
            admin_domains.ensure_admin_domain_initialized()

        self.assertIsNone(models.get_setting("admin_domain_initialized"))
        self.assertIsNone(models.get_setting("admin_domain"))

    def test_saved_domain_is_not_overwritten_by_env(self):
        self.clear_admin_domain()
        models.set_setting("admin_domain", "saved.example.com")
        models.set_setting("admin_domain_initialized", "1")
        with patch.object(admin_domains, "ADMIN_DOMAIN", "old.example.com"):
            admin_domains.ensure_admin_domain_initialized()
        self.assertEqual(models.get_setting("admin_domain"), "saved.example.com")


if __name__ == "__main__":
    unittest.main()
