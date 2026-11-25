"""安装完成后的验证：配置完整性、数据库与表结构、管理员密码、目录写入。

注意：本模块在 .env 写入并且进程环境变量设置完成之后才会被调用，
因此可以安全地导入 app.* 模块。
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import bcrypt

from installer.env_io import read_env_file


def _check(name, ok, detail):
    return {"name": name, "ok": bool(ok), "detail": detail}


def _env_check(env: dict, db_type: str) -> dict:
    required = ["DB_TYPE", "HOST", "PORT", "WORKERS", "UPLOADS_DIR",
                "PRIVATE_UPLOADS_DIR"]
    if db_type == "mysql":
        required += ["MYSQL_HOST", "MYSQL_PORT", "MYSQL_DATABASE", "MYSQL_USER"]
    else:
        required += ["DATABASE_PATH"]
    missing = [key for key in required if not str(env.get(key, "")).strip()]
    if missing:
        return _check("配置文件", False, "缺少配置项：" + "、".join(missing))
    return _check("配置文件", True, f".env 完整（DB_TYPE={env.get('DB_TYPE')}）")


def _db_check(base_dir: Path, admin_username: str, admin_password: str):
    checks = []
    try:
        from app.database import EXPECTED_TABLES, get_db, init_db
        init_db(max_wait_seconds=30)
        conn = get_db()
        existing = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        } if os.environ.get("DB_TYPE", "sqlite").strip().lower() == "sqlite" else {
            row[0] for row in conn.execute("SHOW TABLES").fetchall()
        }
        missing = [t for t in EXPECTED_TABLES if t not in existing]
        checks.append(_check(
            "数据表结构", not missing,
            "全部 15 张业务表已就绪" if not missing else "缺少表：" + "、".join(missing),
        ))
    except Exception as exc:
        checks.append(_check("数据表结构", False, f"数据库初始化失败：{exc}"))
        return checks

    try:
        row = conn.execute(
            "SELECT password_hash FROM admins WHERE username=?", (admin_username,)
        ).fetchone()
        if row is None:
            checks.append(_check("管理员账号", False, f"未找到管理员 {admin_username}"))
        else:
            ok = bcrypt.checkpw(admin_password.encode(), row["password_hash"].encode())
            checks.append(_check(
                "管理员账号", ok,
                "管理员密码校验通过" if ok else "管理员密码校验失败（bcrypt 不匹配）",
            ))
    except Exception as exc:
        checks.append(_check("管理员账号", False, f"校验失败：{exc}"))
    return checks


def _dir_checks(data: dict) -> list:
    checks = []
    for name, key in (("公开上传目录", "uploads_dir"), ("私有上传目录", "private_uploads_dir")):
        path = Path(data["site"][key])
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".install_verify"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            checks.append(_check(name, True, f"可写：{path}"))
        except OSError as exc:
            checks.append(_check(name, False, f"不可写：{path}（{exc}）"))
    return checks


def verify_installation(data: dict, base_dir: Path) -> dict:
    """安装结果验证（不含启动 HTTP 服务，避免阻塞安装流程）。"""
    env_path = base_dir / ".env"
    env = read_env_file(str(env_path))
    db_type = "mysql" if str(env.get("DB_TYPE", "")).lower() == "mysql" else "sqlite"
    checks = [_env_check(env, db_type)]
    checks.extend(_db_check(base_dir, data["admin"]["username"], data["admin"]["password"]))
    checks.extend(_dir_checks(data))
    try:
        from app.config import APP_VERSION
    except Exception:
        APP_VERSION = "1.2.5"
    checks.append(_check("版本标识", True, f"AMIG {APP_VERSION}"))
    ok = all(item["ok"] for item in checks)
    return {"ok": ok, "checks": checks, "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def verify_http(data: dict, base_dir: Path, timeout_seconds: int = 30) -> dict:
    """启动一次应用子进程并访问首页，确认服务可正常响应。

    仅在本机回环地址上做探测；无论成功与否都会结束子进程。
    """
    import httpx

    port = int(data["site"]["port"])
    url = f"http://127.0.0.1:{port}/"
    env = dict(os.environ)
    proc = subprocess.Popen(
        [sys.executable, "run.py"],
        cwd=str(base_dir),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + timeout_seconds
    last_error = "未能在限定时间内启动服务"
    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                last_error = f"应用进程提前退出（exit code {proc.returncode}）"
                break
            try:
                resp = httpx.get(url, timeout=3.0)
                if resp.status_code < 500:
                    return _check("服务可用性", True, f"GET {url} 返回 {resp.status_code}")
                last_error = f"GET {url} 返回 {resp.status_code}"
            except Exception as exc:
                last_error = str(exc)
            time.sleep(1.0)
        return _check("服务可用性", False, last_error)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


def write_report(report: dict, base_dir: Path) -> str:
    """把安装报告写入 logs/，返回报告文件路径。"""
    log_dir = base_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = log_dir / f"install_report_{stamp}.json"
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return str(path)
