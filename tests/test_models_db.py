"""数据库索引与查询（含 N+1 修复）的集成测试"""
import os
import shutil
import tempfile
import unittest

import app.database as db
import app.models as models
from app.limiter import check_upload_limit


class DbTestCase(unittest.TestCase):
    """使用独立临时数据库的测试基类"""

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.mkdtemp()
        cls._orig = db.DATABASE_PATH
        db.DATABASE_PATH = os.path.join(cls._tmpdir, "test.db")
        db.init_db()

    @classmethod
    def tearDownClass(cls):
        db.close_db()
        db.DATABASE_PATH = cls._orig
        shutil.rmtree(cls._tmpdir, ignore_errors=True)


class TestPerformanceIndexes(DbTestCase):
    def test_performance_indexes_exist(self):
        conn = db.get_db()
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
        names = {r["name"] for r in rows}
        for idx in (
            "idx_images_ip",
            "idx_images_ua_hash",
            "idx_images_status_reviewed",
            "idx_images_status_created",
            "idx_likes_ip",
            "idx_likes_province",
            "idx_likes_image_created",
        ):
            self.assertIn(idx, names)


class TestRecentUploadIps(DbTestCase):
    def _insert_image(self, ip, md5):
        return models.insert_image(
            f"{md5}.webp", f"{md5}_thumb.webp", 1000, 800, 100, 80,
            "desc", "author", md5, ip, "ua", "uahash", "prov", "city",
        )

    def test_like_count_included_and_no_n1(self):
        ip1, ip2 = "1.1.1.1", "2.2.2.2"
        id1 = self._insert_image(ip1, "m1")
        id2 = self._insert_image(ip1, "m2")
        self._insert_image(ip2, "m3")

        # 为 ip1 生成 4 条点赞（不同 ua_hash 避免唯一索引冲突）
        for ua in ("ua_a", "ua_b", "ua_c", "ua_d"):
            self.assertTrue(models.insert_like(id1, ip1, ua, "", ""))

        rows = {r["ip"]: dict(r) for r in models.get_recent_upload_ips(10)}
        self.assertEqual(rows[ip1]["like_count"], 4)
        self.assertEqual(rows[ip2]["like_count"], 0)
        self.assertEqual(rows[ip1]["upload_count"], 2)
        self.assertEqual(rows[ip2]["upload_count"], 1)

    def test_last_upload_time(self):
        ip = "9.9.9.9"
        self._insert_image(ip, "m_last")
        self.assertIsNotNone(models.get_last_upload_time(ip))


class TestUploadLimit(DbTestCase):
    def _insert_image(self, ip, md5):
        return models.insert_image(
            f"{md5}.webp", f"{md5}_thumb.webp", 1000, 800, 100, 80,
            "desc", "author", md5, ip, "ua", "uahash", "prov", "city",
        )

    def test_new_ip_allowed(self):
        allowed, msg = check_upload_limit("6.6.6.6", "uahash")
        self.assertTrue(allowed, msg)

    def test_rapid_upload_blocked(self):
        ip = "5.5.5.5"
        self._insert_image(ip, "m_block")
        allowed, msg = check_upload_limit(ip, "uahash")
        self.assertFalse(allowed)
        self.assertIn("频繁", msg)


if __name__ == "__main__":
    unittest.main()
