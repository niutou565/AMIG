"""安装执行器：后台线程执行「写 .env → 初始化数据库 → 验证 → 生成报告」。

安全约定：
- 安装前校验不通过绝不写 .env；
- 写 .env 前备份旧文件，安装失败时恢复旧 .env；
- 重新安装不删除任何旧数据（数据库初始化是幂等的）；
- 日志与报告不记录任何密码明文。
"""
import os
import shutil
import sys
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path

from installer.env_io import update_env_file
from installer.preflight import run_checks
from installer.state import build_env, validate_all
from installer.verify import verify_installation, write_report

_APP_MODULES = ("app", "init_db")


class InstallExecutor:
    def __init__(self):
        # 可重入锁：start() 持有锁时也会调用 reset()/snapshot()
        self._lock = threading.RLock()
        self.reset()

    def reset(self):
        with self._lock:
            self.status = "idle"
            self.percent = 0
            self.step = "等待开始"
            self.logs = []
            self.error = ""
            self.report = None
            self.checks = []
            self.data = None
            self.base_dir = None
            self._thread = None

    def _log(self, message: str):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
        with self._lock:
            self.logs.append(line)
            if len(self.logs) > 400:
                del self.logs[:100]

    def _set(self, percent=None, step=None, status=None):
        with self._lock:
            if percent is not None:
                self.percent = max(self.percent, int(percent))
            if step is not None:
                self.step = step
            if status is not None:
                self.status = status

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "status": self.status,
                "percent": self.percent,
                "step": self.step,
                "logs": list(self.logs),
                "error": self.error,
                "report": self.report,
                "checks": list(self.checks),
            }

    def start(self, data: dict, base_dir: Path, force_reinstall: bool) -> bool:
        with self._lock:
            if self.status == "running":
                return False
            self.reset()
            self.status = "running"
        self._thread = threading.Thread(
            target=self._run, args=(data, Path(base_dir), force_reinstall), daemon=True
        )
        self._thread.start()
        return True

    # ── 主流程 ───────────────────────────────────────────────────────

    def _run(self, raw_data: dict, base_dir: Path, force_reinstall: bool):
        try:
            self._set(3, "校验安装参数")
            data = validate_all(raw_data, base_dir, force_reinstall)
            self.data = data
            self.base_dir = base_dir
            self._log("参数校验通过")

            self._set(10, "环境检查")
            self.checks = run_checks(data["db"], data["site"], base_dir)
            failed = [item for item in self.checks if not item["ok"]]
            if failed:
                raise RuntimeError(
                    "前置检查未通过：" + "；".join(item["name"] for item in failed)
                )
            self._log("前置检查全部通过")

            self._set(25, "备份旧配置")
            env_path = base_dir / ".env"
            env_backup = self._backup_env(env_path)
            db_backup = self._backup_existing_sqlite(data, base_dir)

            self._set(40, "写入配置")
            env = build_env(data, base_dir)
            update_env_file(str(env_path), env)
            self._log(f".env 已写入（DB_TYPE={env.get('DB_TYPE')}）")

            try:
                self._set(55, "初始化数据库")
                self._apply_env(env)
                self._init_database(data)
                self._set(80, "验证安装结果")
                report = verify_installation(data, base_dir)
                report["installer"] = {
                    "mode": "reinstall" if force_reinstall else "fresh",
                    "env_backup": env_backup,
                    "sqlite_backup": db_backup,
                    "db_type": data["db"]["type"],
                    "host": data["site"]["host"],
                    "port": data["site"]["port"],
                    "admin_username": data["admin"]["username"],
                }
                report["report_path"] = write_report(report, base_dir)
                with self._lock:
                    self.report = report
                if not report["ok"]:
                    bad = [c["name"] for c in report["checks"] if not c["ok"]]
                    raise RuntimeError("安装后验证未通过：" + "、".join(bad))
            except BaseException:
                # 安装失败：恢复旧 .env，避免应用指向不可用的数据库
                self._restore_env(env_path, env_backup)
                raise

            self._set(100, "安装完成", "success")
            self._log("安装完成，可点击「验证核心功能」启动一次应用自检")
        except Exception as exc:
            message = str(exc).strip() or exc.__class__.__name__
            with self._lock:
                self.status = "failed"
                self.error = message
                self.step = "安装失败"
            self._log(f"安装失败：{message}")
            self._log(traceback.format_exc(limit=3))

    # ── 辅助步骤 ─────────────────────────────────────────────────────

    def _backup_env(self, env_path: Path):
        if not env_path.exists():
            return None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = env_path.with_name(f".env.installer_backup_{stamp}")
        shutil.copy2(env_path, target)
        self._log(f"已备份旧配置：{target.name}")
        return str(target)

    def _restore_env(self, env_path: Path, env_backup):
        try:
            if env_backup and Path(env_backup).exists():
                shutil.copy2(env_backup, env_path)
                self._log("已恢复安装前的 .env")
            elif env_path.exists():
                env_path.unlink()
                self._log("已移除本次写入的 .env（安装失败）")
        except OSError as exc:
            self._log(f"恢复 .env 失败：{exc}")

    def _backup_existing_sqlite(self, data: dict, base_dir: Path):
        if data["db"]["type"] != "sqlite":
            return None
        path = Path(data["db"]["database_path"])
        if not path.exists() or path.stat().st_size == 0:
            return None
        backup_dir = base_dir / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = backup_dir / f"install_backup_{stamp}.db"
        import sqlite3
        src = sqlite3.connect(str(path))
        dest = sqlite3.connect(str(target))
        try:
            src.backup(dest)
        finally:
            dest.close()
            src.close()
        self._log(f"已备份旧 SQLite 数据库：{target.name}")
        return str(target)

    def _apply_env(self, env: dict):
        """把配置写入当前进程环境，并清空 app.* 模块缓存后重新导入。"""
        os.environ.update({key: str(value) for key, value in env.items()})
        for name in list(sys.modules):
            if name == "init_db" or name == "app" or name.startswith("app."):
                del sys.modules[name]

    def _init_database(self, data: dict):
        import init_db as init_db_module
        from app.database import init_db, get_db

        init_db(max_wait_seconds=60)
        self._log("表结构初始化完成（幂等，已有数据保持不变）")
        db = get_db()
        init_db_module.seed_default_settings(db)
        init_db_module.seed_default_pages(db)
        init_db_module.seed_default_links(db)
        self._set(70, "创建管理员账号")
        username = data["admin"]["username"]
        existed = db.execute(
            "SELECT id FROM admins WHERE username=?", (username,)
        ).fetchone() is not None
        init_db_module.create_default_admin(
            db, username, data["admin"]["password"]
        )
        if existed:
            self._log(f"管理员 {username} 已存在，保留原密码（重新安装不覆盖账号）")
        else:
            self._log(f"管理员账号已创建：{username}")
