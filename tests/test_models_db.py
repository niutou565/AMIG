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


class TestTitleField(DbTestCase):
    def _insert_approved(self, title, description, author, ip):
        img_id = models.insert_image(
            "f.webp", "t.webp", 1000, 800, 100, 80,
            description, author, f"md5_{ip}", ip, "ua", "uahash",
            "江苏", "南京", title=title,
        )
        db.get_db().execute(
            "UPDATE images SET status='approved' WHERE id=?", (img_id,)
        )
        db.get_db().commit()
        return img_id

    def test_title_search(self):
        id1 = self._insert_approved("风景照片", "美丽风景", "张三", "1.1.1.1")
        id2 = self._insert_approved("美食记录", "好吃", "李四", "2.2.2.2")

        rows = models.search_approved_images("风景", 10, 0)
        self.assertEqual([r["id"] for r in rows], [id1])
        self.assertEqual(models.count_search_approved_images("风景"), 1)

        # 历史图片标题为空时，仍可通过简介搜索到
        db.get_db().execute("UPDATE images SET title='' WHERE id=?", (id2,))
        db.get_db().commit()
        self.assertEqual(models.count_search_approved_images("好吃"), 1)


class TestRandomImage(DbTestCase):
    def _insert_approved(self, ip):
        img_id = models.insert_image(
            "f.webp", "t.webp", 1000, 800, 100, 80,
            "desc", "author", f"md5_{ip}", ip, "ua", "uahash", "江苏", "南京",
        )
        db.get_db().execute(
            "UPDATE images SET status='approved' WHERE id=?", (img_id,)
        )
        db.get_db().commit()
        return img_id

    def test_exclude_returns_other(self):
        id1 = self._insert_approved("3.3.3.3")
        id2 = self._insert_approved("4.4.4.4")
        row = models.get_random_image_prefer_low_heat(exclude_id=id1)
        self.assertEqual(row["id"], id2)
        row = models.get_random_image_prefer_low_heat(exclude_id=id2)
        self.assertEqual(row["id"], id1)

    def test_returns_none_when_no_approved(self):
        db.get_db().execute("DELETE FROM images")
        db.get_db().commit()
        self.assertIsNone(models.get_random_image_prefer_low_heat())


class TestAdminLogs(DbTestCase):
    def test_search_filters(self):
        models.insert_admin_log(1, "admin", "login", "登录成功", "", ip="1.1.1.1", location="北京市")
        models.insert_admin_log(1, "admin", "approve", "通过", "image#1", ip="2.2.2.2", location="海外IP 美国")
        models.insert_admin_log(1, "admin", "delete", "删除", "image#2", ip="1.1.1.1", location="北京市")

        rows = models.search_admin_logs(action="approve")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ip"], "2.2.2.2")

        rows = models.search_admin_logs(ip="1.1.1.1")
        self.assertEqual(len(rows), 2)

        rows = models.search_admin_logs(ip="2.2.2.2")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["location"], "")

        self.assertEqual(models.count_admin_logs(action="delete"), 1)


if __name__ == "__main__":
    unittest.main()
