"""水印字体、字形与布局的回归测试"""
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

import app.image_utils as iu


TEXT_CASES = {
    "纯中文": "中文水印测试",
    "纯英文": "AMIG Watermark",
    "中英混合": "AMIG 图片水印 2026",
    "数字与标点": "0123456789 ，。！？：；（）【】《》、…— .,:;!?()-_/",
}

LAYOUT_SIZES = [
    (1920, 1080),  # 大横图
    (1080, 1920),  # 大竖图
    (800, 600),    # 常规横图
    (600, 800),    # 常规竖图
    (300, 200),    # 小横图
    (200, 300),    # 小竖图
    (120, 90),     # 很小横图
    (80, 60),      # 极小图
]


def _make_image(size=(400, 300), color=(30, 40, 50)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


class TestWatermarkFont(unittest.TestCase):
    def test_bundled_noto_font_is_used_without_system_font(self):
        with patch.object(iu, "WATERMARK_FONT_PATH", str(Path("Z:/not-exists/font.otf"))):
            font = iu._watermark_font(20)
        self.assertEqual(font.getname()[0], "Noto Sans SC")
        self.assertIn("assets", font.path)
        self.assertIn("NotoSansSC-Regular.otf", font.path)
        self.assertTrue(iu._font_supports_chinese(font))

    def test_font_without_chinese_glyphs_is_rejected(self):
        arial = Path("C:/Windows/Fonts/arial.ttf")
        if not arial.is_file():
            self.skipTest("当前环境没有 Arial，无法构造无中文字形字库场景")
        with patch.object(iu, "_watermark_font_candidates", return_value=[arial]):
            with self.assertRaises(iu.WatermarkFontError) as ctx:
                iu._watermark_font(16)
        self.assertIn("缺少简体中文字形", str(ctx.exception))

    def test_all_required_characters_have_real_glyphs(self):
        font = iu._watermark_font(20)
        sample = "".join(TEXT_CASES.values()) + "中文水印上传者匿名时间年月日"
        missing = iu._missing_glyph_chars(font, sample)
        self.assertEqual(missing, [], f"字体缺少字形: {missing}")

    def test_unsupported_character_raises_clear_error(self):
        font = iu._watermark_font(16)
        with self.assertRaises(iu.WatermarkFontError) as ctx:
            iu._assert_text_glyphs(font, ["中文😀"])
        self.assertIn("😀", str(ctx.exception))
        self.assertIn("不支持", str(ctx.exception))

    def test_font_load_failure_returns_no_files_and_clear_message(self):
        tmpdir = tempfile.mkdtemp()
        orig = iu.UPLOADS_DIR
        iu.UPLOADS_DIR = tmpdir
        arial = Path("C:/Windows/Fonts/arial.ttf")
        if not arial.is_file():
            iu.UPLOADS_DIR = orig
            shutil.rmtree(tmpdir, ignore_errors=True)
            self.skipTest("当前环境没有 Arial，无法构造字体加载失败场景")
        try:
            with patch.object(iu, "_watermark_font_candidates", return_value=[arial]):
                result, error = iu.process_image(
                    _make_image(), watermark_text="中文水印", author="测试员",
                    uploaded_at="2026-11-08T12:00:00+00:00"
                )
            self.assertIsNone(result)
            self.assertIn("水印字体加载失败", error)
            self.assertEqual(os.listdir(tmpdir), [])
        finally:
            iu.UPLOADS_DIR = orig
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestWatermarkLayout(unittest.TestCase):
    def _ops(self, size, text):
        canvas = Image.new("RGB", size, (20, 20, 20))
        draw = ImageDraw.Draw(canvas)
        return draw, iu._build_watermark_ops(
            draw, size, text, "中文作者", "2026-11-08T12:34:56+00:00"
        )

    def test_text_is_inside_image_and_does_not_overlap(self):
        for size in LAYOUT_SIZES:
            text = "AMIG 中文水印测试 0123456789 ，。！？"
            with self.subTest(size=size):
                draw, ops = self._ops(size, text)
                self.assertGreaterEqual(len(ops), 2, "应同时包含主水印与上传信息")

                boxes = []
                for line, font, xy, anchor in ops:
                    bbox = draw.textbbox(xy, line, font=font, anchor=anchor)
                    left, top, right, bottom = bbox
                    self.assertGreaterEqual(left, 0)
                    self.assertGreaterEqual(top, 0)
                    self.assertLessEqual(right, size[0])
                    self.assertLessEqual(bottom, size[1])
                    self.assertGreater(right - left, 0)
                    self.assertGreater(bottom - top, 0)
                    boxes.append(bbox)

                for index, first in enumerate(boxes):
                    for second in boxes[index + 1:]:
                        overlap_w = min(first[2], second[2]) - max(first[0], second[0])
                        overlap_h = min(first[3], second[3]) - max(first[1], second[1])
                        self.assertFalse(
                            overlap_w > 0 and overlap_h > 0,
                            f"水印文本重叠: {first} 与 {second}",
                        )

    def test_long_chinese_text_is_wrapped_without_overflow(self):
        text = "超长中文水印文字用于验证换行与截断不会超出图片边界" * 2
        for size in [(300, 200), (200, 300), (120, 90)]:
            with self.subTest(size=size):
                draw, ops = self._ops(size, text)
                for line, font, xy, anchor in ops:
                    bbox = draw.textbbox(xy, line, font=font, anchor=anchor)
                    self.assertGreaterEqual(bbox[0], 0)
                    self.assertGreaterEqual(bbox[1], 0)
                    self.assertLessEqual(bbox[2], size[0])
                    self.assertLessEqual(bbox[3], size[1])

    def test_rendered_glyph_shapes_match_bundled_font(self):
        """实际输出中的字形轮廓应与内置字体渲染结果一致，排除缺字方框。"""
        size = (400, 240)
        text = "中文水印测试"
        output = iu._draw_watermark(Image.new("RGB", size, (0, 0, 0)), text, "", "")
        reference = Image.new("RGB", size, (0, 0, 0))
        draw = ImageDraw.Draw(reference)
        for line, font, xy, anchor in iu._build_watermark_ops(
            draw, size, text, "", ""
        ):
            offset = max(1, round(getattr(font, "size", 12) * 0.06))
            draw.text(
                (xy[0] + offset, xy[1] + offset), line, font=font,
                fill=(0, 0, 0), anchor=anchor
            )
            draw.text(xy, line, font=font, fill=(228, 228, 228), anchor=anchor)

        out_mask = output.convert("L").point(lambda pixel: 255 if pixel > 200 else 0)
        ref_mask = reference.convert("L").point(lambda pixel: 255 if pixel > 200 else 0)
        self.assertIsNotNone(ref_mask.getbbox(), "参考渲染不应为空")
        out_pixels = list(out_mask.getdata())
        ref_pixels = list(ref_mask.getdata())
        self.assertEqual(out_pixels, ref_pixels, "输出字形与内置字体渲染不一致")


class TestWatermarkPipeline(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_uploads = iu.UPLOADS_DIR
        iu.UPLOADS_DIR = self.tmpdir

    def tearDown(self):
        iu.UPLOADS_DIR = self._orig_uploads
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _process(self, size, text, author="中文作者", uploaded_at="2026-11-08T12:34:56+00:00"):
        data = _make_image(size=size, color=(35, 80, 140))
        result, error = iu.process_image(data, watermark_text=text, author=author, uploaded_at=uploaded_at)
        self.assertEqual(error, "")
        self.assertIsNotNone(result)
        path = os.path.join(self.tmpdir, result["filename"])
        with Image.open(path) as img:
            return result, img.convert("RGB")

    def test_watermark_text_categories_render_successfully(self):
        for name, text in TEXT_CASES.items():
            with self.subTest(category=name):
                _, img = self._process((400, 300), text)
                self.assertEqual(img.size, (400, 300))
                # 底部带状区域必须出现明显水印像素
                bottom = img.crop((0, 180, 400, 300)).convert("L")
                self.assertGreater(max(bottom.getdata()), 180, f"{name} 水印不可见")

    def test_landscape_portrait_and_small_images(self):
        for size in LAYOUT_SIZES:
            with self.subTest(size=size):
                _, img = self._process(size, "AMIG 中文水印 2026")
                self.assertEqual(img.size, size)
                bottom = img.crop((0, int(size[1] * 0.6), size[0], size[1]))
                self.assertGreater(max(bottom.convert("L").getdata()), 170, "小图/竖图水印不可见")

    def test_watermark_does_not_obscure_center(self):
        plain = Image.open(io.BytesIO(_make_image(size=(400, 300), color=(35, 80, 140)))).convert("RGB")
        _, marked = self._process((400, 300), "AMIG 中文水印")
        center_box = (80, 60, 320, 240)  # 去掉底部水印区
        before = plain.crop(center_box)
        after = marked.crop(center_box)
        self.assertEqual(list(before.getdata()), list(after.getdata()), "水印遮挡了图片中心区域")

    def test_watermark_is_translucent_not_opaque(self):
        # 直接检查绘制阶段，避免 WebP 有损压缩边缘振铃导致像素被抬到 255
        img = Image.new("RGB", (400, 300), (35, 80, 140))
        marked = iu._draw_watermark(img, "中文水印", "", "")
        bottom = marked.crop((0, 220, 400, 300))
        pixels = list(bottom.getdata())
        bright = [p for p in pixels if max(p) > 180]
        self.assertGreater(len(bright), 0)
        self.assertLess(max(max(p) for p in bright), 255, "水印不应完全不透明")
        self.assertLess(len(bright) / len(pixels), 0.25, "水印遮挡面积过大")


if __name__ == "__main__":
    unittest.main()
