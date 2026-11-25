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
import getpass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DEFAULT_USERNAME = "admin"


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

    print("=" * 50)
    print("  管理员密码重置工具")
    print("=" * 50)

    if not confirm_reset(DEFAULT_USERNAME, skip):
        print("[已取消] 未执行任何修改")
        sys.exit(0)

    password_hash = hash_password(password)
    try:
        from app.database import get_db, transaction, DatabaseError
        from app.models import now_iso

        db = get_db()
        with transaction():
            row = db.execute(
                "SELECT id FROM admins WHERE username=?", (DEFAULT_USERNAME,)
            ).fetchone()

            if row:
                admin_id = row["id"]
                db.execute(
                    "UPDATE admins SET password_hash=?, login_attempts=0, locked_until=NULL "
                    "WHERE id=?",
                    (password_hash, admin_id),
                )
                # 密码重置后所有旧会话必须失效
                db.execute("DELETE FROM sessions WHERE admin_id=?", (admin_id,))
            else:
                db.execute(
                    "INSERT INTO admins (username, password_hash, login_attempts, locked_until, created_at) "
                    "VALUES (?, ?, 0, NULL, ?)",
                    (DEFAULT_USERNAME, password_hash, now_iso()),
                )
                admin_id = db.execute(
                    "SELECT id FROM admins WHERE username=?", (DEFAULT_USERNAME,)
                ).fetchone()["id"]
                db.execute("DELETE FROM sessions WHERE admin_id=?", (admin_id,))

            # 清空登录失败限流，避免重置后仍被旧 IP 计数拦截
            db.execute("DELETE FROM login_rate_limits")
    except DatabaseError as e:
        print(f"[失败] 数据库连接或操作出错: {type(e).__name__}")
        print("       请检查 .env 的 DB_TYPE；sqlite 检查 DATABASE_PATH，")
        print("       mysql 检查 MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD / MYSQL_DATABASE")
        print("       （详细错误信息已写入应用日志，不会显示数据库密码）")
        sys.exit(1)
    except Exception as e:
        print(f"[失败] 发生未预期错误: {type(e).__name__}: {e}")
        sys.exit(1)

    print(f"[成功] 管理员 '{DEFAULT_USERNAME}' 密码已重置，旧会话已全部撤销")
    print("  !! 出于安全考虑，新密码不会再次显示；请立即登录后台确认 !!")
    print("=" * 50)


if __name__ == "__main__":
    main()
