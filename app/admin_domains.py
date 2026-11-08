"""管理域名统一配置。

运行期以数据库 ``settings.admin_domain`` 为唯一权威来源；
``.env`` / systemd 的 ``ADMIN_DOMAIN`` 只用于首次初始化（或旧库迁移）时的默认值，
不会在每次启动时覆盖后台保存的配置。
"""
import ipaddress
import re

from app.config import ADMIN_DOMAIN

# 主机名：允许字母、数字、连字符，单段最长 63，总长 253；不允许通配符
_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?<!-)(?:\.(?!-)[a-z0-9-]{1,63}(?<!-))*$"
)
_IPV4_LIKE_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_SPLIT_RE = re.compile(r"[,;\r\n]+")


def _normalize_single_domain(token):
    """把单个域名归一化为小写主机名；非法输入返回 None。

    处理：协议前缀、末尾斜杠、端口、首尾空白、大小写、末尾点、IDN、通配符与路径。
    """
    token = token.strip().lower()
    if not token:
        return None

    for prefix in ("https://", "http://"):
        if token.startswith(prefix):
            token = token[len(prefix):]
            break

    token = token.rstrip("/")
    if not token:
        return None

    # 明确拒绝路径、查询串、用户信息、空白和通配符
    if any(ch in token for ch in "/?#@ \t*"):
        return None

    # IPv6 字面量：[::1] 或 [::1]:8000
    if token.startswith("["):
        end = token.find("]")
        if end < 0:
            return None
        host = token[1:end]
        rest = token[end + 1:]
        if rest and not (rest.startswith(":") and rest[1:].isdigit()):
            return None
        if rest and not 0 < int(rest[1:]) < 65536:
            return None
        try:
            return str(ipaddress.IPv6Address(host)).lower()
        except ValueError:
            return None

    # 普通域名或 IPv4 带端口
    if ":" in token:
        host, _, port = token.rpartition(":")
        if not host or not port.isdigit() or not 0 < int(port) < 65536:
            return None
        token = host

    token = token.rstrip(".")
    if not token:
        return None

    try:
        ascii_host = token.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        return None

    try:
        return str(ipaddress.ip_address(ascii_host)).lower()
    except ValueError:
        pass

    # 形如 IPv4 但无法解析的输入（如 999.1.1.1）不能当作域名放行
    if _IPV4_LIKE_RE.match(ascii_host):
        return None

    if not _DOMAIN_RE.match(ascii_host):
        return None
    return ascii_host


def normalize_admin_domains(raw):
    """解析管理域名配置，返回 (合法域名列表, 非法输入列表)。

    支持逗号、分号、换行分隔（兼容旧的逗号分隔配置）；自动去重并保持顺序。
    """
    domains = []
    invalid = []
    if raw is None:
        return domains, invalid

    for token in _SPLIT_RE.split(str(raw)):
        token = token.strip()
        if not token:
            continue
        normalized = _normalize_single_domain(token)
        if normalized is None:
            invalid.append(token)
        elif normalized not in domains:
            domains.append(normalized)
    return domains, invalid


def normalize_host_header(value):
    """从 Host 请求头中提取小写主机名，忽略端口、方括号和末尾点。"""
    if not value:
        return ""
    host = str(value).strip().lower()
    if not host:
        return ""
    if host.startswith("["):
        end = host.find("]")
        if end < 0:
            return ""
        return host[1:end]
    if ":" in host:
        host = host.rsplit(":", 1)[0]
    return host.rstrip(".")


def get_admin_domains():
    """读取数据库中的管理域名白名单（运行期唯一权威配置）。"""
    from app.models import get_setting

    raw = get_setting("admin_domain", "") or ""
    domains, _ = normalize_admin_domains(raw)
    return set(domains)


def get_request_host(request):
    return normalize_host_header(request.headers.get("host", ""))


def is_loopback_host(host):
    """精确判定回环地址，避免 `127.` 前缀误放行（如 127.evil.com）。"""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_loopback_client(request):
    client = request.client
    return bool(client) and is_loopback_host(client.host)


def is_admin_access(request):
    """判断当前请求是否有权访问 /admin。

    仅信任 Host 头（Nginx 必须使用 ``proxy_set_header Host $host``），
    不信任 ``X-Forwarded-Host``，避免任意 Host/转发头成为可信管理域名。
    """
    host = get_request_host(request)
    if not host:
        return False
    if is_loopback_host(host):
        # 回环 Host 只对本机客户端放行，防止伪造 `Host: localhost` 绕过限制
        return is_loopback_client(request)
    domains = get_admin_domains()
    return bool(domains) and host in domains


def ensure_admin_domain_initialized():
    """首次运行或旧库迁移时，用 .env 的 ADMIN_DOMAIN 初始化数据库配置。

    通过 ``admin_domain_initialized`` 标记保证只执行一次：
    之后即使 .env 保留旧域名，也不会覆盖后台保存的新配置。
    """
    from app.models import get_setting, set_setting

    if get_setting("admin_domain_initialized") == "1":
        return

    current, _ = normalize_admin_domains(get_setting("admin_domain", "") or "")
    if current:
        normalized = ",".join(current)
        if (get_setting("admin_domain", "") or "") != normalized:
            set_setting("admin_domain", normalized)
    else:
        env_domains, _ = normalize_admin_domains(ADMIN_DOMAIN)
        if env_domains:
            set_setting("admin_domain", ",".join(env_domains))
    set_setting("admin_domain_initialized", "1")
