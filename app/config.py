import ipaddress
import logging
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# 应用版本信息
APP_NAME = "AMIG"
APP_VERSION = "1.1.0"

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

# ── SQLite 连接与 WAL 配置（第四阶段）──────────────────────────────
# 写锁等待上限（毫秒）：超时后返回明确的 database is locked，而不是无限阻塞请求
DB_BUSY_TIMEOUT_MS = int(os.getenv("DB_BUSY_TIMEOUT_MS", "5000"))
# WAL 下 synchronous=FULL 每次提交都 fsync，持久性最强；允许在充分评估后设为 NORMAL
# （WAL+NORMAL 在应用崩溃时安全，仅极端断电可能丢失最后一个事务）。默认不降低保障。
DB_SYNCHRONOUS = os.getenv("DB_SYNCHRONOUS", "FULL").strip().upper()
if DB_SYNCHRONOUS not in ("FULL", "NORMAL", "OFF"):
    _RAW_DB_SYNCHRONOUS = os.getenv("DB_SYNCHRONOUS", "").strip()
    print(
        f"[配置警告] DB_SYNCHRONOUS 值无效：{_RAW_DB_SYNCHRONOUS or '(空)'}，"
        "已回退为 FULL。可选值：FULL / NORMAL / OFF"
    )
    DB_SYNCHRONOUS = "FULL"
# WAL 自动 checkpoint 阈值（页数，每页约 4KB；1000 页≈4MB），0 表示关闭自动 checkpoint
DB_WAL_AUTOCHECKPOINT_PAGES = int(os.getenv("DB_WAL_AUTOCHECKPOINT_PAGES", "1000"))
# 慢查询诊断阈值（毫秒），仅记录超过阈值的查询；0 表示关闭慢查询日志
DB_SLOW_QUERY_MS = int(os.getenv("DB_SLOW_QUERY_MS", "200"))
# 备份文件根目录（默认放在项目 backups/ 下，不属于任何 Web 静态目录）
BACKUP_DIR = os.getenv("BACKUP_DIR", str(BASE_DIR / "backups"))
# 备份默认保留份数
BACKUP_KEEP = int(os.getenv("BACKUP_KEEP", "7"))
# 结构迁移前是否自动做一次数据库快照备份（默认开启；备份失败则不执行迁移）
DB_BACKUP_BEFORE_MIGRATION = os.getenv(
    "DB_BACKUP_BEFORE_MIGRATION", "true"
).strip().lower() in ("1", "true", "yes", "on")
UPLOADS_DIR = os.getenv("UPLOADS_DIR", str(BASE_DIR / "uploads"))
# 待审核/已拒绝图片的私有目录；必须位于 Web 服务器不能直接静态访问的位置。
PRIVATE_UPLOADS_DIR = os.getenv(
    "PRIVATE_UPLOADS_DIR", str(BASE_DIR / "private_uploads")
)
IP_API_TIMEOUT = float(os.getenv("IP_API_TIMEOUT", "3"))
IP_API_CACHE_TTL = int(os.getenv("IP_API_CACHE_TTL", "300"))

# 可信反向代理地址（仅当请求来自这些地址时，才信任 X-Real-IP 等代理头）
# 只接受 IP 或 CIDR；默认仅信任本机回环，防止客户端伪造请求头绕过限流/封禁。
def _parse_trusted_proxies(raw):
    """解析可信代理列表，忽略非法项与 0.0.0.0/0、::/0 这类通配配置。

    通配信任等于允许任何客户端伪造 X-Real-IP / X-Forwarded-Proto，
    因此必须拒绝；主机名也不接受（避免 DNS 解析结果变化导致信任范围失控）。
    """
    logger = logging.getLogger(__name__)
    proxies = set()
    for token in str(raw or "").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            network = ipaddress.ip_network(token, strict=False)
        except ValueError:
            logger.warning(
                "TRUSTED_PROXIES 忽略非法项 %r：仅支持 IP 或 CIDR，不支持主机名", token
            )
            continue
        if network.prefixlen == 0:
            logger.error(
                "TRUSTED_PROXIES 拒绝通配项 %r：允许全部来源会使用户可伪造 "
                "X-Real-IP / X-Forwarded-Proto",
                token,
            )
            continue
        proxies.add(token)
    return proxies


TRUSTED_PROXIES = _parse_trusted_proxies(
    os.getenv("TRUSTED_PROXIES", "127.0.0.1,::1")
)

# Cookie Secure 标志：HTTPS 部署下必须为 True，防止 Cookie 经 HTTP 明文传输
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").strip().lower() in ("1", "true", "yes", "on")

# HSTS（HTTP 严格传输安全）
HSTS_MAX_AGE = int(os.getenv("HSTS_MAX_AGE", "31536000"))
HSTS_INCLUDE_SUBDOMAINS = os.getenv("HSTS_INCLUDE_SUBDOMAINS", "true").strip().lower() in ("1", "true", "yes", "on")

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

# 水印字体：默认使用项目内置的 Noto Sans SC（SIL OFL 1.1，支持简体中文），
# 可通过 WATERMARK_FONT_PATH 环境变量覆盖为其他支持中文的字体文件。
_watermark_font_env = os.getenv("WATERMARK_FONT_PATH")
if _watermark_font_env:
    _watermark_font_path = Path(_watermark_font_env)
    if not _watermark_font_path.is_absolute():
        _watermark_font_path = BASE_DIR / _watermark_font_path
else:
    _watermark_font_path = BASE_DIR / "app" / "assets" / "fonts" / "NotoSansSC-Regular.otf"
WATERMARK_FONT_PATH = str(_watermark_font_path)

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

# 登录限流（数据库共享，多 worker 生效）
# 注意：第二阶段起不再使用“全局账户锁定”，避免多 IP 攻击者把管理员锁死；
# 实际拒绝由 LOGIN_IP_MAX_ATTEMPTS 按来源 IP 执行，窗口结束后自动恢复。
LOGIN_IP_MAX_ATTEMPTS = int(os.getenv("LOGIN_IP_MAX_ATTEMPTS", "5"))
LOGIN_IP_WINDOW_MINUTES = int(os.getenv("LOGIN_IP_WINDOW_MINUTES", "15"))

# 兼容旧配置项：不再参与登录拒绝逻辑（旧版全局账户锁定已取消）
LOGIN_MAX_ATTEMPTS = int(os.getenv("LOGIN_MAX_ATTEMPTS", "5"))
LOGIN_LOCK_MINUTES = int(os.getenv("LOGIN_LOCK_MINUTES", "15"))

# 管理员 Session：绝对有效期 + 空闲超时（分钟）
SESSION_TTL_HOURS = int(os.getenv("SESSION_TTL_HOURS", "24"))
SESSION_IDLE_MINUTES = int(os.getenv("SESSION_IDLE_MINUTES", "120"))

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


def log_deployment_warnings():
    """在启动时输出会影响生产安全的配置提示（不阻断启动）。"""
    logger = logging.getLogger(__name__)
    if not COOKIE_SECURE:
        logger.warning(
            "COOKIE_SECURE=false：Cookie 可通过 HTTP 明文传输，"
            "仅应在本地 HTTP 调试时使用；生产 HTTPS 环境必须设为 true"
        )
    if not TRUSTED_PROXIES:
        logger.warning(
            "TRUSTED_PROXIES 为空：不会信任任何 X-Real-IP / X-Forwarded-Proto；"
            "部署在 Nginx 反向代理后时，客户端 IP 识别与 HTTPS 判断会失效"
        )