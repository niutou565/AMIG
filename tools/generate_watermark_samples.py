"""生成水印人工检查样图：覆盖中英文、数字标点、横竖图和小尺寸图片。"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app.image_utils as image_utils  # noqa: E402

OUTPUT_DIR = ROOT / "docs" / "watermark_samples"

SAMPLES = [
    ("01_chinese_landscape.png", (1600, 900), "中文水印测试 · 匿名图片社区", "中文作者", "2026-11-08T12:34:56+00:00"),
    ("02_english_landscape.png", (1600, 900), "AMIG Watermark 2026", "Anonymous", "2026-11-08T12:34:56+00:00"),
    ("03_mixed_landscape.png", (1600, 900), "AMIG 图片水印 · 2026 社区", "混合作者", "2026-11-08T12:34:56+00:00"),
    ("04_digits_punct_landscape.png", (1600, 900), "0123456789 ，。！？：；（）【】《》、…—", "标点测试", "2026-11-08T12:34:56+00:00"),
    ("05_chinese_portrait.png", (900, 1400), "中文竖图水印测试", "竖图作者", "2026-11-08T12:34:56+00:00"),
    ("06_chinese_small.png", (320, 240), "中文小图水印", "小图作者", "2026-11-08T12:34:56+00:00"),
    ("07_chinese_tiny.png", (160, 120), "中文极小图", "匿名用户", "2026-11-08T12:34:56+00:00"),
    ("08_long_text_small.png", (320, 240), "超长中文水印文字用于验证换行与截断不会超出图片边界", "长文本作者", "2026-11-08T12:34:56+00:00"),
]


def make_background(size):
    """生成带纹理的渐变背景，方便观察水印透明度与可读性。"""
    width, height = size
    img = Image.new("RGB", size)
    draw = ImageDraw.Draw(img)
    for y in range(height):
        ratio = y / max(1, height - 1)
        color = (
            int(20 + 90 * ratio),
            int(35 + 55 * (1 - ratio)),
            int(70 + 80 * ratio),
        )
        draw.line((0, y, width, y), fill=color)
    for index in range(6):
        x = int(width * (index + 1) / 7)
        draw.ellipse(
            (x - 30, height // 3 - 30, x + 30, height // 3 + 30),
            fill=(180, 170, 150),
        )
    return img


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for filename, size, text, author, uploaded_at in SAMPLES:
        img = image_utils._draw_watermark(make_background(size), text, author, uploaded_at)
        path = OUTPUT_DIR / filename
        img.save(path, format="PNG")
        print(f"generated {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
