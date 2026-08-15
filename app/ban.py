"""封禁检查"""
from app.models import is_banned as check_ban


def check_ban_status(ip: str, ua_hash_val: str) -> tuple:
    """返回 (is_banned, message)"""
    if check_ban(ip, ua_hash_val):
        return (True, "您已被限制使用此功能")
    return (False, "")