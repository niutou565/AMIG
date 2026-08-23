"""上传者展示文案格式化单元测试"""
import unittest

from app.display import location_label, uploader_label


class TestUploaderLabel(unittest.TestCase):
    def test_legacy_english_location_is_translated(self):
        self.assertEqual(location_label("Guangxi", "Gangkou"), "广西港口区")
        self.assertEqual(
            uploader_label(self._row("Guangxi", "Gangkou", "", "cn")),
            "广西港口区的匿名用户",
        )

    def _row(self, province="", city="", author="", region=""):
        return {
            "province": province,
            "city": city,
            "author": author,
            "region": region,
        }

    def test_named_with_region(self):
        self.assertEqual(
            uploader_label(self._row("广西", "柳州", "张三", "cn")),
            "广西柳州的张三",
        )

    def test_named_without_region(self):
        self.assertEqual(
            uploader_label(self._row("", "", "张三", "")),
            "未知地区的张三",
        )

    def test_named_overseas(self):
        self.assertEqual(
            uploader_label(self._row("", "", "张三", "overseas")),
            "海外地区的张三",
        )

    def test_anonymous_mainland(self):
        self.assertEqual(
            uploader_label(self._row("广西", "柳州", "", "cn")),
            "广西柳州的匿名用户",
        )

    def test_anonymous_overseas(self):
        self.assertEqual(
            uploader_label(self._row("", "", "", "overseas")),
            "海外地区的匿名用户",
        )

    def test_anonymous_unknown(self):
        self.assertEqual(
            uploader_label(self._row("", "", "", "")),
            "未知地区的匿名用户",
        )


if __name__ == "__main__":
    unittest.main()
