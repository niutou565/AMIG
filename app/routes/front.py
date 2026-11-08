"""前台路由（无需登录）"""
from fastapi import APIRouter, Request, Form, File, UploadFile, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response, FileResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from app.config import BASE_DIR, MAX_DESC_LENGTH, MAX_TITLE_LENGTH, MAX_AUTHOR_LENGTH, GALLERY_PER_PAGE, MAX_UPLOAD_SIZE
from app.models import (
    get_random_approved_images, get_latest_approved_images,
    search_approved_images, count_search_approved_images,
    get_approved_image_by_id, get_images_by_status, count_images_by_status,
    get_likes_by_image, count_likes_by_image, has_liked,
    insert_image, get_visible_pages, get_visible_links,
    get_page_by_slug, get_setting, get_setting_int,
    count_images_admin,
    check_content_md5_duplicate,
    now_iso, ua_hash, generate_filename,
    increment_view, should_count_view,
    increment_daily_stat, today_str,
)
from app.stats import get_top_cached
from app.ip_geo import get_client_ip, get_ip_location, get_ip_region
from app.limiter import check_upload_limit
from app.ban import check_ban_status
from app.image_utils import validate_image, process_image, delete_image_files
from app.middleware import check_sensitive
from app.display import location_label, uploader_label, safe_color
import logging
import os
import threading
import httpx

router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
templates.env.filters["location_label"] = location_label
templates.env.filters["uploader_label"] = uploader_label
templates.env.filters["safe_color"] = safe_color

# 限制并发图片处理数量，避免大量并发上传耗尽线程池与 CPU
_upload_semaphore = threading.Semaphore(4)


def _process_image_limited(file_data, watermark_text, author, uploaded_at):
    with _upload_semaphore:
        return process_image(file_data, watermark_text, author, uploaded_at)


def get_settings_context():
    """获取站点设置上下文"""
    from app.models import get_all_settings
    settings = get_all_settings()
    return {
        "site_name": settings.get("site_name", "匿名图片分享"),
        "site_description": settings.get("site_description", ""),
        "logo_large": settings.get("logo_large", ""),
        "logo_small": settings.get("logo_small", ""),
        "footer_icp": settings.get("footer_icp", ""),
        "footer_copyright": settings.get("footer_copyright", ""),
        "accent_color": settings.get("theme_accent_color", "#6c5ce7"),
        "allow_author": settings.get("allow_author", "1") == "1",
        "enable_captcha": settings.get("enable_captcha", "0") == "1",
        "turnstile_site_key": settings.get("turnstile_site_key", ""),
    }


def get_common_context(request: Request):
    """Build common template context."""
    ctx = get_settings_context()
    ctx["pages"] = get_visible_pages()
    ctx["links"] = get_visible_links()
    # CSRF token (set by CSRFMiddleware, always available via request.state)
    ctx["csrf_token"] = getattr(request.state, "csrf_token", "")
    return ctx


logger = logging.getLogger(__name__)


async def verify_turnstile(token: str, ip: str) -> tuple[bool, str]:
    """服务端校验 Cloudflare Turnstile token，返回结果及安全的用户提示。"""
    secret = get_setting("turnstile_secret_key", "").strip()
    if not secret:
        return False, "人机验证配置不完整，请联系管理员"
    if not token:
        return False, "请完成人机验证"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                data={"secret": secret, "response": token, "remoteip": ip},
            )
            response.raise_for_status()
            result = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Turnstile 校验请求失败: %s", exc)
        return False, "人机验证服务暂时不可用，请稍后重试"

    if result.get("success"):
        return True, ""

    error_codes = result.get("error-codes", [])
    logger.warning("Turnstile 校验被拒绝: %s", error_codes)
    if "timeout-or-duplicate" in error_codes:
        return False, "人机验证已过期，请重新验证后提交"
    if "invalid-input-secret" in error_codes or "missing-input-secret" in error_codes:
        return False, "人机验证配置异常，请联系管理员"
    return False, "人机验证失败，请重新验证后提交"


# ========== 首页 ==========

@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    ctx = get_common_context(request)
    ctx["random_images"] = get_random_approved_images(12)
    ctx["latest_images"] = get_latest_approved_images(12)
    ctx["top_images"] = get_top_cached(12)
    return templates.TemplateResponse(request, "index.html", ctx)


# ========== 图库 ==========

@router.get("/gallery", response_class=HTMLResponse)
async def gallery(request: Request, q: str = "", page: int = 1, sort: str = "latest"):
    ctx = get_common_context(request)
    offset = (page - 1) * GALLERY_PER_PAGE
    sort_order_map = {
        "hot": "like_count DESC",
        "latest": "created_at DESC",
        "oldest": "created_at ASC",
    }
    order_by = sort_order_map.get(sort, "created_at DESC")

    if q:
        images = search_approved_images(q, GALLERY_PER_PAGE, offset)
        total = count_search_approved_images(q)
    else:
        images = get_images_by_status("approved", GALLERY_PER_PAGE, offset, order_by)
        total = count_images_by_status("approved")
    ctx["images"] = images
    ctx["query"] = q
    ctx["sort"] = sort
    ctx["page"] = page
    ctx["total"] = total
    ctx["total_pages"] = max(1, (total + GALLERY_PER_PAGE - 1) // GALLERY_PER_PAGE)
    return templates.TemplateResponse(request, "gallery.html", ctx)


# ========== 热度榜 ==========

@router.get("/top", response_class=HTMLResponse)
async def top_list(request: Request, page: int = 1):
    ctx = get_common_context(request)
    window_days = get_setting_int("top_window_days", 30)
    show_count = get_setting_int("top_show_count", 12)
    from app.config import TOP_PER_PAGE
    offset = (page - 1) * TOP_PER_PAGE
    from app.models import get_top_images, count_top_images
    ctx["images"] = get_top_images(window_days, show_count, limit=TOP_PER_PAGE, offset=offset)
    ctx["total"] = count_top_images(window_days)
    ctx["page"] = page
    ctx["total_pages"] = max(1, (ctx["total"] + TOP_PER_PAGE - 1) // TOP_PER_PAGE)
    ctx["window_days"] = window_days
    return templates.TemplateResponse(request, "top.html", ctx)


# ========== 图片详情 ==========

@router.get("/image/{image_id}", response_class=HTMLResponse)
async def image_detail(request: Request, image_id: int):
    image = get_approved_image_by_id(image_id)
    if not image:
        raise HTTPException(status_code=404)

    ctx = get_common_context(request)
    ctx["image"] = image

    ip = get_client_ip(request)
    ua_val = request.headers.get("user-agent", "")

    # 浏览计数（去重）
    if should_count_view(image_id, ip):
        increment_view(image_id)
        increment_daily_stat(today_str(), "view_count")

    # 是否已赞
    ctx["has_liked"] = has_liked(image_id, ip, ua_hash(ua_val))

    # 点赞列表
    from app.config import LIKES_PER_PAGE
    ctx["likes"] = get_likes_by_image(image_id, LIKES_PER_PAGE, 0)
    ctx["likes_count"] = count_likes_by_image(image_id)

    return templates.TemplateResponse(request, "image_detail.html", ctx)


# ========== 上传页 ==========

def _prepare_upload_context(request: Request):
    """构建上传页上下文（含 Turnstile 站点密钥）"""
    ctx = get_common_context(request)
    ctx["enable_captcha"] = get_setting("enable_captcha", "0") == "1"
    ctx["turnstile_site_key"] = get_setting("turnstile_site_key", "")
    return ctx


@router.get("/upload", response_class=HTMLResponse)
async def upload_page(request: Request):
    ctx = _prepare_upload_context(request)
    return templates.TemplateResponse(request, "upload.html", ctx)


@router.post("/upload", response_class=HTMLResponse)
async def upload_action(
    request: Request,
    image: UploadFile = File(...),
    title: str = Form(""),
    description: str = Form(""),
    author: str = Form(""),
    agree: str = Form(""),
    honeypot: str = Form(""),  # 蜜罐字段
    csrf_token: str = Form(""),
    turnstile_response: str = Form("", alias="cf-turnstile-response"),
):
    ctx = _prepare_upload_context(request)

    # 蜜罐检查
    if honeypot:
        return templates.TemplateResponse(request, "upload.html", {**ctx, "error": "上传失败"})

    # CSRF 检查
    from app.middleware import validate_csrf
    if not validate_csrf(request, csrf_token):
        return templates.TemplateResponse(request, "upload.html", {**ctx, "error": "请求无效，请刷新页面后重试"})

    # 同意站规
    if agree != "1":
        return templates.TemplateResponse(request, "upload.html", {**ctx, "error": "请先阅读并同意站规"})

    # 人机验证（Cloudflare Turnstile）
    ip = get_client_ip(request)
    if get_setting("enable_captcha", "0") == "1":
        verified, captcha_error = await verify_turnstile(turnstile_response.strip(), ip)
        if not verified:
            return templates.TemplateResponse(request, "upload.html", {**ctx, "error": captcha_error})

    # 输入校验
    title = title.strip()[:MAX_TITLE_LENGTH]
    description = description.strip()[:MAX_DESC_LENGTH]
    author = author.strip()[:MAX_AUTHOR_LENGTH]
    if not get_setting("allow_author", "1") == "1":
        author = ""

    if not title:
        return templates.TemplateResponse(request, "upload.html", {**ctx, "error": "请输入标题"})

    # 敏感词检查
    if check_sensitive(title) or check_sensitive(description) or (author and check_sensitive(author)):
        return templates.TemplateResponse(request, "upload.html", {**ctx, "error": "内容包含敏感词，请修改后重试"})

    ua_val = request.headers.get("user-agent", "")
    ua_hash_val = ua_hash(ua_val)

    # 封禁检查
    banned, ban_msg = check_ban_status(ip, ua_hash_val)
    if banned:
        return templates.TemplateResponse(request, "upload.html", {**ctx, "error": ban_msg})

    # 限流检查
    allowed, limit_msg = check_upload_limit(ip, ua_hash_val)
    if not allowed:
        return templates.TemplateResponse(request, "upload.html", {**ctx, "error": limit_msg})

    # 预检 Content-Length（粗略拦截超大请求，避免先完整读入内存）
    content_length = request.headers.get("content-length", "")
    if content_length.isdigit() and int(content_length) > MAX_UPLOAD_SIZE + 1024 * 1024:
        return templates.TemplateResponse(request, "upload.html", {**ctx, "error": "图片大小超过限制"})

    # 分块读取文件，边读边限制大小（bytearray 避免大文件反复拼接产生额外内存拷贝）
    file_data = bytearray()
    while True:
        chunk = await image.read(1024 * 1024)
        if not chunk:
            break
        file_data.extend(chunk)
        if len(file_data) > MAX_UPLOAD_SIZE:
            return templates.TemplateResponse(request, "upload.html", {**ctx, "error": "图片大小超过限制"})

    if not file_data:
        return templates.TemplateResponse(request, "upload.html", {**ctx, "error": "请选择图片文件"})
    file_data = bytes(file_data)

    # 验证图片
    valid, err_msg = validate_image(file_data, image.filename or "unknown.jpg")
    if not valid:
        return templates.TemplateResponse(request, "upload.html", {**ctx, "error": err_msg})

    # 处理图片（在独立线程池中执行，避免阻塞事件循环；并发受信号量限制）
    uploaded_at = now_iso()
    watermark_text = get_setting("watermark_text", "").strip()
    result, err_msg = await run_in_threadpool(
        _process_image_limited, file_data, watermark_text, author, uploaded_at
    )
    if not result:
        return templates.TemplateResponse(request, "upload.html", {**ctx, "error": f"图片处理失败：{err_msg}"})

    # 以下流程已把图片写入磁盘；若数据库操作失败或流程异常，必须清理文件，避免孤儿文件
    saved = False
    try:
        # 图片去重
        if get_setting("enable_dedup", "1") == "1":
            is_dup = check_content_md5_duplicate(result["content_md5"], ip)
            if is_dup:
                from app.models import insert_image
                province, city = await run_in_threadpool(get_ip_location, ip)
                region = await run_in_threadpool(get_ip_region, ip)
                insert_image(
                    result["filename"], result["thumb_filename"],
                    result["original_size"], result["compressed_size"],
                    result["width"], result["height"],
                    description, author, result["content_md5"],
                    ip, ua_val, ua_hash_val, province, city, is_dup=1, title=title, region=region
                )
                saved = True
                try:
                    increment_daily_stat(today_str(), "upload_count")
                except Exception:
                    logger.warning("更新上传统计失败（图片已入库）", exc_info=True)
                return templates.TemplateResponse(request, "upload.html", {
                    **ctx, "success": "已提交，等待审核通过后公开展示（系统检测到疑似重复图片，将标记审核）"
                })

        # 存库
        province, city = await run_in_threadpool(get_ip_location, ip)
        region = await run_in_threadpool(get_ip_region, ip)
        from app.models import insert_image
        insert_image(
            result["filename"], result["thumb_filename"],
            result["original_size"], result["compressed_size"],
            result["width"], result["height"],
            description, author, result["content_md5"],
            ip, ua_val, ua_hash_val, province, city, title=title, region=region
        )
        saved = True

        # 统计失败不应让用户以为上传失败（图片已成功入库）
        try:
            increment_daily_stat(today_str(), "upload_count")
        except Exception:
            logger.warning("更新上传统计失败（图片已入库）", exc_info=True)

        return templates.TemplateResponse(request, "upload.html", {
            **ctx, "success": "已提交，等待审核通过后公开展示"
        })
    except Exception:
        logger.exception("上传图片保存失败")
        return templates.TemplateResponse(request, "upload.html", {
            **ctx, "error": "保存失败，请稍后重试"
        })
    finally:
        if not saved:
            delete_image_files(result["filename"], result["thumb_filename"])


# ========== 自定义页面 ==========

@router.get("/page/{slug}", response_class=HTMLResponse)
async def custom_page(request: Request, slug: str):
    page = get_page_by_slug(slug)
    if not page:
        raise HTTPException(status_code=404)
    ctx = get_common_context(request)
    ctx["page"] = page
    return templates.TemplateResponse(request, "page.html", ctx)


# ========== 静态文件 ==========

@router.get("/robots.txt")
async def robots():
    # 不写入 Disallow: /admin 等敏感路径，避免反向暴露后台入口给扫描器
    return Response(
        "User-agent: *\nAllow: /\n",
        media_type="text/plain"
    )