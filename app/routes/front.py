"""前台路由（无需登录）"""
from fastapi import APIRouter, Request, Form, File, UploadFile, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response, FileResponse
from fastapi.templating import Jinja2Templates
from app.config import BASE_DIR, MAX_DESC_LENGTH, MAX_AUTHOR_LENGTH, GALLERY_PER_PAGE
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
from app.ip_geo import get_client_ip, get_ip_location
from app.limiter import check_upload_limit
from app.ban import check_ban_status
from app.image_utils import validate_image, process_image
from app.middleware import check_sensitive
import os

router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


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
    }


def get_common_context(request: Request):
    """Build common template context."""
    ctx = get_settings_context()
    ctx["request"] = request
    ctx["pages"] = get_visible_pages()
    ctx["links"] = get_visible_links()
    # CSRF token (set by CSRFMiddleware, always available via request.state)
    ctx["csrf_token"] = getattr(request.state, "csrf_token", "")
    return ctx


# ========== 首页 ==========

@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    ctx = get_common_context(request)
    ctx["random_images"] = get_random_approved_images(12)
    ctx["latest_images"] = get_latest_approved_images(12)
    ctx["top_images"] = get_top_cached(12)
    return templates.TemplateResponse("index.html", ctx)


# ========== 图库 ==========

@router.get("/gallery", response_class=HTMLResponse)
async def gallery(request: Request, q: str = "", page: int = 1):
    ctx = get_common_context(request)
    offset = (page - 1) * GALLERY_PER_PAGE
    if q:
        images = search_approved_images(q, GALLERY_PER_PAGE, offset)
        total = count_search_approved_images(q)
    else:
        images = get_images_by_status("approved", GALLERY_PER_PAGE, offset, "reviewed_at DESC")
        total = count_images_by_status("approved")
    ctx["images"] = images
    ctx["query"] = q
    ctx["page"] = page
    ctx["total"] = total
    ctx["total_pages"] = max(1, (total + GALLERY_PER_PAGE - 1) // GALLERY_PER_PAGE)
    return templates.TemplateResponse("gallery.html", ctx)


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
    return templates.TemplateResponse("top.html", ctx)


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

    # 上一张/下一张
    from app.database import get_db
    db = get_db()
    prev_row = db.execute(
        "SELECT id FROM images WHERE status='approved' AND reviewed_at < ? ORDER BY reviewed_at DESC LIMIT 1",
        (image["reviewed_at"],)
    ).fetchone()
    next_row = db.execute(
        "SELECT id FROM images WHERE status='approved' AND reviewed_at > ? ORDER BY reviewed_at ASC LIMIT 1",
        (image["reviewed_at"],)
    ).fetchone()
    ctx["prev_id"] = prev_row[0] if prev_row else None
    ctx["next_id"] = next_row[0] if next_row else None

    return templates.TemplateResponse("image_detail.html", ctx)


# ========== 上传页 ==========

def _prepare_upload_context(request: Request):
    """构建上传页上下文（含验证码数字，每次渲染重新生成）"""
    ctx = get_common_context(request)
    ctx["enable_captcha"] = get_setting("enable_captcha", "0") == "1"
    if ctx["enable_captcha"]:
        import random
        ctx["captcha_a"] = random.randint(1, 20)
        ctx["captcha_b"] = random.randint(1, 20)
    return ctx


@router.get("/upload", response_class=HTMLResponse)
async def upload_page(request: Request):
    ctx = _prepare_upload_context(request)
    return templates.TemplateResponse("upload.html", ctx)


@router.post("/upload", response_class=HTMLResponse)
async def upload_action(
    request: Request,
    image: UploadFile = File(...),
    description: str = Form(""),
    author: str = Form(""),
    agree: str = Form(""),
    honeypot: str = Form(""),  # 蜜罐字段
    csrf_token: str = Form(""),
    captcha_answer: str = Form(""),
    captcha_a: str = Form(""),
    captcha_b: str = Form(""),
):
    ctx = _prepare_upload_context(request)

    # 蜜罐检查
    if honeypot:
        return templates.TemplateResponse("upload.html", {**ctx, "error": "上传失败"})

    # CSRF 检查
    from app.middleware import validate_csrf
    if not validate_csrf(request, csrf_token):
        return templates.TemplateResponse("upload.html", {**ctx, "error": "请求无效，请刷新页面后重试"})

    # 同意站规
    if agree != "1":
        return templates.TemplateResponse("upload.html", {**ctx, "error": "请先阅读并同意站规"})

    # 验证码
    if get_setting("enable_captcha", "0") == "1":
        try:
            expected = int(captcha_a) + int(captcha_b)
            if str(captcha_answer).strip() != str(expected):
                return templates.TemplateResponse("upload.html", {**ctx, "error": "验证码错误"})
        except (ValueError, TypeError):
            return templates.TemplateResponse("upload.html", {**ctx, "error": "验证码错误"})

    # 输入校验
    description = description.strip()[:MAX_DESC_LENGTH]
    author = author.strip()[:MAX_AUTHOR_LENGTH]
    if not get_setting("allow_author", "1") == "1":
        author = ""

    if not description:
        return templates.TemplateResponse("upload.html", {**ctx, "error": "请输入简介"})

    # 敏感词检查
    if check_sensitive(description) or (author and check_sensitive(author)):
        return templates.TemplateResponse("upload.html", {**ctx, "error": "内容包含敏感词，请修改后重试"})

    ip = get_client_ip(request)
    ua_val = request.headers.get("user-agent", "")
    ua_hash_val = ua_hash(ua_val)

    # 封禁检查
    banned, ban_msg = check_ban_status(ip, ua_hash_val)
    if banned:
        return templates.TemplateResponse("upload.html", {**ctx, "error": ban_msg})

    # 限流检查
    allowed, limit_msg = check_upload_limit(ip, ua_hash_val)
    if not allowed:
        return templates.TemplateResponse("upload.html", {**ctx, "error": limit_msg})

    # 读取文件
    file_data = await image.read()
    if not file_data:
        return templates.TemplateResponse("upload.html", {**ctx, "error": "请选择图片文件"})

    # 验证图片
    valid, err_msg = validate_image(file_data, image.filename or "unknown.jpg")
    if not valid:
        return templates.TemplateResponse("upload.html", {**ctx, "error": err_msg})

    # 处理图片
    result, err_msg = process_image(file_data)
    if not result:
        return templates.TemplateResponse("upload.html", {**ctx, "error": f"图片处理失败：{err_msg}"})

    # 图片去重
    if get_setting("enable_dedup", "1") == "1":
        is_dup = check_content_md5_duplicate(result["content_md5"], ip)
        if is_dup:
            from app.models import insert_image
            province, city = get_ip_location(ip)
            img_id = insert_image(
                result["filename"], result["thumb_filename"],
                result["original_size"], result["compressed_size"],
                result["width"], result["height"],
                description, author, result["content_md5"],
                ip, ua_val, ua_hash_val, province, city, is_dup=1
            )
            increment_daily_stat(today_str(), "upload_count")
            return templates.TemplateResponse("upload.html", {
                **ctx, "success": "已提交，等待审核通过后公开展示（系统检测到疑似重复图片，将标记审核）"
            })

    # 存库
    province, city = get_ip_location(ip)
    from app.models import insert_image
    img_id = insert_image(
        result["filename"], result["thumb_filename"],
        result["original_size"], result["compressed_size"],
        result["width"], result["height"],
        description, author, result["content_md5"],
        ip, ua_val, ua_hash_val, province, city
    )

    # 更新统计
    increment_daily_stat(today_str(), "upload_count")

    return templates.TemplateResponse("upload.html", {
        **ctx, "success": "已提交，等待审核通过后公开展示"
    })


# ========== 自定义页面 ==========

@router.get("/page/{slug}", response_class=HTMLResponse)
async def custom_page(request: Request, slug: str):
    page = get_page_by_slug(slug)
    if not page:
        raise HTTPException(status_code=404)
    ctx = get_common_context(request)
    ctx["page"] = page
    return templates.TemplateResponse("page.html", ctx)


# ========== 静态文件 ==========

@router.get("/robots.txt")
async def robots():
    return Response(
        "User-agent: *\nAllow: /\nDisallow: /admin\n",
        media_type="text/plain"
    )