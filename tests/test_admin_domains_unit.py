"""管理域名白名单纯函数单元测试

覆盖多域名输入解析、非法字符过滤、Host 头归一化与伪造 Host 场景。
"""
import unittest
from unittest.mock import patch

import app.admin_domains as admin_domains


class FakeClient:
    def __init__(self, host):
        self.host = host


class FakeRequest:
    """只暴露 is_admin_access 所需的最小请求接口"""

    def __init__(self, host=None, client_host="testclient", forwarded_host=None):
        self.headers = {}
        if host is not None:
            self.headers["host"] = host
        if forwarded_host is not None:
            self.headers["x-forwarded-host"] = forwarded_host
        self.client = FakeClient(client_host) if client_host else None


class NormalizeAdminDomainsTest(unittest.TestCase):
    def test_multiple_domains_comma_semicolon_newline(self):
        domains, invalid = admin_domains.normalize_admin_domains(
            "a.test,b.test;c.test\r\nd.test"
        )
        self.assertEqual(domains, ["a.test", "b.test", "c.test", "d.test"])
        self.assertEqual(invalid, [])

    def test_multiple_domains_keep_order_and_deduplicate(self):
        domains, invalid = admin_domains.normalize_admin_domains(
            "b.test, a.test\nB.TEST, b.test:443, a.test/"
        )
        self.assertEqual(domains, ["b.test", "a.test"])
        self.assertEqual(invalid, [])

    def test_surrounding_whitespace_is_ignored(self):
        domains, invalid = admin_domains.normalize_admin_domains(
            "  a.test \n\t b.test\t \r\n"
        )
        self.assertEqual(domains, ["a.test", "b.test"])
        self.assertEqual(invalid, [])

    def test_empty_input_returns_empty(self):
        self.assertEqual(admin_domains.normalize_admin_domains(""), ([], []))
        self.assertEqual(admin_domains.normalize_admin_domains(" , ;\n "), ([], []))
        self.assertEqual(admin_domains.normalize_admin_domains(None), ([], []))

    def test_case_protocol_port_and_trailing_slash_normalized(self):
        domains, invalid = admin_domains.normalize_admin_domains(
            "HTTPS://ADMIN.Example.COM:8443///, HTTP://other.test."
        )
        self.assertEqual(domains, ["admin.example.com", "other.test"])
        self.assertEqual(invalid, [])

    def test_ipv4_and_ipv6_normalized(self):
        domains, invalid = admin_domains.normalize_admin_domains(
            "127.0.0.1:8080, [::1]:8000, [2001:DB8::1]"
        )
        self.assertEqual(domains, ["127.0.0.1", "::1", "2001:db8::1"])
        self.assertEqual(invalid, [])

    def test_idn_domain_normalized_to_punycode(self):
        expected = "管理.example.com".encode("idna").decode("ascii").lower()
        domains, invalid = admin_domains.normalize_admin_domains("管理.example.com")
        self.assertEqual(domains, [expected])
        self.assertEqual(invalid, [])

    def test_label_length_boundary(self):
        valid_label = "a" * 63
        too_long_label = "a" * 64
        domains, invalid = admin_domains.normalize_admin_domains(
            f"{valid_label}.test, {too_long_label}.test"
        )
        self.assertEqual(domains, [f"{valid_label}.test"])
        self.assertEqual(invalid, [f"{too_long_label}.test"])

    def test_invalid_domains_are_reported(self):
        raw = ",".join([
            "admin example.com",       # 内部空格
            "admin_example.com",       # 下划线
            "admin!.example.com",      # 非法字符
            "*.example.com",           # 通配符
            "https://admin.example.com/path",  # 路径
            "https://user@admin.example.com",  # 用户信息
            "admin.example.com?x=1",   # 查询串
            "admin.example.com#frag",  # 片段
            "admin.example.com:0",     # 端口越界
            "admin.example.com:65536", # 端口越界
            "admin.example.com:abc",   # 非数字端口
            "999.1.1.1",               # 伪 IPv4
            "01.2.3.4",                # 带前导零的 IPv4
            "[::1",                    # IPv6 括号未闭合
            "[::1]tail",               # IPv6 后接非法内容
            "2001:db8::1",             # 未加方括号的 IPv6
            "admin.example.com\u0000", # 控制字符
        ])
        domains, invalid = admin_domains.normalize_admin_domains(raw)
        self.assertEqual(domains, [])
        self.assertEqual(len(invalid), 17, invalid)

    def test_valid_domains_survive_alongside_invalid_ones(self):
        domains, invalid = admin_domains.normalize_admin_domains(
            "good.test, bad/domain, *.wild.test, another.good.test"
        )
        self.assertEqual(domains, ["good.test", "another.good.test"])
        self.assertEqual(invalid, ["bad/domain", "*.wild.test"])


class NormalizeHostHeaderTest(unittest.TestCase):
    def test_host_header_with_port_and_case(self):
        self.assertEqual(
            admin_domains.normalize_host_header("ADMIN.Example.COM:8443"),
            "admin.example.com",
        )

    def test_host_header_trailing_dot(self):
        self.assertEqual(
            admin_domains.normalize_host_header("admin.example.com."),
            "admin.example.com",
        )

    def test_host_header_ipv6_brackets(self):
        self.assertEqual(admin_domains.normalize_host_header("[::1]:8000"), "::1")
        self.assertEqual(admin_domains.normalize_host_header("[::1]"), "::1")

    def test_host_header_empty_or_malformed(self):
        self.assertEqual(admin_domains.normalize_host_header(""), "")
        self.assertEqual(admin_domains.normalize_host_header(None), "")
        self.assertEqual(admin_domains.normalize_host_header("   "), "")
        self.assertEqual(admin_domains.normalize_host_header("[::1"), "")


class IsLoopbackHostTest(unittest.TestCase):
    def test_loopback_hosts(self):
        for host in ("localhost", "127.0.0.1", "127.0.0.2", "::1"):
            self.assertTrue(admin_domains.is_loopback_host(host), host)

    def test_non_loopback_hosts(self):
        for host in ("127.evil.com", "0.0.0.0", "203.0.113.9", "example.com", ""):
            self.assertFalse(admin_domains.is_loopback_host(host), host)


class IsAdminAccessTest(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(
            admin_domains, "get_admin_domains", return_value={"admin.test", "alt.test"}
        )
        self.addCleanup(patcher.stop)
        self.mocked_domains = patcher.start()

    def test_exact_whitelist_match(self):
        self.assertTrue(admin_domains.is_admin_access(FakeRequest("admin.test")))
        self.assertTrue(admin_domains.is_admin_access(FakeRequest("alt.test")))

    def test_whitelist_match_with_port_case_and_trailing_dot(self):
        self.assertTrue(
            admin_domains.is_admin_access(FakeRequest("ADMIN.TEST:8443"))
        )
        self.assertTrue(admin_domains.is_admin_access(FakeRequest("alt.test.")))

    def test_unknown_or_forged_host_rejected(self):
        for host in (
            "evil.test",
            "admin.test.evil.test",   # 后缀伪造
            "xadmin.test",            # 前缀伪造
            "admin.test,evil.test",   # 多 Host 注入
            "*",
            "",
        ):
            self.assertFalse(
                admin_domains.is_admin_access(FakeRequest(host)), host
            )

    def test_missing_host_header_rejected(self):
        self.assertFalse(admin_domains.is_admin_access(FakeRequest(None)))

    def test_forwarded_host_header_is_not_trusted(self):
        # Host 不在白名单，即使伪造 X-Forwarded-Host 也不能获得后台权限
        self.assertFalse(
            admin_domains.is_admin_access(
                FakeRequest("evil.test", forwarded_host="admin.test")
            )
        )

    def test_loopback_host_requires_loopback_client(self):
        self.assertFalse(
            admin_domains.is_admin_access(
                FakeRequest("localhost", client_host="203.0.113.9")
            )
        )
        self.assertFalse(
            admin_domains.is_admin_access(
                FakeRequest("127.0.0.1", client_host="testclient")
            )
        )

    def test_loopback_host_allowed_for_local_client(self):
        self.assertTrue(
            admin_domains.is_admin_access(
                FakeRequest("localhost", client_host="127.0.0.1")
            )
        )
        self.assertTrue(
            admin_domains.is_admin_access(
                FakeRequest("127.0.0.1:8000", client_host="127.0.0.1")
            )
        )
        self.assertTrue(
            admin_domains.is_admin_access(
                FakeRequest("[::1]:8000", client_host="::1")
            )
        )

    def test_deleted_domain_loses_access(self):
        self.assertTrue(admin_domains.is_admin_access(FakeRequest("alt.test")))
        self.mocked_domains.return_value = {"admin.test"}
        self.assertFalse(admin_domains.is_admin_access(FakeRequest("alt.test")))

    def test_no_whitelist_rejects_public_domain(self):
        self.mocked_domains.return_value = set()
        self.assertFalse(admin_domains.is_admin_access(FakeRequest("admin.test")))
        # 本机开发仍可访问
        self.assertTrue(
            admin_domains.is_admin_access(
                FakeRequest("127.0.0.1", client_host="127.0.0.1")
            )
        )


if __name__ == "__main__":
    unittest.main()
