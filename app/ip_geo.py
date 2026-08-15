"""IP 归属地查询，基于 ip2region xdb"""
import os
from app.config import IP2REGION_PATH

_searcher = None


def init_ip2region():
    global _searcher
    if not os.path.exists(IP2REGION_PATH):
        print(f"[WARN] ip2region.xdb not found at {IP2REGION_PATH}, IP geo disabled")
        return
    try:
        from ip2region import xdbSearcher
        _searcher = xdbSearcher.loadVectorIndexFromFile(IP2REGION_PATH)
        print(f"[INFO] ip2region loaded from {IP2REGION_PATH}")
    except ImportError:
        print("[WARN] xdbSearcher module not available, IP geo disabled")
    except Exception as e:
        print(f"[WARN] Failed to load ip2region: {e}")


def get_ip_location(ip: str):
    """返回 (province, city)"""
    if _searcher is None:
        return ("", "")
    try:
        result = _searcher.search(ip)
        if not result:
            return ("", "")
        parts = result.split("|")
        if len(parts) >= 5:
            country = parts[0]
            province = parts[2] if parts[2] != "0" else ""
            city = parts[3] if parts[3] != "0" else ""
            # 处理内网/本地 IP
            if country == "0" and province == "":
                return ("", "")
            # 特殊处理：直辖市
            if province and province.endswith("市") and not city:
                city = province
            return (province, city)
        return ("", "")
    except Exception:
        return ("", "")


def get_client_ip(request):
    """获取客户端真实 IP

    优先级：CF-Connecting-IP（Cloudflare）> X-Real-IP（Nginx 设置的 $remote_addr）
    > X-Forwarded-For 末位（由最近可信代理追加）> 直连地址。
    不信任 X-Forwarded-For 的首个值，避免客户端伪造 IP 绕过限流/封禁。
    """
    cf_ip = request.headers.get("CF-Connecting-IP")
    if cf_ip:
        return cf_ip.strip()
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    if request.client:
        return request.client.host
    return "127.0.0.1"