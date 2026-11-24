"""图片处理工具"""
import os
import hashlib
import logging
import shutil
import uuid
import warnings
from datetime import datetime
from functools import lru_cache
from pathlib import Path, PurePosixPath
from PIL import Image, ImageDraw, ImageFont
from app.config import (
    UPLOADS_DIR, PRIVATE_UPLOADS_DIR, MAX_UPLOAD_SIZE, MAX_IMAGE_PIXELS,
    ALLOWED_EXTENSIONS, MAX_IMAGE_DIMENSION, THUMB_DIMENSION,
    WEBP_QUALITY, WEBP_EFFORT, WATERMARK_FONT_PATH, MAX_LOGO_DIMENSION
)

# 设置像素上限防解压炸弹
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
# 将 Pillow 的超像素告警升级为异常，确保超过上限的图片直接拒绝而不是仅告警
warnings.simplefilter("error", Image.DecompressionBombWarning)

PIXEL_LIMIT_ERROR = f"图片像素总数超过安全限制（最大 {MAX_IMAGE_PIXELS // 10000} 万像素）"
ANIMATED_IMAGE_ERROR = "暂不支持动图（多帧图片），请上传单帧图片"
LOGGER = logging.getLogger(__name__)

MAGIC_BYTES = {
    b'\xff\xd8\xff': 'jpg',
    b'\x89PNG\r\n\x1a\n': 'png',
    b'RIFF': 'webp',  # RIFF....WEBP
}

_FORMAT_BY_EXT = {".jpg": "jpg", ".jpeg": "jpg", ".png": "png", ".webp": "webp"}
_CONTENT_TYPE_FORMATS = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/pjpeg": "jpg",
    "image/png": "png",
    "image/x-png": "png",
    "image/webp": "webp",
}
_ALLOWED_OCTET_TYPES = {"", "application/octet-stream", "binary/octet-stream"}


def detect_image_format(data: bytes):
    """根据文件头返回实际图片格式（jpg/png/webp），无法识别返回 None。"""
    if data[:3] == b'\xff\xd8\xff':
        return "jpg"
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return "png"
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return "webp"
    return None


def validate_image(file_data: bytes, filename: str, content_type=None) -> tuple:
    """校验图片：扩展名、声明类型、文件头、像素上限与实际解码。"""
    from io import BytesIO

    # 扩展名检查
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return (False, f"不支持的图片格式：{ext}，仅支持 jpg、png、webp")

    # 大小检查
    if len(file_data) > MAX_UPLOAD_SIZE:
        return (False, f"图片大小超过限制（最大 {MAX_UPLOAD_SIZE // 1024 // 1024}MB）")

    expected_format = _FORMAT_BY_EXT.get(ext)
    actual_format = detect_image_format(file_data)
    if expected_format is None or actual_format != expected_format:
        return (False, "图片格式与文件内容不匹配，请上传正确的图片文件")

    # Content-Type 是客户端声明值，只作为附加一致性校验；无法替代内容检测。
    declared = str(content_type or "").split(";")[0].strip().lower()
    if declared not in _ALLOWED_OCTET_TYPES:
        declared_format = _CONTENT_TYPE_FORMATS.get(declared)
        if declared_format is None:
            return (False, f"不支持的 Content-Type：{declared}")
        if declared_format != actual_format:
            return (False, "声明的 Content-Type 与实际图片内容不匹配")

    # 实际解码校验：拒绝损坏/截断图片、解压炸弹与动图（当前处理管线只支持单帧）
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(file_data)) as probe:
                if probe.width * probe.height > MAX_IMAGE_PIXELS:
                    return (False, PIXEL_LIMIT_ERROR)
                if (getattr(probe, "is_animated", False)
                        or int(getattr(probe, "n_frames", 1) or 1) > 1):
                    return (False, ANIMATED_IMAGE_ERROR)
                probe.verify()
    except (Image.DecompressionBombWarning, Image.DecompressionBombError):
        return (False, PIXEL_LIMIT_ERROR)
    except Exception:
        return (False, "图片文件损坏或无法解码，请重新选择图片")

    return (True, "")


def check_magic_bytes(data: bytes, ext: str) -> bool:
    """验证文件头魔数（保留旧接口供测试/调用方使用）。"""
    expected = _FORMAT_BY_EXT.get(ext)
    return expected is not None and detect_image_format(data) == expected


def resolve_upload_path(relative, must_exist=True, uploads_dir=None):
    """把数据库中的相对路径安全解析到上传目录内。

    拒绝绝对路径、盘符、`..`、NUL、越界路径与符号链接；不存在时返回 None。
    """
    if relative is None:
        return None
    normalized = str(relative).replace("\\", "/")
    if not normalized or "\x00" in normalized or normalized.startswith("/"):
        return None
    parts = PurePosixPath(normalized).parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        return None
    if ":" in parts[0]:
        return None

    base = Path(uploads_dir or UPLOADS_DIR).resolve()
    try:
        candidate = (base / Path(*parts)).resolve()
    except (OSError, RuntimeError):
        return None
    try:
        candidate.relative_to(base)
    except ValueError:
        return None
    if candidate.is_symlink():
        return None
    if must_exist and not candidate.is_file():
        return None
    return candidate


def _atomic_write(path: str, data: bytes):
    """先写临时文件再原子替换，避免进程中断留下半截文件。"""
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "wb") as f:
            f.write(data)
        os.replace(tmp_path, path)
    except OSError:
        # 写入或替换失败时清理临时文件，避免上传目录残留 .tmp 垃圾
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise


class WatermarkFontError(RuntimeError):
    """水印字体不可用，或字体缺少简体中文字形"""


_BUNDLED_WATERMARK_FONT = Path(__file__).resolve().parent / "assets" / "fonts" / "NotoSansSC-Regular.otf"
# 用于检测字体是否真的包含中文字形（包含常用标点）
_FONT_PROBE_TEXT = "中文水印上传者匿名时间年月日"
_FONT_PROBE_PUNCT = "，。！？：；（）【】《》、…—"


def _watermark_font_candidates():
    """返回水印字体候选路径，内置字体优先，系统字体仅作为兜底。"""
    candidates = [Path(WATERMARK_FONT_PATH), _BUNDLED_WATERMARK_FONT]
    candidates.extend([
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf"),
        Path("/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc"),
        Path("C:/Windows/Fonts/Noto Sans SC (TrueType).otf"),
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
    ])
    unique = []
    seen = set()
    for path in candidates:
        key = str(path).lower()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


@lru_cache(maxsize=128)
def _load_font_file(path: str, size: int):
    return ImageFont.truetype(path, int(size))


def _missing_glyph_chars(font, text) -> list:
    """返回字体中缺少字形（会渲染成 .notdef 方框）的字符。"""
    try:
        notdef = font.getmask("\uffff").histogram()
    except Exception:
        return list(text)
    missing = []
    for char in text:
        if char.isspace():
            continue
        try:
            if font.getmask(char).histogram() == notdef:
                missing.append(char)
        except Exception:
            missing.append(char)
    return missing


def _font_supports_chinese(font) -> bool:
    """确认字体具备中文字形而不是渲染成方框。"""
    return not _missing_glyph_chars(font, _FONT_PROBE_TEXT + _FONT_PROBE_PUNCT)


@lru_cache(maxsize=32)
def _font_file_supports_chinese(path: str) -> bool:
    """按字体文件缓存中文支持检测结果，避免每个字号都重复探测。"""
    try:
        probe_font = _load_font_file(path, 16)
    except (OSError, ValueError):
        return False
    return _font_supports_chinese(probe_font)


def _assert_text_glyphs(font, texts):
    """水印文字含字体不支持的字符时给出明确错误，避免 silently 输出缺字方框。"""
    sample = "".join(texts)
    missing = sorted(set(_missing_glyph_chars(font, sample)))
    if missing:
        raise WatermarkFontError(
            "水印文字包含当前字体不支持的字符："
            f"{'、'.join(missing)}，请更换 WATERMARK_FONT_PATH 字体或修改水印文字"
        )


def _watermark_font(size: int):
    """加载支持简体中文的水印字体；失败时抛出明确异常，绝不静默使用无中文字形的字体。"""
    errors = []
    for path in _watermark_font_candidates():
        if not path.is_file():
            continue
        try:
            font = _load_font_file(str(path.resolve()), max(1, int(size)))
        except (OSError, ValueError) as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        if not _font_file_supports_chinese(str(path.resolve())):
            errors.append(f"{path.name}: 缺少简体中文字形")
            continue
        return font

    detail = "；".join(errors) if errors else "未找到候选字体文件"
    raise WatermarkFontError(
        "水印字体加载失败：请配置 WATERMARK_FONT_PATH 指向支持简体中文的字体文件"
        f"（{detail}）"
    )


def _text_width(draw, text, font) -> float:
    return draw.textlength(text, font=font)


def _line_height(font) -> int:
    ascent, descent = font.getmetrics()
    return ascent + descent


def _truncate_text(draw, text, font, max_width: float) -> str:
    """按像素宽度截断并加省略号，确保文字不会超出可用区域。"""
    if max_width <= 0:
        return ""
    if _text_width(draw, text, font) <= max_width:
        return text
    ellipsis = "…"
    if _text_width(draw, ellipsis, font) > max_width:
        return ""
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if _text_width(draw, text[:mid] + ellipsis, font) <= max_width:
            low = mid
        else:
            high = mid - 1
    return text[:low] + ellipsis


def _fit_multiline_font(draw, texts, max_width: float, start_size: int, min_size: int = 7):
    """选择能让所有单行文本都放下的最大字号。"""
    for size in range(start_size, min_size - 1, -1):
        font = _watermark_font(size)
        if all(_text_width(draw, text, font) <= max_width for text in texts):
            return size, font
    return min_size, _watermark_font(min_size)


def _wrap_text(draw, text, font, max_width: float, max_lines: int):
    """按像素宽度逐字符换行，最多保留 max_lines 行，其余截断。"""
    if _text_width(draw, text, font) <= max_width:
        return [text]
    lines = []
    current = ""
    for char in text:
        candidate = current + char
        if _text_width(draw, candidate, font) <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = char
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        merged = "".join(lines[max_lines - 1:])
        lines = lines[:max_lines - 1] + [_truncate_text(draw, merged, font, max_width)]
    return lines


def _watermark_info_texts(author, uploaded_at, min_side):
    if min_side < 80:
        # 极小图片用单行紧凑信息，避免两行水印占满画面
        parts = [author or "匿名用户"]
        if uploaded_at:
            parts.append(uploaded_at)
        return [" · ".join(parts)]
    return [
        f"上传者：{author or '匿名用户'}",
        f"上传时间：{uploaded_at or '未知'}",
    ]


def _build_watermark_ops(draw, canvas_size, watermark_text, author, uploaded_at):
    """计算水印绘制指令 (text, font, (x, y), anchor)。

    布局规则：右下水印信息两行右对齐；主水印优先放在左下，与信息块横向
    空间不足时改为堆叠在信息块上方。所有文本都会按像素宽度收缩/换行/截断，
    确保不越界、不重叠。
    """
    width, height = canvas_size
    if not any((watermark_text, author, uploaded_at)):
        return []

    min_side = min(width, height)
    padding = max(3, min(24, round(min_side * 0.02)))
    available_w = max(1, width - 2 * padding)
    available_h = max(1, height - 2 * padding)
    base_size = max(8, min(34, round(min_side * 0.035)))

    info_texts = _watermark_info_texts(author, uploaded_at, min_side)
    info_size, info_font = _fit_multiline_font(
        draw, info_texts, available_w, max(7, round(base_size * 0.78))
    )
    info_texts = [_truncate_text(draw, text, info_font, available_w) for text in info_texts]
    info_width = max(_text_width(draw, text, info_font) for text in info_texts)
    info_line_h = _line_height(info_font)
    info_line_gap = max(2, round(info_size * 0.2))
    info_h = len(info_texts) * info_line_h + (len(info_texts) - 1) * info_line_gap
    info_ascent, info_descent = info_font.getmetrics()

    info_ops = []
    bottom_baseline = height - padding - info_descent
    baseline = bottom_baseline
    for text in reversed(info_texts):
        info_ops.append((text, info_font, (width - padding, baseline), "rs"))
        baseline -= info_line_h + info_line_gap

    main_text = (watermark_text or "").strip()
    _assert_text_glyphs(info_font, [main_text, author or "", uploaded_at or ""])
    if not main_text:
        return info_ops

    block_gap = max(4, round(base_size * 0.5))
    main_ops = []
    side_width = available_w - info_width - block_gap
    side_by_side = False

    # 优先左下布局：主水印与右下信息块并排
    if side_width > 0:
        for size in range(base_size, 8, -1):
            font = _watermark_font(size)
            if _text_width(draw, main_text, font) <= side_width:
                main_ascent, main_descent = font.getmetrics()
                if main_ascent + main_descent <= available_h:
                    main_baseline = height - padding - main_descent
                    main_ops.append((main_text, font, (padding, main_baseline), "ls"))
                    side_by_side = True
                break

    if not side_by_side:
        # 横向放不下时堆叠在信息块上方，最多两行，并同步收缩字号
        main_line_gap = max(2, round(base_size * 0.25))
        main_lines = None
        main_font = None
        for size in range(base_size, 7, -1):
            font = _watermark_font(size)
            lines = _wrap_text(draw, main_text, font, available_w, max_lines=2)
            line_h = _line_height(font)
            gap = max(2, round(size * 0.25))
            block_h = len(lines) * line_h + (len(lines) - 1) * gap
            if block_h + block_gap + info_h <= available_h:
                main_lines, main_font = lines, font
                main_line_gap = gap
                break
        if main_lines is None:
            main_font = _watermark_font(8)
            main_lines = _wrap_text(draw, main_text, main_font, available_w, max_lines=2)
            main_line_gap = max(2, round(8 * 0.25))

        main_descent = main_font.getmetrics()[1]
        main_line_h = _line_height(main_font)
        info_top = bottom_baseline - (len(info_texts) - 1) * (info_line_h + info_line_gap) - info_ascent
        main_bottom_baseline = info_top - block_gap - main_descent
        main_count = len(main_lines)
        for index, line in enumerate(main_lines):
            line_baseline = main_bottom_baseline - (main_count - 1 - index) * (main_line_h + main_line_gap)
            main_ops.append((line, main_font, (padding, line_baseline), "ls"))

    return main_ops + info_ops


def _draw_watermark(img, watermark_text, author, uploaded_at, preserve_alpha=False):
    if not any((watermark_text, author, uploaded_at)):
        return img
    canvas = img.convert("RGBA")
    # 在透明图层绘制水印后再合成，确保文字半透明而不是直接覆盖原图
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for text, font, xy, anchor in _build_watermark_ops(
        draw, canvas.size, watermark_text, author, uploaded_at
    ):
        offset = max(1, round(getattr(font, "size", 12) * 0.06))
        draw.text(
            (xy[0] + offset, xy[1] + offset), text, font=font,
            fill=(0, 0, 0, 160), anchor=anchor
        )
        draw.text(xy, text, font=font, fill=(255, 255, 255, 228), anchor=anchor)
    composed = Image.alpha_composite(canvas, overlay)
    # 保留透明通道：透明 PNG/WebP 不能因为加水印被静默转为不透明 RGB
    return composed if preserve_alpha else composed.convert("RGB")


def _open_image_checked(file_data: bytes):
    """打开图片并将 Pillow 的超像素告警升级为异常。

    除模块级 warnings 过滤器外，这里再使用局部 catch_warnings：
    即使调用方（如测试框架）重置了全局 warning 过滤器，超限图片依然会被拒绝。
    """
    from io import BytesIO

    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        img = Image.open(BytesIO(file_data))
    # Image.open 只读取文件头，此时尚未解码像素数据；在此严格校验总像素数
    if img.width * img.height > MAX_IMAGE_PIXELS:
        img.close()
        raise Image.DecompressionBombError(PIXEL_LIMIT_ERROR)
    return img


# Logo 上传支持的扩展名与对应内部格式（SVG 由调用方在扩展名白名单中禁止）
_LOGO_FORMAT_BY_EXT = {".jpg": "jpg", ".jpeg": "jpg", ".png": "png", ".webp": "webp", ".ico": "ico"}


def _detect_logo_format(data: bytes):
    """根据文件头返回 Logo 实际格式（jpg/png/webp/ico），无法识别返回 None。"""
    fmt = detect_image_format(data)
    if fmt:
        return fmt
    if data[:4] == b'\x00\x00\x01\x00':
        return "ico"
    return None


def process_logo(file_data: bytes, ext: str) -> tuple:
    """校验并重新编码 Logo 图片，返回 (reencoded_bytes, ext, error)。

    与用户图片上传共用同一套安全处理链路：
    - 文件头魔数与扩展名一致性校验（ico 魔数单独识别）；
    - 实际解码校验（含解压炸弹与动图拒绝）；
    - Logo 像素尺寸限制（MAX_LOGO_DIMENSION）；
    - 标准格式重新编码，剥离隐藏数据/恶意载荷后由调用方原子写入。
    ICO 统一重编码为 PNG（扩展名随返回值变化）。
    成功时 error 为空字符串；失败时 data 为 None。
    """
    from io import BytesIO

    if ext not in _LOGO_FORMAT_BY_EXT:
        return (None, "", "不支持的Logo格式")
    if len(file_data) > MAX_UPLOAD_SIZE:
        return (None, "", f"Logo 文件过大（最大 {MAX_UPLOAD_SIZE // 1024 // 1024}MB）")

    expected = _LOGO_FORMAT_BY_EXT[ext]
    actual = _detect_logo_format(file_data)
    if actual != expected:
        return (None, "", "Logo 文件格式与内容不匹配")

    img = None
    try:
        try:
            img = _open_image_checked(file_data)
        except (Image.DecompressionBombWarning, Image.DecompressionBombError):
            return (None, "", PIXEL_LIMIT_ERROR)
        except Exception:
            return (None, "", "Logo 文件损坏或无法解码")

        if (getattr(img, "is_animated", False)
                or int(getattr(img, "n_frames", 1) or 1) > 1):
            return (None, "", ANIMATED_IMAGE_ERROR)
        if max(img.size) > MAX_LOGO_DIMENSION:
            return (None, "", f"Logo 尺寸超过限制（最大边长 {MAX_LOGO_DIMENSION}px）")

        # 目标格式：ico 重编码为 png，其余保持原格式
        out_ext = "png" if actual == "ico" else actual
        had_alpha = (img.mode in ("RGBA", "LA")
                     or (img.mode == "P" and "transparency" in img.info))
        if out_ext == "jpg":
            out = img.convert("RGB")
        elif had_alpha:
            out = img.convert("RGBA")
        elif img.mode == "P":
            out = img.convert("RGB")
        else:
            out = img

        output = BytesIO()
        if out_ext == "jpg":
            out.save(output, format="JPEG", quality=90)
        elif out_ext == "png":
            out.save(output, format="PNG")
        else:
            out.save(output, format="WEBP", quality=WEBP_QUALITY, method=WEBP_EFFORT)
        return (output.getvalue(), f".{out_ext}", "")
    except Exception:
        LOGGER.warning("Logo 重编码失败（扩展名 %s）", ext, exc_info=True)
        return (None, "", "Logo 文件损坏或无法解码")
    finally:
        if img is not None:
            img.close()


def process_image(file_data: bytes, watermark_text="", author="", uploaded_at="",
                  target_dir=None) -> tuple:
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
    img = None
    thumb = None

    try:
        try:
            img = _open_image_checked(file_data)
        except (Image.DecompressionBombWarning, Image.DecompressionBombError):
            return (None, PIXEL_LIMIT_ERROR)
        except Exception:
            return (None, "图片文件损坏或无法解码，请重新选择图片")

        if (getattr(img, "is_animated", False)
                or int(getattr(img, "n_frames", 1) or 1) > 1):
            img.close()
            return (None, ANIMATED_IMAGE_ERROR)

        # 检查在上面的辅助函数中已完成（仅读取文件头，尚未解码像素数据）

        # 剥离 EXIF；只对有透明信息的图片保留 Alpha，避免所有 PNG 都被转成 RGBA
        had_alpha = (
            img.mode in ('RGBA', 'LA')
            or (img.mode == 'P' and 'transparency' in img.info)
        )
        if img.mode not in ('RGB', 'RGBA'):
            img = img.convert('RGBA' if had_alpha else 'RGB')

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

        img = _draw_watermark(
            img, watermark_text, author, uploaded_at, preserve_alpha=had_alpha
        )

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

        full_dir = os.path.join(target_dir or UPLOADS_DIR, subdir)
        os.makedirs(full_dir, exist_ok=True)

        # 原子写入文件
        _atomic_write(os.path.join(full_dir, filename), compressed_data)
        _atomic_write(os.path.join(full_dir, thumb_filename), thumb_data)

        thumb.close()
        img.close()

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
    except WatermarkFontError as exc:
        _cleanup_partial_files(full_dir, filename, thumb_filename)
        _close_image(img)
        _close_image(thumb)
        return (None, str(exc))
    except OSError:
        _cleanup_partial_files(full_dir, filename, thumb_filename)
        _close_image(img)
        _close_image(thumb)
        return (None, "图片保存失败，请稍后重试")
    except Exception:
        # 处理失败时清理可能残留的文件，避免留下孤儿文件
        _cleanup_partial_files(full_dir, filename, thumb_filename)
        _close_image(img)
        _close_image(thumb)
        return (None, "图片处理失败")


def _close_image(image):
    if image is not None:
        try:
            image.close()
        except Exception:
            pass


def _cleanup_partial_files(full_dir, filename, thumb_filename):
    if full_dir:
        for name in (filename, thumb_filename):
            if name:
                try:
                    path = os.path.join(full_dir, name)
                    if os.path.exists(path):
                        os.remove(path)
                except OSError:
                    pass


def delete_image_files(filename, thumb_filename, uploads_dir=None):
    """删除图片文件，返回删除失败/非法路径的列表。

    路径必须解析在指定上传目录内，避免数据库中的异常值导致删除任意服务器文件。
    """
    failures = []
    for fname in (filename, thumb_filename):
        if not fname:
            continue
        fpath = resolve_upload_path(
            fname, must_exist=False, uploads_dir=uploads_dir or UPLOADS_DIR
        )
        if fpath is None:
            failures.append(str(fname))
            LOGGER.warning("拒绝删除非法上传路径: %s", fname)
            continue
        if not fpath.exists():
            continue
        try:
            os.remove(fpath)
        except OSError as exc:
            failures.append(str(fname))
            LOGGER.warning("删除上传文件失败 %s: %s", fname, exc)
    return failures


def promote_image_files(filename, thumb_filename, source_dir=None, target_dir=None):
    """把待审图片从私有目录移动到公开目录；失败时回滚已移动的文件。

    返回失败列表；空列表表示原图与缩略图都已在目标目录（或无需移动）。
    """
    source_base = source_dir or PRIVATE_UPLOADS_DIR
    target_base = target_dir or UPLOADS_DIR
    failures = []
    moved = []

    for fname in (filename, thumb_filename):
        if not fname:
            continue
        src = resolve_upload_path(fname, must_exist=False, uploads_dir=source_base)
        dst = resolve_upload_path(fname, must_exist=False, uploads_dir=target_base)
        if src is None or dst is None:
            failures.append(str(fname))
            LOGGER.warning("拒绝移动非法上传路径: %s", fname)
            continue
        if src.exists() and dst.exists():
            failures.append(str(fname))
            LOGGER.warning("目标文件已存在，拒绝覆盖: %s", fname)
            continue
        if src.exists():
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                moved.append((src, dst))
            except OSError as exc:
                failures.append(str(fname))
                LOGGER.warning("移动上传文件失败 %s: %s", fname, exc)
        elif not dst.exists():
            failures.append(str(fname))
            LOGGER.warning("待审图片文件不存在: %s", fname)

    if failures:
        for src, dst in reversed(moved):
            try:
                if dst.exists() and not src.exists():
                    src.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(dst), str(src))
            except OSError as exc:
                LOGGER.warning("回滚上传文件失败 %s: %s", dst, exc)
    return failures