"""FastAPI application entry point"""
import logging
import os
import traceback
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from app.config import (
    UPLOADS_DIR, PRIVATE_UPLOADS_DIR, BASE_DIR, APP_VERSION,
    log_deployment_warnings,
)
from app.admin_domains import ensure_admin_domain_initialized
from app.database import init_db, close_all_db, checkpoint_wal
from app.middleware import AdminDomainMiddleware, CSRFMiddleware, SecurityHeadersMiddleware, UploadBodyLimitMiddleware, load_sensitive_words
from app.display import safe_color
from app.routes import front, admin, api, media

# Ensure directories exist
os.makedirs(UPLOADS_DIR, exist_ok=True)
os.makedirs(PRIVATE_UPLOADS_DIR, exist_ok=True)
os.makedirs(BASE_DIR / "data", exist_ok=True)

# Initialize templates at module level
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
templates.env.filters["safe_color"] = safe_color

app = FastAPI(
    title="Anonymous Image Sharing",
    version=APP_VERSION,
    docs_url=None,
    redoc_url=None,
)

# Add middleware (last added = outermost wrapper)
app.add_middleware(AdminDomainMiddleware)
app.add_middleware(CSRFMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
# 最外层：上传请求体大小与并发限制，必须在 multipart 解析阶段之前生效
app.add_middleware(UploadBodyLimitMiddleware)

# Mount static files
class CachedStaticFiles(StaticFiles):
    """静态资源在 ETag/Last-Modified 之外补充浏览器缓存头。

    生产环境 Nginx 的 /static/ 通常配置了 expires；直接由应用提供静态文件
    （本地开发或未配置 Nginx 缓存）时，缺少 Cache-Control 会导致每次导航
    都重新验证。CSS/JS 引用带有版本查询串，可按天缓存。
    """

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code in (200, 304):
            response.headers.setdefault("Cache-Control", "public, max-age=86400")
        return response


static_dir = BASE_DIR / "app" / "static"
if static_dir.exists():
    app.mount("/static", CachedStaticFiles(directory=str(static_dir)), name="static")

# /uploads 不再作为静态目录直接暴露：统一由受控路由按审核状态与管理员身份鉴权
# （见 app/routes/media.py）。Nginx 也必须把 /uploads 反代到应用，不能配置 alias。

# Exception handlers
@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    return templates.TemplateResponse(request, "404.html", {}, status_code=404)


@app.exception_handler(500)
async def internal_error_handler(request: Request, exc):
    traceback.print_exc()
    return JSONResponse({"error": "Internal server error"}, status_code=500)


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc):
    traceback.print_exc()
    # 不向客户端泄露内部异常详情
    return JSONResponse({"error": "服务器内部错误"}, status_code=500)

# Register routes
app.include_router(front.router)
app.include_router(admin.router)
app.include_router(api.router)
app.include_router(media.router)


@app.on_event("startup")
async def startup():
    log_deployment_warnings()
    init_db()
    # 仅首次运行/旧库迁移时用 .env 初始化管理域名；之后数据库为唯一权威配置
    ensure_admin_domain_initialized()
    # 从数据库加载敏感词；不加载会导致重启后已配置的敏感词过滤失效
    load_sensitive_words()
    print("[INFO] Application started")


@app.on_event("shutdown")
async def shutdown():
    # 正常退出前做一次 checkpoint，把 WAL 内容合并回主库，避免 -wal 文件长期增长；
    # 随后关闭本进程创建的所有线程连接（线程池连接不会走请求级清理）。
    try:
        checkpoint_wal("TRUNCATE")
    except Exception:  # 退出阶段失败不应阻断进程关闭
        logging.getLogger(__name__).warning("退出前 WAL checkpoint 失败", exc_info=True)
    close_all_db()
    print("[INFO] Application stopped")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)