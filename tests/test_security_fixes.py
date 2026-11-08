"""1.0.8 安全性与稳定性修复的回归测试"""
import asyncio
import html
import io
import os
import re
import shutil
import tempfile
import time
import unittest
from html.parser import HTMLParser
from unittest.mock import patch

from PIL import Image
from fastapi.testclient import TestClient

import app.auth as auth
import app.database as database
import app.image_utils as image_utils
import app.models as models
import app.routes.admin as admin_routes
import app.routes.front as front
from app.auth import hash_password
from app.main import app

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


class _StartTagCollector(HTMLParser):
    """收集所有起始标签及其属性，用于校验 data-* 属性"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def _image_bytes(fmt="PNG", size=(10, 10)):
    buf = io.BytesIO()
    Image.new("RGB", size, (40, 120, 220)).save(buf, format=fmt)
    return buf.getvalue()


def _list_files(root):
    result = set()
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            result.add(os.path.join(dirpath, name))
    return result


class TestPixelLimit(unittest.TestCase):
    """解压炸弹防护：警告升级为异常，且解码前校验像素总数"""

    def test_over_limit_rejected_before_decode(self):
        data = _image_bytes(size=(200, 100))  # 20000 像素
        with patch.object(image_utils, "MAX_IMAGE_PIXELS", 100):
            result, error = image_utils.process_image(data)
        self.assertIsNone(result)
        self.assertEqual(error, image_utils.PIXEL_LIMIT_ERROR)

    def test_pillow_bomb_warning_escalated_to_error(self):
        data = _image_bytes(size=(20, 10))  # 200 像素：超过上限但未达 2 倍
        with patch.object(Image, "MAX_IMAGE_PIXELS", 100):
            # 走应用自己的打开路径，确保不依赖全局 warnings 过滤器是否被重置
            with self.assertRaises(Image.DecompressionBombWarning):
                image_utils._open_image_checked(data)

    def test_pillow_bomb_error_rejected(self):
        data = _image_bytes(size=(300, 100))  # 30000 像素：超过 2 倍上限
        with patch.object(Image, "MAX_IMAGE_PIXELS", 100):
            with self.assertRaises(Image.DecompressionBombError):
                image_utils._open_image_checked(data)

    def test_process_image_catches_escalated_warning(self):
        data = _image_bytes(size=(200, 100))
        with patch.object(Image, "MAX_IMAGE_PIXELS", 100), \
             patch.object(image_utils, "MAX_IMAGE_PIXELS", 100):
            result, error = image_utils.process_image(data)
        self.assertIsNone(result)
        self.assertEqual(error, image_utils.PIXEL_LIMIT_ERROR)

    def test_image_within_limit_is_processed(self):
        data = _image_bytes(size=(10, 10))
        tmpdir = tempfile.mkdtemp()
        try:
            with patch.object(image_utils, "UPLOADS_DIR", tmpdir):
                result, error = image_utils.process_image(data)
            self.assertEqual(error, "")
            self.assertIsNotNone(result)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_process_image_cleans_files_when_write_fails(self):
        """文件写入失败时应清理已落盘的部分文件，避免孤儿文件"""
        data = _image_bytes(size=(10, 10))
        tmpdir = tempfile.mkdtemp()
        real_write = image_utils._atomic_write
        calls = {"count": 0}

        def flaky_write(path, payload):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("disk full")
            real_write(path, payload)

        try:
            with patch.object(image_utils, "UPLOADS_DIR", tmpdir), \
                 patch.object(image_utils, "_atomic_write", side_effect=flaky_write):
                result, error = image_utils.process_image(data)
            self.assertIsNone(result)
            self.assertEqual(error, "图片处理失败")
            self.assertEqual(_list_files(tmpdir), set())
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class _FakeUpload:
    """模拟 UploadFile，按请求大小分块返回数据"""

    def __init__(self, filename, data):
        self.filename = filename
        self._data = data
        self.pos = 0
        self.read_calls = 0

    async def read(self, size=-1):
        self.read_calls += 1
        if size is None or size < 0:
            size = len(self._data) - self.pos
        chunk = self._data[self.pos:self.pos + size]
        self.pos += len(chunk)
        return chunk


class TestLogoSave(unittest.TestCase):
    """Logo 上传应分块读取，超限立即终止并清理临时文件"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_uploads = admin_routes.UPLOADS_DIR
        admin_routes.UPLOADS_DIR = self.tmpdir

    def tearDown(self):
        admin_routes.UPLOADS_DIR = self._orig_uploads
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_small_logo_saved(self):
        upload = _FakeUpload("logo.png", b"abc")
        result = asyncio.run(admin_routes._save_logo(upload, "logo_large"))
        self.assertIsNotNone(result)
        self.assertTrue(os.path.isfile(os.path.join(self.tmpdir, result)))
        with open(os.path.join(self.tmpdir, result), "rb") as f:
            self.assertEqual(f.read(), b"abc")
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, result + ".tmp")))

    def test_invalid_extension_rejected_without_reading(self):
        upload = _FakeUpload("logo.svg", b"<svg/>")
        result = asyncio.run(admin_routes._save_logo(upload, "logo_small"))
        self.assertIsNone(result)
        self.assertEqual(upload.read_calls, 0)

    def test_oversized_logo_aborts_early_and_cleans_temp(self):
        data = b"x" * (admin_routes.MAX_UPLOAD_SIZE + 5 * 1024 * 1024)
        upload = _FakeUpload("logo.png", data)
        result = asyncio.run(admin_routes._save_logo(upload, "logo_large"))
        self.assertIsNone(result)
        # 超过上限后立即停止，未把全部内容读入内存
        self.assertLessEqual(upload.pos, admin_routes.MAX_UPLOAD_SIZE + 1024 * 1024)
        self.assertEqual(_list_files(self.tmpdir), set())

    def test_empty_logo_rejected(self):
        upload = _FakeUpload("logo.png", b"")
        result = asyncio.run(admin_routes._save_logo(upload, "logo_large"))
        self.assertIsNone(result)
        self.assertEqual(_list_files(self.tmpdir), set())


class TestLoginIpCleanup(unittest.TestCase):
    """登录 IP 限流缓存应清理过期记录"""

    def setUp(self):
        with auth._login_ip_lock:
            auth._login_ip_attempts.clear()
            auth._last_login_ip_cleanup = 0.0

    def tearDown(self):
        with auth._login_ip_lock:
            auth._login_ip_attempts.clear()
            auth._last_login_ip_cleanup = 0.0

    def test_cleanup_removes_expired_entries(self):
        now = time.time()
        auth._login_ip_attempts["old-ip"] = [2, now - auth._LOGIN_IP_WINDOW_SECONDS - 1]
        auth._login_ip_attempts["fresh-ip"] = [1, now]
        auth._cleanup_login_ip_attempts(now)
        self.assertNotIn("old-ip", auth._login_ip_attempts)
        self.assertIn("fresh-ip", auth._login_ip_attempts)

    def test_periodic_cleanup_triggered_by_limit_check(self):
        now = time.time()
        auth._login_ip_attempts["expired-ip"] = [1, now - auth._LOGIN_IP_WINDOW_SECONDS - 1]
        auth._last_login_ip_cleanup = 0.0
        self.assertTrue(auth.check_login_ip_limit("new-ip"))
        self.assertNotIn("expired-ip", auth._login_ip_attempts)


class TestSecurityIntegration(unittest.TestCase):
    """面向关键安全边界的端到端测试"""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="amig-sec-")
        cls.db_path = os.path.join(cls.tmpdir, "app.db")
        cls.upload_dir = os.path.join(cls.tmpdir, "uploads")
        cls._orig_db = database.DATABASE_PATH
        cls._orig_uploads = image_utils.UPLOADS_DIR
        cls._orig_admin_uploads = admin_routes.UPLOADS_DIR
        database.close_db()
        database.DATABASE_PATH = cls.db_path
        image_utils.UPLOADS_DIR = cls.upload_dir
        admin_routes.UPLOADS_DIR = cls.upload_dir
        database.init_db()
        models.set_setting("admin_domain", "admin.test")
        models.set_setting("enable_captcha", "0")
        models.set_setting("enable_dedup", "0")

    @classmethod
    def tearDownClass(cls):
        database.close_db()
        database.DATABASE_PATH = cls._orig_db
        image_utils.UPLOADS_DIR = cls._orig_uploads
        admin_routes.UPLOADS_DIR = cls._orig_admin_uploads
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def client(self, host="admin.test"):
        return TestClient(app, base_url=f"https://{host}", raise_server_exceptions=False)

    def csrf(self, response):
        match = CSRF_RE.search(response.text)
        self.assertIsNotNone(match, "页面未提供 CSRF 表单令牌")
        return match.group(1)

    def add_admin(self, username, password):
        conn = database.get_db()
        conn.execute(
            "INSERT INTO admins (username, password_hash, created_at) VALUES (?,?,?)",
            (username, hash_password(password), models.now_iso()),
        )
        conn.commit()
        return conn.execute(
            "SELECT id FROM admins WHERE username=?", (username,)
        ).fetchone()["id"]

    def login(self, client, username, password):
        auth._login_ip_attempts.clear()
        page = client.get("/admin/login")
        self.assertEqual(page.status_code, 200)
        result = client.post(
            "/admin/login",
            data={"username": username, "password": password, "csrf_token": self.csrf(page)},
            follow_redirects=False,
        )
        self.assertEqual(result.status_code, 302, result.text)
        self.assertIn("admin_session", result.cookies)
        return result.cookies["admin_session"]

    def _clear_links(self):
        conn = database.get_db()
        conn.execute("DELETE FROM friend_links")
        conn.commit()

    def _save_link(self, client, token, name, url, sort_order=0, is_visible=1):
        response = client.post(
            "/admin/links/save",
            data={
                "csrf_token": token, "link_id": "", "name": name,
                "url": url, "sort_order": str(sort_order), "is_visible": str(is_visible),
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        conn = database.get_db()
        return conn.execute(
            "SELECT id FROM friend_links WHERE name=? ORDER BY id DESC LIMIT 1", (name,)
        ).fetchone()["id"]

    def _link_buttons(self, page_html):
        """解析页面中带 edit-link-btn 的按钮及其 data-* 属性"""
        parser = _StartTagCollector()
        parser.feed(page_html)
        return [
            attrs for tag, attrs in parser.tags
            if tag == "button" and "edit-link-btn" in attrs.get("class", "").split()
        ]

    def _unescape(self, value):
        return html.unescape(value)

    def test_friend_link_data_attributes_roundtrip_exact_values(self):
        """data-* 属性应完整承载数据库值，引号和标签不会突破属性边界"""
        self.add_admin("linkattruser", "pass")
        client = self.client()
        self.login(client, "linkattruser", "pass")
        self._clear_links()
        token = self.csrf(client.get("/admin/links"))

        name = '"><script>alert(1)</script>'
        url = 'https://example.com/?q="><svg onload=alert(2)>'
        link_id = self._save_link(client, token, name, url, sort_order=7, is_visible=0)

        page = client.get("/admin/links").text
        buttons = self._link_buttons(page)
        self.assertEqual(len(buttons), 1)
        attrs = buttons[0]

        self.assertEqual(attrs["data-link-id"], str(link_id))
        self.assertEqual(self._unescape(attrs["data-link-name"]), name)
        self.assertEqual(self._unescape(attrs["data-link-url"]), url)
        self.assertEqual(attrs["data-link-sort"], "7")
        self.assertEqual(attrs["data-link-visible"], "0")
        # 按钮上不应出现任何内联事件处理器
        self.assertFalse([key for key in attrs if key.startswith("on")])
        # 恶意内容只能作为转义后的属性值存在，不能成为真实标签
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertNotIn("<svg onload=alert(2)>", page)

    def test_friend_link_data_attributes_match_each_row(self):
        """多行链接的 data-* 属性应逐行对应，而不是共用同一份数据"""
        self.add_admin("linkrowuser", "pass")
        client = self.client()
        self.login(client, "linkrowuser", "pass")
        self._clear_links()
        token = self.csrf(client.get("/admin/links"))

        first_name = "第一行 <b>加粗</b>"
        second_name = '第二行 " & 引号'
        first_url = "https://example.com/first"
        second_url = 'https://example.com/second?x="y"'
        first_id = self._save_link(client, token, first_name, first_url, sort_order=1)
        second_id = self._save_link(client, token, second_name, second_url, sort_order=2)

        buttons = self._link_buttons(client.get("/admin/links").text)
        self.assertEqual(len(buttons), 2)
        by_id = {attrs["data-link-id"]: attrs for attrs in buttons}

        self.assertEqual(by_id[str(first_id)]["data-link-name"], self._unescape(first_name))
        self.assertEqual(by_id[str(first_id)]["data-link-url"], first_url)
        self.assertEqual(by_id[str(second_id)]["data-link-name"], self._unescape(second_name))
        self.assertEqual(by_id[str(second_id)]["data-link-url"], self._unescape(second_url))
        self.assertEqual(by_id[str(second_id)]["data-link-sort"], "2")

    def test_friend_link_inline_script_does_not_embed_user_data(self):
        """内联脚本不应包含数据库内容，只能通过 dataset 读取 data-*"""
        self.add_admin("linkscriptuser", "pass")
        client = self.client()
        self.login(client, "linkscriptuser", "pass")
        self._clear_links()
        token = self.csrf(client.get("/admin/links"))

        marker = "XSS_MARKER_UNIQUE"
        self._save_link(client, token, marker, f"https://example.com/{marker}")
        page = client.get("/admin/links").text

        inline_scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", page, re.S)
        self.assertTrue(inline_scripts, "页面应包含内联脚本")
        script_body = "\n".join(inline_scripts)
        self.assertNotIn(marker, script_body)
        self.assertNotIn("editLink(", script_body)
        self.assertIn("btn.dataset", script_body)
        self.assertIn("data.linkName", script_body)

    def test_change_password_revokes_other_sessions_and_reissues(self):
        admin_id = self.add_admin("pwuser", "old-pass")
        client = self.client()
        old_session = self.login(client, "pwuser", "old-pass")
        other_session = auth.create_session(admin_id, "pwuser")

        page = client.get("/admin/change-password")
        token = self.csrf(page)
        response = client.post(
            "/admin/change-password",
            data={"csrf_token": token, "new_password": "new-pass", "confirm_password": "new-pass"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("pwuser", response.text, "改密后页面丢失了当前管理员上下文")
        new_session = client.cookies.get("admin_session")
        self.assertNotEqual(new_session, old_session)

        conn = database.get_db()
        for sid in (old_session, other_session):
            self.assertIsNone(
                conn.execute("SELECT 1 FROM sessions WHERE session_id=?", (sid,)).fetchone()
            )
        self.assertIsNotNone(
            conn.execute("SELECT 1 FROM sessions WHERE session_id=?", (new_session,)).fetchone()
        )

        self.assertEqual(client.get("/admin/dashboard").status_code, 200)
        stale = self.client()
        stale.cookies.set("admin_session", old_session)
        stale_response = stale.get("/admin/dashboard", follow_redirects=False)
        self.assertEqual(stale_response.status_code, 302)
        self.assertEqual(stale_response.headers["location"], "/admin/login")

    def test_friend_links_use_data_attributes_and_escape_payload(self):
        self.add_admin("linkuser", "pass")
        client = self.client()
        self.login(client, "linkuser", "pass")
        token = self.csrf(client.get("/admin/links"))
        payload = "'); alert(1);//"
        bad_url = 'https://example.com/" onmouseover="alert(1)'
        response = client.post(
            "/admin/links/save",
            data={
                "csrf_token": token, "link_id": "", "name": payload,
                "url": bad_url, "sort_order": "0", "is_visible": "1",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)

        html = client.get("/admin/links").text
        self.assertNotIn('onclick="editLink(', html)
        self.assertIn("data-link-name=", html)
        self.assertIn("data.linkName", html)
        self.assertNotIn(payload, html)
        self.assertNotIn('onmouseover="alert(1)"', html)

    def test_logout_requires_post_and_csrf(self):
        self.add_admin("logoutuser", "pass")
        client = self.client()
        session_id = self.login(client, "logoutuser", "pass")

        self.assertEqual(client.get("/admin/logout", follow_redirects=False).status_code, 405)

        no_csrf = client.post("/admin/logout", data={}, follow_redirects=False)
        self.assertEqual(no_csrf.status_code, 302)
        self.assertEqual(client.get("/admin/dashboard", follow_redirects=False).status_code, 200)
        self.assertIsNotNone(
            database.get_db().execute(
                "SELECT 1 FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
        )

        token = self.csrf(client.get("/admin/dashboard"))
        response = client.post(
            "/admin/logout", data={"csrf_token": token}, follow_redirects=False
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/admin/login")
        self.assertIsNone(
            database.get_db().execute(
                "SELECT 1 FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
        )
        self.assertEqual(client.get("/admin/dashboard", follow_redirects=False).status_code, 302)

    def test_upload_cleans_files_when_database_insert_fails(self):
        client = self.client("upload.test")
        before = _list_files(self.upload_dir)
        with patch.object(front, "get_client_ip", return_value="10.0.1.1"), \
             patch.object(front, "get_ip_location", return_value=("", "")), \
             patch.object(front, "get_ip_region", return_value=""), \
             patch.object(models, "insert_image", side_effect=RuntimeError("db failed")):
            page = client.get("/upload")
            response = client.post(
                "/upload",
                data={"csrf_token": self.csrf(page), "title": "孤儿文件测试", "agree": "1"},
                files={"image": ("sample.png", _image_bytes(size=(40, 30)), "image/png")},
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("保存失败", response.text)
        self.assertEqual(_list_files(self.upload_dir), before)

    def _post_upload(self, client, ip, data, filename="sample.png", title="上传流程测试"):
        page = client.get("/upload")
        with patch.object(front, "get_client_ip", return_value=ip), \
             patch.object(front, "get_ip_location", return_value=("", "")), \
             patch.object(front, "get_ip_region", return_value=""):
            return client.post(
                "/upload",
                data={"csrf_token": self.csrf(page), "title": title, "agree": "1"},
                files={"image": (filename, data, "image/png")},
            )

    def test_upload_success_persists_pending_image_and_files(self):
        client = self.client("upload.test")
        ip = "10.0.2.1"
        response = self._post_upload(client, ip, _image_bytes(size=(60, 40)))
        self.assertEqual(response.status_code, 200)
        self.assertIn("已提交，等待审核通过后公开展示", response.text)
        row = database.get_db().execute(
            "SELECT * FROM images WHERE ip=? ORDER BY id DESC LIMIT 1", (ip,)
        ).fetchone()
        self.assertIsNotNone(row, "上传成功后数据库没有记录")
        self.assertEqual(row["status"], "pending")
        for name in (row["filename"], row["thumb_filename"]):
            self.assertTrue(
                os.path.isfile(os.path.join(self.upload_dir, name)),
                f"上传成功但文件缺失：{name}",
            )

    def test_upload_rejects_empty_file(self):
        client = self.client("upload.test")
        before = _list_files(self.upload_dir)
        response = self._post_upload(client, "10.0.2.2", b"")
        self.assertEqual(response.status_code, 200)
        self.assertIn("请选择图片文件", response.text)
        self.assertEqual(_list_files(self.upload_dir), before)

    def test_upload_rejects_unsupported_extension(self):
        client = self.client("upload.test")
        before = _list_files(self.upload_dir)
        response = self._post_upload(
            client, "10.0.2.3", b"GIF89a\x01\x00\x01\x00", filename="anim.gif"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("不支持的图片格式", response.text)
        self.assertEqual(_list_files(self.upload_dir), before)

    def test_upload_rejects_content_type_mismatch(self):
        client = self.client("upload.test")
        before = _list_files(self.upload_dir)
        response = self._post_upload(client, "10.0.2.4", b"not an image", filename="fake.png")
        self.assertEqual(response.status_code, 200)
        self.assertIn("图片格式与文件内容不匹配", response.text)
        self.assertEqual(_list_files(self.upload_dir), before)

    def test_upload_rejects_oversized_file_without_leaving_files(self):
        client = self.client("upload.test")
        before = _list_files(self.upload_dir)
        with patch.object(front, "MAX_UPLOAD_SIZE", 100):
            response = self._post_upload(client, "10.0.2.5", b"x" * 200)
        self.assertEqual(response.status_code, 200)
        self.assertIn("图片大小超过限制", response.text)
        self.assertEqual(_list_files(self.upload_dir), before)

    def test_upload_second_request_is_rate_limited(self):
        """连续两次提交：第二次应被上传间隔拦截，避免重复入库"""
        client = self.client("upload.test")
        ip = "10.0.2.6"
        first = self._post_upload(client, ip, _image_bytes(size=(50, 30)))
        self.assertIn("已提交", first.text)
        second = self._post_upload(client, ip, _image_bytes(size=(50, 30)))
        self.assertEqual(second.status_code, 200)
        self.assertIn("操作过于频繁", second.text)
        count = database.get_db().execute(
            "SELECT COUNT(*) AS c FROM images WHERE ip=?", (ip,)
        ).fetchone()["c"]
        self.assertEqual(count, 1, "重复提交产生了多条图片记录")

    def test_upload_requires_csrf_and_agreement(self):
        client = self.client("upload.test")
        page = client.get("/upload")
        no_csrf = client.post(
            "/upload",
            data={"title": "缺少令牌", "agree": "1"},
            files={"image": ("sample.png", _image_bytes(), "image/png")},
        )
        self.assertIn("请求无效", no_csrf.text)
        no_agree = client.post(
            "/upload",
            data={"csrf_token": self.csrf(page), "title": "未同意站规"},
            files={"image": ("sample.png", _image_bytes(), "image/png")},
        )
        self.assertIn("请先阅读并同意站规", no_agree.text)

    def test_upload_page_exposes_progress_cancel_and_local_validation(self):
        """前端上传体验的关键节点：本地校验、进度、取消、超时与防重复"""
        page = self.client("upload.test").get("/upload").text
        for marker in (
            'id="uploadProgress"', 'id="cancelUpload"', 'id="fileError"',
            "sniffType", "verifySignature", "xhr.abort()",
            "xhr.ontimeout", "xhr.onabort", "uploading",
        ):
            self.assertIn(marker, page, f"上传页缺少关键实现：{marker}")

    def test_admin_delete_keeps_files_when_database_delete_fails(self):
        image_id = models.insert_image(
            "2026/11/keep.webp", "2026/11/keep_thumb.webp",
            1000, 800, 100, 80, "", "", "md5-keep", "1.1.1.1", "ua", "uah",
            "", "",
        )
        paths = [
            os.path.join(self.upload_dir, "2026/11/keep.webp"),
            os.path.join(self.upload_dir, "2026/11/keep_thumb.webp"),
        ]
        os.makedirs(os.path.dirname(paths[0]), exist_ok=True)
        for path in paths:
            with open(path, "wb") as f:
                f.write(b"data")

        self.add_admin("deleteuser", "pass")
        client = self.client()
        self.login(client, "deleteuser", "pass")
        token = self.csrf(client.get(f"/admin/images/{image_id}"))

        with patch.object(admin_routes, "delete_image", side_effect=RuntimeError("db failed")):
            failed = client.post(
                f"/admin/images/{image_id}/delete",
                data={"csrf_token": token},
            )
        self.assertEqual(failed.status_code, 500)
        self.assertIsNotNone(models.get_image_by_id(image_id))
        for path in paths:
            self.assertTrue(os.path.isfile(path), f"数据库失败后文件被误删: {path}")

        success = client.post(
            f"/admin/images/{image_id}/delete",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        self.assertEqual(success.status_code, 302)
        self.assertIsNone(models.get_image_by_id(image_id))
        for path in paths:
            self.assertFalse(os.path.exists(path), f"数据库删除后文件未清理: {path}")

    def test_custom_page_content_is_escaped_and_keeps_newlines(self):
        self.add_admin("pageuser", "pass")
        client = self.client()
        self.login(client, "pageuser", "pass")
        token = self.csrf(client.get("/admin/pages"))
        content = "<script>alert('x')</script>\n第二行"
        response = client.post(
            "/admin/pages/save",
            data={
                "csrf_token": token, "page_id": "", "slug": "xss-page",
                "title": "转义页面", "content": content, "sort_order": "0", "is_visible": "1",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)

        public = client.get("/page/xss-page")
        self.assertEqual(public.status_code, 200)
        self.assertIn("&lt;script&gt;", public.text)
        self.assertNotIn("<script>alert", public.text)
        self.assertIn("第二行", public.text)


class DbUnitTestCase(unittest.TestCase):
    """使用独立临时数据库的单元测试基类"""

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.mkdtemp()
        cls._orig = database.DATABASE_PATH
        database.close_db()
        database.DATABASE_PATH = os.path.join(cls._tmpdir, "unit.db")
        database.init_db()

    @classmethod
    def tearDownClass(cls):
        database.close_db()
        database.DATABASE_PATH = cls._orig
        shutil.rmtree(cls._tmpdir, ignore_errors=True)

    def _insert_approved(self, md5, like_count):
        image_id = models.insert_image(
            f"{md5}.webp", f"{md5}_thumb.webp", 1000, 800, 100, 80,
            "", "", md5, md5, "ua", "uah", "", "",
        )
        conn = database.get_db()
        conn.execute(
            "UPDATE images SET status='approved', like_count=? WHERE id=?",
            (like_count, image_id),
        )
        conn.commit()
        return image_id


class TestRandomImageWeighting(DbUnitTestCase):
    """随机图片应数据库端加权抽取，并返回单行"""

    def setUp(self):
        conn = database.get_db()
        conn.execute("DELETE FROM likes")
        conn.execute("DELETE FROM images")
        conn.commit()

    def test_random_returns_single_id_and_prefers_low_heat(self):
        low_id = self._insert_approved("low", 0)
        for index in range(40):
            self._insert_approved(f"high{index}", 100)

        low_count = 0
        for _ in range(60):
            row = models.get_random_image_prefer_low_heat()
            self.assertIsNotNone(row)
            self.assertEqual(len(row), 1, "随机接口不应查询整行/全量记录")
            if row["id"] == low_id:
                low_count += 1
        self.assertGreaterEqual(low_count, 40, "低热度图片未被优先抽取")

    def test_exclude_parameter_still_works(self):
        low_id = self._insert_approved("low-ex", 0)
        other_id = self._insert_approved("other-ex", 0)
        for exclude in (low_id, other_id):
            row = models.get_random_image_prefer_low_heat(exclude_id=exclude)
            self.assertNotEqual(row["id"], exclude)


class TestSessionCleanupThrottle(DbUnitTestCase):
    def test_get_session_cleanup_is_throttled(self):
        with patch.object(auth, "_cleanup_sessions") as cleanup:
            auth._last_session_cleanup = 0.0
            auth.get_session("missing-1")
            auth.get_session("missing-2")
            self.assertEqual(cleanup.call_count, 1)

    def test_force_cleanup_bypasses_interval(self):
        with patch.object(auth, "_cleanup_sessions") as cleanup:
            auth._last_session_cleanup = time.time()
            auth._maybe_cleanup_sessions(force=True)
            self.assertEqual(cleanup.call_count, 1)


if __name__ == "__main__":
    unittest.main()
