"""FastAPI application entry point"""
import os
import traceback
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from app.config import SECRET_KEY, UPLOADS_DIR, BASE_DIR
from app.database import init_db, close_db
from app.ip_geo import init_ip2region
from app.middleware import AdminDomainMiddleware, CSRFMiddleware
from app.routes import front, admin, api

# Ensure directories exist
os.makedirs(UPLOADS_DIR, exist_ok=True)
os.makedirs(BASE_DIR / "data", exist_ok=True)

# Initialize templates at module level
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

app = FastAPI(
    title="Anonymous Image Sharing",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
)

# Add middleware (last added = outermost wrapper)
app.add_middleware(AdminDomainMiddleware)
app.add_middleware(CSRFMiddleware)

# Mount static files
static_dir = BASE_DIR / "app" / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

uploads_dir = BASE_DIR / "uploads"
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
    return JSONResponse({"error": str(exc)}, status_code=500)

# Register routes
app.include_router(front.router)
app.include_router(admin.router)
app.include_router(api.router)


@app.on_event("startup")
async def startup():
    init_db()
    init_ip2region()
    print("[INFO] Application started")


@app.on_event("shutdown")
async def shutdown():
    close_db()
    print("[INFO] Application stopped")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)