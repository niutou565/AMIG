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
        # 使用项目内置的纯 Python 实现（app/xdbSearcher.py），无需外部依赖
        from app.xdbSearcher import loadVectorIndexFromFile
        vi = loadVectorIndexFromFile(IP2REGION_PATH)
        from app.xdbSearcher import XdbSearcher
        _searcher = XdbSearcher(dbfile=IP2REGION_PATH, vectorIndex=vi)
        print(f"[INFO] ip2region loaded from {IP2REGION_PATH}")
    except ImportError as e:
        print(f"[WARN] xdbSearcher module not available: {e}, IP geo disabled")
    except Exception as e:
        print(f"[WARN] Failed to load ip2region: {e}, IP geo disabled")


def get_ip_location(ip: str):
    """返回 (province, city)"""
    if _searcher is None:
        return ("", "")
    try:
        result = _searcher.search(ip)
        if not result:
            return ("", "")
        parts = result.split("|")
        if len(parts) >= 4:
            # 新版 v4.xdb 格式: 国家 | 省份 | 城市 | ISP | 国家码
            # 兼容旧 v2.xdb 格式: 国家 | 区域 | 省份 | 城市 | ISP
            country = parts[0].strip()
            if len(parts) >= 5 and (parts[4] in ("0", "") or len(parts[4]) == 2):
                # v4 格式: 5段,末位是 CN/US/AU 等双字母 ISO code 或 0
                province_raw = parts[1].strip()
                city_raw = parts[2].strip()
            else:
                # v2 格式: 5段格式 国家|区域|省份|城市|ISP
                province_raw = parts[2].strip() if len(parts) > 2 else ""
                city_raw = parts[3].strip() if len(parts) > 3 else ""

            # 跳过"0"占位符
            province = "" if province_raw in ("0", "") else province_raw
            city = "" if city_raw in ("0", "") else city_raw

            # 内网 / 保留地址 / IANA 特殊用途 → 不展示位置
            if country in ("Reserved", "内网IP", "loopback", "IANA") or country.startswith("0"):
                return ("", "")
            if province in ("Reserved", "内网IP") or city in ("Reserved", "内网IP"):
                return ("", "")

            # 只对中国地址展示 省/市；海外 IP 一般精确不到中国意义上的「省市」
            # 而且省名字段是英文州名（Queensland/California），混在中文里不美观，
            # 所以只在 country 是中国时返回真实省份 + 城市。
            if country not in ("中国", "China", "CN"):
                return ("", "")

            # 直辖市去重：北京市北京市 / 上海市上海市 / 天津市天津市 / 重庆市重庆市
            direct_cities = ("北京市", "上海市", "天津市", "重庆市")
            if province in direct_cities and city == province:
                city = ""
            # 兼容：province 是省份全称但 city 写成直辖市名的情况
            if province == city and province.endswith("市"):
                city = ""

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