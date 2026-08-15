"""统计数据与热度榜缓存"""
import time
from app.models import (
    get_top_images, get_setting_int, get_setting, get_daily_stats,
    get_stats_range, get_top_like_provinces, get_top_images_by_likes,
    get_active_ip_count, get_ban_count, count_images_by_status,
    count_uploads_today_total, increment_daily_stat, ensure_daily_stats,
    today_str
)
from app.config import DEFAULT_TOP_WINDOW_DAYS, DEFAULT_TOP_SHOW_COUNT, TOP_CACHE_TTL

# 热度榜缓存 {cache_key: (data, timestamp)}
_top_cache = {}


def get_top_cached(limit=None):
    """获取缓存的热度榜首页数据"""
    window_days = get_setting_int("top_window_days", DEFAULT_TOP_WINDOW_DAYS)
    show_count = get_setting_int("top_show_count", DEFAULT_TOP_SHOW_COUNT)
    cache_key = f"top_{window_days}_{show_count}"

    now = time.time()
    cached = _top_cache.get(cache_key)
    if cached and (now - cached[1]) < TOP_CACHE_TTL:
        return cached[0]

    data = get_top_images(window_days, show_count, limit=limit or show_count)
    _top_cache[cache_key] = (data, now)
    return data


def invalidate_top_cache():
    """使热度榜缓存失效"""
    _top_cache.clear()


def get_dashboard_data():
    """获取仪表盘数据"""
    today = today_str()
    ensure_daily_stats(today)
    today_stats = get_daily_stats(today)

    pending = count_images_by_status("pending")

    today_row = today_stats if today_stats else {
        "upload_count": 0, "approved_count": 0, "rejected_count": 0,
        "like_count": 0, "view_count": 0
    }

    total_processed = today_row["approved_count"] + today_row["rejected_count"]
    pass_rate = round(today_row["approved_count"] / total_processed * 100, 1) if total_processed > 0 else 0

    # 7天和30天趋势
    stats_7d = get_stats_range(7)
    stats_30d = get_stats_range(30)

    # 点赞地区分布
    province_top = get_top_like_provinces(10)

    # 热门图片
    window_days = get_setting_int("top_window_days", DEFAULT_TOP_WINDOW_DAYS)
    hot_window = get_top_images_by_likes(10, window_days)
    hot_all = get_top_images_by_likes(10)

    # 活跃IP和封禁数
    active_ips = get_active_ip_count(7)
    ban_count = get_ban_count()

    return {
        "today": today_row,
        "pending": pending,
        "pass_rate": pass_rate,
        "stats_7d": stats_7d,
        "stats_30d": stats_30d,
        "province_top": province_top,
        "hot_window": hot_window,
        "hot_all": hot_all,
        "active_ips": active_ips,
        "ban_count": ban_count,
    }