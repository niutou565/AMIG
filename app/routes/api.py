"""API 路由（点赞等）"""
from urllib.parse import urlparse
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from app.models import (
    insert_like, has_liked, get_approved_image_by_id,
    increment_like_count, increment_daily_stat, today_str,
    ua_hash, get_random_image_prefer_low_heat,
)
from app.ip_geo import get_client_ip, get_ip_location
from app.limiter import check_like_limit
from app.ban import check_ban_status

router = APIRouter(prefix="/api")


def _origin_is_valid(request: Request) -> bool:
    """校验 Origin/Referer 与 Host 一致，防止跨站请求伪造（CSRF）。

    浏览器发起的跨站请求会携带 Origin（或 Referer），据此判断来源是否同源；
    无 Origin/Referer 的非浏览器客户端（如 curl）放行。
    """
    host = request.headers.get("host", "")
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")

    def netloc(url: str) -> str:
        try:
            return urlparse(url).netloc
        except Exception:
            return ""

    if origin:
        return netloc(origin) == host
    if referer:
        return netloc(referer) == host
    return True


@router.post("/like/{image_id}")
async def like_image(request: Request, image_id: int):
    """点赞接口"""
    image = get_approved_image_by_id(image_id)
    if not image:
        return JSONResponse({"error": "图片不存在"}, status_code=404)

    # 防跨站请求伪造：校验请求来源
    if not _origin_is_valid(request):
        return JSONResponse({"error": "请求来源非法"}, status_code=403)

    ip = get_client_ip(request)
    ua_val = request.headers.get("user-agent", "")
    ua_hash_val = ua_hash(ua_val)

    # 封禁检查
    banned, ban_msg = check_ban_status(ip, ua_hash_val)
    if banned:
        return JSONResponse({"error": ban_msg}, status_code=403)

    # 限流检查
    allowed, limit_msg = check_like_limit(ip)
    if not allowed:
        return JSONResponse({"error": limit_msg}, status_code=429)

    # 已赞检查
    if has_liked(image_id, ip, ua_hash_val):
        return JSONResponse({"error": "您已经赞过这张图片了"}, status_code=400)

    # 解析 IP 归属地
    province, city = await run_in_threadpool(get_ip_location, ip)

    # 插入点赞
    success = insert_like(image_id, ip, ua_hash_val, province, city)
    if not success:
        return JSONResponse({"error": "您已经赞过这张图片了"}, status_code=400)

    # 更新计数
    increment_like_count(image_id)
    increment_daily_stat(today_str(), "like_count")

    # 获取最新点赞数
    from app.models import count_likes_by_image
    new_count = count_likes_by_image(image_id)

    return JSONResponse({
        "success": True,
        "like_count": new_count,
        "message": "点赞成功"
    })


@router.get("/random-image")
async def random_image(request: Request, exclude: int = 0):
    """随机返回一张图片，优先展示热度较低的图片"""
    exclude_id = exclude if exclude else None
    image = get_random_image_prefer_low_heat(exclude_id)
    if not image:
        return JSONResponse({"error": "暂无更多图片"}, status_code=404)
    return JSONResponse({"id": image["id"]})