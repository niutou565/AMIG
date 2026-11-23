#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""管理员密码重置脚本（适用于宝塔面板 / 命令行）

安全设计：
- 不再接受 --password 命令行参数，避免密码出现在进程参数和 shell 历史中；
- 默认使用 getpass 交互输入；自动化场景可通过 --stdin 从标准输入读取；
- 重置成功后在同一事务内撤销全部旧会话、清除登录失败统计；
- 不在任何日志或终端输出中打印新密码。
"""

import os
import sys
import sqlite3
import getpass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DEFAULT_USERNAME = "admin"


def resolve_db_path():
    env_path = os.getenv("DATABASE_PATH")
    if env_path:
        return env_path
    base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "data", "app.db")


def hash_password(password):
    try:
        import bcrypt
    except ImportError:
        print("[失败] 缺少依赖 bcrypt，请先执行: pip install bcrypt")
        sys.exit(1)
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _read_password(use_stdin):
    from app.password_policy import validate_password

    if use_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
        if not password:
            print("[失败] 标准输入未提供密码")
            sys.exit(1)
    else:
        try:
            password = getpass.getpass("请输入新密码: ")
            confirm = getpass.getpass("请再次输入确认: ")
        except EOFError:
            print("[失败] 非交互环境下请使用 --stdin 从标准输入提供密码")
            sys.exit(1)
        if password != confirm:
            print("[失败] 两次输入不一致")
            sys.exit(1)

    error = validate_password(password)
    if error:
        print(f"[失败] {error}")
        sys.exit(1)
    return password


def confirm_reset(username, skip):
    if skip:
        return True
    print("-" * 50)
    print("即将重置管理员密码：")
    print(f"  用户名 : {username}")
    print("-" * 50)
    try:
        answer = input("确认执行？输入 y/yes 继续，其他任意键取消: ").strip().lower()
    except EOFError:
        print("[失败] 非交互环境下需传入 --yes 参数才能执行（防止误操作）")
        return False
    return answer in ("y", "yes")


def main():
    args = sys.argv[1:]
    skip = "--yes" in args
    use_stdin = "--stdin" in args

    if any(arg == "--password" or arg.startswith("--password=") for arg in args):
        print("[失败] 出于安全原因已移除 --password 参数。")
        print("       交互式请直接运行；自动化请使用: python reset_password.py --stdin --yes")
        sys.exit(1)

    password = _read_password(use_stdin)

    db_path = resolve_db_path()
    print("=" * 50)
    print("  管理员密码重置工具")
    print("=" * 50)

    if not os.path.exists(db_path):
        print(f"[失败] 未找到数据库文件: {db_path}")
        print("       请确认脚本位于项目根目录，或通过 DATABASE_PATH 环境变量指定路径")
        sys.exit(1)

    if not confirm_reset(DEFAULT_USERNAME, skip):
        print("[已取消] 未执行任何修改")
        sys.exit(0)

    password_hash = hash_password(password)
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                row = conn.execute(
                    "SELECT id FROM admins WHERE username=?", (DEFAULT_USERNAME,)
                ).fetchone()

                if row:
                    admin_id = row["id"]
                    conn.execute(
                        "UPDATE admins SET password_hash=?, login_attempts=0, locked_until=NULL "
                        "WHERE id=?",
                        (password_hash, admin_id),
                    )
                    # 密码重置后所有旧会话必须失效
                    conn.execute("DELETE FROM sessions WHERE admin_id=?", (admin_id,))
                else:
                    import datetime
                    created_at = datetime.datetime.now(datetime.timezone.utc).isoformat(
                        timespec="seconds"
                    )
                    conn.execute(
                        "INSERT INTO admins (username, password_hash, login_attempts, locked_until, created_at) "
                        "VALUES (?, ?, 0, NULL, ?)",
                        (DEFAULT_USERNAME, password_hash, created_at),
                    )
                    admin_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                    conn.execute("DELETE FROM sessions WHERE admin_id=?", (admin_id,))

                # 清空登录失败限流，避免重置后仍被旧 IP 计数拦截
                conn.execute("DELETE FROM login_rate_limits")
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        print(f"[失败] 数据库操作出错: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"[失败] 发生未预期错误: {e}")
        sys.exit(1)

    print(f"[成功] 管理员 '{DEFAULT_USERNAME}' 密码已重置，旧会话已全部撤销")
    print("  !! 出于安全考虑，新密码不会再次显示；请立即登录后台确认 !!")
    print("=" * 50)


if __name__ == "__main__":
    main()
