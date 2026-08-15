import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# 从 .env 文件加载环境变量（必须在读取 os.getenv 之前执行，否则 ADMIN_DOMAIN 等不生效）
_env_path = BASE_DIR / ".env"
if _env_path.exists():
    with open(_env_path, "r", encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                _k = _k.strip()
                _v = _v.strip().strip('"').strip("'")
                if _k and _k not in os.environ:
                    os.environ[_k] = _v

# 从环境变量加载
ADMIN_DOMAIN = os.getenv("ADMIN_DOMAIN", "")
DATABASE_PATH = os.getenv("DATABASE_PATH", str(BASE_DIR / "data" / "app.db"))
UPLOADS_DIR = os.getenv("UPLOADS_DIR", str(BASE_DIR / "uploads"))
IP2REGION_PATH = os.getenv("IP2REGION_PATH", str(BASE_DIR / "data" / "ip2region.xdb"))
IP2REGION_V6_PATH = os.getenv("IP2REGION_V6_PATH", str(BASE_DIR / "data" / "ip2region_v6.xdb"))

# 可信反向代理地址（仅当请求来自这些地址时，才信任 X-Real-IP 等代理头）
# 默认仅信任本机回环，防止客户端伪造请求头绕过限流/封禁。
TRUSTED_PROXIES = {
    p.strip()
    for p in os.getenv("TRUSTED_PROXIES", "127.0.0.1,::1,localhost").split(",")
    if p.strip()
}

# 图片限制
MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10MB
MAX_IMAGE_PIXELS = 25_000_000       # 2500万像素
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MAX_DESC_LENGTH = 200
MAX_TITLE_LENGTH = 100
MAX_AUTHOR_LENGTH = 30

# 图片处理
MAX_IMAGE_DIMENSION = 1920
THUMB_DIMENSION = 480
WEBP_QUALITY = 78
WEBP_EFFORT = 3  # 低 effort 降低 CPU

# 默认限流值
DEFAULT_UPLOAD_PER_IP = 10
DEFAULT_UPLOAD_PER_UA = 10
DEFAULT_UPLOAD_DAILY_TOTAL = 500
DEFAULT_UPLOAD_INTERVAL = 30
DEFAULT_LIKE_PER_MINUTE = 10
DEFAULT_TOP_WINDOW_DAYS = 30
DEFAULT_TOP_SHOW_COUNT = 12

# 热度榜缓存时间（秒）
TOP_CACHE_TTL = 300

# 登录锁定
LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCK_MINUTES = 15

# 浏览去重窗口（秒）
VIEW_DEDUP_WINDOW = 600

# 图片去重窗口（天）
DEDUP_WINDOW_DAYS = 30
DEDUP_SAME_IP_HOURS = 24

# 图片列表每页数量
GALLERY_PER_PAGE = 24
ADMIN_PER_PAGE = 24
TOP_PER_PAGE = 24
LIKES_PER_PAGE = 20