"""MySQL 数据库访问层：连接池、线程本地连接、事务管理与库表初始化。

设计要点：
- 业务模块统一通过 get_db() 获取连接包装器，不直接依赖具体驱动（PyMySQL）。
- SQL 仍使用 ``?`` 占位符（与历史代码一致），执行前统一翻译为 MySQL 的 ``%s``。
  注意：SQL 语句本身不允许包含字面量 ``?``；带参数的语句也不能含字面量 ``%``
  （LIKE 模式一律通过参数传入），无参数语句则可以包含字面量 ``%``。
- 行对象 Row 同时支持 ``row["col"]`` / ``row[0]`` / ``keys()`` / ``get()``，
  与原 sqlite3.Row 用法兼容，业务代码无需逐行修改。
- 连接采用「线程本地缓存 + 连接池」：同一请求内的多次 get_db() 复用同一连接；
  空闲超过 MYSQL_PING_IDLE_SECONDS 的连接在下次使用前自动 ping，失效自动重连。
- autocommit=True：单条语句即时提交（与原 SQLite 每函数 commit 的行为一致）；
  多语句原子性通过 transaction() 显式开启事务 / SAVEPOINT 保证。
- 上传额度检查等「读-改-写」操作用 GET_LOCK 串行化（见 models.reserve_upload_quota）。
"""
import contextlib
import contextvars
import logging
import queue
import re
import threading
import time
from datetime import datetime, timezone

import pymysql
from pymysql.cursors import DictCursor
from pymysql.err import (
    DatabaseError,
    IntegrityError,
    MySQLError,
    OperationalError,
)

from app.config import (
    BACKUP_DIR,
    MYSQL_CHARSET,
    MYSQL_COLLATION,
    MYSQL_DATABASE,
    MYSQL_HOST,
    MYSQL_PASSWORD,
    MYSQL_PING_IDLE_SECONDS,
    MYSQL_POOL_MAX_OVERFLOW,
    MYSQL_POOL_SIZE,
    MYSQL_POOL_TIMEOUT,
    MYSQL_PORT,
    MYSQL_USER,
)

_logger = logging.getLogger(__name__)

# 当前结构版本（与历史 SQLite schema_migrations 版本号衔接）
SCHEMA_VERSION = 6

# 以上 from-import 使 MySQL_* 配置成为本模块属性，测试可 monkeypatch 覆盖，
# 覆盖后调用 _reset_pool() 使连接池按新配置重建。

# 允许的数据库标识符（防止配置中的库名注入到 DDL）
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_$]+$")

# 单条语句遇到死锁（1213）/锁等待超时（1205）时的最大自动重试次数
_DEADLOCK_RETRY_LIMIT = 3


# ── 行对象：兼容 sqlite3.Row 的访问方式 ───────────────────────────────


class Row:
    """行对象：支持 row["col"]、row[0]、keys()、get()、in、len 与值迭代。"""

    __slots__ = ("_data", "_keys")

    def __init__(self, data: dict):
        self._data = data
        self._keys = list(data.keys())

    def keys(self):
        return list(self._keys)

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __getitem__(self, key):
        if isinstance(key, int):
            if -len(self._keys) <= key < len(self._keys):
                return self._data[self._keys[key]]
            raise IndexError(f"行不存在第 {key} 列")
        if key in self._data:
            return self._data[key]
        raise IndexError(f"行不存在字段：{key!r}")

    def __contains__(self, key):
        return key in self._data

    def __len__(self):
        return len(self._data)

    def __iter__(self):
        return iter(self._data.values())

    def __repr__(self):
        return repr(self._data)


class CursorWrapper:
    """游标包装器：提供 lastrowid / rowcount / fetchone / fetchall。"""

    __slots__ = ("_cur",)

    def __init__(self, cursor):
        self._cur = cursor

    @property
    def lastrowid(self):
        return self._cur.lastrowid

    @property
    def rowcount(self):
        return self._cur.rowcount

    def fetchone(self):
        row = self._cur.fetchone()
        return Row(row) if row is not None else None

    def fetchall(self):
        return [Row(r) for r in self._cur.fetchall()]

    def close(self):
        try:
            self._cur.close()
        except Exception:
            pass


def _translate_placeholders(sql: str) -> str:
    """把 qmark 占位符（?）翻译为 PyMySQL 的 %s。"""
    if "?" in sql:
        return sql.replace("?", "%s")
    return sql


def _is_gone_away(exc: OperationalError) -> bool:
    """判断是否为连接失效类错误（服务器断开、超时等），可安全重连重试。"""
    code = exc.args[0] if exc.args else None
    return code in (2006, 2013)


# ── 连接包装器 ───────────────────────────────────────────────────────


class ConnectionWrapper:
    """连接包装器：占位符翻译、空闲健康检查、失效重连、慢查询日志。"""

    __slots__ = ("_conn", "_last_used", "_closed", "_pooled", "_tx_active")

    def __init__(self, raw_conn):
        self._conn = raw_conn
        self._last_used = time.monotonic()
        self._closed = False
        # 是否计入连接池总量（归还/丢弃时只递减一次，避免重复计数）
        self._pooled = False
        # 是否处于显式事务（begin 之后、commit/rollback 之前）
        self._tx_active = False

    def execute(self, sql, params=()):
        if self._closed:
            raise DatabaseError("数据库连接已关闭，不能执行查询")
        sql = _translate_placeholders(sql)
        # 无参数时传 None，避免 PyMySQL 对 SQL 中的字面量 % 做格式化
        # （如 LIKE '%关键词%' 这类不带参数的检索语句）
        bind = params if params else None
        self._ensure_alive()
        start = time.monotonic()
        attempt = 0
        while True:
            attempt += 1
            try:
                cursor = self._conn.cursor()
                cursor.execute(sql, bind)
                break
            except OperationalError as exc:
                code = exc.args[0] if exc.args else None
                # 连接被服务器断开：重连后重试一次
                if _is_gone_away(exc) and attempt == 1 and self._reconnect():
                    continue
                # 单条语句（非显式事务）遇到死锁/锁等待超时可安全重试：
                # MySQL 会回滚该语句，重试不会产生重复写入。
                # 显式事务中绝不重试（服务端可能已回滚整个事务）。
                if (code in (1213, 1205) and not self._tx_active
                        and attempt <= _DEADLOCK_RETRY_LIMIT):
                    _logger.warning(
                        "检测到%s，第 %d 次重试: %s",
                        "死锁" if code == 1213 else "锁等待超时",
                        attempt, sql[:120],
                    )
                    time.sleep(0.01 * attempt)
                    continue
                raise
        elapsed_ms = (time.monotonic() - start) * 1000
        self._last_used = time.monotonic()
        _log_slow_query(sql, elapsed_ms)
        return CursorWrapper(cursor)

    def _ensure_alive(self):
        idle = time.monotonic() - self._last_used
        if idle > MYSQL_PING_IDLE_SECONDS:
            try:
                self._conn.ping(reconnect=True)
            except Exception as exc:
                raise DatabaseError(f"数据库连接已失效且无法重连: {exc}") from exc

    def _reconnect(self) -> bool:
        try:
            self._conn.ping(reconnect=True)
            return True
        except Exception:
            return False

    def begin(self):
        """显式开启事务（autocommit=True 时的多语句原子操作入口）。"""
        self._conn.begin()
        self._tx_active = True
        self._last_used = time.monotonic()

    def commit(self):
        self._conn.commit()
        self._tx_active = False
        self._last_used = time.monotonic()

    def rollback(self):
        try:
            self._conn.rollback()
        except Exception:
            pass
        self._tx_active = False
        self._last_used = time.monotonic()

    def close(self):
        self._closed = True
        try:
            self._conn.close()
        except Exception:
            pass

    def raw(self):
        """返回底层驱动连接（仅供工具类脚本使用，业务代码不要使用）。"""
        return self._conn


def _log_slow_query(sql: str, elapsed_ms: float):
    from app.config import DB_SLOW_QUERY_MS

    if DB_SLOW_QUERY_MS > 0 and elapsed_ms > DB_SLOW_QUERY_MS:
        _logger.warning(
            "慢查询 %.1fms: %s", elapsed_ms, sql[:300]
        )


# ── 连接池 ───────────────────────────────────────────────────────────


class _ConnectionPool:
    """简单连接池：常驻 MYSQL_POOL_SIZE，可溢出至 SIZE+MAX_OVERFLOW 总量。

    闲置连接放入 LifoQueue 复用；超出上限时阻塞等待其他线程归还。
    """

    def __init__(self):
        self._idle: queue.LifoQueue = queue.LifoQueue()
        self._lock = threading.Lock()
        self._created = 0

    def _max_total(self) -> int:
        return MYSQL_POOL_SIZE + MYSQL_POOL_MAX_OVERFLOW

    def _new_connection(self):
        return pymysql.connect(
            host=MYSQL_HOST,
            port=MYSQL_PORT,
            user=MYSQL_USER,
            password=MYSQL_PASSWORD,
            database=MYSQL_DATABASE,
            charset=MYSQL_CHARSET,
            collation=MYSQL_COLLATION,
            autocommit=True,
            cursorclass=DictCursor,
            connect_timeout=10,
        )

    def checkout(self):
        deadline = time.monotonic() + MYSQL_POOL_TIMEOUT
        while True:
            try:
                conn = self._idle.get_nowait()
                return conn
            except queue.Empty:
                pass
            with self._lock:
                if self._created < self._max_total():
                    self._created += 1
                    try:
                        conn = ConnectionWrapper(self._new_connection())
                        conn._pooled = True
                        return conn
                    except Exception:
                        self._created -= 1
                        raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DatabaseError(
                    "数据库连接池已耗尽：请在低峰期重试，或调大 MYSQL_POOL_SIZE / "
                    "MYSQL_POOL_MAX_OVERFLOW（注意不超过 MySQL 的 max_connections）。"
                )
            try:
                return self._idle.get(timeout=remaining)
            except queue.Empty:
                continue

    def checkin(self, conn: ConnectionWrapper):
        if conn._closed:
            self._discard(conn)
            return
        try:
            # 归还前回滚可能残留的事务，避免"带事务的连接"回到池里影响后续请求
            conn.rollback()
            conn._conn.ping(reconnect=False)
            self._idle.put(conn)
        except Exception:
            self._discard(conn)

    def _discard(self, conn: ConnectionWrapper):
        """关闭连接并（仅一次）归还池容量计数。"""
        with self._lock:
            if conn._pooled:
                conn._pooled = False
                self._created -= 1
        conn.close()

    def reset(self):
        """丢弃所有闲置连接（配置变更后调用；不影响正在使用的连接）。"""
        while True:
            try:
                conn = self._idle.get_nowait()
            except queue.Empty:
                break
            self._discard(conn)

    def idle_count(self) -> int:
        return self._idle.qsize()


_POOL = _ConnectionPool()

# 线程本地连接缓存：CLI 脚本、后台线程等无请求作用域的场景使用
_local = threading.local()
# 全部已签出的连接（用于 close_all_db 收尾）
_all_wrappers: set = set()
_all_lock = threading.Lock()

# 请求级连接作用域：由 HTTP 中间件建立（见 app/middleware.py: DBConnectionMiddleware）。
# 这里用可变 holder 对象而不是直接存连接：同步路由在线程池线程执行，
# contextvars 会以副本形式带入工作线程，但工作线程内的赋值不会回传；
# holder 对象本身是共享引用，因此工作线程签出的连接能被事件循环里的中间件收尾归还。
_request_holder = contextvars.ContextVar("amig_request_db", default=None)


class _RequestHolder:
    __slots__ = ("conn",)

    def __init__(self):
        self.conn = None


def begin_request_scope():
    """建立请求级连接作用域，返回 (holder, token)，交给中间件收尾。"""
    holder = _RequestHolder()
    return holder, _request_holder.set(holder)


def end_request_scope(holder, token):
    """请求结束时归还连接并撤销作用域（异常路径也必须调用）。"""
    try:
        conn = holder.conn
        holder.conn = None
        _release_connection(conn)
    finally:
        try:
            _request_holder.reset(token)
        except ValueError:
            # token 已在其他上下文重置，忽略即可
            pass


def _checkout():
    conn = _POOL.checkout()
    with _all_lock:
        _all_wrappers.add(conn)
    return conn


def _release_connection(conn):
    if conn is None:
        return
    with _all_lock:
        _all_wrappers.discard(conn)
    _POOL.checkin(conn)


# ── 对外接口 ─────────────────────────────────────────────────────────


def get_db() -> ConnectionWrapper:
    """获取当前请求（或当前线程）的数据库连接，需要时从连接池签出。"""
    holder = _request_holder.get()
    if holder is not None:
        conn = holder.conn
        if conn is not None and not conn._closed:
            return conn
        if conn is not None:
            # 连接已失效：丢弃后重新签出
            _POOL._discard(conn)
            with _all_lock:
                _all_wrappers.discard(conn)
        holder.conn = _checkout()
        return holder.conn

    conn = getattr(_local, "conn", None)
    if conn is not None and not conn._closed:
        return conn
    if conn is not None:
        _POOL._discard(conn)
        with _all_lock:
            _all_wrappers.discard(conn)
    conn = _checkout()
    _local.conn = conn
    return conn


def close_db():
    """归还当前请求/线程的数据库连接（可重复调用）。"""
    holder = _request_holder.get()
    if holder is not None:
        conn = holder.conn
        holder.conn = None
        _release_connection(conn)
        _local.tx_depth = 0
        return

    conn = getattr(_local, "conn", None)
    if conn is not None:
        _local.conn = None
        _local.tx_depth = 0
        _release_connection(conn)


def close_all_db():
    """关闭全部已签出的连接并重置连接池（应用关闭或测试收尾时调用）。"""
    with _all_lock:
        wrappers = list(_all_wrappers)
        _all_wrappers.clear()
    for conn in wrappers:
        _POOL._discard(conn)
    _POOL.reset()


def _reset_pool():
    """测试辅助：配置变更后重置连接池与线程本地缓存。"""
    close_all_db()
    _local.conn = None
    _local.tx_depth = 0


@contextlib.contextmanager
def transaction():
    """事务上下文管理器，支持嵌套（内层用 SAVEPOINT）。

    with transaction() as conn:
        conn.execute(...)
        conn.execute(...)
    # 正常退出自动提交；异常自动回滚并抛出
    """
    conn = get_db()
    depth = getattr(_local, "tx_depth", 0)
    _local.tx_depth = depth + 1
    savepoint = f"sp_{depth}_{threading.get_ident()}"
    outermost = depth == 0
    try:
        if outermost:
            conn.begin()
        else:
            conn.execute(f"SAVEPOINT {savepoint}")
        yield conn
        if outermost:
            conn.commit()
        else:
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except BaseException:
        if outermost:
            conn.rollback()
        else:
            try:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            except DatabaseError:
                _logger.exception("回滚保存点失败")
        raise
    finally:
        _local.tx_depth = depth


def run_in_transaction_with_retry(func, retries=_DEADLOCK_RETRY_LIMIT):
    """在事务中执行 func(conn)，遇到死锁/锁等待超时时整体重试事务。

    死锁发生时 MySQL 会回滚整个事务，因此必须在事务边界重试（而不是重试单条
    语句）。func 必须可重复执行（幂等或其内部已处理唯一约束冲突）。
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            with transaction() as conn:
                return func(conn)
        except OperationalError as exc:
            code = exc.args[0] if exc.args else None
            if code in (1213, 1205) and attempt < retries:
                _logger.warning(
                    "事务执行遇到%s，第 %d 次整体重试",
                    "死锁" if code == 1213 else "锁等待超时", attempt,
                )
                time.sleep(0.01 * attempt)
                continue
            raise


def acquire_named_lock(conn: ConnectionWrapper, name: str, timeout: int) -> bool:
    """获取 MySQL 命名互斥锁（用于「读-改-写」原子化），失败返回 False。

    锁与连接绑定，必须与 release_named_lock 成对使用，异常路径也要释放。
    """
    row = conn.execute("SELECT GET_LOCK(%s, %s) AS locked", (name, timeout)).fetchone()
    return row is not None and row["locked"] == 1


def release_named_lock(conn: ConnectionWrapper, name: str):
    conn.execute("SELECT RELEASE_LOCK(%s)", (name,))


# ── 表结构初始化 ─────────────────────────────────────────────────────

_SCHEMA_DDL = [
    """
    CREATE TABLE IF NOT EXISTS images (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        filename        VARCHAR(255) NOT NULL,
        thumb_filename  VARCHAR(255) NOT NULL DEFAULT '',
        original_size   INT NOT NULL DEFAULT 0,
        compressed_size INT NOT NULL DEFAULT 0,
        width           INT NOT NULL DEFAULT 0,
        height          INT NOT NULL DEFAULT 0,
        description     TEXT NULL,
        title           VARCHAR(200) NOT NULL DEFAULT '',
        author          VARCHAR(100) NULL DEFAULT NULL,
        content_md5     VARCHAR(64) NOT NULL DEFAULT '',
        ip              VARCHAR(64) NOT NULL DEFAULT '',
        ua              VARCHAR(512) NULL,
        ua_hash         VARCHAR(64) NOT NULL DEFAULT '',
        province        VARCHAR(64) NOT NULL DEFAULT '',
        city            VARCHAR(64) NOT NULL DEFAULT '',
        region          VARCHAR(128) NOT NULL DEFAULT '',
        status          VARCHAR(16) NOT NULL DEFAULT 'pending',
        reject_reason   TEXT NULL,
        admin_edited    TINYINT NOT NULL DEFAULT 0,
        created_at      VARCHAR(40) NOT NULL,
        reviewed_at     VARCHAR(40) NULL DEFAULT NULL,
        views           INT NOT NULL DEFAULT 0,
        like_count      INT NOT NULL DEFAULT 0,
        is_duplicate    TINYINT NOT NULL DEFAULT 0,
        KEY idx_images_status (status),
        KEY idx_images_reviewed_at (reviewed_at),
        KEY idx_images_like_count (like_count),
        KEY idx_images_content_md5 (content_md5),
        KEY idx_images_created_at (created_at),
        KEY idx_images_ip (ip),
        KEY idx_images_ua_hash (ua_hash),
        KEY idx_images_filename (filename),
        KEY idx_images_thumb_filename (thumb_filename),
        KEY idx_images_status_reviewed (status, reviewed_at),
        KEY idx_images_status_created (status, created_at),
        KEY idx_images_status_like (status, like_count)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS likes (
        id          INT AUTO_INCREMENT PRIMARY KEY,
        image_id    INT NOT NULL,
        ip          VARCHAR(64) NOT NULL,
        ua_hash     VARCHAR(64) NOT NULL,
        province    VARCHAR(64) NOT NULL DEFAULT '',
        city        VARCHAR(64) NOT NULL DEFAULT '',
        created_at  VARCHAR(40) NOT NULL,
        UNIQUE KEY idx_likes_fingerprint (image_id, ip, ua_hash),
        KEY idx_likes_created_at (created_at),
        KEY idx_likes_image_id (image_id),
        KEY idx_likes_ip (ip),
        KEY idx_likes_province (province),
        KEY idx_likes_image_created (image_id, created_at),
        CONSTRAINT fk_likes_image FOREIGN KEY (image_id)
            REFERENCES images(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS ban_list (
        id          INT AUTO_INCREMENT PRIMARY KEY,
        target      VARCHAR(255) NOT NULL,
        type        VARCHAR(16) NOT NULL,
        reason      VARCHAR(500) NOT NULL DEFAULT '',
        admin_id    INT NOT NULL,
        created_at  VARCHAR(40) NOT NULL,
        is_active   TINYINT NOT NULL DEFAULT 1,
        KEY idx_ban_list_target (target),
        KEY idx_ban_list_type (type)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS admins (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        username        VARCHAR(64) NOT NULL UNIQUE,
        password_hash   VARCHAR(128) NOT NULL,
        login_attempts  INT NOT NULL DEFAULT 0,
        locked_until    VARCHAR(40) NULL DEFAULT NULL,
        created_at      VARCHAR(40) NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        session_id   VARCHAR(64) PRIMARY KEY,
        admin_id     INT NOT NULL,
        username     VARCHAR(64) NOT NULL,
        created_at   VARCHAR(40) NOT NULL,
        expires_at   VARCHAR(40) NOT NULL,
        csrf_token   VARCHAR(64) NOT NULL DEFAULT '',
        last_seen_at VARCHAR(40) NOT NULL DEFAULT '',
        KEY idx_sessions_expires_at (expires_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS login_rate_limits (
        `key`         VARCHAR(100) PRIMARY KEY,
        attempts      INT NOT NULL DEFAULT 0,
        window_start  VARCHAR(40) NOT NULL,
        updated_at    VARCHAR(40) NOT NULL,
        KEY idx_login_rate_limits_window (window_start)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS settings (
        `key`    VARCHAR(100) PRIMARY KEY,
        value    TEXT NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS upload_reservations (
        id          INT AUTO_INCREMENT PRIMARY KEY,
        ip          VARCHAR(64) NOT NULL,
        ua_hash     VARCHAR(64) NOT NULL,
        created_at  VARCHAR(40) NOT NULL,
        KEY idx_upload_reservations_ip (ip, created_at),
        KEY idx_upload_reservations_created (created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS pages (
        id          INT AUTO_INCREMENT PRIMARY KEY,
        slug        VARCHAR(100) NOT NULL UNIQUE,
        title       VARCHAR(200) NOT NULL,
        content     TEXT NULL,
        sort_order  INT NOT NULL DEFAULT 0,
        is_visible  TINYINT NOT NULL DEFAULT 1,
        updated_at  VARCHAR(40) NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS friend_links (
        id          INT AUTO_INCREMENT PRIMARY KEY,
        name        VARCHAR(100) NOT NULL,
        url         VARCHAR(500) NOT NULL,
        sort_order  INT NOT NULL DEFAULT 0,
        is_visible  TINYINT NOT NULL DEFAULT 1,
        created_at  VARCHAR(40) NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_stats (
        `date`          VARCHAR(10) PRIMARY KEY,
        upload_count    INT NOT NULL DEFAULT 0,
        approved_count  INT NOT NULL DEFAULT 0,
        rejected_count  INT NOT NULL DEFAULT 0,
        like_count      INT NOT NULL DEFAULT 0,
        view_count      INT NOT NULL DEFAULT 0
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS admin_logs (
        id          INT AUTO_INCREMENT PRIMARY KEY,
        admin_id    INT NOT NULL,
        admin_name  VARCHAR(64) NOT NULL,
        action      VARCHAR(50) NOT NULL,
        detail      TEXT NULL,
        target      VARCHAR(255) NOT NULL DEFAULT '',
        ip          VARCHAR(64) NOT NULL DEFAULT '',
        location    VARCHAR(128) NOT NULL DEFAULT '',
        created_at  VARCHAR(40) NOT NULL,
        KEY idx_admin_logs_created_at (created_at),
        KEY idx_admin_logs_ip (ip),
        KEY idx_admin_logs_action (action),
        KEY idx_admin_logs_action_created (action, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS view_sessions (
        id          INT AUTO_INCREMENT PRIMARY KEY,
        image_id    INT NOT NULL,
        ip          VARCHAR(64) NOT NULL,
        viewed_at   VARCHAR(40) NOT NULL,
        UNIQUE KEY idx_view_sessions_unique (image_id, ip),
        KEY idx_view_sessions_lookup (image_id, ip, viewed_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS cooling (
        id                  INT AUTO_INCREMENT PRIMARY KEY,
        ip                  VARCHAR(64) NOT NULL UNIQUE,
        reject_count        INT NOT NULL DEFAULT 0,
        cooldown_multiplier INT NOT NULL DEFAULT 1,
        updated_at          VARCHAR(40) NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version     INT PRIMARY KEY,
        applied_at  VARCHAR(40) NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
]

EXPECTED_TABLES = (
    "images", "likes", "ban_list", "admins", "sessions", "login_rate_limits",
    "settings", "upload_reservations", "pages", "friend_links", "daily_stats",
    "admin_logs", "view_sessions", "cooling", "schema_migrations",
)


def _validate_database_name(name: str):
    if not name or not _IDENTIFIER_RE.match(name):
        raise DatabaseError(f"MySQL 数据库名非法：{name!r}（仅允许字母数字与 _ $）")


def _ensure_database():
    """连接到 MySQL 服务器并确保目标库存在（utf8mb4）。"""
    _validate_database_name(MYSQL_DATABASE)
    conn = pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        charset=MYSQL_CHARSET,
        autocommit=True,
        connect_timeout=10,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{MYSQL_DATABASE}` "
                f"CHARACTER SET {MYSQL_CHARSET} COLLATE {MYSQL_COLLATION}"
            )
    finally:
        conn.close()


def _applied_versions(conn) -> set:
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {int(r[0]) for r in rows}


def _record_version(conn, version):
    conn.execute(
        "INSERT IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        (version, datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )


def _init_db_once():
    _ensure_database()
    conn = get_db()
    for ddl in _SCHEMA_DDL:
        conn.execute(ddl)
    applied = _applied_versions(conn)
    with transaction():
        for version in (4, 5, 6, SCHEMA_VERSION):
            _record_version(conn, version)


def init_db(max_wait_seconds=60.0):
    """初始化数据库表结构；启动阶段连接不可用时重试至多 max_wait_seconds 秒。

    多 worker 同时启动时，CREATE TABLE IF NOT EXISTS 幂等且 InnoDB DDL 自带
    互斥，短暂冲突会以指数退避重试消化；真正的连接错误在超时后抛出。
    """
    deadline = time.monotonic() + max_wait_seconds
    delay = 0.1
    while True:
        try:
            _init_db_once()
            return
        except (OperationalError, DatabaseError) as exc:
            message = str(exc).lower()
            transient = any(
                kw in message
                for kw in ("lock", "timeout", "timed out", "gone away", "refused", "can't connect")
            )
            if not transient or time.monotonic() >= deadline:
                raise
            _logger.warning("数据库暂不可用（%s），%.1f 秒后重试...", exc, delay)
            close_db()
            time.sleep(delay)
            delay = min(delay * 2, 2.0)


def get_db_status() -> dict:
    """数据库健康信息（check_db 工具使用）：版本、字符集、连接数等。"""
    conn = get_db()
    version = conn.execute("SELECT VERSION() AS v").fetchone()["v"]
    charset_row = conn.execute(
        "SELECT @@character_set_database AS cs, @@collation_database AS co"
    ).fetchone()
    max_conn = conn.execute("SELECT @@max_connections AS mc").fetchone()["mc"]
    threads = conn.execute("SHOW STATUS LIKE 'Threads_connected'").fetchall()
    return {
        "server_version": version,
        "charset": charset_row["cs"],
        "collation": charset_row["co"],
        "max_connections": max_conn,
        "threads_connected": threads[0]["Value"] if threads else None,
        "pool_idle": _POOL.idle_count(),
    }
