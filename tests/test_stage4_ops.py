"""第四阶段：MySQL 备份、恢复、健康检查与上传目录一致性测试。

全部使用独立的随机 MySQL 测试库与临时备份/上传目录，
不接触开发库、真实 backups/ 与 uploads/。
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import app.database as db
import app.image_utils as image_utils
import app.upload_consistency as consistency
from app import config
from tests.mysql_test_utils import create_test_database, drop_test_database
from tools import backup_db, check_db, mysql_cli, restore_db

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _iso(days_ago=0, seconds=0):
    moment = datetime.now(timezone.utc) - timedelta(days=days_ago, seconds=seconds)
    return moment.isoformat(timespec="seconds")


class OpsBase(unittest.TestCase):
    """每个用例独立的测试库 + 备份目录 + 上传目录。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="amig_ops_")
        self.backup_dir = os.path.join(self.tmpdir, "backups")
        self.uploads = os.path.join(self.tmpdir, "uploads")
        self.private = os.path.join(self.tmpdir, "private_uploads")
        for path in (self.backup_dir, self.uploads, self.private):
            os.makedirs(path, exist_ok=True)

        self.db_name = create_test_database()

        # 工具与业务模块都通过 app.config / image_utils 读取目录，统一改指临时目录
        self._patches = [
            mock.patch.object(config, "BACKUP_DIR", self.backup_dir),
            mock.patch.object(config, "UPLOADS_DIR", self.uploads),
            mock.patch.object(config, "PRIVATE_UPLOADS_DIR", self.private),
            mock.patch.object(image_utils, "UPLOADS_DIR", self.uploads),
            mock.patch.object(image_utils, "PRIVATE_UPLOADS_DIR", self.private),
        ]
        for patcher in self._patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self._patches):
            patcher.stop()
        db.close_all_db()
        drop_test_database(self.db_name)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def add_image(self, status="approved", filename=None, thumb=None,
                  days_ago=0, like_count=0):
        filename = filename or f"{os.urandom(6).hex()}.webp"
        thumb = thumb or f"thumb_{os.urandom(4).hex()}.webp"
        conn = db.get_db()
        cursor = conn.execute(
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
        return cursor.lastrowid

    def write_file(self, directory, name, size=64):
        path = os.path.join(directory, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"RIFF0000WEBP" + b"x" * size)
        return path

    def write_archive(self, filename, entries):
        """entries: {归档内路径: bytes}"""
        path = os.path.join(self.backup_dir, filename)
        with tarfile.open(path, "w:gz") as tar:
            for arcname, data in entries.items():
                info = tarfile.TarInfo(arcname)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return path

    def row_count(self, table):
        return db.get_db().execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]

    def make_backup(self, stamp="20260101T000000Z"):
        """用真实 mysqldump 生成一份备份，返回路径。"""
        target = os.path.join(self.backup_dir, f"backup_{stamp}.sql")
        ok, detail = backup_db._dump_database(target)
        self.assertTrue(ok, detail)
        return target

    def subprocess_env(self):
        env = dict(os.environ)
        env.update({
            "MYSQL_HOST": config.MYSQL_HOST,
            "MYSQL_PORT": str(config.MYSQL_PORT),
            "MYSQL_USER": config.MYSQL_USER,
            "MYSQL_PASSWORD": config.MYSQL_PASSWORD,
            "MYSQL_DATABASE": self.db_name,
            "BACKUP_DIR": self.backup_dir,
            "UPLOADS_DIR": self.uploads,
            "PRIVATE_UPLOADS_DIR": self.private,
        })
        return env


class TestBackupTool(OpsBase):
    def _run_main(self, extra_args=()):
        argv = ["backup_db.py", "--json"] + list(extra_args)
        with mock.patch.object(sys, "argv", argv), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = backup_db.main()
        return code, out.getvalue()

    def test_backup_creates_verified_file(self):
        self.add_image(filename="a.webp")
        code, output = self._run_main()
        self.assertEqual(code, 0, output)
        data = json.loads(output)
        self.assertTrue(data["ok"], data)
        self.assertTrue(os.path.exists(data["backup_file"]))
        self.assertEqual(data["row_counts"]["images"], 1)
        # 行数清单与备份同生共死
        self.assertTrue(os.path.exists(data["backup_file"] + ".manifest.json"))
        ok, detail, info = restore_db.verify_backup(data["backup_file"])
        self.assertTrue(ok, detail)
        self.assertIn("images", info["tables"])

    def test_verify_dump_detects_corrupted_file(self):
        broken = os.path.join(self.tmpdir, "broken.sql")
        with open(broken, "wb") as f:
            f.write(b"this is not a mysqldump file" * 10)
        ok, detail = backup_db._verify_dump(broken)
        self.assertFalse(ok)
        self.assertIn("Dump completed", detail)

    def test_verify_dump_detects_missing_tables(self):
        partial = os.path.join(self.tmpdir, "partial.sql")
        with open(partial, "w", encoding="utf-8") as f:
            f.write("-- MySQL dump\n-- Dump completed on 2026-01-01\n")
        ok, detail = backup_db._verify_dump(partial)
        self.assertFalse(ok)
        self.assertIn("业务表结构", detail)

    def test_prune_keeps_newest_n(self):
        names = [f"backup_2026010{i}T000000Z.sql" for i in range(1, 6)]
        for name in names:
            with open(os.path.join(self.backup_dir, name), "wb") as f:
                f.write(b"x")
            with open(os.path.join(self.backup_dir, name + ".manifest.json"), "w") as f:
                f.write("{}")
        removed = backup_db._prune(self.backup_dir, keep=2)
        remaining = sorted(n for n in os.listdir(self.backup_dir) if n.endswith(".sql"))
        self.assertEqual(remaining, names[-2:])
        self.assertEqual(len(removed), 3)
        self.assertFalse(os.path.exists(
            os.path.join(self.backup_dir, names[0] + ".manifest.json")))

    def test_prune_never_removes_latest(self):
        name = "backup_20260101T000000Z.sql"
        with open(os.path.join(self.backup_dir, name), "wb") as f:
            f.write(b"x")
        backup_db._prune(self.backup_dir, keep=0)
        self.assertTrue(os.path.exists(os.path.join(self.backup_dir, name)),
                        "keep=0 时不应删除任何备份")

    def test_backup_failure_does_not_overwrite_existing(self):
        """备份失败时不得删除或覆盖已有备份，也不留下 .part 残留。"""
        existing = os.path.join(self.backup_dir, "backup_20260101T000000Z.sql")
        with open(existing, "wb") as f:
            f.write(b"old backup")
        with mock.patch.object(mysql_cli, "find_mysqldump", return_value=None):
            code, output = self._run_main()
        self.assertEqual(code, 1, output)
        with open(existing, "rb") as f:
            self.assertEqual(f.read(), b"old backup")
        self.assertFalse(any(n.endswith(".part") for n in os.listdir(self.backup_dir)))

    def test_upload_archive_failure_keeps_existing_and_no_partial(self):
        """上传归档失败时不得覆盖已有归档，也不留下 .part 残留。"""
        target = os.path.join(self.backup_dir, "backup_20260101T000000Z_uploads.tar.gz")
        with open(target, "wb") as f:
            f.write(b"old archive")
        self.write_file(self.uploads, "a.webp")
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
        with mock.patch.object(backup_db, "_stamp", return_value="20260101T000000Z"):
            code1, out1 = self._run_main()
            code2, out2 = self._run_main()
        self.assertEqual((code1, code2), (0, 0), out1 + out2)
        backups = sorted(
            n for n in os.listdir(self.backup_dir)
            if n.startswith("backup_") and n.endswith(".sql")
        )
        self.assertEqual(len(backups), 2, backups)
        self.assertTrue(backups[1].endswith("_2.sql"), backups)


class TestRestoreTool(OpsBase):
    def test_verify_backup_rejects_missing_tables(self):
        fake = os.path.join(self.tmpdir, "fake.sql")
        with open(fake, "w", encoding="utf-8") as f:
            f.write("-- dump of unrelated table\nCREATE TABLE `unrelated` (x int);\n"
                    "-- Dump completed on 2026-01-01\n")
        ok, detail, _ = restore_db.verify_backup(fake)
        self.assertFalse(ok)
        self.assertIn("关键表", detail)

    def test_restore_replaces_database(self):
        self.add_image(filename="a.webp")
        self.add_image(filename="b.webp")
        backup = self.make_backup()
        db.get_db().execute("DELETE FROM likes")
        db.get_db().execute("DELETE FROM images")
        db.get_db().commit()
        self.assertEqual(self.row_count("images"), 0)

        ok, detail = restore_db.restore_database(backup)
        self.assertTrue(ok, detail)
        self.assertEqual(self.row_count("images"), 2)

    def test_restore_rejects_corrupted_backup(self):
        broken = os.path.join(self.tmpdir, "broken.sql")
        with open(broken, "wb") as f:
            f.write(b"garbage" * 100)
        ok, detail, _ = restore_db.verify_backup(broken)
        self.assertFalse(ok)

    def test_safety_copy_created_before_restore(self):
        self.add_image()
        safety = os.path.join(self.backup_dir, "pre_restore_20260101T000000Z.sql")
        ok, detail = restore_db._safety_dump(safety)
        self.assertTrue(ok, detail)
        self.assertTrue(os.path.basename(safety).startswith("pre_restore_"))
        with open(safety, "rb") as f:
            f.seek(max(0, os.path.getsize(safety) - 200))
            self.assertIn(b"Dump completed", f.read())

    def test_restore_failure_keeps_original_database(self):
        """导入失败时数据库内容必须保持原样。"""
        self.add_image(filename="keep.webp")
        self.add_image(filename="keep2.webp")
        backup = self.make_backup()
        db.get_db().execute(
            "INSERT INTO settings (`key`, value) VALUES ('live_marker', '1')"
        )
        db.get_db().commit()

        with mock.patch.object(restore_db, "_import_dump",
                               return_value=(False, "simulated import failure")):
            ok, detail = restore_db.restore_database(backup)
        self.assertFalse(ok)
        self.assertIn("simulated", detail)
        self.assertEqual(self.row_count("images"), 2, "恢复失败后原有数据丢失")
        self.assertEqual(
            db.get_db().execute(
                "SELECT value FROM settings WHERE `key`='live_marker'"
            ).fetchone()["value"],
            "1",
        )

    def test_cli_requires_yes(self):
        self.add_image()
        backup = self.make_backup()
        proc = subprocess.run(
            [sys.executable, "tools/restore_db.py", "--backup", backup],
            cwd=ROOT, env=self.subprocess_env(), capture_output=True, text=True,
            encoding="utf-8", errors="replace")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("预演", proc.stdout)
        self.assertEqual(self.row_count("images"), 1)

    def test_cli_list_shows_backups(self):
        self.add_image()
        self.make_backup()
        proc = subprocess.run(
            [sys.executable, "tools/restore_db.py", "--list"],
            cwd=ROOT, env=self.subprocess_env(), capture_output=True, text=True,
            encoding="utf-8", errors="replace")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("backup_20260101T000000Z.sql", proc.stdout)

    def test_cli_check_passes_for_valid_backup(self):
        self.add_image()
        backup = self.make_backup()
        proc = subprocess.run(
            [sys.executable, "tools/restore_db.py", "--backup", backup, "--check", "--json"],
            cwd=ROOT, env=self.subprocess_env(), capture_output=True, text=True,
            encoding="utf-8", errors="replace")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        data = json.loads(proc.stdout)
        self.assertTrue(data["ok"], data)


class TestUploadRestore(OpsBase):
    """上传目录恢复：必须与备份配对、整体替换、失败可回滚。"""

    def _backup(self, entries=None, images=1):
        for index in range(images):
            self.add_image(filename=f"img{index}.webp", thumb=f"img{index}_t.webp")
        backup = self.make_backup()
        if entries is not None:
            self.write_archive("backup_20260101T000000Z_uploads.tar.gz", entries)
        return backup

    def test_restore_requires_paired_archive(self):
        """恢复 A 备份时不得误用 B 备份的上传归档。"""
        backup = self._backup(entries=None, images=0)
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
            "uploads/img0.webp": b"NEW",
            "uploads/img0_t.webp": b"NEW_T",
            "private_uploads/pending.webp": b"PENDING",
        })
        old = self.write_file(self.uploads, "old.webp")

        ok, message = restore_db.restore_uploads(backup, force=True)
        self.assertTrue(ok, message)
        with open(os.path.join(self.uploads, "img0.webp"), "rb") as f:
            self.assertEqual(f.read(), b"NEW")
        self.assertFalse(os.path.exists(old), "旧文件未随目录替换")
        self.assertTrue(os.path.exists(os.path.join(self.private, "pending.webp")))

        kept = [
            n for n in os.listdir(self.tmpdir)
            if n.startswith("uploads.pre_restore_") or n.startswith("private_uploads.pre_restore_")
        ]
        self.assertEqual(len(kept), 2, kept)
        old_dir = os.path.join(self.tmpdir, next(n for n in kept if n.startswith("uploads.")))
        self.assertTrue(os.path.exists(os.path.join(old_dir, "old.webp")))

    def test_restore_reports_missing_referenced_files(self):
        """数据库引用的图片文件不在归档中时必须报告，而不是假成功。"""
        self.add_image(filename="need.webp", thumb="need_t.webp")
        backup = self.make_backup()
        self.write_archive("backup_20260101T000000Z_uploads.tar.gz", {
            "uploads/other.webp": b"OTHER",
        })
        ok, message = restore_db.restore_uploads(backup, force=True)
        self.assertFalse(ok)
        self.assertIn("need.webp", message)

    def test_restore_rolls_back_when_swap_fails(self):
        backup = self._backup({
            "uploads/img0.webp": b"NEW",
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
        self.assertFalse(os.path.exists(os.path.join(self.uploads, "img0.webp")))
        self.assertFalse(os.path.exists(os.path.join(self.private, "pending.webp")))
        self.assertFalse(any(
            ".restore_new_" in n for n in os.listdir(self.tmpdir)
        ), "解包临时目录未清理")


class TestBackupSetCheck(OpsBase):
    def test_complete_backup_set_passes(self):
        self.add_image(filename="a.webp", thumb="a_t.webp")
        backup = self.make_backup()
        self.write_archive("backup_20260101T000000Z_uploads.tar.gz", {
            "uploads/a.webp": b"A",
            "private_uploads/a_t.webp": b"T",
        })
        report = restore_db.check_backup_set(backup)
        self.assertTrue(report["ok"], report["error"])
        self.assertEqual(report["uploads_files"], 2)

    def test_database_only_backup_passes_with_warning(self):
        self.add_image()
        backup = self.make_backup()
        report = restore_db.check_backup_set(backup)
        self.assertTrue(report["ok"])
        self.assertTrue(report["warning"])

    def test_cli_check_returns_nonzero_for_broken_backup(self):
        broken = os.path.join(self.backup_dir, "backup_20260101T000000Z.sql")
        with open(broken, "wb") as f:
            f.write(b"garbage")
        proc = subprocess.run(
            [sys.executable, "tools/restore_db.py", "--backup", broken, "--check", "--json"],
            cwd=ROOT, env=self.subprocess_env(), capture_output=True, text=True,
            encoding="utf-8", errors="replace")
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(json.loads(proc.stdout)["ok"])


class TestCheckDbTool(OpsBase):
    def test_healthy_database_passes(self):
        self.add_image()
        with open(os.path.join(self.backup_dir, "backup_20260101T000000Z.sql"), "wb") as f:
            f.write(b"x")
        report = check_db.collect(deep=True)
        self.assertTrue(report["ok"], report["critical"])
        self.assertEqual(report["server"]["charset"], "utf8mb4")
        self.assertIn("images", report["tables"])
        self.assertEqual(report["like_count_drift"], 0)
        self.assertNotIn("check_table", report.get("critical", []))

    def test_detects_like_count_drift(self):
        image_id = self.add_image(like_count=5)
        db.get_db().execute(
            "INSERT INTO likes (image_id, ip, ua_hash, province, city, created_at)"
            " VALUES (?, '1.1.1.1', 'uh', 'p', 'c', ?)", (image_id, _iso()))
        db.get_db().commit()
        report = check_db.collect()
        self.assertGreaterEqual(report.get("like_count_drift", 0), 1)
        self.assertTrue(any("like_count" in w for w in report["warnings"]))

    def test_detects_backup_dir_inside_uploads(self):
        with mock.patch.object(config, "BACKUP_DIR",
                               os.path.join(self.uploads, "backups")):
            report = check_db.collect()
        self.assertFalse(report["ok"])
        self.assertTrue(any("备份目录" in c for c in report["critical"]))

    def test_unreachable_database_reported(self):
        original = db.MYSQL_DATABASE
        try:
            db._reset_pool()
            db.MYSQL_DATABASE = "amig_missing_database_xyz"
            report = check_db.collect()
        finally:
            db.MYSQL_DATABASE = original
            db._reset_pool()
        self.assertFalse(report["ok"])
        self.assertTrue(any("无法连接" in c for c in report["critical"]))

    def test_check_is_read_only(self):
        """健康检查不得修改数据。"""
        self.add_image()
        before = self.row_count("images")
        check_db.collect(deep=True)
        self.assertEqual(self.row_count("images"), before)


class TestUploadConsistency(OpsBase):
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
