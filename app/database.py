"""数据库访问层：同时支持 SQLite 与 MySQL 两种后端。

后端由 ``DB_TYPE`` 决定（sqlite / mysql），对业务代码暴露同一套接口：
- 业务模块统一通过 get_db() 获取连接包装器，不直接依赖具体驱动。
- SQL 统一使用 ``?`` 占位符：SQLite 原生支持；MySQL 在执行前翻译为 ``%s``。
  注意（MySQL 模式）：SQL 语句本身不允许包含字面量 ``?``；带参数的语句也不能含
  字面量 ``%``（LIKE 模式一律通过参数传入），无参数语句可以包含字面量 ``%``。
- 行对象 Row 同时支持 ``row["col"]`` / ``row[0]`` / ``keys()`` / ``get()``，
  与 sqlite3.Row 用法兼容，业务代码无需逐行修改。
- MySQL：线程本地缓存 + 连接池，空闲连接自动 ping 重连；autocommit=True，
  多语句原子性通过 transaction() 显式开启事务 / SAVEPOINT 保证。
- SQLite：线程本地连接（check_same_thread=False，不跨线程共享），WAL 模式，
  transaction() 外层使用 ``BEGIN IMMEDIATE`` 提前拿写锁，避免升级锁失败。
- 方言差异（UPSERT / 随机数 / 命名锁等）集中在本模块的助手函数中，
  models / auth 等业务模块只调用助手，不写死某一后端的语法。
- 上传额度检查等「读-改-写」操作需要串行化：MySQL 用 GET_LOCK，
  SQLite 用进程内 threading.Lock（因此 SQLite 模式请保持 WORKERS=1）。
"""
import contextlib
import contextvars
import logging
import os
import queue
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone

from app.config import (
    BACKUP_DIR,
    DATABASE_PATH,
    DB_BUSY_TIMEOUT_MS,
    DB_SYNCHRONOUS,
    DB_TYPE,
    IS_SQLITE,
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

if IS_SQLITE:
    # SQLite 模式不导入 PyMySQL：1.1.x 老版本用户升级后即使没装 PyMySQL 也能直接启动
    DatabaseError = sqlite3.DatabaseError
    IntegrityError = sqlite3.IntegrityError
    OperationalError = sqlite3.OperationalError
    MySQLError = sqlite3.DatabaseError
else:
    try:
        import pymysql
        from pymysql.cursors import DictCursor
        from pymysql.err import (
            DatabaseError,
            IntegrityError,
            MySQLError,
            OperationalError,
        )
    except ImportError as _exc:  # pragma: no cover - 环境缺少依赖时的明确提示
        raise SystemExit(
            "[配置错误] DB_TYPE=mysql 需要安装 PyMySQL（pip install PyMySQL）；"
            "或改用 DB_TYPE=sqlite。原始错误：%s" % _exc
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


# ── 数据库切换期间的写入守卫 ─────────────────────────────────────────


class DatabaseSwitchInProgressError(DatabaseError):
    """数据库切换进行中（或切换成功等待重启），业务写入被临时拒绝。"""


# 仅匹配会修改数据的语句；SELECT / PRAGMA / BEGIN / COMMIT / SAVEPOINT 不受影响
_WRITE_STMT_RE = re.compile(
    r"^\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP|TRUNCATE|RENAME)\b",
    re.IGNORECASE,
)


def _assert_writable(sql: str) -> None:
    """切换期间拒绝业务写入。

    守卫下沉到连接层：业务模块（点赞、浏览量、统计、审核、封禁、设置、
    操作日志等）无需逐个改造，任何经 get_db() 的写语句都会被统一拦截。
    迁移器使用独立的适配器连接，不经过这里，因此切换流程自身不受影响。
    """
    if not _WRITE_STMT_RE.match(sql):
        return
    # 延迟导入：db_migration 在模块级依赖本模块，避免循环导入
    from app import db_migration

    if db_migration.switch_blocking_writes():
        raise DatabaseSwitchInProgressError(
            "数据库切换进行中，写入已暂停，请稍后重试"
        )


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
        _assert_writable(sql)
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


# ── SQLite 连接包装器（与 ConnectionWrapper 同接口） ─────────────────


class SqliteCursorWrapper:
    """SQLite 游标包装器：把 sqlite3.Row 转换为统一的 Row 对象。"""

    __slots__ = ("_cur",)

    def __init__(self, cursor):
        self._cur = cursor

    @property
    def lastrowid(self):
        return self._cur.lastrowid

    @property
    def rowcount(self):
        return self._cur.rowcount

    @staticmethod
    def _wrap(row):
        if row is None:
            return None
        return Row({key: row[key] for key in row.keys()})

    def fetchone(self):
        return self._wrap(self._cur.fetchone())

    def fetchall(self):
        return [self._wrap(r) for r in self._cur.fetchall()]

    def close(self):
        try:
            self._cur.close()
        except Exception:
            pass


def _sqlite_is_locked(exc) -> bool:
    """SQLite 写锁冲突（database is locked / busy）时可安全重试。"""
    message = str(exc).lower()
    return "locked" in message or "busy" in message


class SqliteConnection:
    """SQLite 连接包装器：线程本地使用，接口与 ConnectionWrapper 一致。

    连接以 autocommit 模式打开（isolation_level=None），单条语句立即提交；
    transaction() 外层用 ``BEGIN IMMEDIATE`` 提前获取写锁，避免 WAL 模式下
    「读事务升级写事务」失败。连接不跨线程共享，因此不需要连接池。
    """

    __slots__ = ("_conn", "_last_used", "_closed", "_pooled", "_tx_active")

    def __init__(self, path: str):
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(
            path,
            timeout=DB_BUSY_TIMEOUT_MS / 1000.0,
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._apply_pragmas()
        self._last_used = time.monotonic()
        self._closed = False
        self._pooled = False
        self._tx_active = False

    def _apply_pragmas(self):
        try:
            current = self._conn.execute("PRAGMA journal_mode").fetchone()[0]
            if str(current).lower() != "wal":
                self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            # 内存库等场景不支持 WAL，忽略即可
            pass
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute(f"PRAGMA busy_timeout={int(DB_BUSY_TIMEOUT_MS)}")
        self._conn.execute(f"PRAGMA synchronous={int(DB_SYNCHRONOUS)}")
        self._conn.execute("PRAGMA temp_store=MEMORY")

    def execute(self, sql, params=()):
        if self._closed:
            raise DatabaseError("数据库连接已关闭，不能执行查询")
        _assert_writable(sql)
        start = time.monotonic()
        attempt = 0
        while True:
            attempt += 1
            try:
                cursor = self._conn.execute(sql, tuple(params or ()))
                break
            except sqlite3.OperationalError as exc:
                # 显式事务内不重试（可能已有部分语句成功），由事务级重试兜底
                if (not self._tx_active and _sqlite_is_locked(exc)
                        and attempt <= _DEADLOCK_RETRY_LIMIT):
                    _logger.warning(
                        "SQLite 数据库被锁定，第 %d 次重试: %s", attempt, sql[:120]
                    )
                    time.sleep(0.02 * attempt)
                    continue
                raise
        self._last_used = time.monotonic()
        _log_slow_query(sql, (time.monotonic() - start) * 1000)
        return SqliteCursorWrapper(cursor)

    def begin(self):
        if self._conn.in_transaction:
            # 已有未结束的事务：复用，不重复 BEGIN
            self._tx_active = True
            return
        self._conn.execute("BEGIN IMMEDIATE")
        self._tx_active = True
        self._last_used = time.monotonic()

    def commit(self):
        self._conn.commit()
        self._tx_active = False
        self._last_used = time.monotonic()

    def rollback(self):
        try:
            self._conn.rollback()
        except sqlite3.Error:
            pass
        self._tx_active = False
        self._last_used = time.monotonic()

    def close(self):
        self._closed = True
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def raw(self):
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
    if IS_SQLITE:
        conn = SqliteConnection(DATABASE_PATH)
    else:
        conn = _POOL.checkout()
    with _all_lock:
        _all_wrappers.add(conn)
    return conn


def _discard_connection(conn):
    """丢弃一个连接（SQLite 直接关闭；MySQL 关闭并归还池计数）。"""
    if conn is None:
        return
    with _all_lock:
        _all_wrappers.discard(conn)
    if IS_SQLITE:
        conn.close()
    else:
        _POOL._discard(conn)


def _release_connection(conn):
    if conn is None:
        return
    with _all_lock:
        _all_wrappers.discard(conn)
    if IS_SQLITE:
        # SQLite 连接不进入池：直接关闭，下次使用时重新打开
        conn.close()
    else:
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
            _discard_connection(conn)
        holder.conn = _checkout()
        return holder.conn

    conn = getattr(_local, "conn", None)
    if conn is not None and not conn._closed:
        return conn
    if conn is not None:
        _discard_connection(conn)
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
        if IS_SQLITE:
            conn.close()
        else:
            _POOL._discard(conn)
    if not IS_SQLITE:
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
            if IS_SQLITE:
                if _sqlite_is_locked(exc) and attempt < retries:
                    _logger.warning(
                        "事务执行遇到数据库锁定，第 %d 次整体重试", attempt
                    )
                    time.sleep(0.05 * attempt)
                    continue
                raise
            code = exc.args[0] if exc.args else None
            if code in (1213, 1205) and attempt < retries:
                _logger.warning(
                    "事务执行遇到%s，第 %d 次整体重试",
                    "死锁" if code == 1213 else "锁等待超时", attempt,
                )
                time.sleep(0.01 * attempt)
                continue
            raise


# SQLite 模式的命名锁：进程内互斥（SQLite 模式请保持 WORKERS=1，
# 多进程部署请改用 MySQL）。记录「线程 → 已持有的锁」，保证成对释放。
_sqlite_named_locks: dict = {}
_sqlite_named_locks_guard = threading.Lock()
_sqlite_held_locks = threading.local()


def acquire_named_lock(conn, name: str, timeout: int) -> bool:
    """获取命名互斥锁（用于「读-改-写」原子化），失败返回 False。

    MySQL 使用 GET_LOCK（跨连接/多 worker 生效），SQLite 使用进程内
    threading.Lock（单进程内生效）。必须与 release_named_lock 成对使用，
    异常路径也要释放。
    """
    if IS_SQLITE:
        with _sqlite_named_locks_guard:
            lock = _sqlite_named_locks.setdefault(name, threading.Lock())
        if not lock.acquire(timeout=timeout):
            return False
        held = getattr(_sqlite_held_locks, "names", None)
        if held is None:
            held = set()
            _sqlite_held_locks.names = held
        held.add(name)
        return True
    row = conn.execute(
        "SELECT GET_LOCK(%s, %s) AS locked", (name, timeout)
    ).fetchone()
    return row is not None and row["locked"] == 1


def release_named_lock(conn, name: str):
    if IS_SQLITE:
        held = getattr(_sqlite_held_locks, "names", None)
        if held and name in held:
            held.discard(name)
            with _sqlite_named_locks_guard:
                lock = _sqlite_named_locks.get(name)
            if lock is not None:
                try:
                    lock.release()
                except RuntimeError:
                    pass
        return
    conn.execute("SELECT RELEASE_LOCK(%s)", (name,))


# ── 方言助手：业务模块只调用这些函数，不写死某一后端的语法 ───────────


def upsert_suffix(mysql_clause: str, sqlite_clause: str) -> str:
    """返回 UPSERT 子句（含前导空格）。

    用法：``"INSERT INTO ... VALUES (?,?) " + upsert_suffix(
        "ON DUPLICATE KEY UPDATE value = VALUES(value)",
        "ON CONFLICT(`key`) DO UPDATE SET value = excluded.value",
    )``
    """
    return f" {sqlite_clause}" if IS_SQLITE else f" {mysql_clause}"


def random_unit_expr() -> str:
    """返回 (0, 1] 区间的均匀随机浮点表达式（用于加权随机抽样）。"""
    if IS_SQLITE:
        return "((abs(random()) % 1000000007 + 1) / 1000000008.0)"
    return "((FLOOR(RAND() * 1000000007) + 1) / 1000000008.0)"


def least_expr(a: str, b: str) -> str:
    """返回两者取小的 SQL 表达式（MySQL 用 LEAST，SQLite 用标量 MIN）。"""
    return f"MIN({a}, {b})" if IS_SQLITE else f"LEAST({a}, {b})"


def greatest_expr(a: str, b: str) -> str:
    """返回两者取大的 SQL 表达式（MySQL 用 GREATEST，SQLite 用标量 MAX）。"""
    return f"MAX({a}, {b})" if IS_SQLITE else f"GREATEST({a}, {b})"


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


# ── SQLite 表结构（与上面的 MySQL DDL 保持同列同索引） ────────────────

_SQLITE_SCHEMA_DDL = [
    """
    CREATE TABLE IF NOT EXISTS images (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        filename        TEXT NOT NULL,
        thumb_filename  TEXT NOT NULL DEFAULT '',
        original_size   INTEGER NOT NULL DEFAULT 0,
        compressed_size INTEGER NOT NULL DEFAULT 0,
        width           INTEGER NOT NULL DEFAULT 0,
        height          INTEGER NOT NULL DEFAULT 0,
        description     TEXT NULL DEFAULT '',
        title           TEXT NOT NULL DEFAULT '',
        author          TEXT NULL DEFAULT NULL,
        content_md5     TEXT NOT NULL DEFAULT '',
        ip              TEXT NOT NULL DEFAULT '',
        ua              TEXT NULL DEFAULT '',
        ua_hash         TEXT NOT NULL DEFAULT '',
        province        TEXT NOT NULL DEFAULT '',
        city            TEXT NOT NULL DEFAULT '',
        region          TEXT NOT NULL DEFAULT '',
        status          TEXT NOT NULL DEFAULT 'pending',
        reject_reason   TEXT NULL DEFAULT NULL,
        admin_edited    INTEGER NOT NULL DEFAULT 0,
        created_at      TEXT NOT NULL,
        reviewed_at     TEXT NULL DEFAULT NULL,
        views           INTEGER NOT NULL DEFAULT 0,
        like_count      INTEGER NOT NULL DEFAULT 0,
        is_duplicate    INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS likes (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        image_id    INTEGER NOT NULL,
        ip          TEXT NOT NULL,
        ua_hash     TEXT NOT NULL,
        province    TEXT NOT NULL DEFAULT '',
        city        TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL,
        FOREIGN KEY (image_id) REFERENCES images(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ban_list (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        target      TEXT NOT NULL,
        type        TEXT NOT NULL,
        reason      TEXT NOT NULL DEFAULT '',
        admin_id    INTEGER NOT NULL,
        created_at  TEXT NOT NULL,
        is_active   INTEGER NOT NULL DEFAULT 1
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS admins (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        username        TEXT NOT NULL UNIQUE,
        password_hash   TEXT NOT NULL,
        login_attempts  INTEGER NOT NULL DEFAULT 0,
        locked_until    TEXT NULL DEFAULT NULL,
        created_at      TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        session_id   TEXT PRIMARY KEY,
        admin_id     INTEGER NOT NULL,
        username     TEXT NOT NULL,
        created_at   TEXT NOT NULL,
        expires_at   TEXT NOT NULL,
        csrf_token   TEXT NOT NULL DEFAULT '',
        last_seen_at TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS login_rate_limits (
        `key`         TEXT PRIMARY KEY,
        attempts      INTEGER NOT NULL DEFAULT 0,
        window_start  TEXT NOT NULL,
        updated_at    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS settings (
        `key`    TEXT PRIMARY KEY,
        value    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS upload_reservations (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ip          TEXT NOT NULL,
        ua_hash     TEXT NOT NULL,
        created_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pages (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        slug        TEXT NOT NULL UNIQUE,
        title       TEXT NOT NULL,
        content     TEXT NULL DEFAULT '',
        sort_order  INTEGER NOT NULL DEFAULT 0,
        is_visible  INTEGER NOT NULL DEFAULT 1,
        updated_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS friend_links (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT NOT NULL,
        url         TEXT NOT NULL,
        sort_order  INTEGER NOT NULL DEFAULT 0,
        is_visible  INTEGER NOT NULL DEFAULT 1,
        created_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_stats (
        `date`          TEXT PRIMARY KEY,
        upload_count    INTEGER NOT NULL DEFAULT 0,
        approved_count  INTEGER NOT NULL DEFAULT 0,
        rejected_count  INTEGER NOT NULL DEFAULT 0,
        like_count      INTEGER NOT NULL DEFAULT 0,
        view_count      INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS admin_logs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        admin_id    INTEGER NOT NULL,
        admin_name  TEXT NOT NULL,
        action      TEXT NOT NULL,
        detail      TEXT NULL DEFAULT '',
        target      TEXT NOT NULL DEFAULT '',
        ip          TEXT NOT NULL DEFAULT '',
        location    TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS view_sessions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        image_id    INTEGER NOT NULL,
        ip          TEXT NOT NULL,
        viewed_at   TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cooling (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        ip                  TEXT NOT NULL UNIQUE,
        reject_count        INTEGER NOT NULL DEFAULT 0,
        cooldown_multiplier INTEGER NOT NULL DEFAULT 1,
        updated_at          TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version     INTEGER PRIMARY KEY,
        applied_at  TEXT NOT NULL
    )
    """,
]

_SQLITE_INDEX_DDL = [
    "CREATE INDEX IF NOT EXISTS idx_images_status ON images(status)",
    "CREATE INDEX IF NOT EXISTS idx_images_reviewed_at ON images(reviewed_at)",
    "CREATE INDEX IF NOT EXISTS idx_images_like_count ON images(like_count)",
    "CREATE INDEX IF NOT EXISTS idx_images_content_md5 ON images(content_md5)",
    "CREATE INDEX IF NOT EXISTS idx_images_created_at ON images(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_images_ip ON images(ip)",
    "CREATE INDEX IF NOT EXISTS idx_images_ua_hash ON images(ua_hash)",
    "CREATE INDEX IF NOT EXISTS idx_images_filename ON images(filename)",
    "CREATE INDEX IF NOT EXISTS idx_images_thumb_filename ON images(thumb_filename)",
    "CREATE INDEX IF NOT EXISTS idx_images_status_reviewed ON images(status, reviewed_at)",
    "CREATE INDEX IF NOT EXISTS idx_images_status_created ON images(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_images_status_like ON images(status, like_count)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_likes_fingerprint ON likes(image_id, ip, ua_hash)",
    "CREATE INDEX IF NOT EXISTS idx_likes_created_at ON likes(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_likes_image_id ON likes(image_id)",
    "CREATE INDEX IF NOT EXISTS idx_likes_ip ON likes(ip)",
    "CREATE INDEX IF NOT EXISTS idx_likes_province ON likes(province)",
    "CREATE INDEX IF NOT EXISTS idx_likes_image_created ON likes(image_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_ban_list_target ON ban_list(target)",
    "CREATE INDEX IF NOT EXISTS idx_ban_list_type ON ban_list(type)",
    "CREATE INDEX IF NOT EXISTS idx_admin_logs_created_at ON admin_logs(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_admin_logs_ip ON admin_logs(ip)",
    "CREATE INDEX IF NOT EXISTS idx_admin_logs_action ON admin_logs(action)",
    "CREATE INDEX IF NOT EXISTS idx_admin_logs_action_created ON admin_logs(action, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_login_rate_limits_window ON login_rate_limits(window_start)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_view_sessions_unique ON view_sessions(image_id, ip)",
    "CREATE INDEX IF NOT EXISTS idx_view_sessions_lookup ON view_sessions(image_id, ip, viewed_at)",
    "CREATE INDEX IF NOT EXISTS idx_upload_reservations_ip ON upload_reservations(ip, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_upload_reservations_created ON upload_reservations(created_at)",
]

# 老版本 SQLite 库补列：1.1.x 及更早版本的库可能缺少这些列。
# 必须在建索引之前执行（否则 CREATE INDEX 会因列不存在失败）。
_SQLITE_ADD_COLUMN_SPECS = {
    "images": [
        ("content_md5", "content_md5 TEXT NOT NULL DEFAULT ''"),
        ("ip", "ip TEXT NOT NULL DEFAULT ''"),
        ("ua", "ua TEXT DEFAULT ''"),
        ("ua_hash", "ua_hash TEXT NOT NULL DEFAULT ''"),
        ("province", "province TEXT DEFAULT ''"),
        ("city", "city TEXT DEFAULT ''"),
        ("title", "title TEXT DEFAULT ''"),
        ("region", "region TEXT DEFAULT ''"),
        ("status", "status TEXT DEFAULT 'pending'"),
        ("reject_reason", "reject_reason TEXT DEFAULT NULL"),
        ("admin_edited", "admin_edited INTEGER DEFAULT 0"),
        ("reviewed_at", "reviewed_at TEXT DEFAULT NULL"),
        ("views", "views INTEGER DEFAULT 0"),
        ("like_count", "like_count INTEGER DEFAULT 0"),
        ("is_duplicate", "is_duplicate INTEGER DEFAULT 0"),
    ],
    "admin_logs": [
        ("ip", "ip TEXT DEFAULT ''"),
        ("location", "location TEXT DEFAULT ''"),
    ],
    "sessions": [
        ("csrf_token", "csrf_token TEXT DEFAULT ''"),
        ("last_seen_at", "last_seen_at TEXT DEFAULT ''"),
    ],
    "likes": [
        ("province", "province TEXT DEFAULT ''"),
        ("city", "city TEXT DEFAULT ''"),
    ],
}


def _sqlite_add_column(conn, table, col_def):
    """为已存在的表补列；列已存在时忽略（多 worker 并发启动也安全）。"""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")
    except DatabaseError as exc:
        if "duplicate column" not in str(exc).lower():
            raise


def _validate_database_name(name: str):
    if not name or not _IDENTIFIER_RE.match(name):
        raise DatabaseError(f"MySQL 数据库名非法：{name!r}（仅允许字母数字与 _ $）")


def _ensure_database():
    """确保目标数据库可用：MySQL 建库；SQLite 建目录。"""
    if IS_SQLITE:
        parent = os.path.dirname(os.path.abspath(DATABASE_PATH))
        if parent:
            os.makedirs(parent, exist_ok=True)
        return
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
    try:
        rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    except DatabaseError:
        # 更早期的库没有版本表
        return set()
    return {int(r[0]) for r in rows}


def _record_version(conn, version):
    if IS_SQLITE:
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        return
    conn.execute(
        "INSERT IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        (version, datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )


def _init_db_once_mysql():
    _ensure_database()
    conn = get_db()
    for ddl in _SCHEMA_DDL:
        conn.execute(ddl)
    with transaction():
        for version in (4, 5, 6, SCHEMA_VERSION):
            _record_version(conn, version)


def _init_db_once_sqlite():
    """SQLite 建表 + 老库补列 + 建索引 + 版本记录（全部幂等）。

    执行顺序固定为「建表 → 补列 → 建索引 → 记录版本」：
    老版本库缺列时必须先补列，否则 CREATE INDEX 会失败。
    """
    _ensure_database()
    conn = get_db()
    for ddl in _SQLITE_SCHEMA_DDL:
        conn.execute(ddl)
    for table, specs in _SQLITE_ADD_COLUMN_SPECS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for col_name, col_def in specs:
            if col_name not in existing:
                _sqlite_add_column(conn, table, col_def)
    # 旧会话没有活动时间时用创建时间回填，避免升级后立即全部失效
    try:
        conn.execute(
            "UPDATE sessions SET last_seen_at=created_at "
            "WHERE last_seen_at='' OR last_seen_at IS NULL"
        )
    except DatabaseError:
        pass
    for ddl in _SQLITE_INDEX_DDL:
        conn.execute(ddl)
    with transaction():
        for version in (4, 5, 6, SCHEMA_VERSION):
            _record_version(conn, version)


def _init_db_once():
    """按当前后端执行一次幂等初始化（供 init_db 重试调用）。"""
    if IS_SQLITE:
        _init_db_once_sqlite()
    else:
        _init_db_once_mysql()


def init_db(max_wait_seconds=60.0):
    """初始化数据库表结构；启动阶段连接不可用时重试至多 max_wait_seconds 秒。

    多 worker 同时启动时 DDL 幂等，短暂锁冲突以指数退避重试消化；
    真正的连接错误在超时后抛出。
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
                for kw in ("lock", "busy", "timeout", "timed out", "gone away",
                           "refused", "can't connect")
            )
            if not transient or time.monotonic() >= deadline:
                raise
            _logger.warning("数据库暂不可用（%s），%.1f 秒后重试...", exc, delay)
            close_db()
            time.sleep(delay)
            delay = min(delay * 2, 2.0)


def get_db_status() -> dict:
    """数据库健康信息（check_db 工具使用）：按后端返回各自的诊断字段。"""
    conn = get_db()
    if IS_SQLITE:
        def _pragma(name):
            row = conn.execute(f"PRAGMA {name}").fetchone()
            return row[0] if row is not None else None

        return {
            "db_type": "sqlite",
            "database_path": DATABASE_PATH,
            "sqlite_version": sqlite3.sqlite_version,
            "journal_mode": _pragma("journal_mode"),
            "synchronous": _pragma("synchronous"),
            "busy_timeout": _pragma("busy_timeout"),
            "foreign_keys": _pragma("foreign_keys"),
            "table_count": len(_existing_sqlite_tables(conn)),
        }
    version = conn.execute("SELECT VERSION() AS v").fetchone()["v"]
    charset_row = conn.execute(
        "SELECT @@character_set_database AS cs, @@collation_database AS co"
    ).fetchone()
    max_conn = conn.execute("SELECT @@max_connections AS mc").fetchone()["mc"]
    threads = conn.execute("SHOW STATUS LIKE 'Threads_connected'").fetchall()
    return {
        "db_type": "mysql",
        "server_version": version,
        "charset": charset_row["cs"],
        "collation": charset_row["co"],
        "max_connections": max_conn,
        "threads_connected": threads[0]["Value"] if threads else None,
        "pool_idle": _POOL.idle_count(),
    }


def _existing_sqlite_tables(conn) -> set:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {r["name"] for r in rows}
