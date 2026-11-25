"""安装向导的 FastAPI 应用（临时服务，仅在运行 install.py 期间存在）。

安全设计：
- 所有路由都要求 URL 中的随机令牌（secrets.token_urlsafe(32)），令牌不符一律 404，
  避免被扫描到安装入口；
- 只监听用户指定的地址（默认 127.0.0.1），远程安装需显式 --remote；
- 响应禁止缓存、禁止被 iframe 嵌入。
"""
import hmac
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from installer.executor import InstallExecutor
from installer.preflight import _check_mysql, _check_sqlite, run_checks
from installer.state import (
    STEPS, existing_installation, normalize_db, normalize_site, validate_all,
)
from installer.verify import verify_http

BASE_DIR = Path(__file__).resolve().parent.parent
_TEMPLATE = Path(__file__).resolve().parent / "templates" / "install.html"


def _default_site(base_dir: Path) -> dict:
    return {
        "admin_domain": "",
        "host": "127.0.0.1",
        "port": 8000,
        "workers": 1,
        "uploads_dir": str(base_dir / "uploads"),
        "private_uploads_dir": str(base_dir / "private_uploads"),
    }


def create_app(token: str, base_dir: Path = None) -> FastAPI:
    base_dir = Path(base_dir or BASE_DIR)
    executor = InstallExecutor()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    def _authorized(request: Request) -> bool:
        supplied = request.path_params.get("token", "")
        return bool(token) and hmac.compare_digest(str(supplied), str(token))

    def _not_found():
        return JSONResponse({"error": "Not Found"}, status_code=404)

    async def _json_body(request: Request) -> dict:
        try:
            data = await request.json()
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    @app.get("/", include_in_schema=False)
    async def index():
        return JSONResponse({"error": "Not Found"}, status_code=404)

    @app.get("/install/{token}", response_class=HTMLResponse)
    async def wizard(request: Request):
        if not _authorized(request):
            return _not_found()
        html = _TEMPLATE.read_text(encoding="utf-8").replace("__TOKEN__", str(token))
        return HTMLResponse(html)

    @app.get("/install/{token}/api/status")
    async def status(request: Request):
        if not _authorized(request):
            return _not_found()
        info = existing_installation(base_dir)
        return {
            "version": "1.2.5",
            "steps": list(STEPS),
            "existing": info,
            "default_site": _default_site(base_dir),
            "progress": executor.snapshot(),
        }

    @app.post("/install/{token}/api/preflight")
    async def preflight(request: Request):
        if not _authorized(request):
            return _not_found()
        body = await _json_body(request)
        try:
            db = normalize_db(body.get("db"), base_dir)
            site = normalize_site(body.get("site") or _default_site(base_dir), base_dir)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        checks = run_checks(db, site, base_dir)
        return {"ok": all(item["ok"] for item in checks), "checks": checks}

    @app.post("/install/{token}/api/test-db")
    async def test_db(request: Request):
        if not _authorized(request):
            return _not_found()
        body = await _json_body(request)
        try:
            db = normalize_db(body.get("db"), base_dir)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        items = _check_mysql(db) if db["type"] == "mysql" else _check_sqlite(db["database_path"])
        return {"ok": all(item["ok"] for item in items), "checks": items}

    @app.post("/install/{token}/api/install")
    async def install(request: Request):
        if not _authorized(request):
            return _not_found()
        body = await _json_body(request)
        force = bool(body.get("force_reinstall"))
        try:
            validate_all(body, base_dir, force)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        started = executor.start(body, base_dir, force)
        if not started:
            return JSONResponse(
                {"ok": False, "error": "安装任务正在执行中，请稍候"}, status_code=409
            )
        return {"ok": True, "started": True}

    @app.get("/install/{token}/api/progress")
    async def progress(request: Request):
        if not _authorized(request):
            return _not_found()
        return executor.snapshot()

    @app.post("/install/{token}/api/verify-http")
    async def verify_http_now(request: Request):
        if not _authorized(request):
            return _not_found()
        if executor.data is None:
            return JSONResponse({"ok": False, "error": "请先完成安装"}, status_code=400)
        check = verify_http(executor.data, base_dir)
        return {"ok": check["ok"], "check": check}

    @app.get("/install/{token}/api/report")
    async def report(request: Request):
        if not _authorized(request):
            return _not_found()
        if not executor.report:
            return JSONResponse({"ok": False, "error": "安装尚未完成"}, status_code=404)
        return executor.report

    return app


def lan_addresses() -> list:
    """尽力枚举本机局域网 IP，用于在控制台提示远程安装地址。"""
    import socket

    addresses = set()
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None):
            addr = info[4][0]
            if ":" not in addr and not addr.startswith("127."):
                addresses.add(addr)
    except OSError:
        pass
    return sorted(addresses)
