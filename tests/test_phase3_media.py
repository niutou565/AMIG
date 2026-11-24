# -*- coding: utf-8 -*-
"""第三阶段：受控图片访问、上传验证、文件一致性与旧数据兼容测试。"""

import io
import os
import re
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

from PIL import Image
from fastapi.testclient import TestClient

import app.auth as auth
import app.database as database
import app.image_utils as image_utils
import app.models as models
import app.routes.admin as admin_routes
import app.upload_consistency as consistency
from app.main import app
from tests.mysql_test_utils import create_test_database, drop_test_database
from tools import migrate_private_uploads as migration

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def _reset_db_pool():
    """关闭全部连接并重置连接池（用例间彻底隔离）。"""
    database.close_all_db()


def _png_bytes(size=(20, 20), mode="RGB", color=(40, 120, 220)):
    img = Image.new(mode, size, color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _jpeg_with_orientation(size=(20, 10), orientation=6):
    img = Image.new("RGB", size, (120, 120, 120))
    exif = Image.Exif()
    exif[274] = orientation
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


def _animated_webp_bytes():
    frames = [
        Image.new("RGB", (20, 20), (255, 0, 0)),
        Image.new("RGB", (20, 20), (0, 0, 255)),
    ]
    buf = io.BytesIO()
    frames[0].save(
        buf, format="WEBP", save_all=True, append_images=frames[1:],
        duration=100, loop=0,
    )
    return buf.getvalue()


class Phase3MediaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="amig-phase3-")
        cls.db_name = create_test_database()
        cls._orig_uploads = image_utils.UPLOADS_DIR
        cls._orig_private = image_utils.PRIVATE_UPLOADS_DIR
        cls._orig_admin_uploads = admin_routes.UPLOADS_DIR
        image_utils.UPLOADS_DIR = os.path.join(cls.tmpdir, "uploads")
        image_utils.PRIVATE_UPLOADS_DIR = os.path.join(cls.tmpdir, "private_uploads")
        admin_routes.UPLOADS_DIR = image_utils.UPLOADS_DIR
        os.makedirs(image_utils.UPLOADS_DIR, exist_ok=True)
        os.makedirs(image_utils.PRIVATE_UPLOADS_DIR, exist_ok=True)
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
        drop_test_database(cls.db_name)
        image_utils.UPLOADS_DIR = cls._orig_uploads
        image_utils.PRIVATE_UPLOADS_DIR = cls._orig_private
        admin_routes.UPLOADS_DIR = cls._orig_admin_uploads
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def tearDown(self):
        _reset_db_pool()

    def _client(self, host="site.test"):
        return TestClient(app, base_url=f"https://{host}", raise_server_exceptions=False)

    def _admin_client(self):
        client = self._client("admin.test")
        page = client.get("/admin/login")
        match = CSRF_RE.search(page.text)
        self.assertIsNotNone(match)
        response = client.post(
            "/admin/login",
            data={"username": "admin", "password": self.admin_password,
                  "csrf_token": match.group(1)},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302, response.text[:300])
        return client

    def _insert_image(self, status="pending", content=None):
        unique = f"{time.time_ns()}-{status}"
        relative = f"phase3/{unique}.webp"
        thumb = f"phase3/{unique}_thumb.webp"
        # 待审/已拒绝图片在私有目录，已通过图片在公开目录（与生产存储策略一致）
        base = (image_utils.UPLOADS_DIR if status == "approved"
                else image_utils.PRIVATE_UPLOADS_DIR)
        path = os.path.join(base, relative)
        thumb_path = os.path.join(base, thumb)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = content or b"RIFF\x00\x00\x00\x00WEBPVP8 "
        with open(path, "wb") as f:
            f.write(data)
        with open(thumb_path, "wb") as f:
            f.write(data)
        image_id = models.insert_image(
            relative, thumb, 100, 50, 10, 5, "d", "",
            f"md5-{unique}", "1.2.3.4", "ua", f"uah-{unique}", "", "",
            title="media-test",
        )
        conn = database.get_db()
        conn.execute(
            "UPDATE images SET status=?, reviewed_at=? WHERE id=?",
            (status, models.now_iso() if status == "approved" else None, image_id),
        )
        conn.commit()
        return image_id, relative, thumb

    def test_approved_images_are_public_with_cache_and_etag(self):
        _, relative, thumb = self._insert_image("approved")
        client = self._client()
        for name in (relative, thumb):
            response = client.get(f"/uploads/{name}")
            self.assertEqual(response.status_code, 200, name)
            self.assertEqual(response.headers["content-type"], "image/webp")
            self.assertIn("public", response.headers["cache-control"])
            etag = response.headers["etag"]
            second = client.get(f"/uploads/{name}", headers={"If-None-Match": etag})
            self.assertEqual(second.status_code, 304)
            self.assertNotIn(b"root", second.content)

    def test_approved_images_support_range_requests(self):
        """大图/断点续传依赖 Range；FileResponse 必须返回 206 与正确片段。"""
        _, relative, _ = self._insert_image("approved")
        client = self._client()
        response = client.get(f"/uploads/{relative}", headers={"Range": "bytes=0-3"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b"RIFF")
        self.assertEqual(response.headers["content-range"], "bytes 0-3/16")

    def test_pending_images_are_private_and_admin_can_preview(self):
        _, relative, thumb = self._insert_image("pending")
        public = self._client()
        for name in (relative, thumb):
            response = public.get(f"/uploads/{name}")
            self.assertEqual(response.status_code, 404, name)
            self.assertIn("no-store", response.headers["cache-control"])
            self.assertNotIn(b"RIFF", response.content)

        admin = self._admin_client()
        for name in (relative, thumb):
            response = admin.get(f"/uploads/{name}")
            self.assertEqual(response.status_code, 200, name)
            self.assertIn("no-store", response.headers["cache-control"])
            self.assertIn("noindex", response.headers["X-Robots-Tag"])

    def test_rejected_images_are_not_public(self):
        _, relative, thumb = self._insert_image("rejected")
        public = self._client()
        self.assertEqual(public.get(f"/uploads/{relative}").status_code, 404)
        admin = self._admin_client()
        self.assertEqual(admin.get(f"/uploads/{relative}").status_code, 200)

    def test_unknown_orphan_temp_and_logo_files(self):
        client = self._client()
        os.makedirs(os.path.join(image_utils.UPLOADS_DIR, "phase3"), exist_ok=True)
        with open(os.path.join(image_utils.UPLOADS_DIR, "phase3", "orphan.webp"), "wb") as f:
            f.write(b"orphan")
        with open(os.path.join(image_utils.UPLOADS_DIR, "phase3", "sample.tmp"), "wb") as f:
            f.write(b"temp")
        with open(os.path.join(image_utils.UPLOADS_DIR, "logo_large_test.png"), "wb") as f:
            f.write(_png_bytes())

        self.assertEqual(client.get("/uploads/phase3/orphan.webp").status_code, 404)
        self.assertEqual(client.get("/uploads/phase3/sample.tmp").status_code, 404)
        logo = client.get("/uploads/logo_large_test.png")
        self.assertEqual(logo.status_code, 200)
        self.assertIn("no-cache", logo.headers["cache-control"])

    def test_legacy_pending_file_in_public_dir_stays_private(self):
        """兼容旧版本：待审文件仍在公开目录时，也绝不对普通访客开放。"""
        _, relative, _ = self._insert_image("pending")
        public_path = os.path.join(image_utils.UPLOADS_DIR, relative)
        private_path = os.path.join(image_utils.PRIVATE_UPLOADS_DIR, relative)
        os.makedirs(os.path.dirname(public_path), exist_ok=True)
        shutil.move(private_path, public_path)

        self.assertEqual(self._client().get(f"/uploads/{relative}").status_code, 404)
        admin = self._admin_client()
        self.assertEqual(admin.get(f"/uploads/{relative}").status_code, 200)

    def test_promote_moves_both_files_and_rolls_back_on_conflict(self):
        unique = f"{time.time_ns()}-promote"
        relative = f"phase3/{unique}.webp"
        thumb = f"phase3/{unique}_thumb.webp"
        src_original = os.path.join(image_utils.PRIVATE_UPLOADS_DIR, relative)
        src_thumb = os.path.join(image_utils.PRIVATE_UPLOADS_DIR, thumb)
        os.makedirs(os.path.dirname(src_original), exist_ok=True)
        for path in (src_original, src_thumb):
            with open(path, "wb") as f:
                f.write(b"RIFF\x00\x00\x00\x00WEBPVP8 ")

        self.assertEqual(image_utils.promote_image_files(relative, thumb), [])
        for name in (relative, thumb):
            self.assertTrue(os.path.isfile(os.path.join(image_utils.UPLOADS_DIR, name)))
            self.assertFalse(os.path.exists(os.path.join(image_utils.PRIVATE_UPLOADS_DIR, name)))

        # 重复调用是幂等的：目标已存在且源已不存在时不报错
        self.assertEqual(image_utils.promote_image_files(relative, thumb), [])

        # 源与目标同时存在时拒绝覆盖
        os.makedirs(os.path.dirname(src_original), exist_ok=True)
        for path in (src_original, src_thumb):
            with open(path, "wb") as f:
                f.write(b"duplicate")
        failures = image_utils.promote_image_files(relative, thumb)
        self.assertTrue(failures)
        self.assertTrue(os.path.exists(src_original))
        with open(os.path.join(image_utils.UPLOADS_DIR, relative), "rb") as f:
            self.assertEqual(f.read(), b"RIFF\x00\x00\x00\x00WEBPVP8 ")

    def test_path_traversal_is_rejected(self):
        outside = os.path.join(self.tmpdir, "secret.txt")
        with open(outside, "wb") as f:
            f.write(b"secret")
        self.assertIsNone(image_utils.resolve_upload_path("../secret.txt"))
        self.assertIsNone(image_utils.resolve_upload_path("C:/Windows/win.ini"))
        self.assertIsNone(image_utils.resolve_upload_path("\\..\\secret.txt"))
        self.assertIsNone(image_utils.resolve_upload_path("/etc/passwd"))
        client = self._client()
        self.assertEqual(client.get("/uploads/..%2fsecret.txt").status_code, 404)

    def test_symlink_in_upload_dir_is_rejected(self):
        target = os.path.join(self.tmpdir, "outside-link-target.txt")
        with open(target, "wb") as f:
            f.write(b"secret")
        link = os.path.join(image_utils.UPLOADS_DIR, "link.webp")
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            self.skipTest("当前环境不支持创建符号链接")
        try:
            self.assertIsNone(image_utils.resolve_upload_path("link.webp", must_exist=True))
            self.assertEqual(self._client().get("/uploads/link.webp").status_code, 404)
        finally:
            if os.path.exists(link):
                os.remove(link)

    def test_upload_route_rejects_declared_mime_mismatch(self):
        client = self._client("upload.test")
        page = client.get("/upload")
        match = CSRF_RE.search(page.text)
        self.assertIsNotNone(match)
        response = client.post(
            "/upload",
            data={"csrf_token": match.group(1), "title": "mime-test", "agree": "1"},
            files={"image": ("mime.png", _png_bytes(), "image/jpeg")},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Content-Type", response.text)
        self.assertNotIn("已提交，等待审核", response.text)

    def test_status_changes_take_effect_immediately(self):
        image_id, relative, _ = self._insert_image("pending")
        client = self._client()
        self.assertEqual(client.get(f"/uploads/{relative}").status_code, 404)

        conn = database.get_db()
        conn.execute("UPDATE images SET status='approved', reviewed_at=? WHERE id=?",
                     (models.now_iso(), image_id))
        conn.commit()
        self.assertEqual(client.get(f"/uploads/{relative}").status_code, 200)

        conn.execute("UPDATE images SET status='pending' WHERE id=?", (image_id,))
        conn.commit()
        self.assertEqual(client.get(f"/uploads/{relative}").status_code, 404)

    def test_approve_move_failure_rolls_back_status_and_shows_error(self):
        image_id, relative, thumb = self._insert_image("pending")
        os.remove(os.path.join(image_utils.PRIVATE_UPLOADS_DIR, thumb))

        admin = self._admin_client()
        page = admin.get(f"/admin/review/{image_id}")
        token = CSRF_RE.search(page.text).group(1)
        response = admin.post(
            f"/admin/review/{image_id}",
            data={"csrf_token": token, "action": "approve"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("error=file_move", response.headers["location"])

        row = database.get_db().execute(
            "SELECT status FROM images WHERE id=?", (image_id,)
        ).fetchone()
        self.assertEqual(row["status"], "pending")
        # 原图应已回滚到私有目录，不能出现“状态已发布但文件缺失”
        self.assertTrue(
            os.path.exists(os.path.join(image_utils.PRIVATE_UPLOADS_DIR, relative))
        )
        error_page = admin.get(response.headers["location"])
        self.assertIn("文件发布或删除失败", error_page.text)

    def test_deleted_image_url_stops_working(self):
        image_id, relative, thumb = self._insert_image("approved")
        client = self._client()
        self.assertEqual(client.get(f"/uploads/{relative}").status_code, 200)
        conn = database.get_db()
        conn.execute("DELETE FROM images WHERE id=?", (image_id,))
        conn.commit()
        self.assertEqual(client.get(f"/uploads/{relative}").status_code, 404)
        self.assertEqual(client.get(f"/uploads/{thumb}").status_code, 404)

    def test_missing_file_for_existing_record_returns_404(self):
        _, relative, _ = self._insert_image("approved")
        os.remove(os.path.join(image_utils.UPLOADS_DIR, relative))
        self.assertEqual(self._client().get(f"/uploads/{relative}").status_code, 404)


class Phase3UploadValidationTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="amig-phase3-upload-")
        self._orig_uploads = image_utils.UPLOADS_DIR
        image_utils.UPLOADS_DIR = self.tmpdir
        os.makedirs(self.tmpdir, exist_ok=True)

    def tearDown(self):
        image_utils.UPLOADS_DIR = self._orig_uploads
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_declared_content_type_mismatch_rejected(self):
        data = _png_bytes()
        valid, err = image_utils.validate_image(data, "a.png", "image/jpeg")
        self.assertFalse(valid)
        self.assertIn("Content-Type", err)
        valid, err = image_utils.validate_image(data, "a.png", "text/plain")
        self.assertFalse(valid)
        self.assertIn("Content-Type", err)
        valid, err = image_utils.validate_image(data, "a.png", "image/png")
        self.assertTrue(valid, err)

    def test_corrupt_and_truncated_images_rejected(self):
        data = _png_bytes()
        truncated = data[: max(8, len(data) // 2)]
        valid, err = image_utils.validate_image(truncated, "a.png", "image/png")
        self.assertFalse(valid)
        self.assertIn("损坏", err)

    def test_animated_webp_rejected_with_clear_error(self):
        data = _animated_webp_bytes()
        valid, err = image_utils.validate_image(data, "a.webp", "image/webp")
        self.assertFalse(valid)
        self.assertIn("动图", err)
        result, message = image_utils.process_image(data)
        self.assertIsNone(result)
        self.assertIn("动图", message)

    def test_transparent_png_keeps_alpha(self):
        data = _png_bytes(size=(50, 50), mode="RGBA", color=(0, 0, 0, 0))
        result, err = image_utils.process_image(data)
        self.assertEqual(err, "")
        with Image.open(os.path.join(self.tmpdir, result["filename"])) as output:
            self.assertEqual(output.mode, "RGBA")
            self.assertEqual(output.getchannel("A").getextrema(), (0, 0))

    def test_concurrent_image_processing_does_not_overwrite(self):
        from concurrent.futures import ThreadPoolExecutor
        data = _png_bytes(size=(80, 60))
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: image_utils.process_image(data), range(4)))
        names = set()
        for result, err in results:
            self.assertEqual(err, "")
            self.assertIsNotNone(result)
            names.add(result["filename"])
            self.assertTrue(os.path.exists(os.path.join(self.tmpdir, result["filename"])))
        self.assertEqual(len(names), 4)

    def test_exif_orientation_applied_and_privacy_metadata_removed(self):
        data = _jpeg_with_orientation()
        result, err = image_utils.process_image(data)
        self.assertEqual(err, "")
        self.assertEqual((result["width"], result["height"]), (10, 20))
        with Image.open(os.path.join(self.tmpdir, result["filename"])) as output:
            self.assertNotIn(274, output.getexif())


class UploadConsistencyTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="amig-consistency-")
        self.db_name = create_test_database()
        self._orig_uploads = image_utils.UPLOADS_DIR
        self._orig_private = image_utils.PRIVATE_UPLOADS_DIR
        image_utils.UPLOADS_DIR = os.path.join(self.tmpdir, "uploads")
        image_utils.PRIVATE_UPLOADS_DIR = os.path.join(self.tmpdir, "private_uploads")
        os.makedirs(image_utils.UPLOADS_DIR, exist_ok=True)
        os.makedirs(image_utils.PRIVATE_UPLOADS_DIR, exist_ok=True)

    def tearDown(self):
        drop_test_database(self.db_name)
        _reset_db_pool()
        image_utils.UPLOADS_DIR = self._orig_uploads
        image_utils.PRIVATE_UPLOADS_DIR = self._orig_private
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write(self, relative, data=b"x", base=None):
        path = os.path.join(base or image_utils.UPLOADS_DIR, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_scan_reports_missing_and_orphans_without_deleting(self):
        # 数据库记录引用缺失文件
        models.insert_image(
            "missing/original.webp", "missing/thumb.webp", 1, 1, 1, 1,
            "d", "", "md5-missing", "1.2.3.4", "ua", "uah-missing", "", "",
        )
        old = self._write("orphans/old.webp")
        old_time = time.time() - 10 * 86400
        os.utime(old, (old_time, old_time))
        self._write("orphans/new.webp")
        self._write("orphans/temp.tmp")
        self._write("logo_large_x.png")

        report = consistency.scan_uploads(older_than_days=7)
        self.assertEqual(len(report["missing_original"]), 1)
        self.assertEqual(len(report["missing_thumbnail"]), 1)
        paths = {item["path"] for item in report["orphan_files"]}
        self.assertIn("orphans/old.webp", paths)
        self.assertIn("orphans/new.webp", paths)
        self.assertNotIn("orphans/temp.tmp", paths)
        self.assertIn("orphans/temp.tmp", {item["path"] for item in report["temp_files"]})
        self.assertTrue(os.path.exists(old))

    def test_private_pending_files_are_checked_and_cleaned(self):
        models.insert_image(
            "pending/a.webp", "pending/a_thumb.webp", 1, 1, 1, 1,
            "d", "", "md5-private", "1.2.3.4", "ua", "uah-private", "", "",
        )
        for name in ("pending/a.webp", "pending/a_thumb.webp"):
            self._write(name, base=image_utils.PRIVATE_UPLOADS_DIR)

        old = self._write(
            "pending/orphan.webp", base=image_utils.PRIVATE_UPLOADS_DIR
        )
        old_time = time.time() - 10 * 86400
        os.utime(old, (old_time, old_time))
        self._write("pending/temp.tmp", base=image_utils.PRIVATE_UPLOADS_DIR)

        report = consistency.scan_uploads(older_than_days=7)
        self.assertEqual(report["missing_original"], [])
        self.assertEqual(report["missing_thumbnail"], [])
        self.assertIn("private_uploads", report["private_root"])
        private_orphans = [
            item for item in report["orphan_files"] if item.get("base") == "private"
        ]
        self.assertEqual(
            {item["path"] for item in private_orphans}, {"pending/orphan.webp"}
        )
        self.assertEqual(
            {item["path"] for item in report["temp_files"] if item.get("base") == "private"},
            {"pending/temp.tmp"},
        )

        deleted, failed = consistency.delete_orphans(report, 7)
        self.assertEqual(deleted, ["pending/orphan.webp"])
        self.assertFalse(os.path.exists(old))
        self.assertFalse(failed)
        self.assertTrue(
            os.path.exists(
                os.path.join(image_utils.PRIVATE_UPLOADS_DIR, "pending/a.webp")
            )
        )

    def test_delete_only_old_orphans_and_never_referenced_files(self):
        old = self._write("orphans/old.webp")
        old_time = time.time() - 10 * 86400
        os.utime(old, (old_time, old_time))
        new = self._write("orphans/new.webp")
        self._write("kept.webp")
        models.insert_image(
            "kept.webp", "kept_thumb.webp", 1, 1, 1, 1, "d", "",
            "md5-kept", "1.2.3.4", "ua", "uah-kept", "", "",
        )
        report = consistency.scan_uploads(older_than_days=7)
        deleted, failed = consistency.delete_orphans(report, 7)
        self.assertIn("orphans/old.webp", deleted)
        self.assertNotIn("orphans/new.webp", deleted)
        self.assertNotIn("kept.webp", deleted)
        self.assertTrue(os.path.exists(new))
        self.assertTrue(os.path.exists(os.path.join(image_utils.UPLOADS_DIR, "kept.webp")))
        self.assertFalse(os.path.exists(old))

    def test_old_temp_files_can_be_cleaned_explicitly(self):
        old = self._write("orphans/old.tmp")
        old_time = time.time() - 10 * 86400
        os.utime(old, (old_time, old_time))
        new = self._write("orphans/new.tmp")
        report = consistency.scan_uploads(older_than_days=7)
        deleted, failed = consistency.delete_temp_files(report, 7)
        self.assertIn("orphans/old.tmp", deleted)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(new))
        self.assertFalse(failed)

    def test_migration_moves_legacy_pending_files_idempotently(self):
        models.insert_image(
            "legacy/a.webp", "legacy/a_thumb.webp", 1, 1, 1, 1,
            "d", "", "md5-legacy", "1.2.3.4", "ua", "uah-legacy", "", "",
        )
        for name in ("legacy/a.webp", "legacy/a_thumb.webp"):
            self._write(name)

        moves, skipped, missing, conflicts = migration._plan_moves(("pending",))
        self.assertEqual(len(moves), 2)
        self.assertFalse(skipped or missing or conflicts)
        moved, failed = migration._apply_moves(moves)
        self.assertEqual(len(moved), 2)
        self.assertFalse(failed)
        for name in ("legacy/a.webp", "legacy/a_thumb.webp"):
            self.assertFalse(os.path.exists(os.path.join(image_utils.UPLOADS_DIR, name)))
            self.assertTrue(
                os.path.exists(os.path.join(image_utils.PRIVATE_UPLOADS_DIR, name))
            )

        # 幂等：再次执行不会重复移动，也不会报冲突
        moves, skipped, missing, conflicts = migration._plan_moves(("pending",))
        self.assertEqual(moves, [])
        self.assertEqual(len(skipped), 2)
        self.assertFalse(missing or conflicts)

    def test_delete_image_files_rejects_traversal_and_reports_failures(self):
        outside = os.path.join(self.tmpdir, "outside.txt")
        with open(outside, "wb") as f:
            f.write(b"keep")
        failures = image_utils.delete_image_files("../outside.txt", None)
        self.assertTrue(failures)
        self.assertTrue(os.path.exists(outside))

        with patch.object(image_utils.os, "remove", side_effect=OSError("permission")):
            path = self._write("fail.webp")
            failures = image_utils.delete_image_files("fail.webp", None)
            self.assertTrue(failures)
            self.assertTrue(os.path.exists(path))
