# -*- coding: utf-8 -*-
"""第二阶段安全加固测试：密码、Session、CSRF、权限、限流、响应头、注入。"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient

import app.auth as auth
import app.database as database
import app.image_utils as image_utils
import app.ip_geo as ip_geo
import app.models as models
import app.routes.admin as admin_routes
from app.main import app
from app.password_policy import validate_password

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


class PasswordPolicyTest(unittest.TestCase):
    def test_valid_passwords(self):
        self.assertEqual(validate_password("Strong-Password-2026"), "")
        self.assertEqual(validate_password("这是一个很长很安全的密码"), "")

    def test_rejects_short_all_space_and_common(self):
        self.assertIn("12", validate_password("Short-123"))
        self.assertIn("空格", validate_password(" " * 12))
        self.assertIn("常见", validate_password("password1234"))

    def test_rejects_repeated_patterns(self):
        self.assertIn("重复", validate_password("aaaaaaaaaaaa"))
        self.assertIn("重复", validate_password("abcdabcdabcd"))

    def test_rejects_bcrypt_over_72_bytes_without_truncation(self):
        password = "密" * 40  # 120 UTF-8 bytes
        self.assertIn("72", validate_password(password))
        with self.assertRaises(ValueError):
            auth.hash_password(password)

    def test_bcrypt_hash_is_used(self):
        hashed = auth.hash_password("Strong-Password-2026")
        self.assertTrue(hashed.startswith("$2"))
        self.assertTrue(auth.verify_password("Strong-Password-2026", hashed))
        self.assertFalse(auth.verify_password("Wrong-Password-2026", hashed))


class Stage2SecurityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="amig-stage2-")
        cls._orig_db = database.DATABASE_PATH
        cls._orig_uploads = image_utils.UPLOADS_DIR
        cls._orig_private = image_utils.PRIVATE_UPLOADS_DIR
        cls._orig_admin_uploads = admin_routes.UPLOADS_DIR
        database.close_db()
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
        cls.add_admin("admin", cls.admin_password)

    @classmethod
    def tearDownClass(cls):
        database.close_db()
        database.DATABASE_PATH = cls._orig_db
        image_utils.UPLOADS_DIR = cls._orig_uploads
        image_utils.PRIVATE_UPLOADS_DIR = cls._orig_private
        admin_routes.UPLOADS_DIR = cls._orig_admin_uploads
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    @classmethod
    def add_admin(cls, username, password):
        conn = database.get_db()
        conn.execute(
            "INSERT INTO admins (username, password_hash, created_at) VALUES (?,?,?)",
            (username, auth.hash_password(password), models.now_iso()),
        )
        conn.commit()
        return conn.execute(
            "SELECT id FROM admins WHERE username=?", (username,)
        ).fetchone()["id"]

    def _client(self, scheme="https", host="admin.test"):
        return TestClient(
            app, base_url=f"{scheme}://{host}", raise_server_exceptions=False
        )

    def _csrf(self, client, path):
        response = client.get(path)
        self.assertEqual(response.status_code, 200, response.text[:300])
        match = CSRF_RE.search(response.text)
        self.assertIsNotNone(match, f"{path} 页面缺少 csrf_token")
        return match.group(1)

    def _login(self, client, username=None, password=None, ip=None):
        username = username or "admin"
        password = password or self.admin_password
        token = self._csrf(client, "/admin/login")
        if ip:
            with patch.object(admin_routes, "get_client_ip", return_value=ip):
                response = client.post(
                    "/admin/login",
                    data={"username": username, "password": password, "csrf_token": token},
                    follow_redirects=False,
                )
        else:
            response = client.post(
                "/admin/login",
                data={"username": username, "password": password, "csrf_token": token},
                follow_redirects=False,
            )
        return response

    def _login_ok(self, client, username=None, password=None, ip=None):
        response = self._login(client, username, password, ip)
        self.assertEqual(response.status_code, 302, response.text[:300])
        return response

    def _insert_approved(self, title="安全测试图片"):
        image_id = models.insert_image(
            "stage2.webp", "stage2_thumb.webp", 100, 50, 10, 5,
            "描述", "", f"md5-{title}-{datetime.now(timezone.utc).timestamp()}",
            "1.2.3.4", "ua", "uah", "", "", title=title,
        )
        conn = database.get_db()
        conn.execute(
            "UPDATE images SET status='approved', reviewed_at=? WHERE id=?",
            (models.now_iso(), image_id),
        )
        conn.commit()
        return image_id

    def setUp(self):
        auth.clear_login_rate_limits()

    # ---------- 密码修改 ----------

    def test_01_change_password_requires_current_and_rotates_session(self):
        username = "pw-stage2"
        old_password = "Old-Password-123"
        new_password = "New-Password-456"
        self.add_admin(username, old_password)
        client = self._client()
        self._login_ok(client, username, old_password)
        old_session = client.cookies.get("admin_session")
        old_token = self._csrf(client, "/admin/change-password")

        # 错误当前密码 / 缺失当前密码 / 弱密码 / 确认不一致都必须拒绝
        for data in (
            {"current_password": "Wrong-Password-999", "new_password": new_password,
             "confirm_password": new_password},
            {"new_password": new_password, "confirm_password": new_password},
            {"current_password": old_password, "new_password": "Short-123",
             "confirm_password": "Short-123"},
            {"current_password": old_password, "new_password": new_password,
             "confirm_password": "Different-Password-789"},
        ):
            response = client.post(
                "/admin/change-password",
                data={"csrf_token": old_token, **data},
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn("alert-error", response.text)
        self.assertTrue(
            database.get_db().execute(
                "SELECT 1 FROM sessions WHERE session_id=?", (old_session,)
            ).fetchone()
        )

        response = client.post(
            "/admin/change-password",
            data={
                "csrf_token": old_token,
                "current_password": old_password,
                "new_password": new_password,
                "confirm_password": new_password,
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("密码修改成功", response.text)
        new_session = client.cookies.get("admin_session")
        self.assertNotEqual(new_session, old_session)
        self.assertIsNone(
            database.get_db().execute(
                "SELECT 1 FROM sessions WHERE session_id=?", (old_session,)
            ).fetchone()
        )
        new_token = database.get_db().execute(
            "SELECT csrf_token FROM sessions WHERE session_id=?", (new_session,)
        ).fetchone()["csrf_token"]
        self.assertNotEqual(new_token, old_token)

        # 旧密码失效，新密码可登录
        old_client = self._client()
        old_login = self._login(old_client, username, old_password)
        self.assertEqual(old_login.status_code, 200)
        new_client = self._client()
        self._login_ok(new_client, username, new_password)

    # ---------- Session ----------

    def test_02_session_idle_and_absolute_expiry(self):
        admin_id = self.add_admin("session-stage2", "Session-Password-123")
        sid = auth.create_session(admin_id, "session-stage2")
        self.assertIsNotNone(auth.get_session(sid))

        conn = database.get_db()
        old = (
            datetime.now(timezone.utc)
            - timedelta(minutes=auth.SESSION_IDLE_MINUTES + 1)
        ).isoformat(timespec="seconds")
        conn.execute("UPDATE sessions SET last_seen_at=? WHERE session_id=?", (old, sid))
        conn.commit()
        auth._session_touch.pop(sid, None)
        self.assertIsNone(auth.get_session(sid))
        self.assertIsNone(
            conn.execute("SELECT 1 FROM sessions WHERE session_id=?", (sid,)).fetchone()
        )

        sid = auth.create_session(admin_id, "session-stage2")
        expired = (
            datetime.now(timezone.utc) - timedelta(minutes=1)
        ).isoformat(timespec="seconds")
        conn.execute("UPDATE sessions SET expires_at=? WHERE session_id=?", (expired, sid))
        conn.commit()
        auth._session_touch.pop(sid, None)
        self.assertIsNone(auth.get_session(sid))

    def test_03_login_rotates_fixated_session_and_cookie_flags(self):
        admin_id = self.add_admin("fixation-stage2", "Fixation-Password-123")
        client = self._client()
        token = self._csrf(client, "/admin/login")
        old_session = auth.create_session(admin_id, "fixation-stage2")
        client.cookies.set("admin_session", old_session)
        response = client.post(
            "/admin/login",
            data={"username": "fixation-stage2", "password": "Fixation-Password-123",
                  "csrf_token": token},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        cookies = response.headers.get_list("set-cookie")
        admin_cookie = next(item for item in cookies if item.startswith("admin_session="))
        new_session = admin_cookie.split(";", 1)[0].split("=", 1)[1]
        self.assertNotEqual(new_session, old_session)
        self.assertIsNone(
            database.get_db().execute(
                "SELECT 1 FROM sessions WHERE session_id=?", (old_session,)
            ).fetchone()
        )

        header_lower = admin_cookie.lower()
        self.assertIn("httponly", header_lower)
        self.assertIn("secure", header_lower)
        self.assertIn("samesite=lax", header_lower)
        self.assertIn("path=/", header_lower)
        self.assertIn("max-age=", header_lower)

    # ---------- CSRF / Origin ----------

    def test_04_csrf_token_is_bound_to_session(self):
        client = self._client()
        self._login_ok(client)
        valid_token = self._csrf(client, "/admin/links")
        other_admin_id = self.add_admin("csrf-other", "Csrf-Other-1234")
        other_sid = auth.create_session(other_admin_id, "csrf-other")
        other_token = auth.get_session(other_sid)["csrf_token"]
        cookie_token = client.cookies.get("csrf_token")
        self.assertNotEqual(valid_token, cookie_token)

        before = database.get_db().execute("SELECT COUNT(*) FROM friend_links").fetchone()[0]
        for token in (other_token, cookie_token, "", "wrong-token"):
            response = client.post(
                "/admin/links/save",
                data={"csrf_token": token, "name": "bad", "url": "https://example.com",
                      "sort_order": "0", "is_visible": "1"},
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 302)
        after = database.get_db().execute("SELECT COUNT(*) FROM friend_links").fetchone()[0]
        self.assertEqual(before, after)

        response = client.post(
            "/admin/links/save",
            data={"csrf_token": valid_token, "name": "csrf-ok",
                  "url": "https://example.com", "sort_order": "0", "is_visible": "1"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertIsNotNone(
            database.get_db().execute(
                "SELECT 1 FROM friend_links WHERE name='csrf-ok'"
            ).fetchone()
        )

    def test_05_cross_site_origin_rejected(self):
        image_id = self._insert_approved("origin-image")
        client = self._client()
        token = self._csrf(client, "/admin/login")
        response = client.post(
            "/admin/login",
            data={"username": "admin", "password": self.admin_password, "csrf_token": token},
            headers={"Origin": "https://evil.example"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 403)

        response = client.post(
            f"/api/like/{image_id}", headers={"Origin": "https://evil.example"}
        )
        self.assertEqual(response.status_code, 403)

        response = client.post(
            "/upload", headers={"Origin": "https://evil.example"}, data={}
        )
        self.assertEqual(response.status_code, 403)

        # 同源 Origin 正常放行
        response = client.post(
            "/admin/login",
            data={"username": "admin", "password": self.admin_password, "csrf_token": token},
            headers={"Origin": "https://admin.test"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)

    # ---------- 登录限流 ----------

    def test_06_login_rate_limit_is_db_backed_and_not_globally_locking(self):
        # 无效 CSRF 不消耗失败额度
        auth.clear_login_rate_limits()
        client = self._client()
        page = client.get("/admin/login")
        for _ in range(6):
            client.post(
                "/admin/login",
                data={"username": "admin", "password": "wrong", "csrf_token": "bad"},
                follow_redirects=False,
            )
        self.assertIsNone(
            database.get_db().execute(
                "SELECT 1 FROM login_rate_limits WHERE key='ip:testclient'"
            ).fetchone()
        )

        # 同一 IP 连续真实失败达到阈值后被拒
        auth.clear_login_rate_limits()
        client = self._client()
        token = self._csrf(client, "/admin/login")
        for _ in range(auth.LOGIN_IP_MAX_ATTEMPTS):
            response = client.post(
                "/admin/login",
                data={"username": "admin", "password": "Wrong-Password-999",
                      "csrf_token": token},
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 200)
        response = client.post(
            "/admin/login",
            data={"username": "admin", "password": self.admin_password,
                  "csrf_token": token},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("尝试次数过多", response.text)

        # 多 IP 攻击不能把账户全局锁死：不同 IP 各失败 3 次后，新 IP 仍可正确登录
        auth.clear_login_rate_limits()
        for index in range(3):
            attacker = self._client()
            attacker_token = self._csrf(attacker, "/admin/login")
            with patch.object(admin_routes, "get_client_ip", return_value=f"203.0.113.{index}"):
                for _ in range(3):
                    attacker.post(
                        "/admin/login",
                        data={"username": "admin", "password": "Wrong-Password-999",
                              "csrf_token": attacker_token},
                        follow_redirects=False,
                    )
        good = self._client()
        response = self._login(good, ip="198.51.100.7")
        self.assertEqual(response.status_code, 302)

    # ---------- 权限 ----------

    def test_07_all_admin_routes_require_authentication(self):
        paths = []
        for route in admin_routes.router.routes:
            path = getattr(route, "path", "")
            if not path.startswith("/admin") or "/admin/login" in path:
                continue
            for method in getattr(route, "methods", set()) or set():
                if method in ("GET", "POST"):
                    paths.append((method, path))
        self.assertTrue(paths)
        client = self._client()
        for method, path in paths:
            resolved = re.sub(r"\{[^}]+\}", "1", path)
            with self.subTest(method=method, path=resolved):
                if method == "GET":
                    response = client.get(resolved, follow_redirects=False)
                else:
                    response = client.post(resolved, data={}, follow_redirects=False)
                self.assertIn(response.status_code, (302, 403, 404, 405),
                              f"{method} {resolved} 返回 {response.status_code}")

    # ---------- 响应头 ----------

    def test_08_security_headers_https_and_http(self):
        http_client = self._client(scheme="http")
        response = http_client.get("/")
        csp = response.headers["content-security-policy"]
        self.assertIn("form-action 'self'", csp)
        self.assertIn("object-src 'none'", csp)
        self.assertIn("script-src 'self' 'nonce-", csp)
        self.assertNotIn("script-src 'self' 'unsafe-inline'", csp)
        self.assertIn("style-src 'self' 'nonce-", csp)
        self.assertNotIn("style-src 'self' 'unsafe-inline'", csp)
        self.assertIn("Permissions-Policy", response.headers)
        self.assertNotIn("Strict-Transport-Security", response.headers)

        # 页面内联脚本/样式必须携带与响应头一致的 nonce，否则会被 CSP 阻止
        upload_page = http_client.get("/upload")
        page_csp = upload_page.headers["content-security-policy"]
        match = re.search(r"'nonce-([^']+)'", page_csp)
        self.assertIsNotNone(match)
        self.assertIn(f'nonce="{match.group(1)}"', upload_page.text)
        self.assertIn(f'<style nonce="{match.group(1)}">', upload_page.text)

        https_client = self._client()
        response = https_client.get("/")
        self.assertIn("upgrade-insecure-requests", response.headers["content-security-policy"])
        self.assertIn("Strict-Transport-Security", response.headers)

        # 不受信任来源伪造 X-Forwarded-Proto 不能触发 HSTS
        response = http_client.get("/", headers={"X-Forwarded-Proto": "https"})
        self.assertNotIn("Strict-Transport-Security", response.headers)

        original = set(ip_geo.TRUSTED_PROXIES)
        ip_geo.TRUSTED_PROXIES.add("testclient")
        try:
            response = http_client.get("/", headers={"X-Forwarded-Proto": "https"})
            self.assertIn("Strict-Transport-Security", response.headers)
        finally:
            ip_geo.TRUSTED_PROXIES.clear()
            ip_geo.TRUSTED_PROXIES.update(original)

    # ---------- IP 归属地 ----------

    def test_09_ip_geo_backoff_and_no_ssrf(self):
        ip_geo._reset_failure_backoff()
        with patch.object(ip_geo.urllib.request, "urlopen", side_effect=RuntimeError("down")) as mocked:
            self.assertEqual(ip_geo._lookup("1.1.1.1"), ("", "", ""))
            calls = mocked.call_count
            # 退避期内不再发起请求
            self.assertEqual(ip_geo._lookup("8.8.8.8"), ("", "", ""))
            self.assertEqual(mocked.call_count, calls)
        ip_geo._reset_failure_backoff()

        with patch.object(ip_geo.urllib.request, "urlopen") as mocked:
            self.assertEqual(ip_geo._lookup("127.0.0.1"), ("", "", ""))
            self.assertEqual(ip_geo._lookup("not-an-ip"), ("", "", ""))
            mocked.assert_not_called()

    def test_11_review_batch_route_is_reachable(self):
        first = models.insert_image(
            "batch1.webp", "batch1_thumb.webp", 1, 1, 1, 1, "d", "", "md5-batch-1",
            "1.2.3.4", "ua", "uah1", "", "", title="批量一",
        )
        second = models.insert_image(
            "batch2.webp", "batch2_thumb.webp", 1, 1, 1, 1, "d", "", "md5-batch-2",
            "1.2.3.4", "ua", "uah2", "", "", title="批量二",
        )
        # 待审文件在私有目录；批量通过后应被移动到公开目录
        for name in ("batch1.webp", "batch1_thumb.webp",
                     "batch2.webp", "batch2_thumb.webp"):
            with open(os.path.join(image_utils.PRIVATE_UPLOADS_DIR, name), "wb") as f:
                f.write(b"RIFF\x00\x00\x00\x00WEBPVP8 ")
        client = self._client()
        self._login_ok(client)
        token = self._csrf(client, "/admin/review")
        response = client.post(
            "/admin/review/batch",
            data={"csrf_token": token, "ids": f"{first},{second}", "action": "approved"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        conn = database.get_db()
        for image_id in (first, second):
            row = conn.execute("SELECT status FROM images WHERE id=?", (image_id,)).fetchone()
            self.assertEqual(row["status"], "approved")
        for name in ("batch1.webp", "batch1_thumb.webp",
                     "batch2.webp", "batch2_thumb.webp"):
            self.assertTrue(
                os.path.isfile(os.path.join(image_utils.UPLOADS_DIR, name)),
                f"审核通过后文件应移动到公开目录：{name}",
            )
            self.assertFalse(
                os.path.exists(os.path.join(image_utils.PRIVATE_UPLOADS_DIR, name)),
                f"审核通过后私有目录不应残留文件：{name}",
            )

    # ---------- XSS / SQL ----------

    def test_10_xss_and_sql_injection_regression(self):
        payload = '<script>alert("xss")</script>'
        image_id = self._insert_approved(payload)
        client = self._client()
        detail = client.get(f"/image/{image_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertNotIn(payload, detail.text)
        self.assertIn("&lt;script&gt;", detail.text)

        search = client.get("/gallery", params={"q": "' OR 1=1 --"})
        self.assertEqual(search.status_code, 200)
        self.assertNotIn("OperationalError", search.text)

        rows = models.get_images_by_status(
            "approved", order_by="id; DROP TABLE images"
        )
        self.assertGreaterEqual(len(rows), 1)
        self.assertIsNotNone(
            database.get_db().execute("SELECT 1 FROM images LIMIT 1").fetchone()
        )

        admin_client = self._client()
        self._login_ok(admin_client)
        response = admin_client.get(
            "/admin/images", params={"sort": "id; DROP TABLE images"}
        )
        self.assertEqual(response.status_code, 200)


class DatabaseInitRetryTest(unittest.TestCase):
    def test_retries_on_lock_and_succeeds(self):
        import sqlite3
        with patch.object(
            database, "_init_db_once",
            side_effect=[sqlite3.OperationalError("database is locked"), None],
        ) as mocked, patch.object(database.time, "sleep") as sleeper:
            database.init_db(max_wait_seconds=1)
        self.assertEqual(mocked.call_count, 2)
        sleeper.assert_called_once()

    def test_does_not_retry_unrelated_errors(self):
        import sqlite3
        with patch.object(
            database, "_init_db_once",
            side_effect=sqlite3.OperationalError("no such table: images"),
        ) as mocked:
            with self.assertRaises(sqlite3.OperationalError):
                database.init_db(max_wait_seconds=1)
        self.assertEqual(mocked.call_count, 1)


class LegacySessionMigrationTest(unittest.TestCase):
    def test_old_sessions_get_new_columns_and_csrf_token(self):
        tmpdir = tempfile.mkdtemp(prefix="amig-session-migration-")
        db_path = os.path.join(tmpdir, "app.db")
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, admin_id INTEGER NOT NULL, "
            "username TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL)"
        )
        now = datetime.now(timezone.utc)
        conn.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?)",
            (
                "legacy-session", 1, "legacy-admin",
                now.isoformat(timespec="seconds"),
                (now + timedelta(hours=1)).isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        conn.close()

        original = database.DATABASE_PATH
        database.close_db()
        database.DATABASE_PATH = db_path
        try:
            database.init_db()
            columns = {
                row["name"]
                for row in database.get_db().execute("PRAGMA table_info(sessions)").fetchall()
            }
            self.assertIn("csrf_token", columns)
            self.assertIn("last_seen_at", columns)
            row = database.get_db().execute(
                "SELECT last_seen_at FROM sessions WHERE session_id='legacy-session'"
            ).fetchone()
            self.assertTrue(row["last_seen_at"])

            session = auth.get_session("legacy-session")
            self.assertIsNotNone(session)
            self.assertTrue(session["csrf_token"])
            db_token = database.get_db().execute(
                "SELECT csrf_token FROM sessions WHERE session_id='legacy-session'"
            ).fetchone()["csrf_token"]
            self.assertEqual(session["csrf_token"], db_token)
        finally:
            database.close_db()
            database.DATABASE_PATH = original
            shutil.rmtree(tmpdir, ignore_errors=True)


class ResetPasswordScriptTest(unittest.TestCase):
    def test_reset_script_stdin_revokes_sessions_and_hides_password(self):
        tmpdir = tempfile.mkdtemp(prefix="amig-reset-")
        db_path = os.path.join(tmpdir, "app.db")
        original = database.DATABASE_PATH
        database.close_db()
        database.DATABASE_PATH = db_path
        try:
            database.init_db()
            admin_id = database.get_db().execute(
                "INSERT INTO admins (username, password_hash, created_at) VALUES (?,?,?)",
                ("admin", auth.hash_password("Old-Password-123"), models.now_iso()),
            ).lastrowid
            database.get_db().execute(
                "INSERT INTO sessions (session_id, admin_id, username, created_at, expires_at, "
                "last_seen_at, csrf_token) VALUES (?,?,?,?,?,?,?)",
                ("old-session", admin_id, "admin", models.now_iso(), models.now_iso(),
                 models.now_iso(), "old-token"),
            )
            database.get_db().commit()
        finally:
            database.close_db()
            database.DATABASE_PATH = original

        new_password = "Reset-Password-789"
        env = dict(os.environ, DATABASE_PATH=db_path)
        result = subprocess.run(
            [sys.executable, "reset_password.py", "--stdin", "--yes"],
            input=new_password + "\n",
            capture_output=True,
            text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            env=env,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn(new_password, result.stdout)
        self.assertNotIn("$2b$", result.stdout)

        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT password_hash, login_attempts, locked_until FROM admins").fetchone()
        self.assertTrue(auth.verify_password(new_password, row["password_hash"]))
        self.assertEqual(row["login_attempts"], 0)
        self.assertIsNone(row["locked_until"])
        self.assertIsNone(conn.execute("SELECT 1 FROM sessions").fetchone())
        conn.close()
        shutil.rmtree(tmpdir, ignore_errors=True)

        rejected = subprocess.run(
            [sys.executable, "reset_password.py", "--password", "x", "--yes"],
            capture_output=True, text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            timeout=30,
        )
        self.assertEqual(rejected.returncode, 1)
        self.assertIn("已移除 --password", rejected.stdout)
