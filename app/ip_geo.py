"""IP 归属地查询。

第二阶段安全调整：
- 主数据源改为 HTTPS（ip.zxinc.org，返回中文省/市，支持 IPv4/IPv6）；
- 主数据源失败时回退到 HTTPS 的 ipapi.co（英文；仅用于国家/地区分类，
  中国大陆的省/市在回退时不再写入英文值，避免污染展示）；
- 网络失败后进入短暂退避，避免上游故障拖慢上传/点赞；
- 只缓存归一化后的地区元组，不保存原始响应，也不记录完整用户 IP 日志。
"""
import ipaddress
import json
import re
import threading
import time
import urllib.parse
import urllib.request

from app.config import IP_API_CACHE_TTL, IP_API_TIMEOUT, TRUSTED_PROXIES

_PRIMARY_API_URL = "https://ip.zxinc.org/api.php?type=json&ip={}"
_FALLBACK_API_URL = "https://ipapi.co/{}/json/"
_cache = {}
_cache_lock = threading.Lock()
# 缓存条目上限：避免长期运行后公网 IP 记录无限增长占用内存
_CACHE_MAX_SIZE = 5000
# 上游连续失败后的退避时间（秒）：期间直接返回空值，不阻塞上传流程
_FAILURE_BACKOFF_SECONDS = 60
_next_retry_at = 0.0


def _is_public_ip(ip: str) -> bool:
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return address.is_global


_DIRECT_CITIES = {"北京", "上海", "天津", "重庆"}
_PROVINCE_SUFFIXES = (
    "壮族自治区", "回族自治区", "维吾尔自治区", "特别行政区", "自治区", "省", "区",
)


def _normalize_province(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    for suffix in _PROVINCE_SUFFIXES:
        if value.endswith(suffix) and len(value) > len(suffix):
            value = value[:-len(suffix)]
            break
    if value in _DIRECT_CITIES:
        value += "市"
    return value


def _split_primary_location(value: str):
    """解析主数据源的中文地区字符串，如“中国–广西–柳州”或“中国\\t广西区\\t\\t柳州市”。"""
    parts = [part.strip() for part in re.split(r"[–—\-\t]+|\s{2,}", value or "") if part.strip()]
    if not parts:
        return ("", "", "")
    country = parts[0]
    province = _normalize_province(parts[1]) if len(parts) > 1 else ""
    city = parts[2].strip() if len(parts) > 2 else ""
    return (country, province, city)


def _fetch_json(url: str):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "AMIG-IP-Geo/1.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=IP_API_TIMEOUT) as response:
        raw = response.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def _lookup_primary(ip: str):
    data = _fetch_json(_PRIMARY_API_URL.format(urllib.parse.quote(ip, safe="")))
    if not isinstance(data, dict) or data.get("code") != 0:
        return None
    payload = data.get("data") or {}
    value = payload.get("country") or payload.get("location") or ""
    return _split_primary_location(str(value))


def _lookup_fallback(ip: str):
    data = _fetch_json(_FALLBACK_API_URL.format(urllib.parse.quote(ip, safe="")))
    if not isinstance(data, dict) or data.get("error"):
        return None
    country = str(data.get("country_name") or data.get("country") or "").strip()
    # 回退源只提供英文地区名；仅保留国家用于 cn/overseas 分类，避免写入英文省市
    return (country, "", "")


def _lookup(ip: str):
    """查询 IP，返回 (country, province, city)。失败或非公网地址返回空值。"""
    global _next_retry_at
    if not _is_public_ip(ip):
        return ("", "", "")

    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(ip)
        if cached and now - cached[0] < IP_API_CACHE_TTL:
            return cached[1]
        if cached:
            del _cache[ip]
        if _next_retry_at and now < _next_retry_at:
            return ("", "", "")

    location = ("", "", "")
    try:
        primary = _lookup_primary(ip)
        if primary is not None:
            location = primary
        else:
            fallback = _lookup_fallback(ip)
            if fallback is not None:
                location = fallback
    except Exception:
        # 主源异常时仍尝试一次回退源；回退也失败则进入退避
        try:
            fallback = _lookup_fallback(ip)
            if fallback is not None:
                location = fallback
            else:
                raise RuntimeError("geo lookup failed")
        except Exception:
            with _cache_lock:
                _next_retry_at = time.monotonic() + _FAILURE_BACKOFF_SECONDS

    with _cache_lock:
        if len(_cache) >= _CACHE_MAX_SIZE:
            expired = [key for key, entry in _cache.items() if now - entry[0] >= IP_API_CACHE_TTL]
            for key in expired:
                _cache.pop(key, None)
            # 仍超限时按插入顺序淘汰最旧条目（dict 在 Python 3.7+ 保持插入顺序）
            while len(_cache) >= _CACHE_MAX_SIZE:
                _cache.pop(next(iter(_cache)), None)
        _cache[ip] = (time.monotonic(), location)
    return location


def _reset_failure_backoff():
    """清除失败退避与缓存（测试/运维诊断用）。"""
    global _next_retry_at
    with _cache_lock:
        _next_retry_at = 0.0
        _cache.clear()


def get_ip_location(ip: str):
    """返回 (province, city)，仅中国大陆地址返回省/市，其余返回空串。"""
    country, province, city = _lookup(ip)
    if country not in ("中国", "China", "CN"):
        return ("", "")
    if province in _DIRECT_CITIES or province in {name + "市" for name in _DIRECT_CITIES}:
        if city in _DIRECT_CITIES or city == province:
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
    """检查地址是否为配置的可信代理，支持单个 IP 和 CIDR。"""
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
    """仅在请求来自可信反向代理时信任格式正确的 X-Real-IP。

    无法取得客户端地址时返回空字符串并拒绝信任任何转发头，避免把未知
    来源误当成回环代理，从而接受伪造的 X-Real-IP。
    """
    client_host = request.client.host if request.client else ""
    if not client_host:
        return ""
    if not _is_trusted_proxy(client_host):
        return client_host
    real_ip = request.headers.get("X-Real-IP", "").strip()
    try:
        return str(ipaddress.ip_address(real_ip))
    except ValueError:
        return client_host
