"""image_utils 单元测试与边界情况测试"""
import io
import os
import shutil
import tempfile
import unittest

from PIL import Image, ImageChops

import app.image_utils as iu


def _make_image_bytes(fmt="PNG", size=(10, 10), mode="RGB", color=(255, 0, 0)):
    img = Image.new(mode, size, color)
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def _make_jpeg_with_orientation(size=(20, 10), orientation=6):
    """生成带 EXIF 方向标记的 JPEG"""
    img = Image.new("RGB", size, (120, 120, 120))
    exif = Image.Exif()
    exif[274] = orientation  # Orientation 标签
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


class TestValidateImage(unittest.TestCase):
    def test_valid_jpg(self):
        data = _make_image_bytes("JPEG")
        ok, err = iu.validate_image(data, "a.jpg")
        self.assertTrue(ok, err)

    def test_valid_jpeg_extension_case(self):
        data = _make_image_bytes("JPEG")
        ok, _ = iu.validate_image(data, "a.JPEG")
        self.assertTrue(ok)

    def test_valid_png(self):
        data = _make_image_bytes("PNG")
        ok, err = iu.validate_image(data, "a.png")
        self.assertTrue(ok, err)

    def test_valid_webp(self):
        data = _make_image_bytes("WEBP")
        ok, err = iu.validate_image(data, "a.webp")
        self.assertTrue(ok, err)

    def test_invalid_extension(self):
        data = _make_image_bytes("PNG")
        ok, err = iu.validate_image(data, "a.gif")
        self.assertFalse(ok)

    def test_magic_bytes_mismatch(self):
        # PNG 内容却声称是 jpg
        data = _make_image_bytes("PNG")
        ok, err = iu.validate_image(data, "a.jpg")
        self.assertFalse(ok)

    def test_empty_data(self):
        ok, _ = iu.validate_image(b"", "a.jpg")
        self.assertFalse(ok)

    def test_oversized(self):
        data = b"x" * (iu.MAX_UPLOAD_SIZE + 1)
        ok, err = iu.validate_image(data, "a.jpg")
        self.assertFalse(ok)


class TestProcessImage(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_uploads = iu.UPLOADS_DIR
        iu.UPLOADS_DIR = self.tmpdir

    def tearDown(self):
        iu.UPLOADS_DIR = self._orig_uploads
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_process_basic_png(self):
        data = _make_image_bytes("PNG", size=(100, 80), color=(10, 20, 30))
        res, err = iu.process_image(data)
        self.assertEqual(err, "")
        self.assertEqual(res["width"], 100)
        self.assertEqual(res["height"], 80)
        self.assertGreater(res["compressed_size"], 0)
        self.assertEqual(len(res["content_md5"]), 32)

    def test_process_downscales_oversized(self):
        data = _make_image_bytes("PNG", size=(3000, 1000), color=(1, 2, 3))
        res, err = iu.process_image(data)
        self.assertEqual(err, "")
        self.assertEqual(res["width"], 1920)
        self.assertEqual(res["height"], 640)

    def test_process_invalid_bytes(self):
        res, err = iu.process_image(b"this is not an image")
        self.assertIsNone(res)
        self.assertIsNotNone(err)

    def test_process_transparent_png(self):
        data = _make_image_bytes("PNG", size=(50, 50), mode="RGBA", color=(0, 0, 0, 0))
        res, err = iu.process_image(data)
        self.assertEqual(err, "")
        self.assertEqual(res["width"], 50)
        self.assertEqual(res["height"], 50)

    def test_process_applies_exif_orientation(self):
        # Orientation=6（顺时针旋转90°）应使 20x10 变为 10x20
        data = _make_jpeg_with_orientation(size=(20, 10), orientation=6)
        res, err = iu.process_image(data)
        self.assertEqual(err, "")
        self.assertEqual(res["width"], 10)
        self.assertEqual(res["height"], 20)

    def test_process_applies_watermark_to_original_and_thumbnail(self):
        data = _make_image_bytes("PNG", size=(400, 300), color=(40, 120, 220))
        plain, err = iu.process_image(data)
        self.assertEqual(err, "")
        watermarked, err = iu.process_image(
            data,
            watermark_text="AMIG TEST",
            author="tester",
            uploaded_at="2026-08-24T12:34:56+00:00",
        )
        self.assertEqual(err, "")

        for key in ("filename", "thumb_filename"):
            plain_path = os.path.join(self.tmpdir, plain[key])
            watermarked_path = os.path.join(self.tmpdir, watermarked[key])
            with Image.open(plain_path) as plain_img, Image.open(watermarked_path) as marked_img:
                difference = ImageChops.difference(
                    plain_img.convert("RGB"), marked_img.convert("RGB")
                )
                self.assertIsNotNone(difference.getbbox(), f"{key} 未应用水印")


if __name__ == "__main__":
    unittest.main()
