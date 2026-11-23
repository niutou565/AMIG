"""第四阶段：备份、恢复、健康检查与上传目录一致性测试。

全部在临时目录中执行，不接触真实 data/app.db 与 backups/。
"""
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import app.database as db
import app.upload_consistency as consistency
from app import image_utils
from tools import backup_db, check_db, restore_db

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _iso(days_ago=0, seconds=0):
    moment = datetime.now(timezone.utc) - timedelta(days=days_ago, seconds=seconds)
    return moment.isoformat(timespec="seconds")


class BackupRestoreBase(unittest.TestCase):
    """每个用例独立的库 + 备份目录 + 上传目录。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="amig_ops_")
        self._orig = (db.DATABASE_PATH, db.BACKUP_DIR,
                      image_utils.UPLOADS_DIR, image_utils.PRIVATE_UPLOADS_DIR,
                      restore_db.UPLOADS_DIR, restore_db.PRIVATE_UPLOADS_DIR)
        # 工具模块在导入时绑定了配置常量，测试中一并改指临时目录
        self._orig_tools = (backup_db.DATABASE_PATH, backup_db.BACKUP_DIR,
                            backup_db.UPLOADS_DIR, backup_db.PRIVATE_UPLOADS_DIR,
                            restore_db.DATABASE_PATH, restore_db.BACKUP_DIR)
        self.db_path = os.path.join(self.tmpdir, "app.db")
        self.backup_dir = os.path.join(self.tmpdir, "backups")
        self.uploads = os.path.join(self.tmpdir, "uploads")
        self.private = os.path.join(self.tmpdir, "private_uploads")
        for d in (self.uploads, self.private):
            os.makedirs(d, exist_ok=True)

        db.close_all_db()
        db.DATABASE_PATH = self.db_path
        db.BACKUP_DIR = self.backup_dir
        image_utils.UPLOADS_DIR = self.uploads
        image_utils.PRIVATE_UPLOADS_DIR = self.private
        backup_db.DATABASE_PATH = self.db_path
        backup_db.BACKUP_DIR = self.backup_dir
        backup_db.UPLOADS_DIR = self.uploads
        backup_db.PRIVATE_UPLOADS_DIR = self.private
        restore_db.DATABASE_PATH = self.db_path
        restore_db.BACKUP_DIR = self.backup_dir
        restore_db.UPLOADS_DIR = self.uploads
        restore_db.PRIVATE_UPLOADS_DIR = self.private
        db.init_db()

    def tearDown(self):
        db.close_all_db()
        (db.DATABASE_PATH, db.BACKUP_DIR,
         image_utils.UPLOADS_DIR, image_utils.PRIVATE_UPLOADS_DIR,
         restore_db.UPLOADS_DIR, restore_db.PRIVATE_UPLOADS_DIR) = self._orig
        (backup_db.DATABASE_PATH, backup_db.BACKUP_DIR,
         backup_db.UPLOADS_DIR, backup_db.PRIVATE_UPLOADS_DIR,
         restore_db.DATABASE_PATH, restore_db.BACKUP_DIR) = self._orig_tools
        # 关闭指向临时目录的连接后再删除，避免污染后续测试的真实库
        db.close_all_db()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def add_image(self, status="approved", filename=None, thumb=None,
                  days_ago=0, like_count=0):
        filename = filename or f"{os.urandom(6).hex()}.webp"
        thumb = thumb or f"thumb_{os.urandom(4).hex()}.webp"
        conn = db.get_db()
        conn.execute(
            "INSERT INTO images (filename, thumb_filename, original_size,"
            " compressed_size, width, height, description, title, author,"
            " content_md5, ip, ua, ua_hash, province, city, region, status,"
            " reject_reason, admin_edited, created_at, reviewed_at, views,"
            " like_count, is_duplicate)"
            " VALUES (?,?,1,1,10,10,'','t','a',?,'1.1.1.1','','uh','p','c','',?,NULL,0,?,?,0,?,0)",
            (filename, thumb, os.urandom(8).hex(), status, _iso(days_ago),
             _iso(days_ago) if status == "approved" else None, like_count),
        )
        conn.commit()
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def write_file(self, directory, name, size=64):
        path = os.path.join(directory, name)
        with open(path, "wb") as f:
            f.write(b"RIFF0000WEBP" + b"x" * size)
        return path

    def write_archive(self, filename, entries):
        """entries: {归档内路径: bytes}"""
        path = os.path.join(self.backup_dir, filename)
        os.makedirs(self.backup_dir, exist_ok=True)
        with tarfile.open(path, "w:gz") as tar:
            for arcname, data in entries.items():
                info = tarfile.TarInfo(arcname)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return path

    def row_count(self, table, path=None):
        conn = sqlite3.connect(path or self.db_path)
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            conn.close()


class TestBackupTool(BackupRestoreBase):
    def test_backup_creates_verified_file(self):
        self.add_image()
        os.makedirs(self.backup_dir, exist_ok=True)
        target = os.path.join(self.backup_dir, "backup_test.db")
        backup_db._backup_database(self.db_path, target)
        ok, detail = backup_db._verify_copy(target)
        self.assertTrue(ok, detail)
        self.assertEqual(self.row_count("images", target), 1)

    def test_backup_includes_uncheckpointed_wal_data(self):
        """WAL 中未 checkpoint 的已提交数据必须进入备份（不能用文件复制）。"""
        self.add_image(filename="wal_test.webp")
        wal_path = self.db_path + "-wal"
        self.assertTrue(os.path.exists(wal_path) and os.path.getsize(wal_path) > 0,
                        "前置条件不成立：WAL 中没有待合并数据")

        target = os.path.join(self.tmpdir, "wal_backup.db")
        backup_db._backup_database(self.db_path, target)
        self.assertEqual(self.row_count("images", target), 1,
                         "备份遗漏了 WAL 中已提交的数据")

    def test_verify_detects_corrupted_backup(self):
        broken = os.path.join(self.tmpdir, "broken.db")
        with open(broken, "wb") as f:
            f.write(b"this is not a sqlite database at all" * 10)
        ok, detail = backup_db._verify_copy(broken)
        self.assertFalse(ok)
        self.assertIn("失败", detail)

    def test_prune_keeps_newest_n(self):
        os.makedirs(self.backup_dir, exist_ok=True)
        names = [f"backup_2026010{i}T000000Z.db" for i in range(1, 6)]
        for name in names:
            with open(os.path.join(self.backup_dir, name), "wb") as f:
                f.write(b"x")
        removed = backup_db._prune(self.backup_dir, keep=2)
        remaining = sorted(n for n in os.listdir(self.backup_dir) if n.endswith(".db"))
        self.assertEqual(remaining, names[-2:])
        self.assertEqual(len(removed), 3)

    def test_prune_never_removes_latest(self):
        os.makedirs(self.backup_dir, exist_ok=True)
        for name in ("backup_20260101T000000Z.db",):
            with open(os.path.join(self.backup_dir, name), "wb") as f:
                f.write(b"x")
        backup_db._prune(self.backup_dir, keep=0)
        self.assertEqual(len(os.listdir(self.backup_dir)), 1,
                         "keep=0 时不应删除任何备份")

    def test_backup_does_not_overwrite_existing_on_failure(self):
        """备份失败时不得删除或覆盖已有备份。"""
        os.makedirs(self.backup_dir, exist_ok=True)
        existing = os.path.join(self.backup_dir, "backup_20260101T000000Z.db")
        with open(existing, "wb") as f:
            f.write(b"old backup")
        # 指向不存在的数据库，备份必然失败
        ok, _ = self._run_backup_cli(os.path.join(self.tmpdir, "missing.db"))
        self.assertFalse(ok)
        with open(existing, "rb") as f:
            self.assertEqual(f.read(), b"old backup")

    def test_upload_archive_failure_keeps_existing_and_no_partial(self):
        """上传归档失败时不得覆盖已有归档，也不留下 .part 残留。"""
        os.makedirs(self.backup_dir, exist_ok=True)
        target = os.path.join(self.backup_dir, "backup_20260101T000000Z_uploads.tar.gz")
        with open(target, "wb") as f:
            f.write(b"old archive")
        with mock.patch.object(backup_db.tarfile, "open",
                               side_effect=tarfile.TarError("simulated")):
            ok, _ = backup_db._archive_uploads(target)
        self.assertFalse(ok)
        with open(target, "rb") as f:
            self.assertEqual(f.read(), b"old archive")
        self.assertFalse(any(n.endswith(".part") for n in os.listdir(self.backup_dir)))

    def test_backup_does_not_overwrite_same_second(self):
        """同一秒内连续备份应生成不同文件，不能互相覆盖。"""
        self.add_image()
        argv = ["backup_db.py"]
        with mock.patch.object(backup_db, "_stamp", return_value="20260101T000000Z"), \
                mock.patch.object(sys, "argv", argv), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(backup_db.main(), 0)
            self.assertEqual(backup_db.main(), 0)
        backups = sorted(
            n for n in os.listdir(self.backup_dir)
            if n.startswith("backup_") and n.endswith(".db")
        )
        self.assertEqual(len(backups), 2, backups)
        self.assertTrue(backups[1].endswith("_2.db"), backups)

    def _run_backup_cli(self, db_path):
        env = dict(os.environ)
        env["DATABASE_PATH"] = db_path
        env["BACKUP_DIR"] = self.backup_dir
        proc = subprocess.run([sys.executable, "tools/backup_db.py", "--json"],
                              cwd=ROOT, env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        try:
            data = json.loads(proc.stdout)
        except ValueError:
            data = {}
        return proc.returncode == 0, data


class TestRestoreTool(BackupRestoreBase):
    def _make_backup(self, images=2):
        for _ in range(images):
            self.add_image()
        db.checkpoint_wal("TRUNCATE")
        target = os.path.join(self.backup_dir, "backup_20260101T000000Z.db")
        os.makedirs(self.backup_dir, exist_ok=True)
        backup_db._backup_database(self.db_path, target)
        return target

    def test_verify_backup_rejects_missing_tables(self):
        empty = os.path.join(self.tmpdir, "empty.db")
        conn = sqlite3.connect(empty)
        conn.execute("CREATE TABLE unrelated (x INTEGER)")
        conn.commit()
        conn.close()
        ok, detail, _ = restore_db.verify_backup(empty)
        self.assertFalse(ok)
        self.assertIn("关键表", detail)

    def test_restore_replaces_database_atomically(self):
        backup = self._make_backup(images=2)
        # 模拟误删
        conn = db.get_db()
        conn.execute("DELETE FROM images")
        conn.commit()
        self.assertEqual(self.row_count("images"), 0)

        # 恢复要求在应用停止后执行：先关闭本进程所有连接，否则 Windows 上
        # -wal/-shm 被占用无法删除
        db.close_all_db()
        ok, detail = restore_db.restore_database(backup, self.db_path)
        self.assertTrue(ok, detail)
        self.assertEqual(self.row_count("images"), 2)

    def test_restore_removes_stale_wal_files(self):
        """替换数据库后必须清掉旧库的 -wal/-shm，否则会被重放进新库。"""
        backup = self._make_backup(images=1)
        db.close_all_db()
        # 人为制造旧库遗留的 WAL 文件
        for suffix in ("-wal", "-shm"):
            with open(self.db_path + suffix, "wb") as f:
                f.write(b"stale")
        ok, detail = restore_db.restore_database(backup, self.db_path)
        self.assertTrue(ok, detail)
        self.assertFalse(os.path.exists(self.db_path + "-wal"), "旧 -wal 未清理")
        self.assertFalse(os.path.exists(self.db_path + "-shm"), "旧 -shm 未清理")

    def test_restore_rejects_corrupted_backup(self):
        broken = os.path.join(self.tmpdir, "broken.db")
        with open(broken, "wb") as f:
            f.write(b"garbage" * 100)
        ok, detail, _ = restore_db.verify_backup(broken)
        self.assertFalse(ok)

    def test_safety_copy_created_before_restore(self):
        backup = self._make_backup(images=1)
        safety = restore_db._safety_copy(self.db_path)
        self.assertIsNotNone(safety)
        self.assertTrue(os.path.exists(safety))
        self.assertTrue(os.path.basename(safety).startswith("pre_restore_"))

    def test_restore_failure_keeps_original_database_and_wal(self):
        """替换失败时必须还原原库与 -wal，不能先删 WAL 再失败。"""
        backup = self._make_backup(images=1)
        conn = db.get_db()
        conn.execute("INSERT INTO settings (key, value) VALUES ('live_marker','1')")
        conn.commit()
        db.close_all_db()

        with open(self.db_path, "rb") as f:
            db_bytes = f.read()
        wal_path = self.db_path + "-wal"
        with open(wal_path, "wb") as f:
            f.write(b"uncheckpointed-committed-data")

        real = restore_db._swap_path
        state = {"failed": False}

        def flaky(src, dst):
            # 只让最终 "临时文件 -> 正式库" 这一步失败
            if dst == self.db_path and not state["failed"]:
                state["failed"] = True
                raise OSError("simulated lock")
            return real(src, dst)

        with mock.patch.object(restore_db, "_swap_path", side_effect=flaky):
            ok, detail = restore_db.restore_database(backup, self.db_path)

        self.assertFalse(ok)
        self.assertIn("lock", detail)
        with open(self.db_path, "rb") as f:
            self.assertEqual(f.read(), db_bytes, "恢复失败后原数据库被改动")
        with open(wal_path, "rb") as f:
            self.assertEqual(f.read(), b"uncheckpointed-committed-data",
                             "恢复失败后旧 WAL 丢失")
        self.assertFalse(os.path.exists(self.db_path + ".pre_restore"))
        self.assertFalse(os.path.exists(wal_path + ".pre_restore"))

        os.remove(wal_path)
        conn = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT value FROM settings WHERE key='live_marker'"
                ).fetchone()[0],
                "1",
            )
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM images").fetchone()[0], 1)
        finally:
            conn.close()

    def test_cli_requires_yes(self):
        backup = self._make_backup(images=1)
        env = dict(os.environ)
        env["DATABASE_PATH"] = self.db_path
        env["BACKUP_DIR"] = self.backup_dir
        proc = subprocess.run(
            [sys.executable, "tools/restore_db.py", "--backup", backup],
            cwd=ROOT, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace")
        self.assertEqual(proc.returncode, 2, "未带 --yes 时不应执行恢复")
        self.assertIn("预演", proc.stdout)
        # 数据未被改动
        self.assertEqual(self.row_count("images"), 1)

    def test_cli_preview_without_wal_and_json_output(self):
        """正常停服后没有 -wal，预演仍应成功且 --json 输出可直接解析。"""
        backup = self._make_backup(images=1)
        db.close_all_db()
        for suffix in ("-wal", "-shm"):
            path = self.db_path + suffix
            if os.path.exists(path):
                os.remove(path)

        env = dict(os.environ)
        env["DATABASE_PATH"] = self.db_path
        env["BACKUP_DIR"] = self.backup_dir
        proc = subprocess.run(
            [sys.executable, "tools/restore_db.py", "--backup", backup, "--json"],
            cwd=ROOT, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        data = json.loads(proc.stdout)
        self.assertFalse(data["ok"])
        self.assertIn("预演", data["error"])
        self.assertEqual(self.row_count("images"), 1)

    def test_cli_list_shows_backups(self):
        self._make_backup(images=1)
        env = dict(os.environ)
        env["DATABASE_PATH"] = self.db_path
        env["BACKUP_DIR"] = self.backup_dir
        proc = subprocess.run([sys.executable, "tools/restore_db.py", "--list"],
                              cwd=ROOT, env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("backup_20260101T000000Z.db", proc.stdout)


class TestUploadRestore(BackupRestoreBase):
    """上传目录恢复：必须与备份配对、整体替换、失败可回滚。"""

    def _backup(self, entries, images=1):
        for _ in range(images):
            self.add_image()
        db.checkpoint_wal("TRUNCATE")
        backup = os.path.join(self.backup_dir, "backup_20260101T000000Z.db")
        os.makedirs(self.backup_dir, exist_ok=True)
        backup_db._backup_database(self.db_path, backup)
        self.write_archive("backup_20260101T000000Z_uploads.tar.gz", entries)
        return backup

    def test_restore_requires_paired_archive(self):
        """恢复 A 备份时不得误用 B 备份的上传归档。"""
        backup = os.path.join(self.backup_dir, "backup_20260101T000000Z.db")
        os.makedirs(self.backup_dir, exist_ok=True)
        backup_db._backup_database(self.db_path, backup)
        self.write_archive("backup_20260201T000000Z_uploads.tar.gz",
                           {"uploads/other.webp": b"other"})
        keep = self.write_file(self.uploads, "keep.webp")

        ok, message = restore_db.restore_uploads(backup, force=True)
        self.assertFalse(ok)
        self.assertIn("配对", message)
        self.assertTrue(os.path.exists(keep), "未配对时不应改动上传目录")
        self.assertFalse(os.path.exists(os.path.join(self.uploads, "other.webp")))

    def test_restore_replaces_content_and_keeps_old_dir(self):
        backup = self._backup({
            "uploads/new.webp": b"NEW",
            "private_uploads/pending.webp": b"PENDING",
        })
        old = self.write_file(self.uploads, "old.webp")
        self.write_file(self.private, "old_private.webp")

        ok, message = restore_db.restore_uploads(backup, force=True)
        self.assertTrue(ok, message)
        with open(os.path.join(self.uploads, "new.webp"), "rb") as f:
            self.assertEqual(f.read(), b"NEW")
        self.assertFalse(os.path.exists(old), "旧文件未随目录替换")
        self.assertTrue(os.path.exists(os.path.join(self.private, "pending.webp")))
        self.assertFalse(os.path.exists(os.path.join(self.private, "old_private.webp")))

        kept = [
            n for n in os.listdir(self.tmpdir)
            if n.startswith("uploads.pre_restore_") or n.startswith("private_uploads.pre_restore_")
        ]
        self.assertEqual(len(kept), 2, kept)
        old_dir = os.path.join(self.tmpdir, next(n for n in kept if n.startswith("uploads.")))
        self.assertTrue(os.path.exists(os.path.join(old_dir, "old.webp")))

    def test_restore_rolls_back_when_swap_fails(self):
        backup = self._backup({
            "uploads/new.webp": b"NEW",
            "private_uploads/pending.webp": b"PENDING",
        })
        old = self.write_file(self.uploads, "old.webp")
        private_target = os.path.abspath(self.private)

        real = restore_db._swap_path
        state = {"failed": False}

        def flaky(src, dst):
            if dst == private_target and not state["failed"]:
                state["failed"] = True
                raise OSError("simulated lock")
            return real(src, dst)

        with mock.patch.object(restore_db, "_swap_path", side_effect=flaky):
            ok, message = restore_db.restore_uploads(backup, force=True)

        self.assertFalse(ok)
        self.assertIn("lock", message)
        self.assertTrue(os.path.exists(old), "切换失败后旧上传目录未还原")
        self.assertFalse(os.path.exists(os.path.join(self.uploads, "new.webp")))
        self.assertFalse(os.path.exists(os.path.join(self.private, "pending.webp")))
        self.assertFalse(any(
            ".restore_new_" in n for n in os.listdir(self.tmpdir)
        ), "解包临时目录未清理")


class TestBackupSetCheck(BackupRestoreBase):
    def _backup(self):
        self.add_image(filename="a.webp", thumb="a_t.webp")
        db.checkpoint_wal("TRUNCATE")
        os.makedirs(self.backup_dir, exist_ok=True)
        backup = os.path.join(self.backup_dir, "backup_20260101T000000Z.db")
        backup_db._backup_database(self.db_path, backup)
        return backup

    def test_complete_backup_set_passes(self):
        backup = self._backup()
        self.write_archive("backup_20260101T000000Z_uploads.tar.gz", {
            "uploads/a.webp": b"A",
            "private_uploads/a_t.webp": b"T",
        })
        report = restore_db.check_backup_set(backup)
        self.assertTrue(report["ok"], report["error"])
        self.assertEqual(report["uploads_files"], 2)
        self.assertEqual(report["expected_files"], 2)
        self.assertEqual(report["missing_files"], [])

    def test_missing_referenced_file_is_reported(self):
        backup = self._backup()
        self.write_archive("backup_20260101T000000Z_uploads.tar.gz", {
            "uploads/a.webp": b"A",
        })
        report = restore_db.check_backup_set(backup)
        self.assertFalse(report["ok"])
        self.assertIn("a_t.webp", report["missing_files"])

    def test_database_only_backup_passes_with_warning(self):
        backup = self._backup()
        report = restore_db.check_backup_set(backup)
        self.assertTrue(report["ok"])
        self.assertTrue(report["warning"])

    def test_cli_check_returns_nonzero_for_missing_files(self):
        backup = self._backup()
        self.write_archive("backup_20260101T000000Z_uploads.tar.gz", {
            "uploads/a.webp": b"A",
        })
        env = dict(os.environ)
        env["DATABASE_PATH"] = self.db_path
        env["BACKUP_DIR"] = self.backup_dir
        proc = subprocess.run(
            [sys.executable, "tools/restore_db.py", "--backup", backup, "--check", "--json"],
            cwd=ROOT, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace")
        self.assertEqual(proc.returncode, 1)
        data = json.loads(proc.stdout)
        self.assertFalse(data["ok"])


class TestCheckDbTool(BackupRestoreBase):
    def _collect(self, deep=False, backup_dir=None):
        orig = (check_db.DATABASE_PATH, check_db.BACKUP_DIR,
                check_db.UPLOADS_DIR, check_db.PRIVATE_UPLOADS_DIR)
        check_db.DATABASE_PATH = self.db_path
        check_db.BACKUP_DIR = backup_dir or self.backup_dir
        check_db.UPLOADS_DIR = self.uploads
        check_db.PRIVATE_UPLOADS_DIR = self.private
        try:
            return check_db.collect(deep=deep)
        finally:
            (check_db.DATABASE_PATH, check_db.BACKUP_DIR,
             check_db.UPLOADS_DIR, check_db.PRIVATE_UPLOADS_DIR) = orig

    def test_healthy_database_passes(self):
        self.add_image()
        os.makedirs(self.backup_dir, exist_ok=True)
        with open(os.path.join(self.backup_dir, "backup_20260101T000000Z.db"), "wb") as f:
            f.write(b"x")
        report = self._collect(deep=True)
        self.assertTrue(report["ok"], report["critical"])
        self.assertEqual(report["journal_mode"].lower(), "wal")
        self.assertIn("images", report["tables"])
        self.assertEqual(report["integrity_check"], "ok")

    def test_detects_like_count_drift(self):
        image_id = self.add_image(like_count=5)
        conn = db.get_db()
        conn.execute("INSERT INTO likes (image_id, ip, ua_hash, province, city, created_at)"
                     " VALUES (?, '1.1.1.1', 'uh', 'p', 'c', ?)", (image_id, _iso()))
        conn.commit()
        report = self._collect()
        self.assertGreaterEqual(report.get("like_count_drift", 0), 1)

    def test_detects_backup_dir_inside_uploads(self):
        report = self._collect(backup_dir=os.path.join(self.uploads, "backups"))
        self.assertFalse(report["ok"])
        self.assertTrue(any("备份目录" in c for c in report["critical"]))

    def test_missing_database_reported(self):
        orig = check_db.DATABASE_PATH
        check_db.DATABASE_PATH = os.path.join(self.tmpdir, "nope.db")
        try:
            report = check_db.collect()
        finally:
            check_db.DATABASE_PATH = orig
        self.assertFalse(report["ok"])

    def test_check_is_read_only(self):
        """健康检查不得修改数据，也不应把 -wal 清空。"""
        self.add_image()
        before = self.row_count("images")
        wal_before = os.path.getsize(self.db_path + "-wal") if os.path.exists(
            self.db_path + "-wal") else 0
        self._collect(deep=True)
        self.assertEqual(self.row_count("images"), before)
        wal_after = os.path.getsize(self.db_path + "-wal") if os.path.exists(
            self.db_path + "-wal") else 0
        self.assertEqual(wal_after, wal_before, "健康检查触发了 WAL 变更")


class TestUploadConsistency(BackupRestoreBase):
    def test_reports_missing_files(self):
        self.add_image(status="approved", filename="missing.webp", thumb="missing_t.webp")
        report = consistency.scan_uploads(older_than_days=0)
        self.assertEqual(len(report["missing_original"]), 1)
        self.assertEqual(len(report["missing_thumbnail"]), 1)

    def test_reports_misplaced_approved_file_in_private_dir(self):
        """approved 图片的文件若仍在私有目录，必须被识别为状态/目录不一致。"""
        image_id = self.add_image(status="approved",
                                  filename="approved_in_private.webp",
                                  thumb="approved_in_private_t.webp")
        self.write_file(self.private, "approved_in_private.webp")
        self.write_file(self.private, "approved_in_private_t.webp")

        report = consistency.scan_uploads(older_than_days=0)
        misplaced = report["misplaced_files"]
        self.assertEqual(len(misplaced), 2, misplaced)
        self.assertEqual({m["id"] for m in misplaced}, {image_id})
        self.assertEqual({m["actual"] for m in misplaced}, {"private_uploads"})
        self.assertEqual({m["expected"] for m in misplaced}, {"uploads"})

    def test_reports_pending_file_in_public_dir(self):
        self.add_image(status="pending", filename="pending_public.webp",
                       thumb="pending_public_t.webp")
        self.write_file(self.uploads, "pending_public.webp")
        self.write_file(self.uploads, "pending_public_t.webp")
        report = consistency.scan_uploads(older_than_days=0)
        misplaced = report["misplaced_files"]
        self.assertEqual(len(misplaced), 2)
        self.assertEqual({m["actual"] for m in misplaced}, {"uploads"})
        self.assertEqual({m["expected"] for m in misplaced}, {"private_uploads"})

    def test_correct_layout_reports_no_misplacement(self):
        self.add_image(status="approved", filename="ok.webp", thumb="ok_t.webp")
        self.write_file(self.uploads, "ok.webp")
        self.write_file(self.uploads, "ok_t.webp")
        self.add_image(status="pending", filename="p.webp", thumb="p_t.webp")
        self.write_file(self.private, "p.webp")
        self.write_file(self.private, "p_t.webp")
        report = consistency.scan_uploads(older_than_days=0)
        self.assertEqual(report["misplaced_files"], [])
        self.assertEqual(report["missing_original"], [])
        self.assertEqual(report["missing_thumbnail"], [])

    def test_orphans_are_deleted_only_when_old_enough(self):
        orphan_new = self.write_file(self.uploads, "orphan_new.webp")
        orphan_old = self.write_file(self.uploads, "orphan_old.webp")
        old_time = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
        os.utime(orphan_old, (old_time, old_time))

        report = consistency.scan_uploads(older_than_days=7)
        names = {item["path"] for item in report["orphan_files"]}
        self.assertIn("orphan_old.webp", names)

        deleted, failed = consistency.delete_orphans(report, older_than_days=7)
        self.assertEqual(failed, [])
        self.assertFalse(os.path.exists(orphan_old), "超期孤儿文件未被删除")
        self.assertTrue(os.path.exists(orphan_new), "未超期的孤儿文件被误删")

    def test_delete_orphans_rechecks_database_reference(self):
        """扫描与删除之间若文件被新记录引用，必须放弃删除。"""
        orphan = self.write_file(self.uploads, "racy.webp")
        old_time = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
        os.utime(orphan, (old_time, old_time))
        report = consistency.scan_uploads(older_than_days=7)
        self.assertTrue(any("racy.webp" in item["path"]
                            for item in report["orphan_files"]))

        # 扫描后立刻有数据库记录引用该文件
        self.add_image(status="approved", filename="racy.webp", thumb="racy_t.webp")
        deleted, failed = consistency.delete_orphans(report, older_than_days=7)
        self.assertEqual(deleted, [])
        self.assertEqual(len(failed), 1)
        self.assertTrue(os.path.exists(orphan), "文件已被新记录引用却仍被删除")

    def test_logo_files_not_treated_as_orphans(self):
        self.write_file(self.uploads, "logo_large_20260101.webp")
        report = consistency.scan_uploads(older_than_days=0)
        self.assertTrue(any("logo_large_20260101.webp" in item
                            for item in report["logo_files"]))
        self.assertFalse(any("logo_large_20260101.webp" in item["path"]
                             for item in report["orphan_files"]))


if __name__ == "__main__":
    unittest.main()
