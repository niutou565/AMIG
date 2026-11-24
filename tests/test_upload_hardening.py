"""上传链路加固测试：解析阶段请求体/并发限制、原子额度预约与 Logo 安全校验。

覆盖三类修复：
1. UploadBodyLimitMiddleware：multipart 解析阶段之前的请求体大小与并发限制；
2. reserve_upload_quota：限额检查与额度占用在同一写事务内原子完成；
3. 额度预约在上传成败两条路径上的释放（端到端）。
"""
import asyncio
import io
import os
import re
import shutil
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from PIL import Image
from fastapi.testclient import TestClient

import app.database as database
import app.image_utils as image_utils
import app.middleware as middleware
import app.models as models
from app.main import app
from tests.mysql_test_utils import create_test_database, drop_test_database

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def _reset_db_pool():
    """关闭全部连接并重置连接池（用例间彻底隔离）。"""
    database.close_all_db()


def _image_bytes(fmt="PNG", size=(10, 10)):
    buf = io.BytesIO()
    Image.new("RGB", size, (40, 120, 220)).save(buf, format=fmt)
    return buf.getvalue()


def _scope(path="/upload", method="POST", headers=None):
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "server": ("test", 443),
        "client": ("10.0.0.1", 1234),
        "headers": headers or [],
        "state": {},
    }


class TestReserveUploadQuota(unittest.TestCase):
    """原子额度预约：检查与占用在同一写事务内，并发下不超额"""

    @classmethod
    def setUpClass(cls):
        cls.db_name = create_test_database()

    @classmethod
    def tearDownClass(cls):
        drop_test_database(cls.db_name)

    def setUp(self):
        conn = database.get_db()
        conn.execute("DELETE FROM images")
        conn.execute("DELETE FROM upload_reservations")
        conn.execute("DELETE FROM settings WHERE `key` LIKE 'upload_limit%'")
        conn.execute("DELETE FROM settings WHERE `key` LIKE 'cooldown%'")
        conn.commit()

    def tearDown(self):
        _reset_db_pool()

    def _configure(self, per_ip=5, per_ua=5, daily_total=100, interval=0):
        models.set_setting("upload_limit_per_ip", str(per_ip))
        models.set_setting("upload_limit_per_ua", str(per_ua))
        models.set_setting("upload_limit_daily_total", str(daily_total))
        models.set_setting("upload_interval_seconds", str(interval))

    def test_reservation_occupies_quota_and_release_restores(self):
        self._configure(per_ip=1)
        ok, msg, res_id = models.reserve_upload_quota("10.9.0.1", "ua1")
        self.assertTrue(ok, msg)
        self.assertTrue(res_id)

        # 预约未释放前，第二个请求必须被拒绝（预约已占用额度）
        ok2, msg2, _ = models.reserve_upload_quota("10.9.0.1", "ua2")
        self.assertFalse(ok2)
        self.assertIn("今日上传次数已达上限（1次）", msg2)

        # 释放后额度归还
        models.release_upload_reservation(res_id)
        ok3, msg3, _ = models.reserve_upload_quota("10.9.0.1", "ua2")
        self.assertTrue(ok3, msg3)

    def test_reservation_blocked_by_interval_while_held(self):
        self._configure(per_ip=10, interval=30)
        ok, msg, res_id = models.reserve_upload_quota("10.9.0.2", "ua1")
        self.assertTrue(ok, msg)
        # 进行中的预约也计入上传间隔，防止并发请求同时通过间隔检查
        ok2, msg2, _ = models.reserve_upload_quota("10.9.0.2", "ua2")
        self.assertFalse(ok2)
        self.assertIn("操作过于频繁", msg2)

    def test_daily_total_counts_reservations(self):
        self._configure(daily_total=1, per_ip=100)
        ok, msg, res_id = models.reserve_upload_quota("10.9.1.1", "ua1")
        self.assertTrue(ok, msg)
        ok2, msg2, _ = models.reserve_upload_quota("10.9.1.2", "ua2")
        self.assertFalse(ok2)
        self.assertIn("今日上传名额已满", msg2)

    def test_daily_total_counts_images(self):
        self._configure(daily_total=1, per_ip=100)
        conn = database.get_db()
        conn.execute(
            "INSERT INTO images (filename, thumb_filename, width, height, title, "
            "original_size, compressed_size, description, author, content_md5, "
            "ip, ua_hash, status, created_at) "
            "VALUES ('a.webp', 'a_thumb.webp', 10, 10, 't', 100, 80, '', '', "
            "'md5-x', '10.9.2.1', 'ua', 'approved', ?)",
            (models.now_iso(),),
        )
        conn.commit()
        ok, msg, _ = models.reserve_upload_quota("10.9.2.2", "ua2")
        self.assertFalse(ok)
        self.assertIn("今日上传名额已满", msg)

    def test_stale_reservations_cleaned_lazily(self):
        self._configure(per_ip=1)
        stale = (datetime.now(timezone.utc)
                 - timedelta(seconds=models.UPLOAD_RESERVATION_TTL_SECONDS + 60)).isoformat()
        conn = database.get_db()
        conn.execute(
            "INSERT INTO upload_reservations (ip, ua_hash, created_at) VALUES (?,?,?)",
            ("10.9.3.9", "ua", stale),
        )
        conn.commit()
        ok, msg, res_id = models.reserve_upload_quota("10.9.3.9", "ua1")
        self.assertTrue(ok, msg)
        remaining = database.get_db().execute(
            "SELECT COUNT(*) FROM upload_reservations WHERE created_at < ?", (stale,)
        ).fetchone()[0]
        self.assertEqual(remaining, 0)

    def test_concurrent_reserves_never_exceed_limit(self):
        """并发预约：限制 2 个名额，12 个并发请求只能成功 2 个"""
        self._configure(per_ip=2)
        results = []
        lock = threading.Lock()

        def worker(n):
            ok, msg, res_id = models.reserve_upload_quota("10.9.4.1", f"ua{n}")
            with lock:
                results.append((ok, msg, res_id))

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        successes = [r for r in results if r[0]]
        failures = [r for r in results if not r[0]]
        self.assertEqual(len(successes), 2, f"超出名额的请求被放行: {successes}")
        self.assertEqual(len(failures), 10)
        self.assertTrue(all("已达上限" in msg for _, msg, _ in failures))
        # 释放成功预约后，后续请求可再次获得名额
        for _, _, res_id in successes:
            models.release_upload_reservation(res_id)
        ok, msg, _ = models.reserve_upload_quota("10.9.4.1", "ua-after")
        self.assertTrue(ok, msg)


class TestUploadBodyLimitMiddleware(unittest.TestCase):
    """解析阶段前置限制：Content-Length 预检、chunked 流式计数、并发闸门"""

    def _run(self, mw, scope, messages=None, app_callable=None):
        events = {"app_called": False, "sent": [], "received": []}
        recv_messages = list(messages or [])

        async def default_app(sc, receive, send):
            events["app_called"] = True
            events["app_scope"] = sc
            while True:
                message = await receive()
                events["received"].append(message["type"])
                if message["type"] == "http.disconnect":
                    break

        async def receive():
            if recv_messages:
                return recv_messages.pop(0)
            return {"type": "http.disconnect"}

        async def send(message):
            events["sent"].append(message)

        app = app_callable or default_app
        mw.app = app
        asyncio.run(mw(scope, receive, send))
        return events

    def _mw(self):
        return middleware.UploadBodyLimitMiddleware(None)

    def test_content_length_over_limit_rejected_before_app(self):
        mw = self._mw()
        headers = [(b"content-length", str(middleware.MAX_UPLOAD_BODY_SIZE + 1).encode())]
        events = self._run(mw, _scope(headers=headers))
        self.assertFalse(events["app_called"], "超限请求不应进入应用与 multipart 解析")
        self.assertEqual(events["sent"][0]["type"], "http.response.start")
        self.assertEqual(events["sent"][0]["status"], 413)
        self.assertEqual(events["sent"][-1]["type"], "http.response.body")

    def test_content_length_within_limit_passes_through(self):
        mw = self._mw()
        messages = [{"type": "http.request", "body": b"hello", "more_body": False}]
        headers = [(b"content-length", b"5")]
        events = self._run(mw, _scope(headers=headers), messages)
        self.assertTrue(events["app_called"])
        self.assertEqual(events["received"], ["http.request", "http.disconnect"])

    def test_chunked_over_limit_marks_state_and_disconnects(self):
        mw = self._mw()
        messages = [
            {"type": "http.request", "body": b"A" * 8, "more_body": True},
            {"type": "http.request", "body": b"B" * 8, "more_body": True},
            {"type": "http.request", "body": b"C" * 8, "more_body": False},
        ]
        with patch.object(middleware, "MAX_UPLOAD_BODY_SIZE", 10):
            events = self._run(mw, _scope(), messages)
        # 第二块累计超过限制：向应用发送断开信号终止解析，并标记状态
        self.assertEqual(events["received"], ["http.request", "http.disconnect"])
        self.assertTrue(events["app_scope"]["state"].get("upload_body_too_large"))

    def test_concurrency_gate_returns_503(self):
        mw = self._mw()
        with patch.object(middleware, "_upload_gate", threading.Semaphore(0)):
            events = self._run(mw, _scope())
        self.assertFalse(events["app_called"])
        self.assertEqual(events["sent"][0]["status"], 503)

    def test_non_upload_request_passes_through(self):
        mw = self._mw()
        events = self._run(mw, _scope(path="/gallery", method="GET"))
        self.assertTrue(events["app_called"])
        self.assertEqual(events["sent"], [])


class TestUploadEndpointIntegration(unittest.TestCase):
    """端到端：解析阶段限制接入真实应用；额度预约在成败路径上均被释放"""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="amig-hard-")
        cls.db_name = create_test_database()
        cls.upload_dir = os.path.join(cls.tmpdir, "uploads")
        cls.private_dir = os.path.join(cls.tmpdir, "private_uploads")
        cls._orig_uploads = image_utils.UPLOADS_DIR
        cls._orig_private = image_utils.PRIVATE_UPLOADS_DIR
        image_utils.UPLOADS_DIR = cls.upload_dir
        image_utils.PRIVATE_UPLOADS_DIR = cls.private_dir
        os.makedirs(cls.upload_dir, exist_ok=True)
        os.makedirs(cls.private_dir, exist_ok=True)
        models.set_setting("admin_domain", "admin.test")
        models.set_setting("enable_captcha", "0")
        models.set_setting("enable_dedup", "0")
        models.set_setting("upload_interval_seconds", "0")
        models.set_setting("upload_limit_per_ip", "10")
        models.set_setting("upload_limit_daily_total", "100")

    @classmethod
    def tearDownClass(cls):
        drop_test_database(cls.db_name)
        image_utils.UPLOADS_DIR = cls._orig_uploads
        image_utils.PRIVATE_UPLOADS_DIR = cls._orig_private
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        conn = database.get_db()
        conn.execute("DELETE FROM images")
        conn.execute("DELETE FROM upload_reservations")
        conn.commit()

    def tearDown(self):
        _reset_db_pool()

    def _client(self):
        return TestClient(app, base_url="https://www.example.com", raise_server_exceptions=False)

    def _csrf(self, client):
        page = client.get("/upload")
        self.assertEqual(page.status_code, 200)
        match = CSRF_RE.search(page.text)
        self.assertIsNotNone(match)
        return match.group(1)

    def _reservation_count(self):
        return database.get_db().execute(
            "SELECT COUNT(*) FROM upload_reservations"
        ).fetchone()[0]

    def test_oversized_content_length_rejected_by_middleware(self):
        with patch.object(middleware, "MAX_UPLOAD_BODY_SIZE", 100):
            client = self._client()
            csrf = self._csrf(client)
            response = client.post(
                "/upload",
                data={"title": "t", "agree": "1", "csrf_token": csrf},
                files={"image": ("pic.png", _image_bytes(), "image/png")},
            )
        self.assertEqual(response.status_code, 413)
        # 请求在解析阶段就被拒绝，数据库不应有任何额度预约残留
        self.assertEqual(self._reservation_count(), 0)
        self.assertEqual(
            database.get_db().execute("SELECT COUNT(*) FROM images").fetchone()[0], 0
        )

    def test_reservation_released_after_failed_validation(self):
        client = self._client()
        csrf = self._csrf(client)
        response = client.post(
            "/upload",
            data={"title": "t", "agree": "1", "csrf_token": csrf},
            files={"image": ("evil.png", b"not-an-image", "image/png")},
        )
        self.assertIn("图片格式与文件内容不匹配", response.text)
        self.assertEqual(self._reservation_count(), 0)

    def test_reservation_released_after_successful_upload(self):
        client = self._client()
        csrf = self._csrf(client)
        response = client.post(
            "/upload",
            data={"title": "t", "agree": "1", "csrf_token": csrf},
            files={"image": ("pic.png", _image_bytes(size=(64, 64)), "image/png")},
        )
        self.assertIn("已提交", response.text)
        self.assertEqual(self._reservation_count(), 0)
        self.assertEqual(
            database.get_db().execute("SELECT COUNT(*) FROM images").fetchone()[0], 1
        )

    def test_upload_quota_blocks_after_exhausted(self):
        models.set_setting("upload_limit_per_ip", "1")
        try:
            client = self._client()
            csrf = self._csrf(client)
            first = client.post(
                "/upload",
                data={"title": "t", "agree": "1", "csrf_token": csrf},
                files={"image": ("a.png", _image_bytes(size=(64, 64)), "image/png")},
            )
            self.assertIn("已提交", first.text)
            second = client.post(
                "/upload",
                data={"title": "t2", "agree": "1", "csrf_token": csrf},
                files={"image": ("b.png", _image_bytes(size=(64, 64)), "image/png")},
            )
            self.assertIn("今日上传次数已达上限（1次）", second.text)
            self.assertEqual(self._reservation_count(), 0)
        finally:
            models.set_setting("upload_limit_per_ip", "10")


if __name__ == "__main__":
    unittest.main()
