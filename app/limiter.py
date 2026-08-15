"""限流器"""
import time
from collections import defaultdict
from app.models import (
    count_uploads_today_by_ip, count_uploads_today_by_ua,
    count_uploads_today_total, get_last_upload_time,
    count_likes_by_ip_last_minute, get_cooldown_multiplier,
    get_setting_int
)
from app.config import (
    DEFAULT_UPLOAD_PER_IP, DEFAULT_UPLOAD_PER_UA,
    DEFAULT_UPLOAD_DAILY_TOTAL, DEFAULT_UPLOAD_INTERVAL,
    DEFAULT_LIKE_PER_MINUTE
)


def check_upload_limit(ip: str, ua_hash_val: str) -> tuple:
    """
    检查上传限制，返回 (allowed, error_message)
    """
    per_ip = get_setting_int("upload_limit_per_ip", DEFAULT_UPLOAD_PER_IP)
    per_ua = get_setting_int("upload_limit_per_ua", DEFAULT_UPLOAD_PER_UA)
    daily_total = get_setting_int("upload_limit_daily_total", DEFAULT_UPLOAD_DAILY_TOTAL)
    interval = get_setting_int("upload_interval_seconds", DEFAULT_UPLOAD_INTERVAL)

    # 全站上限
    total = count_uploads_today_total()
    if total >= daily_total:
        return (False, "今日上传名额已满，请明日再来")

    # IP 上限
    ip_count = count_uploads_today_by_ip(ip)
    if ip_count >= per_ip:
        remaining = max(0, per_ip - ip_count)
        return (False, f"今日上传次数已达上限（{per_ip}次），剩余 {remaining} 次")

    # UA 上限
    if per_ua > 0:
        ua_count = count_uploads_today_by_ua(ua_hash_val)
        if ua_count >= per_ua:
            return (False, f"今日上传次数已达上限")

    # 冷却倍数
    mult = get_cooldown_multiplier(ip)
    effective_interval = interval * mult

    # 上传间隔
    last_time = get_last_upload_time(ip)
    if last_time:
        from datetime import datetime, timezone
        from app.models import parse_dt
        last_dt = parse_dt(last_time)
        elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
        if elapsed < effective_interval:
            remaining = int(effective_interval - elapsed)
            if mult > 1:
                return (False, f"操作过于频繁，请 {remaining} 秒后再试（冷却中）")
            return (False, f"操作过于频繁，请 {remaining} 秒后再试")

    return (True, "")


def check_like_limit(ip: str) -> tuple:
    """检查点赞限制"""
    limit = get_setting_int("like_limit_per_minute", DEFAULT_LIKE_PER_MINUTE)
    count = count_likes_by_ip_last_minute(ip)
    if count >= limit:
        return (False, "点赞过于频繁，请稍后再试")
    return (True, "")