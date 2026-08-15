"""数据库初始化脚本：建表、写入默认配置、创建管理员、预置示例内容"""
import os
import sys
import secrets
import bcrypt
from datetime import datetime, timezone

# 确保项目路径在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.database import init_db, get_db
from app.models import now_iso, today_str


def main():
    print("=" * 50)
    print("  匿名图片分享站 - 数据库初始化")
    print("=" * 50)

    # 1. 建表
    print("\n[1/5] 创建数据库表结构...")
    init_db()
    print("  -> 完成")

    db = get_db()

    # 2. 创建管理员
    print("\n[2/5] 创建默认管理员...")
    admin_username = "admin"
    admin_password = secrets.token_hex(8)
    password_hash = bcrypt.hashpw(admin_password.encode(), bcrypt.gensalt()).decode()

    existing = db.execute("SELECT id FROM admins WHERE username=?", (admin_username,)).fetchone()
    if existing:
        print(f"  -> 管理员已存在，跳过创建")
    else:
        db.execute(
            "INSERT INTO admins (username, password_hash, created_at) VALUES (?,?,?)",
            (admin_username, password_hash, now_iso())
        )
        db.commit()
        print(f"  -> 管理员已创建")
        print(f"     用户名: {admin_username}")
        print(f"     密码:   {admin_password}")
        print(f"     !! 请妥善保管此密码，登录后请立即修改 !!")

    # 3. 写入默认配置
    print("\n[3/5] 写入默认配置...")
    default_settings = {
        "site_name": "匿名图片分享",
        "site_description": "一个简单的匿名图片分享社区",
        "footer_icp": "",
        "footer_copyright": "© 2024 匿名图片分享",
        "theme_accent_color": "#6c5ce7",
        "upload_limit_per_ip": "10",
        "upload_limit_per_ua": "10",
        "upload_limit_daily_total": "500",
        "upload_interval_seconds": "30",
        "like_limit_per_minute": "10",
        "top_window_days": "30",
        "top_show_count": "12",
        "allow_author": "1",
        "enable_captcha": "0",
        "enable_dedup": "1",
        "sensitive_words": "",
        "admin_domain": "",
    }
    for key, value in default_settings.items():
        existing = db.execute("SELECT key FROM settings WHERE key=?", (key,)).fetchone()
        if not existing:
            db.execute(
                "INSERT INTO settings (key, value) VALUES (?,?)", (key, value)
            )
    db.commit()
    print("  -> 完成")

    # 4. 预置示例页面
    print("\n[4/5] 预置示例页面...")
    pages = [
        ("about", "关于我们", "这里是关于我们的介绍。", 0),
        ("rules", "使用须知", "请遵守以下规则：\n1. 请勿上传违法内容\n2. 请尊重他人隐私\n3. 禁止上传色情、暴力等内容", 1),
        ("copyright", "版权声明", "本站所有图片版权归上传者所有。", 2),
    ]
    for slug, title, content, sort in pages:
        existing = db.execute("SELECT id FROM pages WHERE slug=?", (slug,)).fetchone()
        if not existing:
            db.execute(
                "INSERT INTO pages (slug, title, content, sort_order, is_visible, updated_at) VALUES (?,?,?,?,1,?)",
                (slug, title, content, sort, now_iso())
            )
    db.commit()
    print("  -> 完成")

    # 5. 预置友情链接
    print("\n[5/5] 预置友情链接...")
    links = [
        ("GitHub", "https://github.com", 0),
        ("Python", "https://python.org", 1),
    ]
    for name, url, sort in links:
        existing = db.execute("SELECT id FROM friend_links WHERE name=?", (name,)).fetchone()
        if not existing:
            db.execute(
                "INSERT INTO friend_links (name, url, sort_order, is_visible, created_at) VALUES (?,?,?,1,?)",
                (name, url, sort, now_iso())
            )
    db.commit()
    print("  -> 完成")

    print("\n" + "=" * 50)
    print("  初始化完成！")
    print("=" * 50)
    print(f"\n  请确保 data/ip2region.xdb 文件已放置到项目 data 目录下")
    print(f"  下载地址: https://github.com/lionsoul2014/ip2region")
    print(f"\n  启动命令: python run.py")
    print(f"  或: uvicorn app.main:app --host 127.0.0.1 --port 8000")


if __name__ == "__main__":
    main()