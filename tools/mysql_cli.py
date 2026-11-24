#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mysqldump / mysql 命令行客户端定位与调用辅助（备份、恢复工具共用）。

定位顺序：
1. 环境变量/配置 MYSQLDUMP_PATH、MYSQL_CLIENT_PATH；
2. PATH；
3. Windows / Linux 常见安装目录。
"""
import os
import shutil
import subprocess
import sys

from app import config


def _candidates(env_path: str, binary: str) -> list:
    out = []
    if env_path:
        out.append(env_path)
    out.append(binary)
    win_dirs = [
        r"C:\Program Files\MySQL\MySQL Server 8.4\bin",
        r"C:\Program Files\MySQL\MySQL Server 8.0\bin",
        r"C:\Program Files\MySQL\MySQL Server 5.7\bin",
        r"C:\xampp\mysql\bin",
        r"C:\phpstudy_pro\Extensions\MySQL5.7.26\bin",
        r"C:\BtSoft\mysql\bin",
    ]
    out.extend(os.path.join(d, binary + ".exe") for d in win_dirs)
    out.extend(os.path.join(d, binary) for d in ("/usr/bin", "/usr/local/bin", "/opt/mysql/bin"))
    return out


def find_client(env_path: str, binary: str):
    """定位客户端可执行文件，找不到返回 None。"""
    for cand in _candidates(env_path, binary):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
        which = shutil.which(cand)
        if which:
            return which
    return None


def find_mysqldump():
    return find_client(config.MYSQLDUMP_PATH, "mysqldump")


def find_mysql_client():
    return find_client(config.MYSQL_CLIENT_PATH, "mysql")


def build_password_args(client_path: str):
    """构造认证参数（不含可执行文件本身，由 run_client 传入）。

    密码通过 MYSQL_PWD 环境变量传递，避免出现在进程命令行参数
    （ps / 任务管理器可见）。client_path 参数保留用于调用方语义清晰。
    """
    env = dict(os.environ)
    env["MYSQL_PWD"] = config.MYSQL_PASSWORD
    return (
        [f"--host={config.MYSQL_HOST}",
         f"--port={str(config.MYSQL_PORT)}",
         f"--user={config.MYSQL_USER}",
         "--default-character-set=utf8mb4"],
        env,
    )


def run_client(client_path: str, extra_args: list, env: dict,
               input_file=None, capture=True, timeout=1800) -> subprocess.CompletedProcess:
    kwargs = dict(env=env, timeout=timeout)
    if capture:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    if input_file is not None:
        # 以二进制方式读入 SQL 文件（编码由客户端 --default-character-set 控制）
        kwargs["stdin"] = open(input_file, "rb")
    try:
        return subprocess.run([client_path] + extra_args, **kwargs)
    finally:
        stdin = kwargs.get("stdin")
        if stdin is not None:
            stdin.close()


def require_clients(need_mysqldump=True):
    """确认客户端可用，缺失时给出明确指引并退出。"""
    missing = []
    dump = find_mysqldump() if need_mysqldump else True
    cli = find_mysql_client()
    if need_mysqldump and not dump:
        missing.append("mysqldump")
    if not cli:
        missing.append("mysql")
    if missing:
        print("[失败] 未找到 MySQL 客户端工具: " + ", ".join(missing))
        print("       请安装 MySQL 客户端工具，或通过环境变量指定路径：")
        print("         MYSQLDUMP_PATH=C:\\Program Files\\MySQL\\MySQL Server 8.0\\bin\\mysqldump.exe")
        print("         MYSQL_CLIENT_PATH=C:\\Program Files\\MySQL\\MySQL Server 8.0\\bin\\mysql.exe")
        sys.exit(1)
    return dump, cli
