"""SQLite 连接、初始化与迁移管理。

第四阶段起：
- 所有连接参数（WAL、busy_timeout、foreign_keys、synchronous、temp_store、
  wal_autocheckpoint）集中在本模块统一设置，业务代码不得自行调整；
- 连接按线程隔离（threading.local），SQLite 连接不跨线程共享；
- 提供 transaction() 上下文管理器保证多条 SQL 的原子提交/回滚；
- 提供 schema_migrations 版本表与幂等迁移；
- 提供 WAL checkpoint 与 PRAGMA 诊断入口，供运维工具调用（不在请求路径执行）。
"""
import contextlib
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from app.config import (
    BACKUP_DIR,
    DATABASE_PATH,
    DB_BACKUP_BEFORE_MIGRATION,
    DB_BUSY_TIMEOUT_MS,
    DB_SYNCHRONOUS,
    DB_WAL_AUTOCHECKPOINT_PAGES,
    DB_SLOW_QUERY_MS,
)

# 当前 schema 版本：前三阶段的表结构在版本 4 被首次纳入版本管理；
# 版本 5（第四阶段）：view_sessions 唯一索引、WAL/事务相关结构收敛；
# 版本 6（第四阶段）：按实测查询计划补充两个复合索引（见 _migrate_to_v6）。
# 今后每次结构变更递增该值，并在 _init_db_once 中实现幂等迁移。
SCHEMA_VERSION = 6

_logger = logging.getLogger(__name__)
_local = threading.local()
# 记录所有线程创建的连接，保证退出时能把线程池中的连接全部关闭，
# 否则 Windows 下残留句柄会阻塞数据库文件替换（备份恢复需要）。
_conn_lock = threading.Lock()
_all_conns = set()


class _TimedConnection(sqlite3.Connection):
    """带慢查询诊断的连接；只记录耗时，不记录参数，避免泄露敏感数据。"""

    def execute(self, sql, parameters=()):
        if DB_SLOW_QUERY_MS <= 0:
            return super().execute(sql, parameters)
        start = time.monotonic()
        try:
            return super().execute(sql, parameters)
        finally:
            self._log_if_slow(sql, start)

    def executemany(self, sql, seq_of_parameters):
        if DB_SLOW_QUERY_MS <= 0:
            return super().executemany(sql, seq_of_parameters)
        start = time.monotonic()
        try:
            return super().executemany(sql, seq_of_parameters)
        finally:
            self._log_if_slow(sql, start)

    def executescript(self, sql_script):
        if DB_SLOW_QUERY_MS <= 0:
            return super().executescript(sql_script)
        start = time.monotonic()
        try:
            return super().executescript(sql_script)
        finally:
            self._log_if_slow(sql_script, start)

    @staticmethod
    def _log_if_slow(sql, start):
        elapsed_ms = (time.monotonic() - start) * 1000
        if elapsed_ms >= DB_SLOW_QUERY_MS:
            head = " ".join(str(sql).split())[:120]
            _logger.warning("慢查询 %.0fms: %s", elapsed_ms, head)


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    """统一连接 PRAGMA；每个新连接只执行一次。"""
    # WAL：多读单写、读写不互斥。journal_mode 是数据库文件的持久属性，
    # 只有真正需要切换时才写（切换需要写锁，多 worker 冷启动时避免无谓争锁）。
    try:
        current = conn.execute("PRAGMA journal_mode").fetchone()[0]
    except sqlite3.Error:
        current = None
    if str(current).lower() != "wal":
        conn.execute("PRAGMA journal_mode=WAL")
    # 外键约束：likes.image_id 依赖 images(id) ON DELETE CASCADE
    conn.execute("PRAGMA foreign_keys=ON")
    # 锁等待上限：超时抛出 OperationalError，由调用方决定重试或报错
    conn.execute(f"PRAGMA busy_timeout={int(DB_BUSY_TIMEOUT_MS)}")
    # 持久性：默认 FULL（WAL 下每次提交 fsync WAL），不静默降低保障
    conn.execute(f"PRAGMA synchronous={DB_SYNCHRONOUS}")
    # 临时表与排序器使用内存，减少磁盘临时文件
    conn.execute("PRAGMA temp_store=MEMORY")
    if DB_WAL_AUTOCHECKPOINT_PAGES > 0:
        conn.execute(
            f"PRAGMA wal_autocheckpoint={int(DB_WAL_AUTOCHECKPOINT_PAGES)}"
        )


def _connect() -> sqlite3.Connection:
    # TEST 场景会替换 DATABASE_PATH，因此这里读取模块级变量而不是配置常量
    path = DATABASE_PATH
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(
        path,
        timeout=DB_BUSY_TIMEOUT_MS / 1000.0,
        check_same_thread=False,
        factory=_TimedConnection,
    )
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn)
    with _conn_lock:
        _all_conns.add(conn)
    return conn


def get_db() -> sqlite3.Connection:
    """获取当前线程独占的数据库连接（不存在则创建）。

    连接保存在 threading.local 中，不会跨线程共享；工作线程复用以避免
    每个请求重新建连，线程结束或显式 close_db() 时释放。

    这里用连接注册表判断连接是否已被 close_all_db() 关闭，而不是每次执行
    ``SELECT 1`` 探活：后者会让每个模型函数都多一次 SQL 往返（前台单页
    可累积近十条），而注册表检查是内存操作且同样能发现已关闭连接。
    """
    conn = getattr(_local, "conn", None)
    if conn is not None:
        with _conn_lock:
            alive = conn in _all_conns
        if alive:
            return conn
        _local.conn = None
    _local.conn = _connect()
    return _local.conn


def _close_conn(conn):
    try:
        conn.close()
    except sqlite3.Error:
        pass
    with _conn_lock:
        _all_conns.discard(conn)


def close_db():
    """关闭当前线程的数据库连接。"""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        _close_conn(conn)
        _local.conn = None


def close_all_db():
    """关闭所有线程创建的连接（进程退出/数据库替换前调用）。

    只有本进程创建的连接会被关闭；其他进程的连接由 busy_timeout 与
    恢复工具的独占检测负责。
    """
    with _conn_lock:
        conns = list(_all_conns)
        _all_conns.clear()
    for conn in conns:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    if getattr(_local, "conn", None) is not None:
        _local.conn = None


@contextlib.contextmanager
def transaction():
    """显式事务上下文：正常结束统一提交，异常统一回滚并向上抛出。

    用于必须原子完成的多语句业务（如点赞：插入点赞行 + 计数 + 日统计）。
    嵌套使用时通过 SAVEPOINT 实现，内层回滚不影响外层已完成的修改。
    """
    conn = get_db()
    depth = getattr(_local, "tx_depth", 0)
    _local.tx_depth = depth + 1
    savepoint = f"sp_{depth}_{threading.get_ident()}"
    if depth > 0:
        conn.execute(f"SAVEPOINT {savepoint}")
    try:
        yield conn
        if depth > 0:
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        else:
            conn.commit()
    except Exception:
        if depth > 0:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        else:
            conn.rollback()
        raise
    finally:
        _local.tx_depth = depth


def checkpoint_wal(mode: str = "PASSIVE") -> dict:
    """执行一次 WAL checkpoint（运维/备份工具调用，禁止在请求路径频繁执行）。"""
    allowed = {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}
    if mode not in allowed:
        raise ValueError(f"非法 checkpoint 模式: {mode}")
    conn = get_db()
    busy, log_frames, checkpointed = conn.execute(
        f"PRAGMA wal_checkpoint({mode})"
    ).fetchone()
    result = {"busy": busy, "log_frames": log_frames, "checkpointed": checkpointed}
    if busy:
        _logger.warning("WAL checkpoint 未完成（有并发读写）: %s", result)
    return result


def get_pragma_status() -> dict:
    """只读返回当前连接的关键 PRAGMA 状态，供健康检查工具使用。"""
    conn = get_db()

    def scalar(pragma):
        row = conn.execute(f"PRAGMA {pragma}").fetchone()
        return row[0] if row is not None else None

    return {
        "sqlite_version": sqlite3.sqlite_version,
        "journal_mode": scalar("journal_mode"),
        "synchronous": scalar("synchronous"),
        "busy_timeout": scalar("busy_timeout"),
        "foreign_keys": scalar("foreign_keys"),
        "temp_store": scalar("temp_store"),
        "wal_autocheckpoint": scalar("wal_autocheckpoint"),
        "user_version": scalar("user_version"),
    }


def _add_col_safe(conn, table, col_def):
    """安全地为表添加列，忽略列已存在的错误。

    col_def 形如 "ip TEXT NOT NULL DEFAULT ''"，即 ADD COLUMN 之后的完整定义。
    """
    col_name = col_def.split()[0]
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")
    except sqlite3.OperationalError:
        # 多 worker 并发启动或列已存在时均忽略
        pass


def _applied_versions(conn) -> set:
    try:
        rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    except sqlite3.OperationalError:
        return set()
    return {int(r[0]) for r in rows}


def _record_version(conn, version):
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        (version, datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )


def _snapshot_before_migration(conn) -> str | None:
    """迁移前把现有数据库快照到备份目录（使用 SQLite 备份 API，WAL 下一致）。

    返回备份文件路径；跳过备份时返回 None。备份失败直接抛出，阻止在不完整
    的数据库上继续执行结构变更。
    """
    if not DB_BACKUP_BEFORE_MIGRATION:
        return None
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = os.path.join(BACKUP_DIR, f"pre_migration_{stamp}_{os.getpid()}.db")
        dest = sqlite3.connect(target)
        try:
            conn.backup(dest)
        finally:
            dest.close()
        _logger.info("迁移前备份完成: %s", target)
        return target
    except (sqlite3.Error, OSError) as exc:
        _logger.error("迁移前备份失败，已中止数据库迁移: %s", exc)
        raise


def _migrate_to_v5(conn):
    """版本 5：浏览去重表收敛为 (image_id, ip) 唯一，防止并发重复计数。

    历史数据中同一 image_id+ip 可能已有重复行，先保留最新一条再建唯一索引。
    """
    conn.execute(
        "DELETE FROM view_sessions WHERE id NOT IN ("
        "SELECT MAX(id) FROM view_sessions GROUP BY image_id, ip)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_view_sessions_unique "
        "ON view_sessions(image_id, ip)"
    )


def _migrate_to_v6(conn):
    """版本 6：按实测查询计划补充两个复合索引。

    1) idx_images_status_like (status, like_count)
       「已通过 + 按点赞排序」是图库热度排序与后台排序的固定组合。旧计划
       只能借 idx_images_status_created 过滤后再 TEMP B-TREE 排序；
       在 3 万条 images 的合成库实测 20.01ms → 0.13ms。
    2) idx_admin_logs_action_created (action, created_at)
       后台日志按操作类型筛选并倒序分页。旧计划用 idx_admin_logs_action
       过滤后仍需 TEMP B-TREE 排序；实测 5.82ms → 0.18ms。

    实测中另外三个候选索引（ip+created_at、ua_hash+created_at、
    content_md5+ip+created_at）对应用查询原本就已是毫秒级以内，收益不足
    1ms 却要多占约 7MB 空间与写放大，因此不引入。
    """
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_images_status_like "
        "ON images(status, like_count)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_admin_logs_action_created "
        "ON admin_logs(action, created_at)"
    )


def init_db(max_wait_seconds=60.0):
    """初始化数据库表结构；多 worker 同时启动时串行重试，避免启动失败。

    实测多进程同时执行 DDL 时，SQLite 会对部分连接返回 database is locked，
    导致 uvicorn 的部分 worker 启动失败。初始化逻辑本身是幂等的，因此对锁冲突
    做指数退避重试；非锁错误仍然直接抛出。
    """
    deadline = time.monotonic() + max_wait_seconds
    delay = 0.1
    while True:
        try:
            _init_db_once()
            return
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "locked" not in message and "busy" not in message:
                raise
            # 丢弃可能仍处于事务/持锁状态的连接，下一轮重新打开
            close_db()
            if time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 2.0)


def _init_db_once():
    """初始化数据库表结构

    注意：为兼容老版本数据库（缺少 ip/ua_hash 等列），执行顺序必须严格为
    「建表 → 补列迁移 → 建索引 → 记录 schema 版本」，否则 CREATE INDEX
    会因列不存在而报错。
    """
    conn = get_db()

    # 已有数据库且结构版本落后时，先做一次一致性快照，再执行任何 DDL/迁移。
    # 新建数据库（无任何表）不需要快照。
    existing_tables = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='images'"
    ).fetchone()[0]
    pending_migration = False
    if existing_tables:
        applied = _applied_versions(conn)
        # 版本表中没有记录（前三阶段的历史库）也视为需要迁移
        pending_migration = SCHEMA_VERSION not in applied
        if pending_migration:
            _snapshot_before_migration(conn)

    # ── 第一阶段：仅建表（不含索引） ──────────────────────────────────
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS images (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            filename        TEXT NOT NULL,
            thumb_filename  TEXT NOT NULL,
            original_size   INTEGER NOT NULL,
            compressed_size INTEGER NOT NULL,
            width           INTEGER NOT NULL,
            height          INTEGER NOT NULL,
            description     TEXT DEFAULT '',
            title           TEXT DEFAULT '',
            author          TEXT DEFAULT NULL,
            content_md5     TEXT NOT NULL,
            ip              TEXT NOT NULL,
            ua              TEXT DEFAULT '',
            ua_hash         TEXT NOT NULL,
            province        TEXT DEFAULT '',
            city            TEXT DEFAULT '',
            region          TEXT DEFAULT '',
            status          TEXT DEFAULT 'pending',
            reject_reason   TEXT DEFAULT NULL,
            admin_edited    INTEGER DEFAULT 0,
            created_at      TEXT NOT NULL,
            reviewed_at     TEXT DEFAULT NULL,
            views           INTEGER DEFAULT 0,
            like_count      INTEGER DEFAULT 0,
            is_duplicate    INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS likes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            image_id    INTEGER NOT NULL,
            ip          TEXT NOT NULL,
            ua_hash     TEXT NOT NULL,
            province    TEXT DEFAULT '',
            city        TEXT DEFAULT '',
            created_at  TEXT NOT NULL,
            FOREIGN KEY (image_id) REFERENCES images(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS ban_list (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            target      TEXT NOT NULL,
            type        TEXT NOT NULL,
            reason      TEXT NOT NULL,
            admin_id    INTEGER NOT NULL,
            created_at  TEXT NOT NULL,
            is_active   INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS admins (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            username        TEXT NOT NULL UNIQUE,
            password_hash   TEXT NOT NULL,
            login_attempts  INTEGER DEFAULT 0,
            locked_until    TEXT DEFAULT NULL,
            created_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sessions (
            session_id  TEXT PRIMARY KEY,
            admin_id    INTEGER NOT NULL,
            username    TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            expires_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS login_rate_limits (
            key          TEXT PRIMARY KEY,
            attempts     INTEGER NOT NULL DEFAULT 0,
            window_start TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settings (
            key     TEXT PRIMARY KEY,
            value   TEXT NOT NULL
        );

        -- 上传额度预约表：在“限额检查 + 额度占用”原子化事务中使用，
        -- 记录尚未完成入库的上传请求（入库或失败后即删除该行）
        CREATE TABLE IF NOT EXISTS upload_reservations (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ip          TEXT NOT NULL,
            ua_hash     TEXT NOT NULL,
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            slug        TEXT NOT NULL UNIQUE,
            title       TEXT NOT NULL,
            content     TEXT DEFAULT '',
            sort_order  INTEGER DEFAULT 0,
            is_visible  INTEGER DEFAULT 1,
            updated_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS friend_links (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            url         TEXT NOT NULL,
            sort_order  INTEGER DEFAULT 0,
            is_visible  INTEGER DEFAULT 1,
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS daily_stats (
            date            TEXT PRIMARY KEY,
            upload_count    INTEGER DEFAULT 0,
            approved_count  INTEGER DEFAULT 0,
            rejected_count  INTEGER DEFAULT 0,
            like_count      INTEGER DEFAULT 0,
            view_count      INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS admin_logs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id    INTEGER NOT NULL,
            admin_name  TEXT NOT NULL,
            action      TEXT NOT NULL,
            detail      TEXT DEFAULT '',
            target      TEXT DEFAULT '',
            ip          TEXT DEFAULT '',
            location    TEXT DEFAULT '',
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS view_sessions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            image_id    INTEGER NOT NULL,
            ip          TEXT NOT NULL,
            viewed_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS cooling (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ip          TEXT NOT NULL UNIQUE,
            reject_count INTEGER DEFAULT 0,
            cooldown_multiplier INTEGER DEFAULT 1,
            updated_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS schema_migrations (
            version     INTEGER PRIMARY KEY,
            applied_at  TEXT NOT NULL
        );
    """)

    # ── 第二阶段：补列迁移（必须在建索引之前完成） ──────────────────────

    # images 表：兼容更早期版本中缺失的基础字段
    img_cols = {r["name"] for r in conn.execute("PRAGMA table_info(images)").fetchall()}
    img_missing = [
        # (列名, ADD COLUMN 定义)
        ("content_md5", "content_md5 TEXT NOT NULL DEFAULT ''"),
        ("ip",          "ip TEXT NOT NULL DEFAULT ''"),
        ("ua",          "ua TEXT DEFAULT ''"),
        ("ua_hash",     "ua_hash TEXT NOT NULL DEFAULT ''"),
        ("province",    "province TEXT DEFAULT ''"),
        ("city",        "city TEXT DEFAULT ''"),
        ("title",       "title TEXT DEFAULT ''"),
        ("region",      "region TEXT DEFAULT ''"),
        ("status",      "status TEXT DEFAULT 'pending'"),
        ("reject_reason", "reject_reason TEXT DEFAULT NULL"),
        ("admin_edited",  "admin_edited INTEGER DEFAULT 0"),
        ("reviewed_at",   "reviewed_at TEXT DEFAULT NULL"),
        ("views",         "views INTEGER DEFAULT 0"),
        ("like_count",    "like_count INTEGER DEFAULT 0"),
        ("is_duplicate",  "is_duplicate INTEGER DEFAULT 0"),
    ]
    for name, defn in img_missing:
        if name not in img_cols:
            _add_col_safe(conn, "images", defn)

    # admin_logs 表
    log_cols = {r["name"] for r in conn.execute("PRAGMA table_info(admin_logs)").fetchall()}
    for name, defn in [
        ("ip",       "ip TEXT DEFAULT ''"),
        ("location", "location TEXT DEFAULT ''"),
    ]:
        if name not in log_cols:
            _add_col_safe(conn, "admin_logs", defn)

    # sessions 表：第二阶段新增“服务端 CSRF 令牌”和“最近活动时间”（空闲超时）
    sess_cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    if "csrf_token" not in sess_cols:
        _add_col_safe(conn, "sessions", "csrf_token TEXT DEFAULT ''")
    if "last_seen_at" not in sess_cols:
        _add_col_safe(conn, "sessions", "last_seen_at TEXT DEFAULT ''")
    # 旧会话没有活动时间时用创建时间回填，避免升级后立刻全部失效。
    # 兼容多 worker 同时启动：极少数情况下列刚由其他进程添加，忽略此处迁移失败，
    # 会话在首次访问时仍会惰性补齐 CSRF/活动时间。
    try:
        conn.execute(
            "UPDATE sessions SET last_seen_at=created_at "
            "WHERE last_seen_at='' OR last_seen_at IS NULL"
        )
    except sqlite3.OperationalError:
        pass

    conn.commit()

    # ── 第三阶段：建索引（此时所有列均已存在） ──────────────────────────
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_images_status ON images(status);
        CREATE INDEX IF NOT EXISTS idx_images_reviewed_at ON images(reviewed_at);
        CREATE INDEX IF NOT EXISTS idx_images_like_count ON images(like_count);
        CREATE INDEX IF NOT EXISTS idx_images_content_md5 ON images(content_md5);
        CREATE INDEX IF NOT EXISTS idx_images_created_at ON images(created_at);
        CREATE INDEX IF NOT EXISTS idx_images_ip ON images(ip);
        CREATE INDEX IF NOT EXISTS idx_images_ua_hash ON images(ua_hash);
        CREATE INDEX IF NOT EXISTS idx_images_filename ON images(filename);
        CREATE INDEX IF NOT EXISTS idx_images_thumb_filename ON images(thumb_filename);
        CREATE INDEX IF NOT EXISTS idx_images_status_reviewed ON images(status, reviewed_at);
        CREATE INDEX IF NOT EXISTS idx_images_status_created ON images(status, created_at);

        CREATE UNIQUE INDEX IF NOT EXISTS idx_likes_fingerprint ON likes(image_id, ip, ua_hash);
        CREATE INDEX IF NOT EXISTS idx_likes_created_at ON likes(created_at);
        CREATE INDEX IF NOT EXISTS idx_likes_image_id ON likes(image_id);
        CREATE INDEX IF NOT EXISTS idx_likes_ip ON likes(ip);
        CREATE INDEX IF NOT EXISTS idx_likes_province ON likes(province);
        CREATE INDEX IF NOT EXISTS idx_likes_image_created ON likes(image_id, created_at);

        CREATE INDEX IF NOT EXISTS idx_ban_list_target ON ban_list(target);
        CREATE INDEX IF NOT EXISTS idx_ban_list_type ON ban_list(type);

        CREATE INDEX IF NOT EXISTS idx_admin_logs_created_at ON admin_logs(created_at);
        CREATE INDEX IF NOT EXISTS idx_admin_logs_ip ON admin_logs(ip);
        CREATE INDEX IF NOT EXISTS idx_admin_logs_action ON admin_logs(action);

        CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at);
        CREATE INDEX IF NOT EXISTS idx_login_rate_limits_window ON login_rate_limits(window_start);

        CREATE INDEX IF NOT EXISTS idx_view_sessions_lookup ON view_sessions(image_id, ip, viewed_at);

        CREATE INDEX IF NOT EXISTS idx_upload_reservations_ip ON upload_reservations(ip, created_at);
        CREATE INDEX IF NOT EXISTS idx_upload_reservations_created ON upload_reservations(created_at);
    """)

    # ── 第四阶段：按版本执行结构迁移，并记录 schema 版本 ─────────────────
    # 每个版本的迁移必须是幂等的，允许多 worker 同时启动时重复执行。
    applied = _applied_versions(conn)
    if 5 not in applied:
        with transaction():
            _migrate_to_v5(conn)
            _record_version(conn, 5)
        _logger.info("数据库迁移到版本 5 完成")
    if 6 not in applied:
        with transaction():
            _migrate_to_v6(conn)
            _record_version(conn, 6)
        _logger.info("数据库迁移到版本 6 完成")
    # 兼容历史库：版本表中没有 4 的记录时补一条（结构已由上面的 DDL 保证）
    with transaction():
        _record_version(conn, 4)
        _record_version(conn, SCHEMA_VERSION)
    if pending_migration:
        _logger.info("数据库结构已升级到版本 %s", SCHEMA_VERSION)
