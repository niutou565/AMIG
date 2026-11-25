import ipaddress
import logging
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# 应用版本信息
APP_NAME = "AMIG"
APP_VERSION = "1.2.5"

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

# ── 数据库后端选择 ────────────────────────────────────────────────
# sqlite：单文件数据库，零依赖，适合小站与宝塔等简单部署；
# mysql ：MySQL 8.0+，适合多 worker / 高并发部署。
# 兼容性：DB_TYPE 缺失时按 sqlite 处理——1.1.x 老版本用户的 .env 没有该键，
# 升级到 1.2.5 后直接启动仍然使用原来的 SQLite 库，数据与配置原样保留。
DB_TYPE = os.getenv("DB_TYPE", "sqlite").strip().lower() or "sqlite"
if DB_TYPE not in ("sqlite", "mysql"):
    raise SystemExit(
        f"[配置错误] DB_TYPE 仅支持 sqlite 或 mysql（当前值：{DB_TYPE or '(空)'}）。"
        "如需切换数据库，请登录后台管理使用「切换数据库」功能。"
    )
IS_SQLITE = DB_TYPE == "sqlite"

# SQLite 数据库文件路径（DB_TYPE=sqlite 时使用；相对路径按项目根目录解析）
_database_path_env = os.getenv("DATABASE_PATH", "data/app.db").strip() or "data/app.db"
_database_path_obj = Path(_database_path_env)
if not _database_path_obj.is_absolute():
    _database_path_obj = BASE_DIR / _database_path_obj
DATABASE_PATH = str(_database_path_obj)
# SQLite 锁等待上限（毫秒）：写锁冲突时最多等待该时长再报错
DB_BUSY_TIMEOUT_MS = int(os.getenv("DB_BUSY_TIMEOUT_MS", "5000"))
# SQLite 持久性级别（1=NORMAL 2=FULL）；WAL 下 NORMAL 已能保证崩溃安全
DB_SYNCHRONOUS = int(os.getenv("DB_SYNCHRONOUS", "1"))

MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1").strip()
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "amig").strip()
MYSQL_USER = os.getenv("MYSQL_USER", "amig").strip()
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
# 建库与连接统一字符集；utf8mb4 保证中文与 emoji 正常存储
MYSQL_CHARSET = os.getenv("MYSQL_CHARSET", "utf8mb4").strip()
MYSQL_COLLATION = os.getenv("MYSQL_COLLATION", "utf8mb4_unicode_ci").strip()
# 连接池：常驻连接数 + 可临时溢出的最大连接数；总计不得超过 MySQL max_connections
MYSQL_POOL_SIZE = int(os.getenv("MYSQL_POOL_SIZE", "10"))
MYSQL_POOL_MAX_OVERFLOW = int(os.getenv("MYSQL_POOL_MAX_OVERFLOW", "20"))
# 从连接池取出连接时若超过 10 秒仍未获取到，直接报错而不是无限排队
MYSQL_POOL_TIMEOUT = int(os.getenv("MYSQL_POOL_TIMEOUT", "10"))
# 连接空闲超过该秒数后，下次使用前先 ping 一次并自动重连失效连接
MYSQL_PING_IDLE_SECONDS = int(os.getenv("MYSQL_PING_IDLE_SECONDS", "60"))
# 上传额度串行化锁（GET_LOCK）的等待秒数
MYSQL_QUOTA_LOCK_TIMEOUT = int(os.getenv("MYSQL_QUOTA_LOCK_TIMEOUT", "5"))
# 慢查询诊断阈值（毫秒），仅记录超过阈值的查询；0 表示关闭慢查询日志
DB_SLOW_QUERY_MS = int(os.getenv("DB_SLOW_QUERY_MS", "200"))
# mysqldump / mysql 客户端路径（备份恢复工具使用）；留空则自动从 PATH 与常见安装目录探测
MYSQLDUMP_PATH = os.getenv("MYSQLDUMP_PATH", "").strip()
MYSQL_CLIENT_PATH = os.getenv("MYSQL_CLIENT_PATH", "").strip()
# 备份文件根目录（默认放在项目 backups/ 下，不属于任何 Web 静态目录）
BACKUP_DIR = os.getenv("BACKUP_DIR", str(BASE_DIR / "backups"))
# 备份默认保留份数
BACKUP_KEEP = int(os.getenv("BACKUP_KEEP", "7"))
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
# multipart 请求体的额外开销（表单字段、边界等），用于请求体总大小限制：
# 该限制由中间件在 multipart 解析阶段之前生效，防止解析阶段消耗磁盘/内存
MAX_UPLOAD_BODY_OVERHEAD = 1024 * 1024
MAX_UPLOAD_BODY_SIZE = MAX_UPLOAD_SIZE + MAX_UPLOAD_BODY_OVERHEAD
# 上传接口（含 multipart 解析阶段）每进程最大并发请求数，超出直接返回 503
UPLOAD_MAX_CONCURRENT = int(os.getenv("UPLOAD_MAX_CONCURRENT", "8"))
MAX_IMAGE_PIXELS = 25_000_000       # 2500万像素
# Logo 图片的最大边长（像素）：Logo 属于站点资源，超出限制的重编码代价高且无必要
MAX_LOGO_DIMENSION = 2048
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