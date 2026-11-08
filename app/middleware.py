"""Middleware: Host validation, admin domain control, CSRF protection"""
import secrets
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

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
    cookie_token = request.cookies.get("csrf_token")
    if not cookie_token or not form_token:
        return False
    return secrets.compare_digest(str(cookie_token), str(form_token))


class CSRFMiddleware(BaseHTTPMiddleware):
    """Ensure CSRF cookie exists and expose token via request.state.csrf_token."""
    async def dispatch(self, request, call_next):
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

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """为所有响应添加安全响应头（纵深防御，建议同时在 Nginx/Cloudflare 层配置）。

    使用 setdefault 避免覆盖反向代理已注入的头；若上游未设置，则使用此处的安全默认值。
    """

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline' https://challenges.cloudflare.com; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "frame-src https://challenges.cloudflare.com; frame-ancestors 'self'; "
            "object-src 'none'; base-uri 'self'",
        )
        hsts = f"max-age={HSTS_MAX_AGE}"
        if HSTS_INCLUDE_SUBDOMAINS:
            hsts += "; includeSubDomains"
        response.headers.setdefault("Strict-Transport-Security", hsts)
        return response