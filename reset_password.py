#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
管理员密码重置脚本（适用于宝塔面板 / 命令行）

功能：重置后台管理员（用户名 admin）的密码，并清除登录失败计数与锁定状态。

用法：
    python reset_password.py                # 交互式输入并确认新密码
    python reset_password.py --yes          # 生成随机强密码并直接重置
    python reset_password.py --password 自定义密码 [--yes]

安全说明：
    - 本脚本只允许在项目根目录或正确指向 data/app.db 的环境下运行
    - 默认要求交互式确认，避免误操作
    - 重置后请立即登录后台修改为强密码
"""

import os
import sys
import secrets
import sqlite3
import getpass

DEFAULT_USERNAME = "admin"
MIN_PASSWORD_LENGTH = 8

# 数据库路径：优先使用环境变量 DATABASE_PATH，否则相对脚本目录 data/app.db
def resolve_db_path():
    env_path = os.getenv("DATABASE_PATH")
    if env_path:
        return env_path
    base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "data", "app.db")


def hash_password(password: str) -> str:
    try:
        import bcrypt
    except ImportError:
        print("[失败] 缺少依赖 bcrypt，请先执行: pip install bcrypt")
        sys.exit(1)
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def confirm_reset(username: str, password: str, skip: bool) -> bool:
    if skip:
        return True
    print("-" * 50)
    print("即将重置管理员密码：")
    print(f"  用户名 : {username}")
    print(f"  新密码 : {password}")
    print("-" * 50)
    try:
        answer = input("确认执行？输入 y/yes 继续，其他任意键取消: ").strip().lower()
    except EOFError:
        # 非交互环境且未传 --yes
        print("[失败] 非交互环境下需传入 --yes 参数才能执行（防止误操作）")
        return False
    return answer in ("y", "yes")


def main():
    args = sys.argv[1:]
    skip = "--yes" in args
    password = None

    # 解析 --password 参数
    for i, a in enumerate(args):
        if a == "--password" and i + 1 < len(args):
            password = args[i + 1]
        elif a.startswith("--password="):
            password = a.split("=", 1)[1]

    # 未显式指定密码时：交互式输入；非交互场景随机生成强密码，绝不使用固定弱密码
    if not password:
        if skip:
            password = secrets.token_hex(12)
        else:
            try:
                p1 = getpass.getpass(f"请输入新密码（至少 {MIN_PASSWORD_LENGTH} 位）: ")
                p2 = getpass.getpass("请再次输入确认: ")
            except EOFError:
                print("[失败] 非交互环境下请通过 --password 或 --yes 指定密码")
                sys.exit(1)
            if p1 != p2:
                print("[失败] 两次输入不一致")
                sys.exit(1)
            password = p1

    if len(password) < MIN_PASSWORD_LENGTH:
        print(f"[失败] 新密码长度不能少于 {MIN_PASSWORD_LENGTH} 位")
        sys.exit(1)

    db_path = resolve_db_path()
    print("=" * 50)
    print("  管理员密码重置工具")
    print("=" * 50)

    if not os.path.exists(db_path):
        print(f"[失败] 未找到数据库文件: {db_path}")
        print("       请确认脚本位于项目根目录，或通过 DATABASE_PATH 环境变量指定路径")
        sys.exit(1)

    if not confirm_reset(DEFAULT_USERNAME, password, skip):
        print("[已取消] 未执行任何修改")
        sys.exit(0)

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT id FROM admins WHERE username=?", (DEFAULT_USERNAME,))
        row = cur.fetchone()

        if row:
            cur.execute(
                "UPDATE admins SET password_hash=?, login_attempts=0, locked_until=NULL WHERE username=?",
                (hash_password(password), DEFAULT_USERNAME),
            )
            conn.commit()
            print(f"[成功] 管理员 '{DEFAULT_USERNAME}' 密码已重置")
        else:
            # 管理员不存在时自动创建
            import datetime
            created_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
            cur.execute(
                "INSERT INTO admins (username, password_hash, login_attempts, locked_until, created_at) "
                "VALUES (?, ?, 0, NULL, ?)",
                (DEFAULT_USERNAME, hash_password(password), created_at),
            )
            conn.commit()
            print(f"[成功] 管理员 '{DEFAULT_USERNAME}' 不存在，已创建并设置密码")
        conn.close()
    except sqlite3.OperationalError as e:
        print(f"[失败] 数据库操作出错: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"[失败] 发生未预期错误: {e}")
        sys.exit(1)

    print(f"  用户名 : {DEFAULT_USERNAME}")
    print(f"  新密码 : {password}")
    print("  !! 请立即登录后台，并通过「修改密码」改为强密码 !!")
    print("=" * 50)


if __name__ == "__main__":
    main()
