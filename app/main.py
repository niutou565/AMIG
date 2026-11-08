"""FastAPI application entry point"""
import os
import traceback
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from app.config import UPLOADS_DIR, BASE_DIR, APP_VERSION
from app.admin_domains import ensure_admin_domain_initialized
from app.database import init_db, close_db
from app.middleware import AdminDomainMiddleware, CSRFMiddleware, SecurityHeadersMiddleware
from app.display import safe_color
from app.routes import front, admin, api

# Ensure directories exist
os.makedirs(UPLOADS_DIR, exist_ok=True)
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

# Mount static files
static_dir = BASE_DIR / "app" / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

uploads_dir = Path(UPLOADS_DIR)
if uploads_dir.exists():
    app.mount("/uploads", StaticFiles(directory=str(uploads_dir)), name="uploads")

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


@app.on_event("startup")
async def startup():
    init_db()
    # 仅首次运行/旧库迁移时用 .env 初始化管理域名；之后数据库为唯一权威配置
    ensure_admin_domain_initialized()
    print("[INFO] Application started")


@app.on_event("shutdown")
async def shutdown():
    close_db()
    print("[INFO] Application stopped")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)