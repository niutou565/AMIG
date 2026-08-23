"""图片处理工具"""
import os
import hashlib
import uuid
from datetime import datetime
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from app.config import (
    UPLOADS_DIR, MAX_UPLOAD_SIZE, MAX_IMAGE_PIXELS,
    ALLOWED_EXTENSIONS, MAX_IMAGE_DIMENSION, THUMB_DIMENSION,
    WEBP_QUALITY, WEBP_EFFORT
)

# 设置像素上限防解压炸弹
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS

MAGIC_BYTES = {
    b'\xff\xd8\xff': 'jpg',
    b'\x89PNG\r\n\x1a\n': 'png',
    b'RIFF': 'webp',  # RIFF....WEBP
}


def validate_image(file_data: bytes, filename: str) -> tuple:
    """
    校验图片，返回 (valid, error_message)
    """
    # 扩展名检查
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return (False, f"不支持的图片格式：{ext}，仅支持 jpg、png、webp")

    # 大小检查
    if len(file_data) > MAX_UPLOAD_SIZE:
        return (False, f"图片大小超过限制（最大 {MAX_UPLOAD_SIZE // 1024 // 1024}MB）")

    # Magic bytes 校验
    if not check_magic_bytes(file_data, ext):
        return (False, "图片格式与文件内容不匹配，请上传正确的图片文件")

    return (True, "")


def check_magic_bytes(data: bytes, ext: str) -> bool:
    """验证文件头魔数"""
    if ext in ('.jpg', '.jpeg'):
        return data[:3] == b'\xff\xd8\xff'
    if ext == '.png':
        return data[:8] == b'\x89PNG\r\n\x1a\n'
    if ext == '.webp':
        return data[:4] == b'RIFF' and data[8:12] == b'WEBP'
    return False


def _atomic_write(path: str, data: bytes):
    """先写临时文件再原子替换，避免进程中断留下半截文件。"""
    tmp_path = path + ".tmp"
    with open(tmp_path, "wb") as f:
        f.write(data)
    os.replace(tmp_path, path)


def _watermark_font(size):
    candidates = (
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    )
    for path in candidates:
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                pass
    return ImageFont.load_default()


def _draw_watermark(img, watermark_text, author, uploaded_at):
    if not any((watermark_text, author, uploaded_at)):
        return img
    canvas = img.convert("RGBA")
    draw = ImageDraw.Draw(canvas)
    padding = max(12, min(canvas.size) // 40)
    font_size = max(8, min(28, min(canvas.size) // 28))
    font = _watermark_font(font_size)
    small_font = _watermark_font(max(8, font_size - 2))
    gap = max(4, font_size // 4)

    def draw_text(text, font_obj, x, y):
        draw.text((x + 2, y + 2), text, font=font_obj, fill=(0, 0, 0, 150))
        draw.text((x, y), text, font=font_obj, fill=(255, 255, 255, 220))

    right_lines = ((f"上传者：{author or '匿名用户'}", small_font), (f"上传时间：{uploaded_at}", small_font))
    right_heights = [draw.textbbox((0, 0), text, font=font_obj)[3] for text, font_obj in right_lines]
    right_y = canvas.height - padding - sum(right_heights) - gap
    for index, ((text, font_obj), height) in enumerate(zip(right_lines, right_heights)):
        text_width = draw.textbbox((0, 0), text, font=font_obj)[2]
        draw_text(text, font_obj, canvas.width - padding - text_width, right_y)
        right_y += height
        if index < len(right_lines) - 1:
            right_y += gap

    if watermark_text:
        text_height = draw.textbbox((0, 0), watermark_text, font=font)[3]
        draw_text(watermark_text, font, padding, canvas.height - padding - text_height)
    return canvas.convert("RGB")


def process_image(file_data: bytes, watermark_text="", author="", uploaded_at="") -> tuple:
    """
    处理图片：剥离 EXIF、缩放到最长边 1920、添加水印、转 WebP、生成缩略图
    返回 (result_dict, error_message)
    result_dict: {filename, thumb_filename, original_size, compressed_size,
                  width, height, content_md5, subdir}
    """
    from io import BytesIO

    full_dir = None
    filename = None
    thumb_filename = None

    try:
        img = Image.open(BytesIO(file_data))

        # 剥离 EXIF（转为 RGB 或 RGBA）
        if img.mode not in ('RGB', 'RGBA'):
            img = img.convert('RGBA')

        # 获取 EXIF 方向并矫正（若有）
        try:
            from PIL import ImageOps
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass

        original_size = len(file_data)
        width, height = img.size

        # 计算 MD5（处理前的内容）
        content_md5 = hashlib.md5(file_data).hexdigest()

        # 缩放（最长边 1920，仅当超出时）
        if max(width, height) > MAX_IMAGE_DIMENSION:
            ratio = MAX_IMAGE_DIMENSION / max(width, height)
            new_width = int(width * ratio)
            new_height = int(height * ratio)
            img = img.resize((new_width, new_height), Image.LANCZOS)
            width, height = new_width, new_height

        img = _draw_watermark(img, watermark_text, author, uploaded_at)

        # 转 WebP
        output = BytesIO()
        img.save(output, format='WEBP', quality=WEBP_QUALITY, method=WEBP_EFFORT)
        compressed_data = output.getvalue()
        compressed_size = len(compressed_data)

        # 生成缩略图
        thumb = img.copy()
        thumb_max = max(thumb.size)
        if thumb_max > THUMB_DIMENSION:
            ratio = THUMB_DIMENSION / thumb_max
            thumb = thumb.resize(
                (int(thumb.size[0] * ratio), int(thumb.size[1] * ratio)),
                Image.LANCZOS
            )

        thumb_output = BytesIO()
        thumb.save(thumb_output, format='WEBP', quality=75, method=WEBP_EFFORT)
        thumb_data = thumb_output.getvalue()

        # 生成文件名和存储路径
        now = datetime.now()
        subdir = f"{now.year}/{now.month:02d}"
        filename = f"{uuid.uuid4().hex}.webp"
        thumb_filename = f"{uuid.uuid4().hex}_thumb.webp"

        full_dir = os.path.join(UPLOADS_DIR, subdir)
        os.makedirs(full_dir, exist_ok=True)

        # 原子写入文件
        _atomic_write(os.path.join(full_dir, filename), compressed_data)
        _atomic_write(os.path.join(full_dir, thumb_filename), thumb_data)

        return ({
            "filename": f"{subdir}/{filename}",
            "thumb_filename": f"{subdir}/{thumb_filename}",
            "original_size": original_size,
            "compressed_size": compressed_size,
            "width": width,
            "height": height,
            "content_md5": content_md5,
            "subdir": subdir,
        }, "")
    except Exception:
        # 处理失败时清理可能残留的文件，避免留下孤儿文件
        if full_dir:
            for name in (filename, thumb_filename):
                if name:
                    try:
                        p = os.path.join(full_dir, name)
                        if os.path.exists(p):
                            os.remove(p)
                    except OSError:
                        pass
        return (None, "图片处理失败")


def delete_image_files(filename, thumb_filename):
    """删除图片文件"""
    for fname in (filename, thumb_filename):
        if fname:
            fpath = os.path.join(UPLOADS_DIR, fname)
            if os.path.exists(fpath):
                try:
                    os.remove(fpath)
                except OSError:
                    pass