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
        import xdbSearcher
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
    """获取客户端真实 IP"""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    if request.client:
        return request.client.host
    return "127.0.0.1"