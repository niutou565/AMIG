"""IP 归属地查询，基于 ip2region xdb（支持 IPv4 + IPv6）"""
import os
import ipaddress
from app.config import IP2REGION_PATH, IP2REGION_V6_PATH, TRUSTED_PROXIES

_v4_searcher = None
_v6_searcher = None


def _load_searcher(path: str):
    """加载指定 xdb 文件，返回 XdbSearcher 或 None"""
    if not path or not os.path.exists(path):
        print(f"[WARN] ip2region db not found at {path}, skipped")
        return None
    try:
        from app.xdbSearcher import (
            XdbSearcher,
            load_header_from_file,
            load_vector_index_from_file,
            version_from_header,
        )
        header = load_header_from_file(path)
        version = version_from_header(header)
        if version is None:
            print(f"[WARN] unknown ip version in {path}, skipped")
            return None
        vi = load_vector_index_from_file(path)
        searcher = XdbSearcher(version, dbfile=path, vector_index=vi)
        print(f"[INFO] ip2region {version.name} loaded from {path}")
        return searcher
    except ImportError as e:
        print(f"[WARN] xdbSearcher module not available: {e}")
        return None
    except Exception as e:
        print(f"[WARN] Failed to load ip2region {path}: {e}")
        return None


def init_ip2region():
    global _v4_searcher, _v6_searcher
    _v4_searcher = _load_searcher(IP2REGION_PATH)
    _v6_searcher = _load_searcher(IP2REGION_V6_PATH)


def _is_ipv6(ip: str) -> bool:
    """判断 IP 是否为 IPv6 地址"""
    try:
        return ipaddress.ip_address(ip).version == 6
    except ValueError:
        return False


def _lookup(ip: str):
    """查询 IP，返回 (country, province, city)。无法确定或内网地址时 country 为空串。"""
    searcher = _v6_searcher if _is_ipv6(ip) else _v4_searcher
    if searcher is None:
        return ("", "", "")
    try:
        result = searcher.search(ip)
        if not result:
            return ("", "", "")
        parts = result.split("|")
        if len(parts) < 4:
            return ("", "", "")

        # 新版 v4.xdb 格式: 国家 | 省份 | 城市 | ISP | 国家码
        # 兼容旧 v2.xdb 格式: 国家 | 区域 | 省份 | 城市 | ISP
        country = parts[0].strip()
        if len(parts) >= 5 and (parts[4] in ("0", "") or len(parts[4]) == 2):
            province_raw = parts[1].strip()
            city_raw = parts[2].strip()
        else:
            province_raw = parts[2].strip() if len(parts) > 2 else ""
            city_raw = parts[3].strip() if len(parts) > 3 else ""

        # 跳过"0"占位符
        province = "" if province_raw in ("0", "") else province_raw
        city = "" if city_raw in ("0", "") else city_raw

        # 内网 / 保留地址 / IANA 特殊用途 → 视为未知地区
        if country in ("Reserved", "内网IP", "loopback", "IANA") or country.startswith("0"):
            return ("", "", "")
        if province in ("Reserved", "内网IP") or city in ("Reserved", "内网IP"):
            return ("", "", "")

        return (country, province, city)
    except Exception:
        return ("", "", "")


def get_ip_location(ip: str):
    """返回 (province, city)，仅中国大陆地址返回省/市，其余返回空串。"""
    country, province, city = _lookup(ip)
    if country not in ("中国", "China", "CN"):
        return ("", "")
    # 直辖市去重：北京市北京市 / 上海市上海市 / 天津市天津市 / 重庆市重庆市
    direct_cities = ("北京市", "上海市", "天津市", "重庆市")
    if province in direct_cities and city == province:
        city = ""
    if province == city and province.endswith("市"):
        city = ""
    return (province, city)


def get_ip_region(ip: str):
    """返回地区分类：cn（中国大陆）/ overseas（海外）/ ""（未知）。"""
    country = _lookup(ip)[0]
    if country in ("中国", "China", "CN"):
        return "cn"
    if country:
        return "overseas"
    return ""


def get_ip_geo_label(ip: str):
    """返回管理员日志用的地理位置文案。

    国内：省/市（城市级，如 "广西柳州" / "北京市"）
    海外：海外IP + 国家（如 "海外IP 美国"）
    未知：未知地区
    """
    country, province, city = _lookup(ip)
    if country in ("中国", "China", "CN"):
        direct_cities = ("北京市", "上海市", "天津市", "重庆市")
        if province in direct_cities and city == province:
            city = ""
        if province == city and province.endswith("市"):
            city = ""
        if province or city:
            return f"{province}{city}"
        return "中国大陆"
    if country:
        return f"海外IP {country}"
    return "未知地区"


def get_client_ip(request):
    """获取客户端真实 IP。

    安全策略：仅在请求直接来自可信反向代理（TRUSTED_PROXIES）时，才信任
    X-Real-IP（由 Nginx 用 $remote_addr 覆盖注入）。绝不无条件信任
    CF-Connecting-IP / X-Forwarded-For 等客户端可伪造的请求头，避免绕过限流/封禁。
    """
    client_host = request.client.host if request.client else "127.0.0.1"

    # 直连或来自非可信来源：直接返回对端地址
    if client_host not in TRUSTED_PROXIES:
        return client_host

    # 来自可信代理：信任其注入的 X-Real-IP
    real_ip = request.headers.get("X-Real-IP")
    if real_ip and real_ip.strip():
        return real_ip.strip()

    return client_host