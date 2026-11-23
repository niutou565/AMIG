# -*- coding: utf-8 -*-
"""受控的上传文件访问路由。

- 已通过审核的图片：公开访问（公开目录，兼容旧版本仍留在公开目录的历史文件）；
- 待审核/已拒绝图片：存放在私有目录，仅已登录管理员可预览，普通访客返回 404；
- Logo 等公开品牌文件：公开访问；
- 其他未知文件（临时文件、孤立文件、越界路径）：404。
"""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response

from app import image_utils
from app.auth import get_current_admin
from app.models import get_image_by_any_filename

router = APIRouter()

_MEDIA_TYPES = {
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

_PUBLIC_CACHE = "public, no-cache"
_PRIVATE_CACHE = "private, no-store, max-age=0"
_LOGO_CACHE = "public, no-cache"
_PUBLIC_LOGO_PREFIXES = ("logo_large_", "logo_small_")


def _normalize_relative(filename: str) -> str:
    return str(filename or "").replace("\\", "/")


def _is_public_logo(relative: str) -> bool:
    """Logo 固定存放在上传目录根目录，使用受控前缀识别。"""
    return "/" not in relative and relative.startswith(_PUBLIC_LOGO_PREFIXES)


def _media_type(path: Path) -> str:
    return _MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")


def _etag(stat) -> str:
    return f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'


def _not_modified(request: Request, etag: str) -> bool:
    header = request.headers.get("if-none-match", "")
    if not header:
        return False
    candidates = {item.strip().lstrip("W/").strip('"') for item in header.split(",")}
    return "*" in candidates or etag.strip('"') in candidates


def _not_found():
    """统一 404：不缓存错误结果，避免审核通过后浏览器继续命中旧的 404。"""
    return PlainTextResponse(
        "Not Found",
        status_code=404,
        headers={"Cache-Control": "no-store"},
    )


def _file_response(request: Request, path: Path, cache_control: str, noindex=False):
    try:
        stat = path.stat()
    except OSError:
        return _not_found()
    etag = _etag(stat)
    headers = {
        "Cache-Control": cache_control,
        "ETag": etag,
    }
    if noindex:
        headers["X-Robots-Tag"] = "noindex, nofollow"
    if _not_modified(request, etag):
        return Response(status_code=304, headers=headers)
    return FileResponse(
        path,
        media_type=_media_type(path),
        headers=headers,
        stat_result=stat,
    )


def _resolve_in_bases(relative: str, bases):
    """按顺序在多个上传根目录中查找文件，返回第一个存在的合法路径。"""
    for base in bases:
        path = image_utils.resolve_upload_path(
            relative, must_exist=True, uploads_dir=base
        )
        if path is not None:
            return path
    return None


@router.api_route("/uploads/{filename:path}", methods=["GET", "HEAD"])
async def serve_upload(request: Request, filename: str):
    relative = _normalize_relative(filename)
    image = get_image_by_any_filename(relative)

    if image is not None:
        if image["status"] == "approved":
            # 新文件在公开目录；兼容审核完成前未迁移的历史文件
            path = _resolve_in_bases(
                relative,
                (image_utils.UPLOADS_DIR, image_utils.PRIVATE_UPLOADS_DIR),
            )
            if path is None:
                return _not_found()
            return _file_response(request, path, _PUBLIC_CACHE)

        # 待审核/已拒绝：私有目录优先，兼容旧版本仍留在公开目录的文件。
        # 无论文件物理位置如何，状态校验始终先于访问，普通访客统一返回 404，
        # 避免暴露文件是否存在。
        if get_current_admin(request):
            path = _resolve_in_bases(
                relative,
                (image_utils.PRIVATE_UPLOADS_DIR, image_utils.UPLOADS_DIR),
            )
            if path is None:
                return _not_found()
            return _file_response(request, path, _PRIVATE_CACHE, noindex=True)
        return _not_found()

    path = image_utils.resolve_upload_path(
        relative, must_exist=True, uploads_dir=image_utils.UPLOADS_DIR
    )
    if _is_public_logo(relative) and path is not None:
        return _file_response(request, path, _LOGO_CACHE)

    return _not_found()
