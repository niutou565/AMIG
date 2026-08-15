"""图片处理工具"""
import os
import hashlib
import uuid
from datetime import datetime
from PIL import Image
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


def process_image(file_data: bytes) -> tuple:
    """
    处理图片：剥离 EXIF、缩放到最长边 1920、转 WebP、生成缩略图
    返回 (result_dict, error_message)
    result_dict: {filename, thumb_filename, original_size, compressed_size,
                  width, height, content_md5, subdir}
    """
    from io import BytesIO

    try:
        img = Image.open(BytesIO(file_data))
    except Exception as e:
        return (None, f"无法打开图片文件：{str(e)}")

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

    # 转 WebP
    output = BytesIO()
    save_kwargs = {"quality": WEBP_QUALITY, "method": WEBP_EFFORT}
    if img.mode == 'RGBA':
        img.save(output, format='WEBP', **save_kwargs)
    else:
        img.save(output, format='WEBP', **save_kwargs)
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
    if thumb.mode == 'RGBA':
        thumb.save(thumb_output, format='WEBP', quality=75, method=WEBP_EFFORT)
    else:
        thumb.save(thumb_output, format='WEBP', quality=75, method=WEBP_EFFORT)
    thumb_data = thumb_output.getvalue()

    # 生成文件名和存储路径
    now = datetime.now()
    subdir = f"{now.year}/{now.month:02d}"
    filename = f"{uuid.uuid4().hex}.webp"
    thumb_filename = f"{uuid.uuid4().hex}_thumb.webp"

    full_dir = os.path.join(UPLOADS_DIR, subdir)
    os.makedirs(full_dir, exist_ok=True)

    # 写入文件
    filepath = os.path.join(full_dir, filename)
    with open(filepath, 'wb') as f:
        f.write(compressed_data)

    thumb_path = os.path.join(full_dir, thumb_filename)
    with open(thumb_path, 'wb') as f:
        f.write(thumb_data)

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