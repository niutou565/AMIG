"""IP 归属地查询，基于 ip-api.com。"""
import ipaddress
import json
import threading
import time
import urllib.parse
import urllib.request

from app.config import IP_API_CACHE_TTL, IP_API_TIMEOUT, TRUSTED_PROXIES

_IP_API_URL = "http://ip-api.com/json/{}"
_cache = {}
_cache_lock = threading.Lock()


def _is_public_ip(ip: str) -> bool:
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return address.is_global


def _lookup(ip: str):
    """查询 IP，返回 (country, province, city)。失败或非公网地址返回空值。"""
    if not _is_public_ip(ip):
        return ("", "", "")

    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(ip)
        if cached and now - cached[0] < IP_API_CACHE_TTL:
            return cached[1]
        if cached:
            del _cache[ip]

    try:
        encoded_ip = urllib.parse.quote(ip, safe="")
        url = f"{_IP_API_URL.format(encoded_ip)}?fields=status,country,regionName,city"
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "AMIG-IP-Geo/1.0"},
        )
        with urllib.request.urlopen(request, timeout=IP_API_TIMEOUT) as response:
            result = response.read().decode("utf-8")
        data = json.loads(result)
        if data.get("status") != "success":
            location = ("", "", "")
        else:
            location = (
                str(data.get("country") or "").strip(),
                str(data.get("regionName") or "").strip(),
                str(data.get("city") or "").strip(),
            )
    except Exception:
        location = ("", "", "")

    with _cache_lock:
        _cache[ip] = (time.monotonic(), location)
    return location


def get_ip_location(ip: str):
    """返回 (province, city)，仅中国大陆地址返回省/市，其余返回空串。"""
    country, province, city = _lookup(ip)
    if country not in ("中国", "China", "CN"):
        return ("", "")
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


def _is_trusted_proxy(ip: str) -> bool:
    """检查地址是否为配置的可信代理，支持单个 IP、主机名和 CIDR。"""
    if ip in TRUSTED_PROXIES:
        return True
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for proxy in TRUSTED_PROXIES:
        try:
            if address in ipaddress.ip_network(proxy, strict=False):
                return True
        except ValueError:
            continue
    return False


def get_client_ip(request):
    """仅在请求来自可信反向代理时信任格式正确的 X-Real-IP。"""
    client_host = request.client.host if request.client else "127.0.0.1"
    if not _is_trusted_proxy(client_host):
        return client_host
    real_ip = request.headers.get("X-Real-IP", "").strip()
    try:
        return str(ipaddress.ip_address(real_ip))
    except ValueError:
        return client_host
