"""后台管理路由（仅管理域名可访问）"""
import re
from fastapi import APIRouter, Request, Form, File, UploadFile, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import escape, Markup
from app.config import (
    BASE_DIR, ADMIN_PER_PAGE, LIKES_PER_PAGE, MAX_DESC_LENGTH, MAX_TITLE_LENGTH,
    UPLOADS_DIR, APP_NAME, APP_VERSION, MAX_UPLOAD_SIZE, SESSION_TTL_HOURS,
    DB_TYPE, DATABASE_PATH, MYSQL_HOST, MYSQL_PORT, MYSQL_DATABASE, MYSQL_USER,
)
from app.models import (
    get_images_by_status, count_images_by_status, get_image_by_id,
    update_image_status, update_image_content, delete_image, get_all_links, get_link_by_id,
    upsert_link, delete_link, get_all_pages, get_page_by_id,
    upsert_page, delete_page, search_admin_logs, count_admin_logs,
    insert_admin_log, set_current_admin_request, get_setting, set_setting, get_all_settings,
    get_ban_list, add_ban, remove_ban, get_recent_upload_ips,
    get_all_cooling, reset_cooldown,
    search_images_admin, count_images_admin, normalize_search_query,
    get_likes_by_image, count_likes_by_image, delete_like,
    delete_likes_by_ids, decrement_like_count,
    get_top_images_by_likes, get_setting_int,
    get_stats_range, get_daily_stats, today_str,
    ensure_daily_stats, get_page_by_slug_any, rollback_image_to_pending,
    try_approve_image, increment_daily_stat,
)
from app.stats import get_dashboard_data, invalidate_top_cache
from app.auth import (
    login, get_current_admin, hash_password, verify_password, destroy_session,
    rotate_session, reset_login_ip_limit, is_login_ip_blocked, record_login_failure,
    get_session,
)
from app.middleware import validate_csrf, validate_admin_csrf, load_sensitive_words, _secure_cookie_for_request
from app.password_policy import validate_password
from app.admin_domains import get_request_host, is_loopback_host, normalize_admin_domains
from app import image_utils
from app.ip_geo import get_client_ip
from app.display import safe_color
import os
import json
import logging
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

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
    """保存上传的 Logo 到 UPLOADS_DIR，返回文件名；非法文件返回 None。

    与用户图片上传共用同一套安全处理链路：分块限长读取 → 扩展名白名单
    （禁止 SVG）→ 实际解码校验（含解压炸弹、动图与像素尺寸限制）→
    标准格式重新编码（剥离隐藏数据/恶意载荷）→ 原子写入。
    """
    if not (hasattr(upload_file, "filename") and upload_file.filename):
        return None
    import uuid
    ext = os.path.splitext(upload_file.filename)[1].lower()
    if ext not in ALLOWED_LOGO_EXTS:
        logger.warning("拒绝不支持的 Logo 格式: %s", upload_file.filename)
        return None

    buffer = bytearray()
    total = 0
    while True:
        chunk = await upload_file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_UPLOAD_SIZE:
            logger.warning("拒绝超大的 Logo 上传: %s", upload_file.filename)
            return None
        buffer.extend(chunk)
    if total == 0:
        return None

    # 解码校验与重编码是 CPU 密集操作，放入线程池避免阻塞事件循环
    data, final_ext, error = await run_in_threadpool(
        image_utils.process_logo, bytes(buffer), ext
    )
    if error:
        logger.warning("拒绝非法的 Logo 上传 %s: %s", upload_file.filename, error)
        return None

    logo_name = f"{prefix}_{uuid.uuid4().hex}{final_ext}"
    os.makedirs(UPLOADS_DIR, exist_ok=True)
    final_path = os.path.join(UPLOADS_DIR, logo_name)
    try:
        image_utils._atomic_write(final_path, data)
    except OSError:
        logger.exception("Logo 写入失败: %s", logo_name)
        return None
    return logo_name


def _set_admin_session_cookie(response, session_id, request):
    """为当前设备写入管理员会话 Cookie（HttpOnly + Secure + SameSite=Lax）。"""
    response.set_cookie(
        key="admin_session",
        value=session_id,
        httponly=True,
        samesite="lax",
        secure=_secure_cookie_for_request(request),
        max_age=SESSION_TTL_HOURS * 3600,
        path="/",
    )


def _delete_admin_session_cookie(response, request):
    """删除会话 Cookie 时保持与写入时一致的属性，确保浏览器真正清除。"""
    response.delete_cookie(
        key="admin_session",
        path="/",
        secure=_secure_cookie_for_request(request),
        httponly=True,
        samesite="lax",
    )


# 站点设置中数值项的最小值；与 templates/admin/settings.html 的 min 属性保持一致。
# 只约束下限（0/负数会直接破坏限流与分页逻辑），不限制上限，避免改变大站点的既有配置。
_NUMERIC_SETTING_MINIMUMS = {
    "upload_limit_per_ip": 1,
    "upload_limit_per_ua": 0,
    "upload_limit_daily_total": 1,
    "upload_interval_seconds": 0,
    "like_limit_per_minute": 1,
    "top_window_days": 1,
    "top_show_count": 1,
}

# 校验提示使用页面上的中文字段名，便于管理员直接定位到出错输入框
_NUMERIC_SETTING_LABELS = {
    "upload_limit_per_ip": "每 IP 每日上限",
    "upload_limit_per_ua": "每 UA 每日上限",
    "upload_limit_daily_total": "全站每日总上限",
    "upload_interval_seconds": "上传间隔（秒）",
    "like_limit_per_minute": "每 IP 每分钟点赞上限",
    "top_window_days": "热度榜窗口天数",
    "top_show_count": "热度榜展示数量",
}


def _validate_numeric_settings(form) -> str:
    """校验提交的数值设置，返回错误提示；合法时返回空串。"""
    for key, minimum in _NUMERIC_SETTING_MINIMUMS.items():
        if key not in form:
            continue
        label = _NUMERIC_SETTING_LABELS.get(key, key)
        raw = str(form.get(key, "")).strip()
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return f"数值设置不合法：{label} 必须是整数"
        if value < minimum:
            return f"数值设置不合法：{label} 不能小于 {minimum}"
    return ""


def _normalized_numeric_settings(form) -> dict:
    """把已通过校验的数值项转为规范化字符串。"""
    return {
        key: str(int(str(form.get(key, "")).strip()))
        for key in _NUMERIC_SETTING_MINIMUMS
        if key in form
    }


def _safe_int(value, default=0):
    """解析表单整数；非法输入回退默认值，避免直接抛出 ValueError 导致 500。"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def admin_required(request: Request):
    """检查管理员登录状态，返回 admin 对象或 None"""
    admin = get_current_admin(request)
    if admin:
        set_current_admin_request(request)
    return admin


def _publish_image(image, admin):
    """发布图片：先把文件移动到公开目录，再条件更新数据库状态。

    顺序至关重要。旧实现先置 status='approved' 再移动文件，两步之间若被匿名
    访问，媒体路由会因为「已通过」而直接把仍在私有目录的文件返回 200，相当于
    未发布成功的内容被公开。改为「先发布文件、后改状态」后：
      - 文件移动失败：状态仍是 pending，不会出现半发布；
      - 状态更新失败：文件回滚到私有目录，同样不会半发布；
      - 并发审核：条件更新只有一个请求成功，其余请求视为已完成（不重复统计）。

    返回 (ok, error_key, failures)。
    """
    failures = image_utils.promote_image_files(
        image["filename"], image["thumb_filename"]
    )
    if failures:
        # 文件未发布，状态保持不变（仍为 pending）
        return False, "file_move", failures

    changed = try_approve_image(
        image["id"], admin_id=admin["id"], admin_name=admin["username"],
    )
    if not changed:
        current = get_image_by_id(image["id"])
        if current is None:
            # 并发删除：刚移动到公开目录的文件属于孤立文件，就地清理
            image_utils.delete_image_files(
                image["filename"], image["thumb_filename"],
                uploads_dir=image_utils.UPLOADS_DIR,
            )
            return False, "not_found", failures
        if (current["status"] or "") != "approved":
            # 极端情况下的更新失败：把文件退回私有目录，避免半发布状态
            rollback = image_utils.promote_image_files(
                image["filename"], image["thumb_filename"],
                source_dir=image_utils.UPLOADS_DIR,
                target_dir=image_utils.PRIVATE_UPLOADS_DIR,
            )
            logger.warning(
                "审核状态更新失败，已回滚文件 image#%s: %s", image["id"], rollback
            )
            return False, "file_move", rollback
        # 已被其他管理员并发通过：文件已在公开目录，视为成功但不重复统计
        return True, None, []

    increment_daily_stat(today_str(), "approved_count")
    return True, None, []


def _delete_image_and_files(image):
    """先删除数据库记录，成功后再删除文件。

    数据库操作失败时文件保持不变；数据库删除成功后才清理文件，避免出现
    「文件已删但记录仍引用」的不一致状态。文件删除失败会记录日志并返回失败列表。
    """
    deleted = delete_image(image["id"])
    if deleted is None:
        # 记录已被并发删除，不再重复删除文件与统计
        return None
    # 同时尝试私有与公开目录：兼容旧版本仍留在公开目录的待审文件
    failures = []
    for base in (image_utils.PRIVATE_UPLOADS_DIR, image_utils.UPLOADS_DIR):
        failures.extend(
            image_utils.delete_image_files(
                image["filename"], image["thumb_filename"], uploads_dir=base
            )
        )
    if failures:
        logger.warning(
            "图片记录已删除，但文件清理失败 image#%s: %s", image["id"], failures
        )
    return failures


# 后台操作结果提示：只接受白名单 key，避免把查询串原样渲染到页面。
# {n} 会被替换为操作条数，可通过 failed 参数追加失败条数。
_ADMIN_FLASH_MESSAGES = {
    "approved": "图片已通过审核并发布",
    "rejected": "图片已拒绝并删除",
    "deleted": "图片已删除",
    "batch_approved": "已批量通过 {n} 张图片",
    "batch_rejected": "已批量删除 {n} 张图片",
    "likes_cleared": "已清除 {n} 条点赞记录",
    "image_saved": "标题 / 简介已更新",
    "banned": "封禁已生效",
    "unbanned": "已解封该目标",
    "cooling_reset": "冷却状态已重置",
    "link_saved": "友情链接已保存",
    "link_deleted": "友情链接已删除",
    "page_saved": "页面已保存",
    "page_deleted": "页面已删除",
    "settings_saved": "设置已保存",
}

_ADMIN_FLASH_ERRORS = {
    "file_move": "文件发布或删除失败，状态已回滚/保留，请查看操作日志。",
    "file_delete": "图片记录已删除，但部分文件清理失败，请查看操作日志并运行上传目录一致性检查。",
    "fields": "请填写完整的必填项。",
    "url": "链接地址必须以 http:// 或 https:// 开头。",
}


def _flash_messages(request: Request):
    """从查询串读取操作结果提示，返回 (success, error, failed_ids)。

    未知 key 一律忽略；failed_ids 仅对批量通过生效（记录仍存在，可跳转详情），
    并限制为 20 个纯数字，避免超长或非法参数进入页面。
    """
    params = request.query_params
    success = ""
    template = _ADMIN_FLASH_MESSAGES.get(params.get("msg", ""))
    if template:
        if "{n}" in template:
            success = template.format(n=_safe_int(params.get("n", ""), 0))
        else:
            success = template
        failed = _safe_int(params.get("failed", ""), 0)
        if failed:
            success += f"；{failed} 项失败，请查看操作日志"
    failed_ids = []
    if params.get("msg") == "batch_approved":
        failed_ids = [
            int(item) for item in params.get("failed_ids", "").split(",")
            if item.isdigit()
        ][:20]
    return success, _ADMIN_FLASH_ERRORS.get(params.get("error", ""), ""), failed_ids


def get_admin_context(request: Request, admin=None, csrf_token=None):
    """Get admin template context.

    后台表单使用与会话绑定的服务端 CSRF 令牌，而不是匿名 Cookie 令牌。
    同时读取上一操作的结果提示（?msg= / ?error=），供 admin_base.html 统一展示。
    """
    if admin is None:
        admin = admin_required(request)
    settings = get_all_settings()
    token = csrf_token or getattr(request.state, "admin_csrf_token", "")
    if not token:
        token = getattr(request.state, "csrf_token", "")
    ctx = {
        "admin": admin,
        "site_name": settings.get("site_name", "匿名图片分享"),
        "accent_color": settings.get("theme_accent_color", "#c9a063"),
        "csrf_token": token,
    }
    success, error, failed_ids = _flash_messages(request)
    if success:
        ctx["success"] = success
    if error:
        ctx["error"] = error
    if failed_ids:
        ctx["flash_failed_ids"] = failed_ids
    return ctx


# ========== 登录 ==========

def get_login_context(request: Request, error: str = ""):
    """登录页上下文：未登录时也需要站点名与强调色，保证登录页与前台视觉一致。"""
    settings = get_all_settings()
    ctx = {
        "csrf_token": getattr(request.state, "csrf_token", ""),
        "site_name": settings.get("site_name", "匿名图片分享"),
        "accent_color": settings.get("theme_accent_color", "#c9a063"),
    }
    if error:
        ctx["error"] = error
    return ctx


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    admin = get_current_admin(request)
    if admin:
        return RedirectResponse(url="/admin/dashboard", status_code=302)
    return templates.TemplateResponse(request, "admin/login.html", get_login_context(request))


@router.post("/login", response_class=HTMLResponse)
async def login_action(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    csrf_token: str = Form(""),
):
    # 登录页没有服务端会话，使用匿名双提交 CSRF + 全局 Origin 校验
    if not validate_csrf(request, csrf_token):
        return templates.TemplateResponse(
            request, "admin/login.html", get_login_context(request, "请求无效，请刷新页面重试")
        )

    client_ip = get_client_ip(request)
    # 只读判断，不在密码校验前消耗失败额度
    if is_login_ip_blocked(client_ip):
        return templates.TemplateResponse(
            request, "admin/login.html", get_login_context(request, "尝试次数过多，请稍后再试")
        )

    success, result, admin_obj = login(username, password)
    if not success:
        blocked = record_login_failure(client_ip)
        # 只记录用户名/IP 与失败事实，不记录密码、CSRF 或会话值
        insert_admin_log(
            admin_obj["id"] if admin_obj else 0,
            (username.strip()[:64] or "(空用户名)"),
            "login_fail",
            "密码校验失败",
            ip=client_ip,
        )
        message = "尝试次数过多，请稍后再试" if blocked else result
        return templates.TemplateResponse(
            request, "admin/login.html", get_login_context(request, message)
        )

    # 登录成功：清除 IP 失败计数与账户失败统计，轮换会话
    reset_login_ip_limit(client_ip)
    old_session = request.cookies.get("admin_session")
    if old_session:
        destroy_session(old_session)
    response = RedirectResponse(url="/admin/dashboard", status_code=302)
    _set_admin_session_cookie(response, result, request)
    set_current_admin_request(request)
    insert_admin_log(admin_obj["id"], admin_obj["username"], "login", "登录成功")
    return response


@router.post("/logout")
async def logout(request: Request, csrf_token: str = Form("")):
    # 退出登录属于状态变更操作，必须使用 POST 并校验 CSRF，避免被跨站链接触发
    if not validate_admin_csrf(request, csrf_token):
        return RedirectResponse(url="/admin/dashboard", status_code=302)
    session_id = request.cookies.get("admin_session")
    if session_id:
        destroy_session(session_id)
    response = RedirectResponse(url="/admin/login", status_code=302)
    _delete_admin_session_cookie(response, request)
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
    page = max(1, page)
    query = normalize_search_query(query)
    ctx["total"] = count_images_admin(status="pending", query=query or None)
    ctx["total_pages"] = max(1, (ctx["total"] + ADMIN_PER_PAGE - 1) // ADMIN_PER_PAGE)
    page = min(page, ctx["total_pages"])
    offset = (page - 1) * ADMIN_PER_PAGE
    ctx["images"] = search_images_admin(status="pending", query=query or None, limit=ADMIN_PER_PAGE, offset=offset)
    ctx["query"] = query
    ctx["search_param"] = "query"
    ctx["page"] = page
    return templates.TemplateResponse(request, "admin/review.html", ctx)


# 注意：/review/batch 必须注册在 /review/{image_id} 之前，否则 "batch" 会被
# 当作 image_id 并返回 422，导致批量通过/删除功能实际不可用。
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
    if not validate_admin_csrf(request, csrf_token):
        return RedirectResponse(url="/admin/review", status_code=302)

    id_list = [int(x.strip()) for x in ids.split(",") if x.strip().isdigit()]
    flash_key = ""
    done_count = 0
    failed_count = 0
    failed_ids = []
    if id_list and action == "approved":
        approved_count = 0
        for iid in id_list:
            image = get_image_by_id(iid)
            if not image or image["status"] == "approved":
                continue
            ok, error_key, detail = _publish_image(image, admin)
            if ok:
                approved_count += 1
            else:
                failed_ids.append(iid)
                if error_key != "not_found":
                    logger.warning("批量发布失败 image#%s: %s", iid, detail)
        detail = f"批量通过 {approved_count}/{len(id_list)} 张图片"
        if failed_ids:
            detail += f"；失败: {failed_ids}"
        insert_admin_log(admin["id"], admin["username"], "batch_approve", detail, str(id_list))
        invalidate_top_cache()
        flash_key = "batch_approved"
        done_count = approved_count
        failed_count = len(failed_ids)
    elif id_list and action == "rejected":
        # 拒绝 = 删除：逐条处理，单条失败不影响其他图片，并在日志中明确记录失败项
        deleted_count = 0
        for iid in id_list:
            img = get_image_by_id(iid)
            if img:
                try:
                    if _delete_image_and_files(img) is not None:
                        deleted_count += 1
                except Exception:
                    failed_ids.append(iid)
                    logger.exception("批量删除图片失败 image#%s", iid)
        if deleted_count:
            increment_daily_stat(today_str(), "rejected_count")
        detail = f"批量删除 {deleted_count}/{len(id_list)} 张图片"
        if failed_ids:
            detail += f"；失败: {failed_ids}"
        insert_admin_log(admin["id"], admin["username"], "batch_reject", detail, str(id_list))
        invalidate_top_cache()
        flash_key = "batch_rejected"
        done_count = deleted_count
        failed_count = len(failed_ids)

    if flash_key:
        url = f"/admin/review?msg={flash_key}&n={done_count}"
        if failed_count:
            url += f"&failed={failed_count}"
            # 批量通过失败时图片仍为待审核，附上 id 便于逐张跳转处理
            if flash_key == "batch_approved" and failed_ids:
                url += "&failed_ids=" + ",".join(str(iid) for iid in failed_ids[:20])
        return RedirectResponse(url=url, status_code=302)
    return RedirectResponse(url="/admin/review", status_code=302)


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
    if not validate_admin_csrf(request, csrf_token):
        return RedirectResponse(url=f"/admin/review/{image_id}", status_code=302)

    image = get_image_by_id(image_id)
    if not image:
        raise HTTPException(status_code=404)

    if action == "approve":
        # 内容编辑与发布分开：管理员编辑的内容先落库（不改变可见性），
        # 再由 _publish_image 以「先移动文件、后条件更新状态」的顺序发布。
        if edit_content == "1":
            new_title = title.strip()
            new_desc = description.strip()
            new_author = author.strip()
            changed_title = new_title if new_title != (image["title"] or "") else image["title"] or ""
            changed_desc = new_desc if new_desc != (image["description"] or "") else image["description"] or ""
            author_changed = new_author != (image["author"] or "")
            if (changed_title != (image["title"] or "")
                    or changed_desc != (image["description"] or "")
                    or author_changed):
                update_image_content(
                    image_id, changed_title, changed_desc,
                    admin_id=admin["id"], admin_name=admin["username"],
                    author=new_author if author_changed else None,
                )
        ok, error_key, detail = _publish_image(image, admin)
        if not ok:
            insert_admin_log(
                admin["id"], admin["username"], "approve_failed",
                f"发布失败，状态保持待审核: {detail}", f"image#{image_id}",
            )
            return RedirectResponse(
                url=f"/admin/review/{image_id}?error=file_move", status_code=302
            )
        invalidate_top_cache()
        return RedirectResponse(url="/admin/review?msg=approved", status_code=302)
    elif action == "reject":
        # 拒绝 = 删除：先删数据库记录，成功后再清理图片文件
        failures = _delete_image_and_files(image)
        insert_admin_log(admin["id"], admin["username"], "reject", "拒绝并删除图片", f"image#{image_id}")
        if failures is None:
            # 已被并发删除，不重复统计冷却
            return RedirectResponse(url="/admin/review", status_code=302)
        increment_daily_stat(today_str(), "rejected_count")
        from app.models import increment_reject_cooldown
        increment_reject_cooldown(image["ip"])
        invalidate_top_cache()
        if failures:
            return RedirectResponse(url="/admin/review?error=file_delete", status_code=302)
        return RedirectResponse(url="/admin/review?msg=rejected", status_code=302)
    elif action == "delete":
        failures = _delete_image_and_files(image)
        insert_admin_log(admin["id"], admin["username"], "delete", "删除图片", f"image#{image_id}")
        if failures:
            return RedirectResponse(url="/admin/review?error=file_delete", status_code=302)
        return RedirectResponse(url="/admin/review?msg=deleted", status_code=302)

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

    page = max(1, page)
    query = normalize_search_query(query)
    ctx["total"] = count_images_admin(status=None, query=query or None)
    ctx["total_pages"] = max(1, (ctx["total"] + ADMIN_PER_PAGE - 1) // ADMIN_PER_PAGE)
    page = min(page, ctx["total_pages"])
    offset = (page - 1) * ADMIN_PER_PAGE
    ctx["images"] = search_images_admin(
        status=None, query=query or None,
        limit=ADMIN_PER_PAGE, offset=offset, order_by=order_by
    )
    ctx["query"] = query
    ctx["search_param"] = "query"
    ctx["sort"] = sort
    ctx["page"] = page
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
    if not validate_admin_csrf(request, csrf_token):
        return RedirectResponse(url=f"/admin/images/{image_id}", status_code=302)

    image = get_image_by_id(image_id)
    if not image:
        raise HTTPException(status_code=404)

    new_title = title.strip()
    new_desc = description.strip()
    error = None
    error_field = ""
    if not new_title:
        error = "标题不能为空"
        error_field = "title"
    elif len(new_title) > MAX_TITLE_LENGTH:
        error = f"标题长度不能超过 {MAX_TITLE_LENGTH} 字"
        error_field = "title"
    elif len(new_desc) > MAX_DESC_LENGTH:
        error = f"简介长度不能超过 {MAX_DESC_LENGTH} 字"
        error_field = "description"

    if error:
        ctx = get_admin_context(request)
        ctx["image"] = image
        ctx["likes"] = get_likes_by_image(image_id, LIKES_PER_PAGE, 0)
        ctx["likes_count"] = count_likes_by_image(image_id)
        ctx["edit_error"] = error
        ctx["edit_error_field"] = error_field
        return templates.TemplateResponse(request, "admin/image_detail.html", ctx)

    update_image_content(image_id, new_title, new_desc, admin["id"], admin["username"])
    return RedirectResponse(url=f"/admin/images/{image_id}?msg=image_saved", status_code=302)


@router.post("/images/{image_id}/delete", response_class=HTMLResponse)
async def admin_image_delete(request: Request, image_id: int, csrf_token: str = Form("")):
    admin = admin_required(request)
    if not admin:
        return RedirectResponse(url="/admin/login", status_code=302)
    if not validate_admin_csrf(request, csrf_token):
        return RedirectResponse(url=f"/admin/images/{image_id}", status_code=302)

    image = get_image_by_id(image_id)
    if image:
        _delete_image_and_files(image)
        insert_admin_log(admin["id"], admin["username"], "delete", "删除图片", f"image#{image_id}")
    return RedirectResponse(url="/admin/images?msg=deleted", status_code=302)


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
    if not validate_admin_csrf(request, csrf_token):
        return RedirectResponse(url=f"/admin/images/{image_id}", status_code=302)

    id_list = [int(x.strip()) for x in like_ids.split(",") if x.strip().isdigit()]
    if id_list:
        delete_likes_by_ids(id_list)
        insert_admin_log(admin["id"], admin["username"], "clear_likes", f"清除 {len(id_list)} 条点赞", f"image#{image_id}")
        return RedirectResponse(
            url=f"/admin/images/{image_id}?msg=likes_cleared&n={len(id_list)}", status_code=302
        )
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
    if not validate_admin_csrf(request, csrf_token):
        return RedirectResponse(url="/admin/users", status_code=302)

    if not target or not ban_type or not reason:
        return RedirectResponse(url="/admin/users?error=fields", status_code=302)

    add_ban(target.strip(), ban_type, reason.strip(), admin["id"])
    insert_admin_log(admin["id"], admin["username"], "ban", f"封禁 {ban_type}: {target}", reason)
    return RedirectResponse(url="/admin/users?msg=banned", status_code=302)


@router.post("/users/unban/{ban_id}", response_class=HTMLResponse)
async def admin_unban_user(request: Request, ban_id: int, csrf_token: str = Form("")):
    admin = admin_required(request)
    if not admin:
        return RedirectResponse(url="/admin/login", status_code=302)
    if not validate_admin_csrf(request, csrf_token):
        return RedirectResponse(url="/admin/users", status_code=302)

    remove_ban(ban_id)
    insert_admin_log(admin["id"], admin["username"], "unban", f"解封 ban#{ban_id}")
    return RedirectResponse(url="/admin/users?msg=unbanned", status_code=302)


@router.post("/users/reset-cooling/{ip}", response_class=HTMLResponse)
async def admin_reset_cooling(request: Request, ip: str, csrf_token: str = Form("")):
    admin = admin_required(request)
    if not admin:
        return RedirectResponse(url="/admin/login", status_code=302)
    if not validate_admin_csrf(request, csrf_token):
        return RedirectResponse(url="/admin/users", status_code=302)

    reset_cooldown(ip)
    insert_admin_log(admin["id"], admin["username"], "reset_cooling", f"重置冷却: {ip}")
    return RedirectResponse(url="/admin/users?msg=cooling_reset", status_code=302)


# ========== 站点设置 ==========

@router.get("/settings", response_class=HTMLResponse)
async def admin_settings(request: Request):
    admin = admin_required(request)
    if not admin:
        return RedirectResponse(url="/admin/login", status_code=302)
    ctx = get_admin_context(request)
    settings = get_all_settings()
    # 展示时统一为归一化后的域名，便于管理员核对
    settings["admin_domain"] = ", ".join(normalize_admin_domains(settings.get("admin_domain", ""))[0])
    ctx["settings"] = settings
    ctx["db_type_text"] = _current_db_description()["text"]
    return templates.TemplateResponse(request, "admin/settings.html", ctx)


@router.post("/settings", response_class=HTMLResponse)
async def admin_settings_save(request: Request, csrf_token: str = Form("")):
    admin = admin_required(request)
    if not admin:
        return RedirectResponse(url="/admin/login", status_code=302)
    if not validate_admin_csrf(request, csrf_token):
        return RedirectResponse(url="/admin/settings", status_code=302)

    form = await request.form()

    # 管理域名：先校验，避免其他设置已保存后才发现域名非法
    domain_submitted = "admin_domain" in form
    if domain_submitted:
        raw_admin_domain = form.get("admin_domain", "")
        new_domains, invalid_domains = normalize_admin_domains(raw_admin_domain)
    else:
        raw_admin_domain = get_setting("admin_domain", "") or ""
        new_domains, invalid_domains = normalize_admin_domains(raw_admin_domain)
    if invalid_domains:
        ctx = get_admin_context(request)
        ctx["settings"] = {**get_all_settings(), "admin_domain": raw_admin_domain}
        ctx["error"] = (
            "管理域名格式无效：" + "、".join(invalid_domains)
            + "。仅允许域名或 IP（可带端口），多个用逗号或换行分隔；"
            "不要包含 http://、路径、通配符或空格。"
        )
        return templates.TemplateResponse(request, "admin/settings.html", ctx)

    if domain_submitted and not new_domains and not is_loopback_host(get_request_host(request)):
        ctx = get_admin_context(request)
        ctx["settings"] = {**get_all_settings(), "admin_domain": raw_admin_domain}
        ctx["error"] = "非本机访问时不能清空管理域名，否则将无法再进入后台。"
        return templates.TemplateResponse(request, "admin/settings.html", ctx)

    enable_captcha = form.get("enable_captcha") == "on"
    site_key = form.get("turnstile_site_key", "").strip()
    secret_key = form.get("turnstile_secret_key", "").strip() or get_setting("turnstile_secret_key", "").strip()
    if enable_captcha and (not site_key or not secret_key):
        ctx = get_admin_context(request)
        ctx["settings"] = {**get_all_settings(), "turnstile_site_key": site_key}
        ctx["error"] = "开启人机验证前，必须同时配置 Site Key 和 Secret Key"
        return templates.TemplateResponse(request, "admin/settings.html", ctx)

    # 数值项服务端校验：浏览器端的 min/max 可被绕过，非法值会直接破坏限流逻辑
    numeric_error = _validate_numeric_settings(form)
    if numeric_error:
        ctx = get_admin_context(request)
        merged = get_all_settings()
        if domain_submitted:
            merged["admin_domain"] = raw_admin_domain
        for key, value in form.items():
            if isinstance(value, str):
                merged[key] = value
        for key in ("allow_author", "enable_captcha", "enable_dedup"):
            merged[key] = "1" if form.get(key) == "on" else "0"
        ctx["settings"] = merged
        ctx["error"] = numeric_error
        return templates.TemplateResponse(request, "admin/settings.html", ctx)
    numeric_values = _normalized_numeric_settings(form)

    setting_keys = [
        "site_name", "site_description", "footer_icp", "footer_copyright",
        "theme_accent_color", "upload_limit_per_ip", "upload_limit_per_ua",
        "upload_limit_daily_total", "upload_interval_seconds",
        "like_limit_per_minute", "top_window_days", "top_show_count",
        "watermark_text", "allow_author", "enable_captcha", "enable_dedup", "sensitive_words",
        "turnstile_site_key",
    ]

    checkbox_keys = ("allow_author", "enable_captcha", "enable_dedup")
    for key in setting_keys:
        # 复选框未勾选时浏览器不会提交该字段，必须显式写 0，不能按“未提交字段”跳过
        if key in checkbox_keys:
            set_setting(key, "1" if form.get(key) == "on" else "0")
            continue
        # 其余未提交的字段保持原值，避免部分提交（如仅修改站点名）清空其他设置
        if key not in form:
            continue
        val = form.get(key, "")
        if key in numeric_values:
            val = numeric_values[key]
        elif key == "watermark_text":
            val = str(val).strip()[:80]
        set_setting(key, val)

    # Turnstile 密钥：留空表示不修改（避免保存时误清空已配置的密钥）
    turnstile_secret = form.get("turnstile_secret_key", "").strip()
    if turnstile_secret:
        set_setting("turnstile_secret_key", turnstile_secret)

    # 管理域名特殊处理：数据库为唯一权威配置，保存后立即生效
    current_raw = get_setting("admin_domain", "") or ""
    old_domains, _ = normalize_admin_domains(current_raw)
    new_admin_domain = ",".join(new_domains)
    if current_raw != new_admin_domain:
        # 即使只是大小写/端口等归一化差异，也把数据库中的原始值统一为标准形式
        set_setting("admin_domain", new_admin_domain)
    if old_domains != new_domains:
        insert_admin_log(
            admin["id"], admin["username"], "config",
            f"管理域名: {', '.join(old_domains) or '(空)'} -> {new_admin_domain or '(空)'}"
        )
    # 标记已由数据库接管，后续启动不再用 .env 覆盖
    set_setting("admin_domain_initialized", "1")

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

    # 若当前访问的域名已不在新白名单内，则不再重定向（否则会 404），
    # 改为提示管理员使用新域名重新登录。
    current_host = get_request_host(request)
    if new_domains and current_host not in new_domains and not is_loopback_host(current_host):
        ctx = get_admin_context(request)
        ctx["new_domains"] = new_domains
        return templates.TemplateResponse(request, "admin/domain_changed.html", ctx)

    return RedirectResponse(url="/admin/settings?msg=settings_saved", status_code=302)


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
    if not validate_admin_csrf(request, csrf_token):
        return RedirectResponse(url="/admin/links", status_code=302)

    # URL 安全校验
    if not url.startswith("http://") and not url.startswith("https://"):
        return RedirectResponse(url="/admin/links?error=url", status_code=302)

    lid = _safe_int(link_id, 0) if link_id else 0
    sort_order_val = _safe_int(sort_order, 0)
    is_visible_val = 1 if is_visible == "1" else 0
    upsert_link(lid, name.strip(), url.strip(), sort_order_val, is_visible_val)
    insert_admin_log(admin["id"], admin["username"], "link", f"{'编辑' if lid else '新增'}友情链接: {name}")
    return RedirectResponse(url="/admin/links?msg=link_saved", status_code=302)


@router.post("/links/delete/{link_id}", response_class=HTMLResponse)
async def admin_links_delete(request: Request, link_id: int, csrf_token: str = Form("")):
    admin = admin_required(request)
    if not admin:
        return RedirectResponse(url="/admin/login", status_code=302)
    if not validate_admin_csrf(request, csrf_token):
        return RedirectResponse(url="/admin/links", status_code=302)

    delete_link(link_id)
    insert_admin_log(admin["id"], admin["username"], "link", f"删除友情链接 #{link_id}")
    return RedirectResponse(url="/admin/links?msg=link_deleted", status_code=302)


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
    if not validate_admin_csrf(request, csrf_token):
        return RedirectResponse(url="/admin/pages", status_code=302)

    pid = _safe_int(page_id, 0) if page_id else 0
    sort_order_val = _safe_int(sort_order, 0)
    is_visible_val = 1 if is_visible == "1" else 0
    clean_slug = slug.strip()
    clean_title = title.strip()

    error = ""
    if not clean_slug:
        error = "Slug 不能为空"
    elif len(clean_slug) > 64 or any(ch in clean_slug for ch in "/?#%"):
        error = "Slug 不能包含 / ? # % 字符，且长度不能超过 64"
    elif not clean_title:
        error = "标题不能为空"
    else:
        existing = get_page_by_slug_any(clean_slug)
        if existing and existing["id"] != pid:
            error = "Slug 已被其他页面使用"

    if error:
        ctx = get_admin_context(request)
        ctx["error"] = error
        ctx["page_obj"] = {
            "id": pid,
            "slug": clean_slug,
            "title": clean_title,
            "content": content,
            "sort_order": sort_order_val,
            "is_visible": is_visible_val,
        }
        return templates.TemplateResponse(request, "admin/page_edit.html", ctx)

    upsert_page(pid, clean_slug, clean_title, content, sort_order_val, is_visible_val)
    insert_admin_log(admin["id"], admin["username"], "page", f"{'编辑' if pid else '新增'}页面: {clean_title}")
    return RedirectResponse(url="/admin/pages?msg=page_saved", status_code=302)


@router.post("/pages/delete/{page_id}", response_class=HTMLResponse)
async def admin_pages_delete(request: Request, page_id: int, csrf_token: str = Form("")):
    admin = admin_required(request)
    if not admin:
        return RedirectResponse(url="/admin/login", status_code=302)
    if not validate_admin_csrf(request, csrf_token):
        return RedirectResponse(url="/admin/pages", status_code=302)

    delete_page(page_id)
    insert_admin_log(admin["id"], admin["username"], "page", f"删除页面 #{page_id}")
    return RedirectResponse(url="/admin/pages?msg=page_deleted", status_code=302)


# ========== 操作日志 ==========

@router.get("/logs", response_class=HTMLResponse)
async def admin_logs(
    request: Request,
    page: int = 1,
    action: str = "",
    ip: str = "",
    q: str = "",
    date_from: str = "",
    date_to: str = "",
):
    admin = admin_required(request)
    if not admin:
        return RedirectResponse(url="/admin/login", status_code=302)
    ctx = get_admin_context(request)
    page = max(1, page)
    offset = (page - 1) * 50

    df = f"{date_from}T00:00:00" if date_from else ""
    dt = f"{date_to}T23:59:59" if date_to else ""

    query = normalize_search_query(q) or None
    ctx["total"] = count_admin_logs(
        action=action or None,
        ip=ip.strip() or None,
        query=query,
        date_from=df or None,
        date_to=dt or None,
    )
    ctx["total_pages"] = max(1, (ctx["total"] + 50 - 1) // 50)
    page = min(page, ctx["total_pages"])
    offset = (page - 1) * 50
    ctx["logs"] = search_admin_logs(
        action=action or None,
        ip=ip.strip() or None,
        query=query,
        date_from=df or None,
        date_to=dt or None,
        limit=50, offset=offset,
    )
    ctx["page"] = page
    ctx["action"] = action
    ctx["ip"] = ip
    ctx["q"] = query or ""
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
    current_password: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
    csrf_token: str = Form(""),
):
    admin = admin_required(request)
    if not admin:
        return RedirectResponse(url="/admin/login", status_code=302)
    if not validate_admin_csrf(request, csrf_token):
        return RedirectResponse(url="/admin/change-password", status_code=302)

    from app.models import update_admin_password

    # 1) 必须验证当前密码，防止仅凭被盗 Cookie 直接改密
    if not current_password or not verify_password(current_password, admin["password_hash"]):
        ctx = get_admin_context(request, admin=admin)
        ctx["error"] = "当前密码不正确"
        return templates.TemplateResponse(request, "admin/change_password.html", ctx)

    # 2) 统一的服务端密码策略
    policy_error = validate_password(new_password)
    if policy_error:
        ctx = get_admin_context(request, admin=admin)
        ctx["error"] = policy_error
        return templates.TemplateResponse(request, "admin/change_password.html", ctx)

    if new_password != confirm_password:
        ctx = get_admin_context(request, admin=admin)
        ctx["error"] = "两次输入的新密码不一致"
        return templates.TemplateResponse(request, "admin/change_password.html", ctx)

    try:
        new_hash = hash_password(new_password)
    except ValueError:
        ctx = get_admin_context(request, admin=admin)
        ctx["error"] = "新密码不符合哈希算法要求"
        return templates.TemplateResponse(request, "admin/change_password.html", ctx)

    update_admin_password(admin["id"], new_hash)

    # 3) 撤销全部旧会话（含当前设备），并为当前设备签发新会话
    new_session = rotate_session(admin["id"], admin["username"])
    new_session_data = get_session(new_session)
    new_csrf = new_session_data["csrf_token"] if new_session_data else ""
    insert_admin_log(admin["id"], admin["username"], "change_password", "修改密码并撤销旧会话")

    ctx = get_admin_context(request, admin=admin, csrf_token=new_csrf)
    ctx["success"] = "密码修改成功，其他设备的登录状态已失效"
    response = templates.TemplateResponse(request, "admin/change_password.html", ctx)
    _set_admin_session_cookie(response, new_session, request)
    return response


# ========== 数据库切换（三次确认，保留数据） ==========

_db_switch_state = {
    "status": "idle", "percent": 0, "step": "", "logs": [], "error": "", "report": None,
}
_db_switch_guard = threading.RLock()


def _switch_snapshot():
    with _db_switch_guard:
        return {
            "status": _db_switch_state["status"],
            "percent": _db_switch_state["percent"],
            "step": _db_switch_state["step"],
            "logs": list(_db_switch_state["logs"]),
            "error": _db_switch_state["error"],
            "report": _db_switch_state["report"],
        }


def _switch_update(percent=None, step=None, status=None, error=None):
    with _db_switch_guard:
        if percent is not None:
            _db_switch_state["percent"] = int(percent)
        if step is not None:
            _db_switch_state["step"] = step
        if status is not None:
            _db_switch_state["status"] = status
        if error is not None:
            _db_switch_state["error"] = error


def _switch_log(message: str):
    stamp = datetime.now().strftime("%H:%M:%S")
    with _db_switch_guard:
        _db_switch_state["logs"].append(f"[{stamp}] {message}")
        if len(_db_switch_state["logs"]) > 300:
            del _db_switch_state["logs"][:100]


def _current_db_description() -> dict:
    if DB_TYPE == "sqlite":
        return {"type": "sqlite", "text": f"SQLite：{DATABASE_PATH}", "database_path": DATABASE_PATH}
    return {
        "type": "mysql",
        "text": f"MySQL：{MYSQL_USER}@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DATABASE}",
        "host": MYSQL_HOST, "port": MYSQL_PORT,
        "database": MYSQL_DATABASE, "user": MYSQL_USER,
    }


def _switch_worker(target: dict, admin: dict):
    from app import db_migration

    table_index = {"value": 0}

    def progress(stage: str, table, done):
        if stage == "backup":
            _switch_update(8, "备份当前数据库")
            _switch_log("正在备份当前数据库…")
        elif stage == "copy":
            table_index["value"] += 1
            percent = min(75, 15 + table_index["value"] * 4)
            _switch_update(percent, f"复制数据：{table}")
            _switch_log(f"{table} 已复制 {done} 行")
        elif stage == "verify":
            _switch_update(85, f"校验数据：{table}")
        elif stage == "env":
            _switch_update(95, "写入配置文件")

    try:
        report = db_migration.switch_database(
            target, env_path=str(BASE_DIR / ".env"), progress=progress
        )
        _switch_update(100, "切换完成（需重启生效）", status="success")
        _switch_log(f"切换完成：{report['total_rows']} 行数据已迁移，备份 {report['backup_path']}")
        insert_admin_log(
            admin["id"], admin["username"], "database_switch",
            f"切换数据库到 {report['target']}；备份 {report['backup_path']}",
        )
        with _db_switch_guard:
            _db_switch_state["report"] = report
    except Exception as exc:
        message = str(exc).strip() or exc.__class__.__name__
        _switch_update(status="failed", step="切换失败", error=message)
        _switch_log(f"切换失败：{message}")
        logger.exception("数据库切换失败")
    finally:
        db_migration.release_switch_lock()


def _switch_page_context(request, admin=None, error="", target=None):
    from app import db_migration
    ctx = get_admin_context(request, admin=admin)
    ctx["current_db"] = _current_db_description()
    ctx["switch"] = _switch_snapshot()
    ctx["switch_locked"] = db_migration.switch_in_progress()
    ctx["target"] = target or {}
    if error:
        ctx["error"] = error
    return ctx


@router.get("/settings/database", response_class=HTMLResponse)
async def admin_database_page(request: Request):
    admin = admin_required(request)
    if not admin:
        return RedirectResponse(url="/admin/login", status_code=302)
    return templates.TemplateResponse(
        request, "admin/database.html", _switch_page_context(request, admin=admin)
    )


@router.get("/settings/database/progress")
async def admin_database_progress(request: Request):
    admin = admin_required(request)
    if not admin:
        return RedirectResponse(url="/admin/login", status_code=302)
    from app import db_migration
    snapshot = _switch_snapshot()
    snapshot["locked"] = db_migration.switch_in_progress()
    return JSONResponse(snapshot)


@router.post("/settings/database/switch", response_class=HTMLResponse)
async def admin_database_switch(request: Request):
    admin = admin_required(request)
    if not admin:
        return RedirectResponse(url="/admin/login", status_code=302)
    form = await request.form()
    csrf_token = form.get("csrf_token", "")
    if not validate_admin_csrf(request, csrf_token):
        return RedirectResponse(url="/admin/settings/database", status_code=302)

    from app import db_migration

    target_raw = {
        "db_type": form.get("target_type", "sqlite"),
        "database_path": form.get("sqlite_path", ""),
        "host": form.get("mysql_host", ""),
        "port": form.get("mysql_port", ""),
        "database": form.get("mysql_database", ""),
        "user": form.get("mysql_user", ""),
        "password": form.get("mysql_password", ""),
    }
    shown_target = {k: v for k, v in target_raw.items() if k != "password"}

    # 三次确认：两个勾选 + 输入确认词
    if form.get("confirm_read") != "on":
        return templates.TemplateResponse(
            request, "admin/database.html",
            _switch_page_context(request, admin=admin,
                                 error="请先勾选「我已知晓切换期间需要停机维护」",
                                 target=shown_target),
        )
    if form.get("confirm_backup") != "on":
        return templates.TemplateResponse(
            request, "admin/database.html",
            _switch_page_context(request, admin=admin,
                                 error="请勾选「我确认理解系统会自动备份当前数据库」",
                                 target=shown_target),
        )
    if str(form.get("confirm_word", "")).strip() != "切换":
        return templates.TemplateResponse(
            request, "admin/database.html",
            _switch_page_context(request, admin=admin,
                                 error="请输入确认词「切换」以完成第三次确认",
                                 target=shown_target),
        )

    try:
        target = db_migration.normalize_target(target_raw)
        current = db_migration.current_source()
        if (target["db_type"] == current["db_type"] and
                (target.get("database_path") == current.get("database_path")) and
                (target.get("host") == current.get("host")) and
                (target.get("database") == current.get("database"))):
            raise db_migration.MigrationError("目标数据库与当前数据库相同，无需切换")
    except db_migration.MigrationError as exc:
        return templates.TemplateResponse(
            request, "admin/database.html",
            _switch_page_context(request, admin=admin, error=str(exc), target=shown_target),
        )

    if not db_migration.acquire_switch_lock():
        return templates.TemplateResponse(
            request, "admin/database.html",
            _switch_page_context(request, admin=admin,
                                 error="已有数据库切换任务正在执行，请等待其完成"),
        )

    with _db_switch_guard:
        _db_switch_state.update({
            "status": "running", "percent": 2, "step": "准备切换",
            "logs": [], "error": "", "report": None,
        })
    _switch_log(f"目标数据库：{db_migration.describe(target)}")
    threading.Thread(
        target=_switch_worker,
        args=(target, {"id": admin["id"], "username": admin["username"]}),
        daemon=True,
    ).start()
    return RedirectResponse(url="/admin/settings/database", status_code=302)