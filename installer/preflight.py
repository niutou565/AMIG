"""安装前置校验：环境依赖、权限、磁盘空间、端口占用与数据库连通性。

所有检查返回结构化结果，失败项附带修复建议（hint），不抛异常，
由页面逐项展示，用户修复后可点击「重新检查」。安装器不会静默跳过失败项。
"""
import importlib
import os
import shutil
import socket
import sqlite3
import sys
from pathlib import Path

MIN_PYTHON = (3, 10)
MIN_FREE_BYTES = 500 * 1024 * 1024  # 500MB

_REQUIRED_MODULES = (
    ("fastapi", "FastAPI 框架"),
    ("uvicorn", "ASGI 服务器"),
    ("multipart", "python-multipart 表单解析"),
    ("PIL", "Pillow 图片处理"),
    ("bcrypt", "bcrypt 密码哈希"),
    ("aiofiles", "aiofiles 异步文件读写"),
    ("jinja2", "Jinja2 模板引擎"),
    ("httpx", "httpx HTTP 客户端"),
)


def _item(name, ok, detail, hint=""):
    return {"name": name, "ok": bool(ok), "detail": detail, "hint": hint}


def _check_python():
    version = sys.version_info
    current = f"{version.major}.{version.minor}.{version.micro}"
    ok = (version.major, version.minor) >= MIN_PYTHON
    return _item(
        "Python 版本", ok, f"当前 {current}，要求 ≥ {MIN_PYTHON[0]}.{MIN_PYTHON[1]}",
        "请升级 Python 到 3.10 及以上版本后重新运行安装脚本",
    )


def _check_dependencies(need_pymysql: bool):
    missing = []
    for module, label in _REQUIRED_MODULES:
        try:
            importlib.import_module(module)
        except Exception:
            missing.append(f"{module}（{label}）")
    if need_pymysql:
        try:
            importlib.import_module("pymysql")
        except Exception:
            missing.append("pymysql（MySQL 驱动）")
    if missing:
        return _item(
            "运行依赖", False, "缺少：" + "、".join(missing),
            "执行 pip install -r requirements.txt 安装依赖后重新检查",
        )
    return _item("运行依赖", True, "全部依赖已安装")


def _check_disk(base_dir: Path):
    try:
        usage = shutil.disk_usage(str(base_dir))
    except OSError as exc:
        return _item("磁盘空间", False, f"无法读取磁盘信息：{exc}", "请检查项目目录所在磁盘")
    free_mb = usage.free // (1024 * 1024)
    ok = usage.free >= MIN_FREE_BYTES
    return _item(
        "磁盘空间", ok, f"可用 {free_mb} MB（要求 ≥ 500 MB）",
        "请清理磁盘空间或更换安装目录后重试",
    )


def _check_writable(name, path: Path, hint):
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".install_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return _item(name, True, f"可写：{path}")
    except OSError as exc:
        return _item(name, False, f"不可写：{path}（{exc}）", hint)


def _check_port(host: str, port: int):
    bind_host = host if host not in ("", "localhost") else "127.0.0.1"
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((bind_host, port))
        return _item("服务端口", True, f"{bind_host}:{port} 可用")
    except OSError as exc:
        return _item(
            "服务端口", False, f"{bind_host}:{port} 不可用（{exc}）",
            "请关闭占用该端口的进程，或在「站点配置」中改用其他端口",
        )
    finally:
        sock.close()


def _check_sqlite(path: str):
    target = Path(path)
    parent_ok = _check_writable(
        "SQLite 数据目录", target.parent,
        "请给数据库目录写权限，或把 DATABASE_PATH 改到可写目录",
    )
    if not parent_ok["ok"]:
        return [parent_ok]
    items = [parent_ok]
    try:
        conn = sqlite3.connect(str(target))
        try:
            conn.execute("SELECT 1")
        finally:
            conn.close()
        items.append(_item("SQLite 数据库文件", True, f"可读写：{target}"))
    except sqlite3.Error as exc:
        items.append(_item(
            "SQLite 数据库文件", False, f"无法打开：{target}（{exc}）",
            "请确认文件未被其他进程独占，或更换数据库路径",
        ))
    return items


def _check_mysql(cfg: dict):
    try:
        import pymysql
    except ImportError:
        return [_item(
            "MySQL 连接", False, "未安装 PyMySQL",
            "执行 pip install PyMySQL 后重新检查",
        )]
    try:
        conn = pymysql.connect(
            host=cfg["host"], port=int(cfg["port"]), user=cfg["user"],
            password=cfg.get("password", ""), charset="utf8mb4",
            autocommit=True, connect_timeout=8,
        )
    except Exception as exc:
        return [_item(
            "MySQL 连接", False, f"无法连接 {cfg['host']}:{cfg['port']}（{exc}）",
            "请确认 MySQL 已启动、地址/端口正确，且该账号允许从本机登录",
        )]
    items = []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT VERSION()")
            version = cur.fetchone()[0]
            items.append(_item("MySQL 连接", True, f"连接成功，服务端版本 {version}"))

            cur.execute(
                "SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name=%s",
                (cfg["database"],),
            )
            exists = cur.fetchone()[0]
            if exists:
                cur.execute(
                    "SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE table_schema=%s AND table_name IN "
                    "('images','admins','settings')",
                    (cfg["database"],),
                )
                business_tables = cur.fetchone()[0]
                if business_tables:
                    items.append(_item(
                        "目标数据库为空", False,
                        f"数据库 {cfg['database']} 已存在业务表",
                        "全新安装建议使用新库；要保留旧数据请改用后台「切换数据库」",
                    ))
                else:
                    items.append(_item("目标数据库", True, f"数据库 {cfg['database']} 已存在且为空"))
            else:
                items.append(_item("目标数据库", True, f"数据库 {cfg['database']} 将在安装时创建"))
    except Exception as exc:
        items.append(_item(
            "MySQL 权限", False, f"查询失败：{exc}",
            "请确认账号具备建库/建表权限（或先手工创建数据库并授权）",
        ))
    finally:
        conn.close()
    return items


def run_checks(db_cfg: dict, site: dict, base_dir: Path) -> list:
    """执行全部检查，返回结果列表（每项含 name/ok/detail/hint）。"""
    need_mysql = (db_cfg or {}).get("type") == "mysql"
    results = [_check_python(), _check_dependencies(need_mysql), _check_disk(base_dir)]
    results.append(_check_writable(
        "项目目录", base_dir, "请给项目目录写权限（安装过程需要写入 .env 与日志）"
    ))
    if site:
        results.append(_check_writable(
            "公开上传目录", Path(site["uploads_dir"]),
            "请给上传目录写权限，或改用其他目录",
        ))
        results.append(_check_writable(
            "私有上传目录", Path(site["private_uploads_dir"]),
            "请给私有上传目录写权限，或改用其他目录",
        ))
        results.append(_check_port(site["host"], site["port"]))
        static_dir = (base_dir / "app" / "static").resolve()
        private_dir = Path(site["private_uploads_dir"]).resolve()
        inside_static = str(private_dir).startswith(str(static_dir) + os.sep)
        results.append(_item(
            "私有目录隔离", not inside_static,
            "私有上传目录不与静态目录重叠" if not inside_static
            else f"私有上传目录位于静态目录内：{private_dir}",
            "私有上传目录必须放在 Web 无法直接访问的位置",
        ))
    if db_cfg:
        if db_cfg.get("type") == "mysql":
            results.extend(_check_mysql(db_cfg))
        elif db_cfg.get("database_path"):
            results.extend(_check_sqlite(db_cfg["database_path"]))
    return results
