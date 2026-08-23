"""后台管理路由（仅管理域名可访问）"""
import re
from fastapi import APIRouter, Request, Form, File, UploadFile, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import escape, Markup
from app.config import BASE_DIR, ADMIN_PER_PAGE, LIKES_PER_PAGE, MAX_DESC_LENGTH, MAX_TITLE_LENGTH, UPLOADS_DIR, APP_NAME, APP_VERSION, MAX_UPLOAD_SIZE
from app.models import (
    get_images_by_status, count_images_by_status, get_image_by_id,
    update_image_status, update_image_content, delete_image, get_all_links, get_link_by_id,
    upsert_link, delete_link, get_all_pages, get_page_by_id,
    upsert_page, delete_page, search_admin_logs, count_admin_logs,
    insert_admin_log, set_current_admin_request, get_setting, set_setting, get_all_settings,
    get_ban_list, add_ban, remove_ban, get_recent_upload_ips,
    get_all_cooling, reset_cooldown,
    search_images_admin, count_images_admin, batch_update_status,
    get_likes_by_image, count_likes_by_image, delete_like,
    delete_likes_by_ids, decrement_like_count,
    get_top_images_by_likes, get_setting_int,
    get_stats_range, get_daily_stats, today_str,
    ensure_daily_stats, count_images_admin,
)
from app.stats import get_dashboard_data, invalidate_top_cache
from app.auth import login, get_current_admin, hash_password, destroy_session, check_login_ip_limit
from app.middleware import validate_csrf, load_sensitive_words
from app.image_utils import delete_image_files
from app.ip_geo import get_client_ip
from app.display import safe_color
import os
import json

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


def highlight(text, keyword):
    """对文本中匹配的关键词进行高亮，返回安全 HTML。

    先转义再高亮，避免日志内容中的 HTML 被当作标签渲染（防 XSS）。
    """
    if text is None:
        return ""
    escaped = escape(str(text))
    if not keyword:
        return escaped
    kw = escape(str(keyword))
    if not kw:
        return escaped
    pattern = re.compile(re.escape(kw), re.IGNORECASE)
    return Markup(pattern.sub(lambda m: f'<mark class="hl">{m.group(0)}</mark>', escaped))


templates.env.filters["highlight"] = highlight
templates.env.globals["app_name"] = APP_NAME
templates.env.globals["app_version"] = APP_VERSION
templates.env.filters["safe_color"] = safe_color

# Logo 允许的扩展名（不含 .svg，避免上传可嵌入脚本的矢量图造成存储型 XSS）
ALLOWED_LOGO_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".ico"}


async def _save_logo(upload_file, prefix):
    """保存上传的 Logo 到 UPLOADS_DIR，返回文件名；非法扩展名、空文件或超大文件返回 None"""
    if not (hasattr(upload_file, "filename") and upload_file.filename):
        return None
    import uuid
    ext = os.path.splitext(upload_file.filename)[1].lower()
    if ext not in ALLOWED_LOGO_EXTS:
        return None
    data = await upload_file.read()
    if not data or len(data) > MAX_UPLOAD_SIZE:
        return None
    logo_name = f"{prefix}_{uuid.uuid4().hex}{ext}"
    os.makedirs(UPLOADS_DIR, exist_ok=True)
    with open(os.path.join(UPLOADS_DIR, logo_name), "wb") as f:
        f.write(data)
    return logo_name


def admin_required(request: Request):
    """检查管理员登录状态，返回 admin 对象或 None"""
    admin = get_current_admin(request)
    if admin:
        set_current_admin_request(request)
    return admin


def get_admin_context(request: Request):
    """Get admin template context."""
    admin = admin_required(request)
    settings = get_all_settings()
    return {
        "admin": admin,
        "site_name": settings.get("site_name", "匿名图片分享"),
        "accent_color": settings.get("theme_accent_color", "#6c5ce7"),
        "csrf_token": getattr(request.state, "csrf_token", ""),
    }


# ========== 登录 ==========

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    admin = get_current_admin(request)
    if admin:
        return RedirectResponse(url="/admin/dashboard", status_code=302)
    return templates.TemplateResponse(request, "admin/login.html", {"csrf_token": getattr(request.state, "csrf_token", "")})


@router.post("/login", response_class=HTMLResponse)
async def login_action(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    csrf_token: str = Form(""),
):
    if not validate_csrf(request, csrf_token):
        return templates.TemplateResponse(request, "admin/login.html", {
            "error": "请求无效，请刷新页面重试", "csrf_token": getattr(request.state, "csrf_token", "")
        })

    # IP 级登录限流，防止攻击者反复错误密码锁定管理员账户
    if not check_login_ip_limit(get_client_ip(request)):
        return templates.TemplateResponse(request, "admin/login.html", {
            "error": "尝试次数过多，请稍后再试", "csrf_token": getattr(request.state, "csrf_token", "")
        })

    success, result, admin_obj = login(username, password)
    if not success:
        return templates.TemplateResponse(request, "admin/login.html", {
            "error": result, "csrf_token": getattr(request.state, "csrf_token", "")
        })

    response = RedirectResponse(url="/admin/dashboard", status_code=302)
    response.set_cookie(
        key="admin_session",
        value=result,
        httponly=True,
        samesite="lax",
        secure=_secure_cookie_for_request(request),
        max_age=86400,
    )
    set_current_admin_request(request)
    insert_admin_log(admin_obj["id"], admin_obj["username"], "login", "登录成功")
    return response


@router.get("/logout")
async def logout(request: Request):
    session_id = request.cookies.get("admin_session")
    if session_id:
        destroy_session(session_id)
    response = RedirectResponse(url="/admin/login", status_code=302)
    response.delete_cookie("admin_session")
    return response


# ========== 仪表盘 ==========

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    admin = admin_required(request)
    if not admin:
        return RedirectResponse(url="/admin/login", status_code=302)
    ctx = get_admin_context(request)
    data = get_dashboard_data()
    # 转换为可 JSON 序列化的列表，供前端图表使用
    data["stats_7d"] = [dict(r) for r in data["stats_7d"]]
    data["stats_30d"] = [dict(r) for r in data["stats_30d"]]
    data["province_top"] = [dict(r) for r in data["province_top"]]
    data["hot_window"] = [dict(r) for r in data["hot_window"]]
    data["hot_all"] = [dict(r) for r in data["hot_all"]]
    ctx["data"] = data
    return templates.TemplateResponse(request, "admin/dashboard.html", ctx)


# ========== 图片审核 ==========

@router.get("/review", response_class=HTMLResponse)
async def review_list(
    request: Request,
    query: str = "",
    page: int = 1,
):
    admin = admin_required(request)
    if not admin:
        return RedirectResponse(url="/admin/login", status_code=302)
    ctx = get_admin_context(request)
    offset = (page - 1) * ADMIN_PER_PAGE
    ctx["images"] = search_images_admin(status="pending", query=query, limit=ADMIN_PER_PAGE, offset=offset)
    ctx["total"] = count_images_admin(status="pending", query=query)
    ctx["query"] = query
    ctx["page"] = page
    ctx["total_pages"] = max(1, (ctx["total"] + ADMIN_PER_PAGE - 1) // ADMIN_PER_PAGE)
    return templates.TemplateResponse(request, "admin/review.html", ctx)


@router.get("/review/{image_id}", response_class=HTMLResponse)
async def review_detail(request: Request, image_id: int):
    admin = admin_required(request)
    if not admin:
        return RedirectResponse(url="/admin/login", status_code=302)
    ctx = get_admin_context(request)
    ctx["image"] = get_image_by_id(image_id)
    if not ctx["image"]:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(request, "admin/review_detail.html", ctx)


@router.post("/review/{image_id}", response_class=HTMLResponse)
async def review_action(
    request: Request,
    image_id: int,
    action: str = Form(""),
    title: str = Form(""),
    description: str = Form(""),
    author: str = Form(""),
    edit_content: str = Form(""),
    csrf_token: str = Form(""),
):
    admin = admin_required(request)
    if not admin:
        return RedirectResponse(url="/admin/login", status_code=302)
    if not validate_csrf(request, csrf_token):
        return RedirectResponse(url=f"/admin/review/{image_id}", status_code=302)

    image = get_image_by_id(image_id)
    if not image:
        raise HTTPException(status_code=404)

    if action == "approve":
        if edit_content == "1":
            new_title = title.strip()
            new_desc = description.strip()
            new_author = author.strip()
            update_image_status(
                image_id, "approved",
                title=new_title if new_title != image["title"] else None,
                description=new_desc if new_desc != image["description"] else None,
                author=new_author if new_author != image["author"] else None,
                admin_id=admin["id"], admin_name=admin["username"]
            )
        else:
            update_image_status(
                image_id, "approved",
                admin_id=admin["id"], admin_name=admin["username"]
            )
        from app.models import increment_daily_stat, today_str
        increment_daily_stat(today_str(), "approved_count")
        invalidate_top_cache()
    elif action == "reject":
        # 拒绝 = 删除：彻底移除图片文件与记录
        delete_image_files(image["filename"], image["thumb_filename"])
        delete_image(image_id)
        insert_admin_log(admin["id"], admin["username"], "reject", "拒绝并删除图片", f"image#{image_id}")
        from app.models import increment_daily_stat, today_str, increment_reject_cooldown
        increment_daily_stat(today_str(), "rejected_count")
        increment_reject_cooldown(image["ip"])
        invalidate_top_cache()
    elif action == "delete":
        delete_image_files(image["filename"], image["thumb_filename"])
        delete_image(image_id)
        insert_admin_log(admin["id"], admin["username"], "delete", "删除图片", f"image#{image_id}")
        return RedirectResponse(url="/admin/review", status_code=302)

    return RedirectResponse(url="/admin/review", status_code=302)


@router.post("/review/batch", response_class=HTMLResponse)
async def review_batch(
    request: Request,
    ids: str = Form(""),
    action: str = Form(""),
    csrf_token: str = Form(""),
):
    admin = admin_required(request)
    if not admin:
        return RedirectResponse(url="/admin/login", status_code=302)
    if not validate_csrf(request, csrf_token):
        return RedirectResponse(url="/admin/review", status_code=302)

    id_list = [int(x.strip()) for x in ids.split(",") if x.strip().isdigit()]
    if id_list and action == "approved":
        batch_update_status(id_list, "approved", admin["id"], admin["username"])
        from app.models import increment_daily_stat, today_str
        increment_daily_stat(today_str(), "approved_count")
        invalidate_top_cache()
    elif id_list and action == "rejected":
        # 拒绝 = 删除：批量删除图片文件与记录
        for iid in id_list:
            img = get_image_by_id(iid)
            if img:
                delete_image_files(img["filename"], img["thumb_filename"])
                delete_image(iid)
        from app.models import increment_daily_stat, today_str
        increment_daily_stat(today_str(), "rejected_count")
        insert_admin_log(admin["id"], admin["username"], "batch_reject", f"批量删除 {len(id_list)} 张图片", str(id_list))
        invalidate_top_cache()

    return RedirectResponse(url="/admin/review", status_code=302)


# ========== 图片管理 ==========

@router.get("/images", response_class=HTMLResponse)
async def admin_images(
    request: Request,
    query: str = "",
    sort: str = "created_at_desc",
    page: int = 1,
):
    admin = admin_required(request)
    if not admin:
        return RedirectResponse(url="/admin/login", status_code=302)
    ctx = get_admin_context(request)

    order_map = {
        "created_at_desc": "created_at DESC",
        "like_count_desc": "like_count DESC",
        "views_desc": "views DESC",
    }
    order_by = order_map.get(sort, "created_at DESC")

    offset = (page - 1) * ADMIN_PER_PAGE
    ctx["images"] = search_images_admin(
        status=None, query=query or None,
        limit=ADMIN_PER_PAGE, offset=offset, order_by=order_by
    )
    ctx["total"] = count_images_admin(status=None, query=query or None)
    ctx["query"] = query
    ctx["sort"] = sort
    ctx["page"] = page
    ctx["total_pages"] = max(1, (ctx["total"] + ADMIN_PER_PAGE - 1) // ADMIN_PER_PAGE)
    return templates.TemplateResponse(request, "admin/images.html", ctx)


@router.get("/images/{image_id}", response_class=HTMLResponse)
async def admin_image_detail(request: Request, image_id: int):
    admin = admin_required(request)
    if not admin:
        return RedirectResponse(url="/admin/login", status_code=302)
    ctx = get_admin_context(request)
    ctx["image"] = get_image_by_id(image_id)
    if not ctx["image"]:
        raise HTTPException(status_code=404)
    ctx["likes"] = get_likes_by_image(image_id, LIKES_PER_PAGE, 0)
    ctx["likes_count"] = count_likes_by_image(image_id)
    return templates.TemplateResponse(request, "admin/image_detail.html", ctx)


@router.post("/images/{image_id}/edit", response_class=HTMLResponse)
async def admin_image_edit(
    request: Request,
    image_id: int,
    title: str = Form(""),
    description: str = Form(""),
    csrf_token: str = Form(""),
):
    admin = admin_required(request)
    if not admin:
        return RedirectResponse(url="/admin/login", status_code=302)
    if not validate_csrf(request, csrf_token):
        return RedirectResponse(url=f"/admin/images/{image_id}", status_code=302)

    image = get_image_by_id(image_id)
    if not image:
        raise HTTPException(status_code=404)

    new_title = title.strip()
    new_desc = description.strip()
    error = None
    if not new_title:
        error = "标题不能为空"
    elif len(new_title) > MAX_TITLE_LENGTH:
        error = f"标题长度不能超过 {MAX_TITLE_LENGTH} 字"
    elif len(new_desc) > MAX_DESC_LENGTH:
        error = f"简介长度不能超过 {MAX_DESC_LENGTH} 字"

    if error:
        ctx = get_admin_context(request)
        ctx["image"] = image
        ctx["likes"] = get_likes_by_image(image_id, LIKES_PER_PAGE, 0)
        ctx["likes_count"] = count_likes_by_image(image_id)
        ctx["edit_error"] = error
        return templates.TemplateResponse(request, "admin/image_detail.html", ctx)

    update_image_content(image_id, new_title, new_desc, admin["id"], admin["username"])
    return RedirectResponse(url=f"/admin/images/{image_id}?saved=1", status_code=302)


@router.post("/images/{image_id}/delete", response_class=HTMLResponse)
async def admin_image_delete(request: Request, image_id: int, csrf_token: str = Form("")):
    admin = admin_required(request)
    if not admin:
        return RedirectResponse(url="/admin/login", status_code=302)
    if not validate_csrf(request, csrf_token):
        return RedirectResponse(url=f"/admin/images/{image_id}", status_code=302)

    image = get_image_by_id(image_id)
    if image:
        delete_image_files(image["filename"], image["thumb_filename"])
        delete_image(image_id)
        insert_admin_log(admin["id"], admin["username"], "delete", "删除图片", f"image#{image_id}")
    return RedirectResponse(url="/admin/images", status_code=302)


@router.post("/images/{image_id}/clear-likes", response_class=HTMLResponse)
async def admin_clear_likes(
    request: Request,
    image_id: int,
    like_ids: str = Form(""),
    csrf_token: str = Form(""),
):
    admin = admin_required(request)
    if not admin:
        return RedirectResponse(url="/admin/login", status_code=302)
    if not validate_csrf(request, csrf_token):
        return RedirectResponse(url=f"/admin/images/{image_id}", status_code=302)

    id_list = [int(x.strip()) for x in like_ids.split(",") if x.strip().isdigit()]
    if id_list:
        delete_likes_by_ids(id_list)
        insert_admin_log(admin["id"], admin["username"], "clear_likes", f"清除 {len(id_list)} 条点赞", f"image#{image_id}")
    return RedirectResponse(url=f"/admin/images/{image_id}", status_code=302)


# ========== IP/用户管理 ==========

@router.get("/users", response_class=HTMLResponse)
async def admin_users(request: Request):
    admin = admin_required(request)
    if not admin:
        return RedirectResponse(url="/admin/login", status_code=302)
    ctx = get_admin_context(request)
    # like_count 已由 get_recent_upload_ips 子查询一并返回，避免 N+1 查询
    ctx["ips"] = [dict(r) for r in get_recent_upload_ips(50)]
    ctx["bans"] = get_ban_list()
    ctx["coolings"] = get_all_cooling()
    return templates.TemplateResponse(request, "admin/users.html", ctx)


@router.post("/users/ban", response_class=HTMLResponse)
async def admin_ban_user(
    request: Request,
    target: str = Form(""),
    ban_type: str = Form(""),
    reason: str = Form(""),
    csrf_token: str = Form(""),
):
    admin = admin_required(request)
    if not admin:
        return RedirectResponse(url="/admin/login", status_code=302)
    if not validate_csrf(request, csrf_token):
        return RedirectResponse(url="/admin/users", status_code=302)

    if not target or not ban_type or not reason:
        return RedirectResponse(url="/admin/users", status_code=302)

    add_ban(target.strip(), ban_type, reason.strip(), admin["id"])
    insert_admin_log(admin["id"], admin["username"], "ban", f"封禁 {ban_type}: {target}", reason)
    return RedirectResponse(url="/admin/users", status_code=302)


@router.post("/users/unban/{ban_id}", response_class=HTMLResponse)
async def admin_unban_user(request: Request, ban_id: int, csrf_token: str = Form("")):
    admin = admin_required(request)
    if not admin:
        return RedirectResponse(url="/admin/login", status_code=302)
    if not validate_csrf(request, csrf_token):
        return RedirectResponse(url="/admin/users", status_code=302)

    remove_ban(ban_id)
    insert_admin_log(admin["id"], admin["username"], "unban", f"解封 ban#{ban_id}")
    return RedirectResponse(url="/admin/users", status_code=302)


@router.post("/users/reset-cooling/{ip}", response_class=HTMLResponse)
async def admin_reset_cooling(request: Request, ip: str, csrf_token: str = Form("")):
    admin = admin_required(request)
    if not admin:
        return RedirectResponse(url="/admin/login", status_code=302)
    if not validate_csrf(request, csrf_token):
        return RedirectResponse(url="/admin/users", status_code=302)

    reset_cooldown(ip)
    insert_admin_log(admin["id"], admin["username"], "reset_cooling", f"重置冷却: {ip}")
    return RedirectResponse(url="/admin/users", status_code=302)


# ========== 站点设置 ==========

@router.get("/settings", response_class=HTMLResponse)
async def admin_settings(request: Request):
    admin = admin_required(request)
    if not admin:
        return RedirectResponse(url="/admin/login", status_code=302)
    ctx = get_admin_context(request)
    ctx["settings"] = get_all_settings()
    return templates.TemplateResponse(request, "admin/settings.html", ctx)


@router.post("/settings", response_class=HTMLResponse)
async def admin_settings_save(request: Request, csrf_token: str = Form("")):
    admin = admin_required(request)
    if not admin:
        return RedirectResponse(url="/admin/login", status_code=302)
    if not validate_csrf(request, csrf_token):
        return RedirectResponse(url="/admin/settings", status_code=302)

    form = await request.form()
    setting_keys = [
        "site_name", "site_description", "footer_icp", "footer_copyright",
        "theme_accent_color", "upload_limit_per_ip", "upload_limit_per_ua",
        "upload_limit_daily_total", "upload_interval_seconds",
        "like_limit_per_minute", "top_window_days", "top_show_count",
        "allow_author", "enable_captcha", "enable_dedup", "sensitive_words",
        "turnstile_site_key",
    ]

    # 保存旧管理域名
    old_admin_domain = get_setting("admin_domain", "")

    for key in setting_keys:
        val = form.get(key, "")
        if key in ("allow_author", "enable_captcha", "enable_dedup"):
            val = "1" if val == "on" else "0"
        set_setting(key, val)

    # Turnstile 密钥：留空表示不修改（避免保存时误清空已配置的密钥）
    turnstile_secret = form.get("turnstile_secret_key", "").strip()
    if turnstile_secret:
        set_setting("turnstile_secret_key", turnstile_secret)

    # 管理域名特殊处理
    new_admin_domain = form.get("admin_domain", "")
    if new_admin_domain != old_admin_domain:
        set_setting("admin_domain", new_admin_domain)
        insert_admin_log(admin["id"], admin["username"], "config", f"管理域名: {old_admin_domain} -> {new_admin_domain}")

    # 上传 logo（校验扩展名并使用 UPLOADS_DIR 绝对路径）
    logo_name = await _save_logo(form.get("logo_large_file"), "logo_large")
    if logo_name:
        set_setting("logo_large", logo_name)

    logo_name = await _save_logo(form.get("logo_small_file"), "logo_small")
    if logo_name:
        set_setting("logo_small", logo_name)

    # 重新加载敏感词
    load_sensitive_words()

    # 使缓存失效
    invalidate_top_cache()

    insert_admin_log(admin["id"], admin["username"], "config", "更新站点设置")
    return RedirectResponse(url="/admin/settings", status_code=302)


# ========== 友情链接管理 ==========

@router.get("/links", response_class=HTMLResponse)
async def admin_links(request: Request):
    admin = admin_required(request)
    if not admin:
        return RedirectResponse(url="/admin/login", status_code=302)
    ctx = get_admin_context(request)
    ctx["links"] = get_all_links()
    return templates.TemplateResponse(request, "admin/links.html", ctx)


@router.post("/links/save", response_class=HTMLResponse)
async def admin_links_save(
    request: Request,
    link_id: str = Form(""),
    name: str = Form(""),
    url: str = Form(""),
    sort_order: str = Form("0"),
    is_visible: str = Form("1"),
    csrf_token: str = Form(""),
):
    admin = admin_required(request)
    if not admin:
        return RedirectResponse(url="/admin/login", status_code=302)
    if not validate_csrf(request, csrf_token):
        return RedirectResponse(url="/admin/links", status_code=302)

    # URL 安全校验
    if not url.startswith("http://") and not url.startswith("https://"):
        return RedirectResponse(url="/admin/links", status_code=302)

    lid = int(link_id) if link_id else 0
    upsert_link(lid, name.strip(), url.strip(), int(sort_order), int(is_visible))
    insert_admin_log(admin["id"], admin["username"], "link", f"{'编辑' if lid else '新增'}友情链接: {name}")
    return RedirectResponse(url="/admin/links", status_code=302)


@router.post("/links/delete/{link_id}", response_class=HTMLResponse)
async def admin_links_delete(request: Request, link_id: int, csrf_token: str = Form("")):
    admin = admin_required(request)
    if not admin:
        return RedirectResponse(url="/admin/login", status_code=302)
    if not validate_csrf(request, csrf_token):
        return RedirectResponse(url="/admin/links", status_code=302)

    delete_link(link_id)
    insert_admin_log(admin["id"], admin["username"], "link", f"删除友情链接 #{link_id}")
    return RedirectResponse(url="/admin/links", status_code=302)


# ========== 页面管理 ==========

@router.get("/pages", response_class=HTMLResponse)
async def admin_pages(request: Request):
    admin = admin_required(request)
    if not admin:
        return RedirectResponse(url="/admin/login", status_code=302)
    ctx = get_admin_context(request)
    ctx["pages"] = get_all_pages()
    return templates.TemplateResponse(request, "admin/pages.html", ctx)


@router.get("/pages/edit/{page_id}", response_class=HTMLResponse)
@router.get("/pages/edit", response_class=HTMLResponse)
async def admin_pages_edit(request: Request, page_id: int = 0):
    admin = admin_required(request)
    if not admin:
        return RedirectResponse(url="/admin/login", status_code=302)
    ctx = get_admin_context(request)
    ctx["page_obj"] = get_page_by_id(page_id) if page_id else None
    return templates.TemplateResponse(request, "admin/page_edit.html", ctx)


@router.post("/pages/save", response_class=HTMLResponse)
async def admin_pages_save(
    request: Request,
    page_id: str = Form(""),
    slug: str = Form(""),
    title: str = Form(""),
    content: str = Form(""),
    sort_order: str = Form("0"),
    is_visible: str = Form("1"),
    csrf_token: str = Form(""),
):
    admin = admin_required(request)
    if not admin:
        return RedirectResponse(url="/admin/login", status_code=302)
    if not validate_csrf(request, csrf_token):
        return RedirectResponse(url="/admin/pages", status_code=302)

    pid = int(page_id) if page_id else 0
    upsert_page(pid, slug.strip(), title.strip(), content, int(sort_order), int(is_visible))
    insert_admin_log(admin["id"], admin["username"], "page", f"{'编辑' if pid else '新增'}页面: {title}")
    return RedirectResponse(url="/admin/pages", status_code=302)


@router.post("/pages/delete/{page_id}", response_class=HTMLResponse)
async def admin_pages_delete(request: Request, page_id: int, csrf_token: str = Form("")):
    admin = admin_required(request)
    if not admin:
        return RedirectResponse(url="/admin/login", status_code=302)
    if not validate_csrf(request, csrf_token):
        return RedirectResponse(url="/admin/pages", status_code=302)

    delete_page(page_id)
    insert_admin_log(admin["id"], admin["username"], "page", f"删除页面 #{page_id}")
    return RedirectResponse(url="/admin/pages", status_code=302)


# ========== 操作日志 ==========

@router.get("/logs", response_class=HTMLResponse)
async def admin_logs(
    request: Request,
    page: int = 1,
    action: str = "",
    ip: str = "",
    location: str = "",
    q: str = "",
    date_from: str = "",
    date_to: str = "",
):
    admin = admin_required(request)
    if not admin:
        return RedirectResponse(url="/admin/login", status_code=302)
    ctx = get_admin_context(request)
    offset = (page - 1) * 50

    df = f"{date_from}T00:00:00" if date_from else ""
    dt = f"{date_to}T23:59:59" if date_to else ""

    query = q.strip() or None
    ctx["logs"] = search_admin_logs(
        action=action or None,
        ip=ip.strip() or None,
        location=location.strip() or None,
        query=query,
        date_from=df or None,
        date_to=dt or None,
        limit=50, offset=offset,
    )
    ctx["total"] = count_admin_logs(
        action=action or None,
        ip=ip.strip() or None,
        location=location.strip() or None,
        query=query,
        date_from=df or None,
        date_to=dt or None,
    )
    ctx["page"] = page
    ctx["total_pages"] = max(1, (ctx["total"] + 50 - 1) // 50)
    ctx["action"] = action
    ctx["ip"] = ip
    ctx["location"] = location
    ctx["q"] = q
    ctx["date_from"] = date_from
    ctx["date_to"] = date_to
    return templates.TemplateResponse(request, "admin/logs.html", ctx)


# ========== 修改密码 ==========

@router.get("/change-password", response_class=HTMLResponse)
async def admin_change_password_page(request: Request):
    admin = admin_required(request)
    if not admin:
        return RedirectResponse(url="/admin/login", status_code=302)
    ctx = get_admin_context(request)
    return templates.TemplateResponse(request, "admin/change_password.html", ctx)


@router.post("/change-password", response_class=HTMLResponse)
async def admin_change_password_action(
    request: Request,
    new_password: str = Form(""),
    confirm_password: str = Form(""),
    csrf_token: str = Form(""),
):
    admin = admin_required(request)
    if not admin:
        return RedirectResponse(url="/admin/login", status_code=302)
    if not validate_csrf(request, csrf_token):
        return RedirectResponse(url="/admin/change-password", status_code=302)

    from app.auth import hash_password
    from app.models import update_admin_password

    if len(new_password) < 6:
        ctx = get_admin_context(request)
        ctx["error"] = "新密码长度不能少于6位"
        return templates.TemplateResponse(request, "admin/change_password.html", ctx)

    if new_password != confirm_password:
        ctx = get_admin_context(request)
        ctx["error"] = "两次输入的新密码不一致"
        return templates.TemplateResponse(request, "admin/change_password.html", ctx)

    update_admin_password(admin["id"], hash_password(new_password))
    insert_admin_log(admin["id"], admin["username"], "change_password", "修改密码")

    ctx = get_admin_context(request)
    ctx["success"] = "密码修改成功"
    return templates.TemplateResponse(request, "admin/change_password.html", ctx)