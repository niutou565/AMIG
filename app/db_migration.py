"""SQLite ↔ MySQL 双向数据迁移执行器（安装向导与后台「切换数据库」共用）。

安全约束（与项目既有约定一致）：
- 目标库必须为空（没有任何业务数据），绝不覆盖已有数据；
- 迁移前先对来源库做完整备份，备份失败立即中止，不碰 .env；
- 逐表复制完成后做「行数 + 首尾抽样」校验，校验失败视为迁移失败；
- 迁移失败不修改 .env，当前运行中的数据库保持不变；
- 迁移完成后只返回报告，由调用方决定何时原子写入 .env（写入后需重启生效）。

数据一致性：复制阶段在来源库开启只读快照事务（SQLite BEGIN / MySQL
REPEATABLE READ + CONSISTENT SNAPSHOT），保证复制期间业务写入不会造成
"半新半旧"的数据。
"""
import os
import re
import sqlite3
import time
from datetime import datetime, timezone

from app import config
from app.database import (
    EXPECTED_TABLES,
    _SCHEMA_DDL,
    _SQLITE_INDEX_DDL,
    _SQLITE_SCHEMA_DDL,
)

# 复制顺序：被引用的表在前（likes → images），校验/展示也按此顺序
TABLE_ORDER = (
    "images", "likes", "ban_list", "admins", "sessions", "login_rate_limits",
    "settings", "upload_reservations", "pages", "friend_links", "daily_stats",
    "admin_logs", "view_sessions", "cooling", "schema_migrations",
)

# 每张表的稳定排序主键（保证抽样校验可重复）
_PK_COLUMNS = {
    "images": "id",
    "likes": "id",
    "ban_list": "id",
    "admins": "id",
    "sessions": "session_id",
    "login_rate_limits": "`key`",
    "settings": "`key`",
    "upload_reservations": "id",
    "pages": "id",
    "friend_links": "id",
    "daily_stats": "`date`",
    "admin_logs": "id",
    "view_sessions": "id",
    "cooling": "id",
    "schema_migrations": "version",
}

BATCH_SIZE = 200
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_$]+$")


class MigrationError(Exception):
    """迁移失败（消息可直接展示给管理员，不包含密码）。"""


def _now_stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _normalize_sqlite_path(path: str) -> str:
    path = str(path or "").strip()
    if not path:
        raise MigrationError("SQLite 数据库文件路径不能为空")
    if not os.path.isabs(path):
        path = os.path.join(str(config.BASE_DIR), path)
    if os.path.isdir(path):
        raise MigrationError(f"SQLite 数据库路径是一个目录：{path}")
    return os.path.abspath(path)


def _validate_mysql_config(cfg: dict):
    for key in ("host", "database", "user"):
        if not str(cfg.get(key) or "").strip():
            raise MigrationError(f"MySQL 参数缺失：{key}")
    if not _IDENTIFIER_RE.match(str(cfg["database"])):
        raise MigrationError("MySQL 数据库名只能包含字母、数字、下划线与 $")
    try:
        int(cfg.get("port", 3306))
    except (TypeError, ValueError):
        raise MigrationError("MySQL 端口必须是整数")


def normalize_target(target: dict) -> dict:
    """规范化目标库配置（校验必填项），返回可直接用于连接/写 .env 的配置。"""
    db_type = str(target.get("db_type", "")).strip().lower()
    if db_type == "sqlite":
        return {
            "db_type": "sqlite",
            "database_path": _normalize_sqlite_path(target.get("database_path")),
        }
    if db_type == "mysql":
        cfg = {
            "db_type": "mysql",
            "host": str(target.get("host") or "127.0.0.1").strip(),
            "port": int(target.get("port") or 3306),
            "database": str(target.get("database") or "").strip(),
            "user": str(target.get("user") or "").strip(),
            "password": str(target.get("password") or ""),
            "charset": str(target.get("charset") or "utf8mb4").strip(),
            "collation": str(target.get("collation") or "utf8mb4_unicode_ci").strip(),
        }
        _validate_mysql_config(cfg)
        return cfg
    raise MigrationError("目标数据库类型仅支持 sqlite 或 mysql")


def current_source() -> dict:
    """当前运行中的数据库配置（来自 .env / app.config）。"""
    if config.IS_SQLITE:
        return {"db_type": "sqlite", "database_path": config.DATABASE_PATH}
    return {
        "db_type": "mysql",
        "host": config.MYSQL_HOST,
        "port": config.MYSQL_PORT,
        "database": config.MYSQL_DATABASE,
        "user": config.MYSQL_USER,
        "password": config.MYSQL_PASSWORD,
        "charset": config.MYSQL_CHARSET,
        "collation": config.MYSQL_COLLATION,
    }


def describe(db_cfg: dict) -> str:
    """面向界面的描述文本（绝不包含密码）。"""
    if db_cfg.get("db_type") == "sqlite":
        return f"SQLite：{db_cfg.get('database_path')}"
    return (
        f"MySQL：{db_cfg.get('user')}@{db_cfg.get('host')}:{db_cfg.get('port')}"
        f"/{db_cfg.get('database')}"
    )


# ── 连接适配器：两种后端统一的最小接口 ───────────────────────────────


class _SqliteAdapter:
    ph = "?"

    def __init__(self, path: str):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(
            path, timeout=config.DB_BUSY_TIMEOUT_MS / 1000.0, isolation_level=None
        )

    def execute(self, sql, params=()):
        return self.conn.execute(sql, tuple(params or ()))

    def executemany(self, sql, rows):
        if rows:
            self.conn.executemany(sql, rows)

    def begin_snapshot(self):
        self.conn.execute("BEGIN")

    def begin_write(self):
        self.conn.execute("BEGIN IMMEDIATE")

    def commit(self):
        self.conn.commit()

    def rollback(self):
        try:
            self.conn.rollback()
        except sqlite3.Error:
            pass

    def close(self):
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    def table_names(self) -> set:
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        return {r[0] for r in rows}

    def columns(self, table: str) -> list:
        return [r[1] for r in self.conn.execute(f"PRAGMA table_info(`{table}`)")]

    def count(self, table: str) -> int:
        return int(self.conn.execute(f"SELECT COUNT(*) FROM `{table}`").fetchone()[0])

    def stream(self, sql, params=()):
        return self.conn.execute(sql, tuple(params or ()))


class _MysqlAdapter:
    ph = "%s"

    def __init__(self, cfg: dict, with_database: bool = True):
        import pymysql

        self.cfg = cfg
        self.conn = pymysql.connect(
            host=cfg["host"],
            port=int(cfg.get("port") or 3306),
            user=cfg["user"],
            password=cfg.get("password", ""),
            database=cfg.get("database") if with_database else None,
            charset=cfg.get("charset") or "utf8mb4",
            collation=cfg.get("collation") or "utf8mb4_unicode_ci",
            autocommit=True,
            connect_timeout=10,
        )

    def execute(self, sql, params=()):
        cur = self.conn.cursor()
        cur.execute(sql, tuple(params or ()) or None)
        return cur

    def executemany(self, sql, rows):
        if rows:
            cur = self.conn.cursor()
            cur.executemany(sql, rows)
            cur.close()

    def begin_snapshot(self):
        self.conn.autocommit(False)
        with self.conn.cursor() as cur:
            cur.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")

    def begin_write(self):
        self.conn.autocommit(False)

    def commit(self):
        self.conn.commit()
        self.conn.autocommit(True)

    def rollback(self):
        try:
            self.conn.rollback()
        except Exception:
            pass
        try:
            self.conn.autocommit(True)
        except Exception:
            pass

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def table_names(self) -> set:
        rows = self.execute("SHOW TABLES").fetchall()
        return {r[0] for r in rows}

    def columns(self, table: str) -> list:
        rows = self.execute(
            "SELECT COLUMN_NAME FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s ORDER BY ORDINAL_POSITION",
            (self.cfg["database"], table),
        ).fetchall()
        return [r[0] for r in rows]

    def count(self, table: str) -> int:
        return int(self.execute(f"SELECT COUNT(*) FROM `{table}`").fetchone()[0])

    def stream(self, sql, params=()):
        cur = self.conn.cursor()
        cur.execute(sql, tuple(params or ()) or None)
        return cur


def _open_adapter(db_cfg: dict):
    if db_cfg.get("db_type") == "sqlite":
        return _SqliteAdapter(db_cfg["database_path"])
    return _MysqlAdapter(db_cfg)


def _ensure_mysql_database(cfg: dict):
    """连接到服务器并确保目标库存在（字符集与主项目一致）。"""
    import pymysql

    conn = pymysql.connect(
        host=cfg["host"],
        port=int(cfg.get("port") or 3306),
        user=cfg["user"],
        password=cfg.get("password", ""),
        charset=cfg.get("charset") or "utf8mb4",
        autocommit=True,
        connect_timeout=10,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{cfg['database']}` "
                f"CHARACTER SET {cfg.get('charset') or 'utf8mb4'} "
                f"COLLATE {cfg.get('collation') or 'utf8mb4_unicode_ci'}"
            )
    finally:
        conn.close()


def test_connection(db_cfg: dict) -> tuple:
    """测试目标库连接（安装向导与后台切换共用），返回 (ok, message)。"""
    try:
        cfg = normalize_target(db_cfg)
    except MigrationError as exc:
        return False, str(exc)
    try:
        if cfg["db_type"] == "mysql":
            _ensure_mysql_database(cfg)
            adapter = _MysqlAdapter(cfg)
        else:
            adapter = _SqliteAdapter(cfg["database_path"])
        adapter.execute("SELECT 1")
        adapter.close()
        return True, f"连接成功：{describe(cfg)}"
    except Exception as exc:
        return False, f"连接失败：{exc}"


# ── 切换锁：防止多个 worker / 多个管理员同时执行切换 ─────────────────

_SWITCH_LOCK_NAME = ".db_switch.lock"
_SWITCH_LOCK_STALE_SECONDS = 1800


def switch_lock_path() -> str:
    return os.path.join(str(config.BACKUP_DIR), _SWITCH_LOCK_NAME)


def switch_in_progress() -> bool:
    """是否有数据库切换任务正在执行（上传接口据此临时拒绝写入）。"""
    path = switch_lock_path()
    if not os.path.exists(path):
        return False
    try:
        if time.time() - os.path.getmtime(path) > _SWITCH_LOCK_STALE_SECONDS:
            os.unlink(path)
            return False
    except OSError:
        return False
    return True


def acquire_switch_lock() -> bool:
    """获取跨进程切换锁（文件独占创建），失败返回 False。"""
    path = switch_lock_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if switch_in_progress():
            return False
        # 过期锁已被清理，重试一次
        try:
            os.unlink(path)
        except OSError:
            return False
        return acquire_switch_lock()
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(f"pid={os.getpid()} at={datetime.now(timezone.utc).isoformat()}\n")
    return True


def release_switch_lock():
    try:
        os.unlink(switch_lock_path())
    except OSError:
        pass


# ── 备份 ─────────────────────────────────────────────────────────────


def _backup_dir() -> str:
    path = str(config.BACKUP_DIR)
    os.makedirs(path, exist_ok=True)
    return path


def backup_sqlite_source(path: str) -> str:
    """用 SQLite Online Backup API 生成一致性备份，返回备份文件路径。"""
    target = os.path.join(_backup_dir(), f"pre_switch_{_now_stamp()}.db")
    src = sqlite3.connect(path, timeout=config.DB_BUSY_TIMEOUT_MS / 1000.0)
    dest = sqlite3.connect(target)
    try:
        src.backup(dest)
    except sqlite3.Error as exc:
        dest.close()
        src.close()
        try:
            os.unlink(target)
        except OSError:
            pass
        raise MigrationError(f"SQLite 备份失败，已中止切换：{exc}")
    finally:
        try:
            dest.close()
            src.close()
        except sqlite3.Error:
            pass
    return target


def backup_mysql_source(cfg: dict) -> str:
    """用 mysqldump 生成一致性备份（密码经 MYSQL_PWD 传递），返回文件路径。"""
    from tools import mysql_cli

    dump_path = mysql_cli.find_mysqldump()
    if not dump_path:
        raise MigrationError(
            "未找到 mysqldump，无法在切换前备份 MySQL 数据；"
            "请安装 MySQL 客户端或设置 MYSQLDUMP_PATH 后重试"
        )
    target = os.path.join(_backup_dir(), f"pre_switch_{_now_stamp()}.sql")
    env = dict(os.environ)
    env["MYSQL_PWD"] = cfg.get("password", "")
    args = [
        f"--host={cfg['host']}",
        f"--port={int(cfg.get('port') or 3306)}",
        f"--user={cfg['user']}",
        "--default-character-set=utf8mb4",
        "--single-transaction",
        "--quick",
        "--no-tablespaces",
        f"--result-file={target}",
        str(cfg["database"]),
    ]
    proc = mysql_cli.run_client(dump_path, args, env)
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        detail = detail[-1] if detail else f"exit code {proc.returncode}"
        try:
            os.unlink(target)
        except OSError:
            pass
        raise MigrationError(f"mysqldump 备份失败，已中止切换：{detail}")
    if not os.path.exists(target) or os.path.getsize(target) == 0:
        raise MigrationError("mysqldump 备份失败：备份文件为空，已中止切换")
    return target


def backup_source(source: dict) -> str:
    if source.get("db_type") == "sqlite":
        return backup_sqlite_source(source["database_path"])
    return backup_mysql_source(source)


# ── 迁移主体 ─────────────────────────────────────────────────────────


def _empty_target_guard(adapter):
    existing = adapter.table_names()
    non_empty = []
    for table in EXPECTED_TABLES:
        if table in existing and adapter.count(table) > 0:
            non_empty.append(table)
    if non_empty:
        raise MigrationError(
            "目标库中已有数据（表：" + ", ".join(non_empty) + "），"
            "为避免覆盖，本次迁移已中止。请用空库或先手动清空目标库。"
        )


def _create_target_schema(adapter, db_type: str):
    if db_type == "sqlite":
        for ddl in _SQLITE_SCHEMA_DDL:
            adapter.execute(ddl)
        for ddl in _SQLITE_INDEX_DDL:
            adapter.execute(ddl)
        return
    for ddl in _SCHEMA_DDL:
        adapter.execute(ddl)


def _copy_table(src, dst, table: str, progress=None) -> int:
    src_cols = src.columns(table)
    if not src_cols:
        return 0
    dst_cols = set(dst.columns(table))
    cols = [c for c in src_cols if c in dst_cols]
    if not cols:
        raise MigrationError(f"表 {table} 在目标库中没有可写入的列")
    col_sql = ", ".join(f"`{c}`" for c in cols)
    order = _PK_COLUMNS.get(table, cols[0])
    select_sql = f"SELECT {col_sql} FROM `{table}` ORDER BY {order}"

    insert_sql = (
        f"INSERT INTO `{table}` ({col_sql}) VALUES "
        f"({', '.join([dst.ph] * len(cols))})"
    )
    total = 0
    cursor = src.stream(select_sql)
    while True:
        rows = cursor.fetchmany(BATCH_SIZE)
        if not rows:
            break
        dst.executemany(insert_sql, [tuple(r) for r in rows])
        total += len(rows)
        if progress:
            progress(table, total)
    return total


def _sample_rows(adapter, table: str, limit: int = 3):
    order = _PK_COLUMNS.get(table, "1")
    head = adapter.execute(
        f"SELECT * FROM `{table}` ORDER BY {order} LIMIT {limit}"
    ).fetchall()
    tail = adapter.execute(
        f"SELECT * FROM `{table}` ORDER BY {order} DESC LIMIT {limit}"
    ).fetchall()
    return [tuple(r) for r in head], [tuple(r) for r in tail]


def _verify(src, dst, expected_counts: dict, progress=None):
    for table in TABLE_ORDER:
        if table not in expected_counts:
            continue
        src_count = expected_counts[table]
        dst_count = dst.count(table)
        if src_count != dst_count:
            raise MigrationError(
                f"校验失败：表 {table} 行数不一致（来源 {src_count}，目标 {dst_count}）"
            )
        if src_count:
            src_head, src_tail = _sample_rows(src, table)
            dst_head, dst_tail = _sample_rows(dst, table)
            if src_head != dst_head or src_tail != dst_tail:
                raise MigrationError(
                    f"校验失败：表 {table} 首尾抽样数据不一致，疑似类型转换异常"
                )
        if progress:
            progress("verify", table, src_count)


def migrate(source: dict, target: dict, progress=None) -> dict:
    """把 source 的全部业务数据复制到空目标库并校验，返回迁移报告。

    progress(stage, table, done)：stage 为 "copy" / "verify"，
    done 为该表已处理行数。不会修改 .env，也不会停止当前应用。
    """
    # 允许同类型迁移（例如更换 MySQL 实例 / 更换 SQLite 文件），但仍要求目标为空
    source = normalize_target(source)
    target = normalize_target(target)

    if target["db_type"] == "mysql":
        _ensure_mysql_database(target)

    started = time.monotonic()
    src = _open_adapter(source)
    dst = _open_adapter(target)
    counts = {}
    try:
        # 目标必须为空
        _empty_target_guard(dst)
        _create_target_schema(dst, target["db_type"])

        # 来源只读快照：复制期间业务写入不会造成"半新半旧"
        src.begin_snapshot()
        src_tables = src.table_names()

        for table in TABLE_ORDER:
            if table not in src_tables:
                continue
            counts[table] = src.count(table)

        # 目标写入放在单事务中：任何中途失败都不会留下半截数据
        dst.begin_write()
        try:
            for table in TABLE_ORDER:
                if table not in counts:
                    continue
                copied = _copy_table(
                    src, dst, table,
                    progress=(lambda t, n, _t=table: progress("copy", _t, n))
                    if progress else None,
                )
                if copied != counts[table]:
                    raise MigrationError(
                        f"复制失败：表 {table} 读取 {counts[table]} 行，实际写入 {copied} 行"
                    )
            dst.commit()
        except BaseException:
            dst.rollback()
            raise

        # 保证目标记录当前结构版本（老库可能缺记录）
        try:
            from app.database import SCHEMA_VERSION
            dst.execute(
                f"INSERT INTO `schema_migrations` (version, applied_at) "
                f"VALUES ({dst.ph}, {dst.ph})"
                + (
                    " ON CONFLICT(version) DO NOTHING"
                    if target["db_type"] == "sqlite" else
                    " ON DUPLICATE KEY UPDATE applied_at = applied_at"
                ),
                (SCHEMA_VERSION, datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
        except Exception:
            # 版本记录不是业务数据，写入失败不影响迁移结果
            pass

        _verify(src, dst, counts, progress=progress)
    finally:
        try:
            src.rollback()
        except Exception:
            pass
        src.close()
        dst.close()

    return {
        "source": describe(source),
        "target": describe(target),
        "tables": counts,
        "total_rows": sum(counts.values()),
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "verified": True,
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def env_updates_for_target(target: dict) -> dict:
    """把目标库配置转换为 .env 键值（切换成功后原子写入）。"""
    target = normalize_target(target)
    if target["db_type"] == "sqlite":
        return {
            "DB_TYPE": "sqlite",
            "DATABASE_PATH": target["database_path"],
        }
    return {
        "DB_TYPE": "mysql",
        "MYSQL_HOST": target["host"],
        "MYSQL_PORT": str(target["port"]),
        "MYSQL_DATABASE": target["database"],
        "MYSQL_USER": target["user"],
        "MYSQL_PASSWORD": target["password"],
        "MYSQL_CHARSET": target["charset"],
        "MYSQL_COLLATION": target["collation"],
    }


def switch_database(target: dict, env_path: str = None, progress=None) -> dict:
    """完整切换流程：备份来源库 → 复制数据 → 校验 → 原子写 .env。

    任一步失败都抛出 MigrationError，且不会修改 .env；成功后需重启应用生效。
    """
    from installer.env_io import update_env_file

    env_path = env_path or str(config.BASE_DIR / ".env")
    source = current_source()
    target = normalize_target(target)
    if target["db_type"] == source["db_type"]:
        same_sqlite = (
            target["db_type"] == "sqlite"
            and os.path.abspath(target["database_path"])
            == os.path.abspath(source["database_path"])
        )
        same_mysql = (
            target["db_type"] == "mysql"
            and target["host"] == source["host"]
            and int(target["port"]) == int(source["port"])
            and target["database"] == source["database"]
        )
        if same_sqlite or same_mysql:
            raise MigrationError("目标数据库与当前数据库相同，无需切换")

    if progress:
        progress("backup", None, 0)
    backup_path = backup_source(source)

    report = migrate(source, target, progress=progress)
    report["backup_path"] = backup_path
    report["needs_restart"] = True

    if progress:
        progress("env", None, 0)
    update_env_file(env_path, env_updates_for_target(target))
    report["env_path"] = env_path
    return report
