"""Middleware: Host validation, admin domain control, CSRF protection"""
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from app.config import ADMIN_DOMAIN
import secrets


# ========== Admin domain checks ==========

def get_admin_domains():
    from app.models import get_setting
    env_domains = ADMIN_DOMAIN
    db_domains = get_setting("admin_domain", "")
    all_domains = set()
    for src in (env_domains, db_domains):
        if src:
            for d in src.split(","):
                d = d.strip().lower()
                if d:
                    all_domains.add(d)
    return all_domains


def is_admin_access(request) -> bool:
    host = request.headers.get("host", "").lower().split(":")[0]
    if host in ("127.0.0.1", "localhost") or host.startswith("127."):
        return True
    domains = get_admin_domains()
    if not domains:
        return False  # 未配置管理域名时，拒绝非本地访问（默认安全）
    return host in domains


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


def set_csrf_cookie(response: Response, token: str):
    response.set_cookie(
        key="csrf_token",
        value=token,
        httponly=True,
        samesite="lax",
        secure=False,
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
            set_csrf_cookie(response, request.state.csrf_token)
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