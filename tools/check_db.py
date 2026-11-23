#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据库健康检查工具（严格只读）。

用法：
    python tools/check_db.py                # 输出健康报告
    python tools/check_db.py --json         # 机器可读输出
    python tools/check_db.py --deep         # 额外执行 integrity_check（大库较慢）

默认只读：以 SQLite URI `mode=ro` 打开数据库，不写任何数据、不执行
checkpoint、不创建 -wal / -shm 文件。检查项与判定阈值见下方常量。

退出码：0 表示无 critical 问题；1 表示存在 critical 问题或无法检查。
"""
import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import (
    BACKUP_DIR,
    DATABASE_PATH,
    DB_BUSY_TIMEOUT_MS,
    DB_SYNCHRONOUS,
    DB_WAL_AUTOCHECKPOINT_PAGES,
    PRIVATE_UPLOADS_DIR,
    UPLOADS_DIR,
)

# WAL 文件超过该大小说明长期未 checkpoint，读性能与恢复时间都会受影响
WAL_WARN_BYTES = 64 * 1024 * 1024
# busy_timeout 低于此值在多 worker 写入时容易直接抛 "database is locked"
BUSY_TIMEOUT_WARN_MS = 1000
# 业务表清单：缺失任意一张都视为结构异常（与 app/database.py 的建表语句保持一致）
EXPECTED_TABLES = {
    "images", "likes", "admin_logs", "admins", "sessions", "settings",
    "friend_links", "pages", "ban_list", "cooling", "login_rate_limits",
    "daily_stats", "view_sessions", "schema_migrations",
}


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.2f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return f"{n:.2f} GB"


def _check_paths(report):
    """备份目录不得位于 Web 可访问路径下，否则备份可被直接下载。"""
    backup = os.path.abspath(str(BACKUP_DIR))
    for label, directory in (("uploads", UPLOADS_DIR), ("private_uploads", PRIVATE_UPLOADS_DIR)):
        target = os.path.abspath(str(directory))
        try:
            inside = os.path.commonpath([backup, target]) == target
        except ValueError:
            inside = False
        if inside:
            report["critical"].append(
                f"备份目录位于 {label} 内，备份文件可能被 Web 直接访问: {backup}"
            )
    if not os.path.isdir(backup):
        report["warnings"].append(f"备份目录不存在（尚未执行备份）: {backup}")
    else:
        # 只统计数据库备份；_uploads.tar.gz 归档与回退点不计入保留份数
        count = len([
            n for n in os.listdir(backup)
            if n.startswith("backup_") and n.endswith(".db")
        ])
        report["backup_count"] = count
        if count == 0:
            report["warnings"].append("备份目录为空，建议至少保留一份可恢复备份")
    # 数据库文件不应落在上传目录中
    db_abs = os.path.abspath(str(DATABASE_PATH))
    for label, directory in (("uploads", UPLOADS_DIR), ("private_uploads", PRIVATE_UPLOADS_DIR)):
        target = os.path.abspath(str(directory))
        try:
            if os.path.commonpath([db_abs, target]) == target:
                report["critical"].append(f"数据库文件位于 {label} 目录内，存在泄露风险: {db_abs}")
        except ValueError:
            pass


def _check_tables(conn, report):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    names = {r[0] for r in rows}
    report["tables"] = sorted(names)
    missing = EXPECTED_TABLES - names
    if missing:
        report["critical"].append(f"缺少业务表: {', '.join(sorted(missing))}")
    index_count = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='index'"
    ).fetchone()[0]
    report["index_count"] = index_count
    if index_count < 20:
        report["warnings"].append(f"索引数量异常偏少（{index_count}），可能未执行完整初始化")


def _check_versions(conn, report):
    try:
        rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
        versions = [r[0] for r in rows]
        report["schema_versions"] = versions
        if not versions:
            report["warnings"].append("schema_migrations 为空，数据库可能从未完成初始化迁移")
    except sqlite3.Error as exc:
        report["warnings"].append(f"无法读取 schema_migrations: {exc}")


def _check_data(conn, report):
    counts = {}
    for table in ("images", "likes", "admin_logs", "sessions", "view_sessions"):
        try:
            counts[table] = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        except sqlite3.Error:
            counts[table] = None
    report["row_counts"] = counts
    # 状态一致性：approved 的图片必须有 reviewed_at，否则审核状态机被绕过
    try:
        bad = conn.execute(
            "SELECT COUNT(*) FROM images WHERE status='approved' AND reviewed_at IS NULL"
        ).fetchone()[0]
        if bad:
            report["warnings"].append(f"{bad} 条 approved 图片缺少 reviewed_at（状态机异常）")
    except sqlite3.Error:
        pass
    # 点赞计数漂移：images.like_count 应与 likes 实际行数一致
    try:
        drift = conn.execute(
            "SELECT COUNT(*) FROM images i WHERE i.like_count <> "
            "(SELECT COUNT(*) FROM likes l WHERE l.image_id = i.id)"
        ).fetchone()[0]
        report["like_count_drift"] = drift
        if drift:
            report["warnings"].append(f"{drift} 条图片的 like_count 与点赞记录数不一致")
    except sqlite3.Error:
        pass


def _check_integrity(conn, report, deep):
    try:
        report["journal_mode"] = conn.execute("PRAGMA journal_mode").fetchone()[0]
    except sqlite3.Error as exc:
        report["critical"].append(f"无法读取 journal_mode: {exc}")
        return
    if str(report["journal_mode"]).lower() != "wal":
        report["warnings"].append(
            f"journal_mode 为 {report['journal_mode']}（非 WAL），多 worker 并发读会互相阻塞"
        )
    try:
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk:
            report["critical"].append(f"存在 {len(fk)} 条外键违规")
    except sqlite3.Error as exc:
        report["warnings"].append(f"foreign_key_check 失败: {exc}")
    if deep:
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            result = (row[0] if row else "") or ""
            report["integrity_check"] = result
            if result.lower() != "ok":
                report["critical"].append(f"integrity_check 未通过: {result[:200]}")
        except sqlite3.Error as exc:
            report["critical"].append(f"integrity_check 执行失败: {exc}")


def _check_files(report):
    db = str(DATABASE_PATH)
    report["db_size"] = _size(db)
    wal = db + "-wal"
    shm = db + "-shm"
    report["wal_size"] = _size(wal)
    report["shm_size"] = _size(shm)
    if report["wal_size"] > WAL_WARN_BYTES:
        report["warnings"].append(
            f"WAL 文件 {_human(report['wal_size'])} 偏大，建议执行 checkpoint(TRUNCATE)"
        )
    if not os.access(db, os.R_OK):
        report["critical"].append(f"数据库文件不可读: {db}")


def collect(deep=False):
    report = {
        "ok": False,
        "database": str(DATABASE_PATH),
        "sqlite_version": sqlite3.sqlite_version,
        "expected": {
            "busy_timeout_ms": DB_BUSY_TIMEOUT_MS,
            "synchronous": DB_SYNCHRONOUS,
            "wal_autocheckpoint_pages": DB_WAL_AUTOCHECKPOINT_PAGES,
        },
        "critical": [],
        "warnings": [],
    }
    db_path = str(DATABASE_PATH)
    if not os.path.exists(db_path):
        report["critical"].append(f"数据库文件不存在: {db_path}")
        return report

    _check_files(report)

    uri = "file:" + db_path.replace("\\", "/") + "?mode=ro"
    try:
        # 只用只读 URI 连接：不会创建 -wal/-shm，也不会触发 checkpoint
        conn = sqlite3.connect(uri, uri=True, timeout=DB_BUSY_TIMEOUT_MS / 1000.0)
    except sqlite3.Error as exc:
        report["critical"].append(f"无法打开数据库: {exc}")
        return report
    try:
        conn.row_factory = sqlite3.Row
        try:
            report["busy_timeout"] = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        except sqlite3.Error:
            report["busy_timeout"] = None
        try:
            report["synchronous"] = conn.execute("PRAGMA synchronous").fetchone()[0]
        except sqlite3.Error:
            report["synchronous"] = None
        if isinstance(report["busy_timeout"], int) and report["busy_timeout"] < BUSY_TIMEOUT_WARN_MS:
            report["warnings"].append(
                f"busy_timeout={report['busy_timeout']}ms 偏低，多 worker 写入容易直接报锁错误"
            )
        try:
            report["page_count"] = conn.execute("PRAGMA page_count").fetchone()[0]
            report["page_size"] = conn.execute("PRAGMA page_size").fetchone()[0]
            report["freelist_count"] = conn.execute("PRAGMA freelist_count").fetchone()[0]
        except sqlite3.Error:
            pass
        _check_integrity(conn, report, deep)
        _check_tables(conn, report)
        _check_versions(conn, report)
        _check_data(conn, report)
    finally:
        conn.close()

    _check_paths(report)
    report["ok"] = not report["critical"]
    return report


def main():
    parser = argparse.ArgumentParser(description="AMIG 数据库健康检查（只读）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出报告")
    parser.add_argument("--deep", action="store_true",
                        help="额外执行 integrity_check（大数据库较慢）")
    args = parser.parse_args()

    try:
        report = collect(deep=args.deep)
    except Exception as exc:  # 工具本身出错也要给出可读结论
        print(f"[失败] 健康检查异常: {exc}")
        return 1

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1

    print("=" * 60)
    print("AMIG 数据库健康检查")
    print("=" * 60)
    print(f"数据库: {report['database']}")
    print(f"SQLite: {report.get('sqlite_version')}")
    if "db_size" in report:
        print(f"主库大小: {_human(report['db_size'])}")
    if "wal_size" in report:
        print(f"WAL 大小: {_human(report['wal_size'])}")
        print(f"SHM 大小: {_human(report['shm_size'])}")
    if "page_count" in report:
        freelist = report.get("freelist_count", 0)
        print(f"页数: {report['page_count']} × {report['page_size']}B，空闲页: {freelist}")
    if "journal_mode" in report:
        print(f"journal_mode: {report['journal_mode']}")
    if "synchronous" in report:
        print(f"synchronous: {report['synchronous']}")
    if "busy_timeout" in report:
        print(f"busy_timeout: {report['busy_timeout']}ms")
    if "tables" in report:
        print(f"业务表: {len(report['tables'])} 张，索引: {report.get('index_count', 0)} 个")
    if "schema_versions" in report:
        print(f"schema 版本: {report['schema_versions']}")
    if "row_counts" in report:
        counts = ", ".join(f"{k}={v}" for k, v in report["row_counts"].items())
        print(f"记录数: {counts}")
    if "like_count_drift" in report:
        print(f"点赞计数漂移: {report['like_count_drift']} 条")
    if "backup_count" in report:
        print(f"备份份数: {report['backup_count']}")
    if report.get("integrity_check"):
        print(f"integrity_check: {report['integrity_check']}")

    if report["critical"]:
        print(f"\n[严重] {len(report['critical'])} 项")
        for item in report["critical"]:
            print(f"  - {item}")
    if report["warnings"]:
        print(f"\n[警告] {len(report['warnings'])} 项")
        for item in report["warnings"]:
            print(f"  - {item}")
    if report["ok"] and not report["warnings"]:
        print("\n[通过] 未发现问题")
    elif report["ok"]:
        print("\n[通过] 无严重问题，但存在警告项")
    else:
        print("\n[失败] 存在严重问题，请先处理后再对外服务")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
