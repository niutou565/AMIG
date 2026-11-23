"""Middleware: Host validation, admin domain control, CSRF protection"""
import secrets
from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse, Response

from app.admin_domains import get_request_host, is_admin_access, is_loopback_host
from app.config import COOKIE_SECURE, HSTS_MAX_AGE, HSTS_INCLUDE_SUBDOMAINS


class AdminDomainMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        if path.startswith("/admin"):
            if not is_admin_access(request):
                return Response(status_code=404)
        response = await call_next(request)
        return response


# ========== CSRF ==========

def generate_csrf_token() -> str:
    return secrets.token_hex(32)


def get_or_create_csrf_token(request) -> str:
    """Get existing CSRF token from cookie, or generate a new one."""
    token = request.cookies.get("csrf_token")
    if token:
        return token
    return generate_csrf_token()


def _secure_cookie_for_request(request) -> bool:
    return COOKIE_SECURE and not is_loopback_host(get_request_host(request))


def set_csrf_cookie(response: Response, token: str, request):
    response.set_cookie(
        key="csrf_token",
        value=token,
        httponly=True,
        samesite="lax",
        secure=_secure_cookie_for_request(request),
        max_age=86400,
        path="/",
    )


def validate_csrf(request, form_token: str = None) -> bool:
    """匿名表单的 CSRF 校验：Cookie / 表单双提交令牌。"""
    cookie_token = request.cookies.get("csrf_token")
    if not cookie_token or not form_token:
        return False
    return secrets.compare_digest(str(cookie_token), str(form_token))


def validate_admin_csrf(request, form_token: str = None) -> bool:
    """后台修改请求的 CSRF 校验：优先使用与会话绑定的服务端令牌。"""
    if not form_token:
        return False

    expected = getattr(request.state, "admin_csrf_token", None)
    if not expected:
        session_id = request.cookies.get("admin_session")
        if not session_id:
            return False
        # 局部导入避免 auth -> models -> database 与中间件的循环依赖
        from app.auth import get_session
        session = get_session(session_id)
        if not session:
            return False
        expected = session.get("csrf_token", "")

    if not expected:
        return False
    return secrets.compare_digest(str(expected), str(form_token))


def _normalize_host(value: str) -> str:
    """归一化 host/端口，忽略默认的 :80/:443 与末尾点。"""
    value = (value or "").strip().lower().rstrip(".")
    if ":" in value:
        host, _, port = value.rpartition(":")
        if port in ("80", "443") and host:
            return host
    return value


def _netloc(url: str) -> str:
    try:
        return _normalize_host(urlparse(url).netloc)
    except (ValueError, TypeError):
        return ""


def validate_same_origin(request) -> bool:
    """校验浏览器发起的修改请求是否同源。

    浏览器跨站 POST 一定会带 Origin（或 Referer）；无这两个头的非浏览器
    客户端（curl / 自动化脚本）放行，它们仍必须提供 CSRF 令牌。
    不信任 X-Forwarded-Host，只比较 Host 与 Origin/Referer 的 netloc。
    """
    host = _normalize_host(request.headers.get("host", ""))
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    if origin:
        return bool(host) and _netloc(origin) == host
    if referer:
        return bool(host) and _netloc(referer) == host
    return True


class CSRFMiddleware(BaseHTTPMiddleware):
    """Ensure CSRF cookie exists and expose token via request.state.csrf_token."""
    async def dispatch(self, request, call_next):
        # 所有修改性请求统一做来源检查；无效来源不消耗登录失败额度
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            if not validate_same_origin(request):
                return PlainTextResponse("请求来源非法", status_code=403)

        # Get or create CSRF token at the start of the request
        existing = request.cookies.get("csrf_token")
        if existing:
            request.state.csrf_token = existing
        else:
            request.state.csrf_token = generate_csrf_token()

        response = await call_next(request)

        # Set cookie if it didn't exist or if we generated a new one
        if not existing:
            set_csrf_cookie(response, request.state.csrf_token, request)
        return response


# ========== Sensitive words ==========

SENSITIVE_WORDS = []


def load_sensitive_words():
    global SENSITIVE_WORDS
    from app.models import get_setting
    words_str = get_setting("sensitive_words", "")
    if words_str:
        SENSITIVE_WORDS = [w.strip() for w in words_str.split(",") if w.strip()]
    else:
        SENSITIVE_WORDS = []


def check_sensitive(text: str) -> bool:
    if not SENSITIVE_WORDS:
        return False
    for word in SENSITIVE_WORDS:
        if word in text:
            return True
    return False


# ========== 安全响应头 ==========

def request_is_https(request) -> bool:
    """判断请求是否经 HTTPS 到达。

    只有来自可信反向代理时才读取 X-Forwarded-Proto，防止客户端伪造。
    """
    if request.url.scheme == "https":
        return True
    client_host = request.client.host if request.client else ""
    if not client_host:
        return False
    from app.ip_geo import _is_trusted_proxy
    if not _is_trusted_proxy(client_host):
        return False
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return proto == "https"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """为所有响应添加安全响应头（纵深防御，建议同时在 Nginx/Cloudflare 层配置）。

    使用 setdefault 避免覆盖反向代理已注入的头；若上游未设置，则使用此处的安全默认值。
    """

    async def dispatch(self, request, call_next):
        # 每次请求生成一次性 CSP nonce：允许本页必需的少量内联脚本，
        # 同时不再放开 script-src 'unsafe-inline'（避免任意外联脚本注入执行）。
        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=()",
        )
        is_https = request_is_https(request)
        csp = (
            "default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}' https://challenges.cloudflare.com; "
            f"style-src 'self' 'nonce-{nonce}'; "
            "img-src 'self' data:; "
            "connect-src 'self' https://challenges.cloudflare.com; "
            "frame-src https://challenges.cloudflare.com; "
            "frame-ancestors 'self'; "
            "object-src 'none'; base-uri 'self'; form-action 'self'; "
            "manifest-src 'self'; worker-src 'none'"
        )
        if is_https:
            csp += "; upgrade-insecure-requests"
        response.headers.setdefault("Content-Security-Policy", csp)
        if is_https:
            hsts = f"max-age={HSTS_MAX_AGE}"
            if HSTS_INCLUDE_SUBDOMAINS:
                hsts += "; includeSubDomains"
            response.headers.setdefault("Strict-Transport-Security", hsts)
        return response