# -*- coding: utf-8 -*-
"""第七阶段：搜索准确性、中文检索、分页排序与权限一致性测试。"""
import os
import unittest

from fastapi.testclient import TestClient

import app.auth as auth
import app.database as db
import app.models as models
from app.main import app
from tests.mysql_test_utils import create_test_database, drop_test_database


def _reset_db_pool():
    """关闭全部连接并重置连接池（用例间彻底隔离）。"""
    db.close_all_db()


class SearchDbCase(unittest.TestCase):
    """每个用例使用独立 MySQL 测试库，避免相互干扰。"""

    def setUp(self):
        self.db_name = create_test_database()

    def tearDown(self):
        drop_test_database(self.db_name)
        _reset_db_pool()

    def add_image(self, title="", description="", author="", status="approved",
                  reviewed_at="2026-01-01T00:00:00+00:00", ip="1.1.1.1",
                  province="江苏", city="南京"):
        conn = db.get_db()
        cursor = conn.execute(
            "INSERT INTO images (filename, thumb_filename, original_size,"
            " compressed_size, width, height, description, title, author,"
            " content_md5, ip, ua, ua_hash, province, city, region, status,"
            " reviewed_at, created_at) VALUES (?,?,1000,800,800,600,?,?,?,"
            "?,?, 'UA','uh',?,?,'',?,?,?)",
            (
                f"{os.urandom(6).hex()}.webp", f"{os.urandom(6).hex()}_t.webp",
                description, title, author, os.urandom(8).hex(), ip,
                province, city, status, reviewed_at,
                "2026-01-01T00:00:00+00:00",
            ),
        )
        conn.commit()
        return cursor.lastrowid

    def search_ids(self, query, limit=50, offset=0):
        return [r["id"] for r in models.search_approved_images(query, limit, offset)]


class TestKeywordHandling(SearchDbCase):
    def test_chinese_keyword_matches_title_description_author(self):
        by_title = self.add_image(title="美丽风景照片")
        by_desc = self.add_image(description="这里的风景很好")
        by_author = self.add_image(author="风景摄影师")
        self.add_image(title="城市夜景")

        ids = self.search_ids("风景")
        self.assertEqual(sorted(ids), sorted([by_title, by_desc, by_author]))

    def test_english_keyword_case_insensitive(self):
        target = self.add_image(title="Sunset Lake")
        self.add_image(title="城市夜景")
        self.assertEqual(self.search_ids("sunset"), [target])
        self.assertEqual(self.search_ids("LAKE"), [target])

    def test_mixed_chinese_english_keyword(self):
        target = self.add_image(title="Sunset 湖畔")
        hit = self.add_image(description="拍摄于 Sunset 湖畔 2024")
        ids = self.search_ids("Sunset 湖畔")
        self.assertIn(target, ids)
        self.assertIn(hit, ids)
        self.assertEqual(self.search_ids("Sunset 沙漠"), [])

    def test_multi_term_requires_all_terms(self):
        both = self.add_image(title="城市 夜景 摄影")
        self.add_image(title="城市 白天")
        self.add_image(description="夜景")
        self.assertEqual(self.search_ids("城市 夜景"), [both])
        self.assertEqual(models.count_search_approved_images("城市 夜景"), 1)

    def test_multi_term_can_match_across_fields(self):
        target = self.add_image(title="城市", description="夜景")
        self.add_image(title="城市", description="白天")
        self.assertEqual(self.search_ids("城市 夜景"), [target])

    def test_multi_term_ranks_more_title_hits_first(self):
        two_title_hits = self.add_image(title="海滩 日落")
        one_title_hit = self.add_image(title="海滩", description="日落")
        no_title_hit = self.add_image(title="旅行", description="海滩 日落")
        self.assertEqual(
            self.search_ids("海滩 日落"),
            [two_title_hits, one_title_hit, no_title_hit],
        )

    def test_multi_term_is_capped(self):
        first_five = self.add_image(title="甲 乙 丙 丁 戊")
        all_terms = self.add_image(title="甲 乙 丙 丁 戊 己 庚")
        query = "甲 乙 丙 丁 戊 己 庚"
        self.assertEqual(len(models.split_search_terms(query)), models.MAX_SEARCH_TERMS)
        # 只使用前 5 个词，因此两条记录都命中
        self.assertEqual(sorted(self.search_ids(query)), sorted([first_five, all_terms]))

    def test_whitespace_is_trimmed_and_collapsed(self):
        target = self.add_image(title="风景 照片")
        self.assertEqual(self.search_ids("  风景 照片  "), [target])
        # 用户输入连续空格时按单个空格匹配
        self.assertEqual(self.search_ids("风景   照片"), [target])

    def test_empty_and_whitespace_only_query(self):
        a = self.add_image(title="一")
        b = self.add_image(title="二")
        self.assertEqual(models.normalize_search_query("   "), "")
        self.assertEqual(models.normalize_search_query(None), "")
        # 空关键词等价于浏览全部已通过图片
        self.assertEqual(sorted(self.search_ids("")), sorted([a, b]))
        self.assertEqual(models.count_search_approved_images("   "), 2)

    def test_like_wildcards_are_literal(self):
        literal_pct = self.add_image(title="100% 纯色")
        literal_underscore = self.add_image(title="a_b 命名")
        self.add_image(title="普通标题")

        # 只有真正含 % 的记录会命中，不再匹配全部记录
        self.assertEqual(self.search_ids("%"), [literal_pct])
        self.assertEqual(self.search_ids("100%"), [literal_pct])
        self.assertEqual(self.search_ids("_"), [literal_underscore])
        self.assertEqual(self.search_ids("a_b"), [literal_underscore])

    def test_backslash_and_quote_do_not_break_sql(self):
        target = self.add_image(title=r"C:\photos\2024")
        self.add_image(title="普通标题")
        self.assertEqual(self.search_ids(r"C:\photos"), [target])
        # 单引号、分号、注释符等按字面匹配，不应抛异常
        for query in ("' OR 1=1 --", "'; DROP TABLE images; --", '"; --', "\\"):
            with self.subTest(query=query):
                self.assertIsInstance(self.search_ids(query), list)

    def test_long_query_is_truncated(self):
        long_query = "风景" * 200
        self.assertEqual(len(models.normalize_search_query(long_query)),
                         models.MAX_SEARCH_QUERY_LENGTH)
        self.assertEqual(self.search_ids(long_query), [])

    def test_count_matches_search_results(self):
        for i in range(7):
            self.add_image(title=f"测试风景 {i}")
        self.add_image(title="无关内容")
        self.assertEqual(models.count_search_approved_images("风景"), 7)
        self.assertEqual(len(self.search_ids("风景", limit=100)), 7)


class TestRelevanceAndPagination(SearchDbCase):
    def test_title_hit_ranks_before_author_and_description(self):
        desc_hit = self.add_image(title="A", description="关键词")
        author_hit = self.add_image(title="B", author="关键词")
        title_hit = self.add_image(title="关键词", description="C")
        self.assertEqual(self.search_ids("关键词"), [title_hit, author_hit, desc_hit])

    def test_same_reviewed_at_pagination_is_stable(self):
        ids = [self.add_image(title=f"同批风景 {i}") for i in range(5)]
        p1 = self.search_ids("同批风景", limit=2, offset=0)
        p2 = self.search_ids("同批风景", limit=2, offset=2)
        p3 = self.search_ids("同批风景", limit=2, offset=4)
        whole = self.search_ids("同批风景", limit=10, offset=0)

        self.assertEqual(p1 + p2 + p3, whole)
        self.assertEqual(len(set(whole)), 5)
        self.assertEqual(sorted(whole), sorted(ids))

    def test_reviewed_at_desc_within_same_tier(self):
        older = self.add_image(title="风景 旧", reviewed_at="2026-01-01T00:00:00+00:00")
        newer = self.add_image(title="风景 新", reviewed_at="2026-02-01T00:00:00+00:00")
        self.assertEqual(self.search_ids("风景"), [newer, older])


class TestSearchPermission(SearchDbCase):
    def test_only_approved_images_are_searchable(self):
        approved = self.add_image(title="审核风景", status="approved")
        self.add_image(title="审核风景", status="pending")
        self.add_image(title="审核风景", status="rejected")

        self.assertEqual(self.search_ids("审核风景"), [approved])
        self.assertEqual(models.count_search_approved_images("审核风景"), 1)

    def test_status_change_updates_front_search(self):
        image_id = self.add_image(title="状态风景", status="approved")
        self.assertEqual(self.search_ids("状态风景"), [image_id])

        db.get_db().execute(
            "UPDATE images SET status='rejected' WHERE id=?", (image_id,)
        )
        db.get_db().commit()
        self.assertEqual(self.search_ids("状态风景"), [])
        self.assertEqual(models.count_search_approved_images("状态风景"), 0)


class TestSearchConsistency(SearchDbCase):
    def test_new_image_becomes_searchable_after_approval(self):
        image_id = self.add_image(title="新增风景", status="pending")
        self.assertEqual(self.search_ids("新增风景"), [])

        models.try_approve_image(image_id)
        self.assertEqual(self.search_ids("新增风景"), [image_id])

    def test_title_or_description_update_is_reflected(self):
        image_id = self.add_image(title="旧标题", description="旧简介")

        models.update_image_content(image_id, title="新标题", description="新简介")
        self.assertEqual(self.search_ids("新标题"), [image_id])
        self.assertEqual(self.search_ids("旧标题"), [])
        self.assertEqual(self.search_ids("新简介"), [image_id])
        self.assertEqual(self.search_ids("旧简介"), [])

    def test_deleted_image_disappears_from_search(self):
        image_id = self.add_image(title="删除风景")
        self.assertEqual(self.search_ids("删除风景"), [image_id])

        models.delete_image(image_id)
        self.assertEqual(self.search_ids("删除风景"), [])
        self.assertEqual(models.count_search_approved_images("删除风景"), 0)


class TestAdminSearch(SearchDbCase):
    def test_admin_search_covers_ip_and_location(self):
        target = self.add_image(title="后台", ip="203.0.113.9",
                                province="广东", city="深圳", status="pending")
        self.add_image(title="后台", ip="198.51.100.1", province="江苏",
                       city="南京", status="pending")

        self.assertEqual(
            [r["id"] for r in models.search_images_admin(query="203.0.113", limit=10)],
            [target])
        self.assertEqual(
            [r["id"] for r in models.search_images_admin(query="深圳", limit=10)],
            [target])

    def test_admin_search_escapes_wildcards(self):
        literal = self.add_image(title="100% 完成", status="pending")
        self.add_image(title="普通待审", status="pending")

        rows = models.search_images_admin(query="%", limit=10)
        self.assertEqual([r["id"] for r in rows], [literal])
        self.assertEqual(models.count_images_admin(query="%"), 1)
        self.assertEqual(
            models.count_images_admin(query="普通"),
            len(models.search_images_admin(query="普通", limit=10)))

    def test_admin_log_search_escapes_wildcards(self):
        models.insert_admin_log(1, "admin", "approve", "详情 100%", "image#1", "1.1.1.1")
        models.insert_admin_log(1, "admin", "approve", "详情 普通", "image#2", "1.1.1.1")

        rows = models.search_admin_logs(query="%")
        self.assertEqual(len(rows), 1)
        self.assertIn("100%", rows[0]["detail"])
        self.assertEqual(models.count_admin_logs(query="%"), 1)


class TestSearchRoutes(unittest.TestCase):
    """路由层：关键词展示、空关键词、越界页码与 XSS 转义。"""

    @classmethod
    def setUpClass(cls):
        cls.db_name = create_test_database()
        models.set_setting("admin_domain", "admin.test")
        models.set_setting("enable_captcha", "0")
        models.set_setting("enable_dedup", "0")
        db.get_db().execute(
            "INSERT INTO admins (username, password_hash, created_at) VALUES (?,?,?)",
            ("tester", auth.hash_password("test-password"), models.now_iso()),
        )
        db.get_db().commit()

        conn = db.get_db()
        for i in range(30):
            conn.execute(
                "INSERT INTO images (filename, thumb_filename, original_size,"
                " compressed_size, width, height, description, title, author,"
                " content_md5, ip, ua, ua_hash, province, city, region, status,"
                " reviewed_at, created_at) VALUES (?,?,1,1,1,1,'',?, '',?,?,"
                " 'UA','uh','','','','approved', '2026-01-01T00:00:00+00:00',"
                " '2026-01-01T00:00:00+00:00')",
                (f"f{i}.webp", f"t{i}.webp", f"风景照片 {i}",
                 f"md5{i}", f"10.0.0.{i}"),
            )
        conn.commit()

    @classmethod
    def tearDownClass(cls):
        drop_test_database(cls.db_name)
        _reset_db_pool()

    def tearDown(self):
        _reset_db_pool()

    def client(self):
        return TestClient(app, base_url="https://admin.test",
                          raise_server_exceptions=False)

    def test_gallery_search_zh(self):
        response = self.client().get("/gallery", params={"q": "风景"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("风景照片", response.text)
        self.assertIn("共 30 张结果", response.text)
        # 翻页链接保留搜索关键词
        self.assertIn("q=%E9%A3%8E%E6%99%AF", response.text)

    def test_gallery_multi_term_search(self):
        client = self.client()
        matched = client.get("/gallery", params={"q": "风景 照片"})
        self.assertEqual(matched.status_code, 200)
        self.assertIn("共 30 张结果", matched.text)
        missing = client.get("/gallery", params={"q": "风景 不存在"})
        self.assertIn("没有找到", missing.text)

    def test_gallery_empty_result_shows_hint(self):
        response = self.client().get("/gallery", params={"q": "不存在的关键词"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("没有找到与「不存在的关键词」相关的图片", response.text)
        self.assertIn("返回全部图片", response.text)

    def test_gallery_whitespace_query_behaves_as_no_search(self):
        response = self.client().get("/gallery", params={"q": "   "})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("搜索「", response.text)

    def test_gallery_out_of_range_page_falls_back_to_last_page(self):
        response = self.client().get("/gallery", params={"q": "风景", "page": 999})
        self.assertEqual(response.status_code, 200)
        self.assertIn("风景照片", response.text)
        self.assertNotIn("没有找到", response.text)
        # 30 条结果、每页 24 条 → 越界页码回落到第 2 页
        self.assertIn("2 / 2", response.text)

    def test_gallery_query_is_escaped(self):
        payload = '"><script>alert(1)</script>'
        response = self.client().get("/gallery", params={"q": payload})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("<script>alert(1)</script>", response.text)
        self.assertIn("&lt;script&gt;", response.text)

    def test_admin_search_page_accepts_wildcard_query(self):
        client = self.client()
        page = client.get("/admin/login")
        import re
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        auth.clear_login_rate_limits()
        login = client.post(
            "/admin/login",
            data={"username": "tester", "password": "test-password",
                  "csrf_token": token},
            follow_redirects=False,
        )
        self.assertEqual(login.status_code, 302)

        for path, params in [
            ("/admin/images", {"query": "%", "page": 999}),
            ("/admin/images", {"query": "风景"}),
            ("/admin/review", {"query": "%"}),
            ("/admin/logs", {"q": "%"}),
        ]:
            with self.subTest(path=path, params=params):
                response = client.get(path, params=params)
                self.assertEqual(response.status_code, 200, response.text[:200])


if __name__ == "__main__":
    unittest.main()
