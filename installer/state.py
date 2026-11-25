"""安装向导的数据模型、步骤校验与 .env 生成（不依赖 app.* 运行时配置）。

安装器是独立进程：在写入 .env 之前绝不导入 app.config，否则会把旧的
环境变量缓存进进程，导致安装结果与用户选择不一致。
"""
import importlib.util
import os
import re
import sqlite3
from pathlib import Path

from installer.env_io import read_env_file

STEPS = ("database", "site", "admin", "confirm", "install")

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_$]+$")
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")


def _load_password_policy():
    """按文件路径加载密码策略模块，避免触发 app 包内的配置导入。"""
    path = Path(__file__).resolve().parent.parent / "app" / "password_policy.py"
    spec = importlib.util.spec_from_file_location("amig_password_policy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def normalize_dir(base_dir: Path, value, default: str) -> str:
    raw = str(value or "").strip() or default
    path = Path(raw)
    if not path.is_absolute():
        path = base_dir / path
    return str(path.resolve())


def normalize_db(raw: dict, base_dir: Path) -> dict:
    """规范化数据库配置；非法时抛出 ValueError（消息面向用户）。"""
    raw = raw or {}
    db_type = str(raw.get("type") or raw.get("db_type") or "").strip().lower()
    if db_type == "sqlite":
        path = str(raw.get("database_path") or "data/app.db").strip()
        if not path:
            raise ValueError("SQLite 数据库文件路径不能为空")
        p = Path(path)
        if not p.is_absolute():
            p = base_dir / p
        return {"type": "sqlite", "database_path": str(p.resolve())}
    if db_type == "mysql":
        cfg = {
            "type": "mysql",
            "host": str(raw.get("host") or "127.0.0.1").strip(),
            "port": int(raw.get("port") or 3306),
            "database": str(raw.get("database") or "").strip(),
            "user": str(raw.get("user") or "").strip(),
            "password": str(raw.get("password") or ""),
        }
        if not cfg["host"]:
            raise ValueError("MySQL 主机不能为空")
        if not 1 <= cfg["port"] <= 65535:
            raise ValueError("MySQL 端口必须在 1-65535 之间")
        if not cfg["database"]:
            raise ValueError("MySQL 数据库名不能为空")
        if not _IDENTIFIER_RE.match(cfg["database"]):
            raise ValueError("MySQL 数据库名只能包含字母、数字、下划线与 $")
        if not cfg["user"]:
            raise ValueError("MySQL 用户名不能为空")
        return cfg
    raise ValueError("请选择数据库类型：SQLite 或 MySQL")


def normalize_site(raw: dict, base_dir: Path) -> dict:
    raw = raw or {}
    try:
        port = int(raw.get("port") or 8000)
        workers = int(raw.get("workers") or 1)
    except (TypeError, ValueError):
        raise ValueError("端口与 worker 数必须是整数")
    if not 1 <= port <= 65535:
        raise ValueError("服务端口必须在 1-65535 之间")
    if not 1 <= workers <= 8:
        raise ValueError("worker 数必须在 1-8 之间")

    uploads = normalize_dir(base_dir, raw.get("uploads_dir"), "uploads")
    private = normalize_dir(base_dir, raw.get("private_uploads_dir"), "private_uploads")
    if private == uploads or private.startswith(uploads + os.sep) or uploads.startswith(private + os.sep):
        raise ValueError("私有上传目录与公开上传目录不能相同或互相包含")
    return {
        "admin_domain": str(raw.get("admin_domain") or "").strip(),
        "host": str(raw.get("host") or "127.0.0.1").strip(),
        "port": port,
        "workers": workers,
        "uploads_dir": uploads,
        "private_uploads_dir": private,
    }


def normalize_admin(raw: dict) -> dict:
    raw = raw or {}
    username = str(raw.get("username") or "admin").strip()
    password = raw.get("password") or ""
    confirm = raw.get("password_confirm") or raw.get("confirm") or ""
    if not _USERNAME_RE.match(username):
        raise ValueError("管理员用户名需为 3-32 位字母、数字、下划线、点或短横线")
    policy = _load_password_policy()
    message = policy.validate_password(password)
    if message:
        raise ValueError(message)
    if password != confirm:
        raise ValueError("两次输入的密码不一致")
    return {"username": username, "password": password}


def validate_all(data: dict, base_dir: Path, force_reinstall: bool = False) -> dict:
    """校验并规范化整份安装数据，返回可直接用于写 .env / 初始化的结构。"""
    if existing_installation(base_dir)["installed"] and not force_reinstall:
        raise ValueError(
            "检测到已有安装：老版本可直接启动应用完成升级，无需运行安装向导。"
            "如确需重新安装，请勾选「覆盖已有安装」（会先备份旧配置与数据库）。"
        )
    db = normalize_db(data.get("db"), base_dir)
    site = normalize_site(data.get("site"), base_dir)
    admin = normalize_admin(data.get("admin"))
    if db["type"] == "sqlite" and site["workers"] > 1:
        # SQLite 的命名锁是进程内锁，多 worker 会破坏上传额度串行化
        site["workers"] = 1
    return {"db": db, "site": site, "admin": admin}


def existing_installation(base_dir: Path) -> dict:
    """检测是否已有安装（用于首屏提示：老用户直接启动即可升级）。"""
    env_path = base_dir / ".env"
    env = read_env_file(str(env_path))
    info = {"installed": False, "reason": "", "database": ""}
    if env_path.exists():
        info["installed"] = True
        info["reason"] = "检测到 .env 配置文件"
        db_type = (env.get("DB_TYPE") or "sqlite").strip().lower()
        if db_type == "mysql":
            info["database"] = (
                f"MySQL：{env.get('MYSQL_USER', '')}@{env.get('MYSQL_HOST', '')}"
                f"/{env.get('MYSQL_DATABASE', '')}"
            )
        else:
            info["database"] = f"SQLite：{env.get('DATABASE_PATH') or 'data/app.db'}"

    sqlite_path = env.get("DATABASE_PATH") or "data/app.db"
    path = Path(sqlite_path)
    if not path.is_absolute():
        path = base_dir / path
    if path.exists():
        try:
            conn = sqlite3.connect(str(path))
            try:
                count = conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
                ).fetchone()[0]
            finally:
                conn.close()
            if count:
                info["installed"] = True
                info["reason"] = f"检测到已有 SQLite 数据库（{count} 张表）"
                info["database"] = f"SQLite：{path}"
        except sqlite3.Error:
            pass
    return info


def build_env(data: dict, base_dir: Path) -> dict:
    """把已规范化数据转换为完整 .env 键值（全新安装）。"""
    db = data["db"]
    site = data["site"]
    env = {
        "ADMIN_DOMAIN": site["admin_domain"],
        "HOST": site["host"],
        "PORT": str(site["port"]),
        "WORKERS": str(site["workers"]),
        "UPLOADS_DIR": site["uploads_dir"],
        "PRIVATE_UPLOADS_DIR": site["private_uploads_dir"],
    }
    if db["type"] == "sqlite":
        env.update({
            "DB_TYPE": "sqlite",
            "DATABASE_PATH": db["database_path"],
        })
    else:
        env.update({
            "DB_TYPE": "mysql",
            "MYSQL_HOST": db["host"],
            "MYSQL_PORT": str(db["port"]),
            "MYSQL_DATABASE": db["database"],
            "MYSQL_USER": db["user"],
            "MYSQL_PASSWORD": db["password"],
            "MYSQL_CHARSET": "utf8mb4",
            "MYSQL_COLLATION": "utf8mb4_unicode_ci",
        })
    return env
