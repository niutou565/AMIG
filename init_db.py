"""数据库初始化脚本：建表、写入默认配置、创建管理员、预置示例内容。

幂等：可重复执行。老版本直接升级到 1.2.5 也由启动流程调用这里的逻辑，
自动补齐缺失的表/列/索引，原有数据与配置保持不变。

安装向导会通过环境变量提供管理员账号与密码：
- AMIG_ADMIN_USERNAME（默认 admin）
- AMIG_ADMIN_PASSWORD（未提供时随机生成并打印一次）
"""
import os
import sys
import secrets
import bcrypt
from datetime import datetime, timezone

# 确保项目路径在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.database import init_db, get_db
from app.models import now_iso, today_str
from app.admin_domains import ensure_admin_domain_initialized, normalize_admin_domains
from app.config import ADMIN_DOMAIN


def create_default_admin(db, username=None, password=None):
    """创建默认管理员（已存在则跳过），返回 (username, password_or_None)。"""
    username = (username or os.getenv("AMIG_ADMIN_USERNAME") or "admin").strip()
    password = password or os.getenv("AMIG_ADMIN_PASSWORD")
    generated = False
    if not password:
        password = secrets.token_hex(16)
        generated = True
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    existing = db.execute("SELECT id FROM admins WHERE username=?", (username,)).fetchone()
    if existing:
        return username, None
    db.execute(
        "INSERT INTO admins (username, password_hash, created_at) VALUES (?,?,?)",
        (username, password_hash, now_iso())
    )
    db.commit()
    return username, password if generated else None


def seed_default_settings(db):
    """写入缺失的默认站点配置（已有键不覆盖）。"""
    default_settings = {
        "site_name": "匿名图片分享",
        "site_description": "一个简单的匿名图片分享社区",
        "footer_icp": "",
        "footer_copyright": "© 2024 匿名图片分享",
        "theme_accent_color": "#c9a063",
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
        "admin_domain": ",".join(normalize_admin_domains(ADMIN_DOMAIN)[0]),
    }
    for key, value in default_settings.items():
        existing = db.execute("SELECT `key` FROM settings WHERE `key`=?", (key,)).fetchone()
        if not existing:
            db.execute(
                "INSERT INTO settings (`key`, value) VALUES (?,?)", (key, value)
            )
    db.commit()
    # 旧库迁移：数据库未配置管理域名时，用 .env 的 ADMIN_DOMAIN 初始化一次
    ensure_admin_domain_initialized()


def seed_default_pages(db):
    """预置示例页面（已存在则跳过）。"""
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


def seed_default_links(db):
    """预置友情链接（已存在则跳过）。"""
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
    username, generated_password = create_default_admin(db)
    if generated_password:
        print("  -> 管理员已创建")
        print(f"     用户名: {username}")
        print(f"     密码:   {generated_password}")
        print("     !! 请妥善保管此密码，登录后请立即修改 !!")
    else:
        print("  -> 管理员已存在，跳过创建")

    # 3. 写入默认配置
    print("\n[3/5] 写入默认配置...")
    seed_default_settings(db)
    print("  -> 完成")

    # 4. 预置示例页面
    print("\n[4/5] 预置示例页面...")
    seed_default_pages(db)
    print("  -> 完成")

    # 5. 预置友情链接
    print("\n[5/5] 预置友情链接...")
    seed_default_links(db)
    print("  -> 完成")

    print("\n" + "=" * 50)
    print("  初始化完成！")
    print("=" * 50)
    print(f"\n  启动命令: python run.py")
    print(f"  或: uvicorn app.main:app --host 127.0.0.1 --port 8000")


if __name__ == "__main__":
    main()
