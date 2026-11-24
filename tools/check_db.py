#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据库健康检查工具（MySQL，严格只读：只执行 SELECT / SHOW / CHECK TABLE）。

用法：
    python tools/check_db.py                # 输出健康报告
    python tools/check_db.py --json         # 机器可读输出
    python tools/check_db.py --deep         # 额外执行 CHECK TABLE（大库较慢）

检查项：连通性、MySQL 版本、字符集、业务表齐全、schema 迁移记录、行数、
审核状态一致性、点赞计数漂移、孤儿点赞记录、备份目录安全与保留状态。

退出码：0 表示无 critical 问题；1 表示存在 critical 问题或无法检查。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config
from app import database
from app.database import EXPECTED_TABLES, DatabaseError, get_db, get_db_status

# 持续统计的业务表（行数用于快速判断库是否"活着"）
COUNT_TABLES = ("images", "likes", "admin_logs", "sessions", "view_sessions")
# 索引数量低于该值提示初始化可能不完整
MIN_INDEX_COUNT = 15


def _human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.2f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return f"{n:.2f} GB"


def _check_paths(report):
    """备份目录不得位于 Web 可访问路径下，否则备份可被直接下载。"""
    backup = os.path.abspath(str(config.BACKUP_DIR))
    for label, directory in (("uploads", config.UPLOADS_DIR), ("private_uploads", config.PRIVATE_UPLOADS_DIR)):
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
            if n.startswith("backup_") and n.endswith(".sql")
        ])
        report["backup_count"] = count
        if count == 0:
            report["warnings"].append("备份目录为空，建议至少保留一份可恢复备份")


def _check_tables(db, report):
    rows = db.execute(
        "SELECT table_name AS name FROM information_schema.tables "
        "WHERE table_schema=? AND table_type='BASE TABLE'",
        (str(database.MYSQL_DATABASE),),
    ).fetchall()
    names = {r["name"] for r in rows}
    report["tables"] = sorted(names)
    missing = set(EXPECTED_TABLES) - names
    if missing:
        report["critical"].append(f"缺少业务表: {', '.join(sorted(missing))}")
    index_count = db.execute(
        "SELECT COUNT(DISTINCT index_name) AS c FROM information_schema.statistics "
        "WHERE table_schema=? AND index_name <> 'PRIMARY'",
        (str(database.MYSQL_DATABASE),),
    ).fetchone()["c"]
    report["index_count"] = index_count
    if index_count < MIN_INDEX_COUNT:
        report["warnings"].append(f"索引数量异常偏少（{index_count}），可能未执行完整初始化")


def _check_versions(db, report):
    try:
        rows = db.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        versions = [r["version"] for r in rows]
        report["schema_versions"] = versions
        if not versions:
            report["warnings"].append("schema_migrations 为空，数据库可能从未完成初始化迁移")
    except DatabaseError as exc:
        report["warnings"].append(f"无法读取 schema_migrations: {exc}")


def _check_data(db, report):
    counts = {}
    for table in COUNT_TABLES:
        try:
            counts[table] = db.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
        except DatabaseError:
            counts[table] = None
    report["row_counts"] = counts
    # 状态一致性：approved 的图片必须有 reviewed_at，否则审核状态机被绕过
    try:
        bad = db.execute(
            "SELECT COUNT(*) AS c FROM images WHERE status='approved' AND reviewed_at IS NULL"
        ).fetchone()["c"]
        if bad:
            report["warnings"].append(f"{bad} 条 approved 图片缺少 reviewed_at（状态机异常）")
    except DatabaseError:
        pass
    # 点赞计数漂移：images.like_count 应与 likes 实际行数一致
    try:
        drift = db.execute(
            "SELECT COUNT(*) AS c FROM images i WHERE i.like_count <> "
            "(SELECT COUNT(*) FROM likes l WHERE l.image_id = i.id)"
        ).fetchone()["c"]
        report["like_count_drift"] = drift
        if drift:
            report["warnings"].append(f"{drift} 条图片的 like_count 与点赞记录数不一致")
    except DatabaseError:
        pass
    # 孤儿点赞：正常由外键约束阻止，恢复/手工导入后可能出现
    try:
        orphan = db.execute(
            "SELECT COUNT(*) AS c FROM likes l LEFT JOIN images i ON l.image_id = i.id "
            "WHERE i.id IS NULL"
        ).fetchone()["c"]
        if orphan:
            report["critical"].append(f"存在 {orphan} 条孤儿点赞记录（image_id 无对应图片）")
    except DatabaseError:
        pass


def _check_charset(report):
    status = report.get("server", {})
    charset = status.get("charset")
    collation = status.get("collation")
    if charset and charset.lower() != "utf8mb4":
        report["critical"].append(
            f"数据库默认字符集为 {charset}（应为 utf8mb4），中文/emoji 可能写入失败"
        )
    elif charset:
        report["charset_ok"] = True


def _check_deep(db, report):
    """CHECK TABLE 检查 InnoDB 表内部一致性（--deep 时执行）。"""
    results = {}
    for table in sorted(EXPECTED_TABLES):
        try:
            row = db.execute(f"CHECK TABLE {table}").fetchone()
            # 列名随驱动返回（Table/Op/Msg_type/Msg_text）
            msg_type = row.get("Msg_type") if row else None
            msg_text = row.get("Msg_text") if row else None
            results[table] = f"{msg_type}: {msg_text}" if row else "no result"
            if msg_type == "Error" or (msg_text and msg_text != "OK"):
                report["critical"].append(f"CHECK TABLE {table}: {msg_text}")
        except DatabaseError as exc:
            results[table] = f"error: {exc}"
            report["critical"].append(f"CHECK TABLE {table} 执行失败: {exc}")
    report["check_table"] = results


def collect(deep=False):
    report = {
        "ok": False,
        "database": str(database.MYSQL_DATABASE),
        "host": f"{config.MYSQL_HOST}:{config.MYSQL_PORT}",
        "critical": [],
        "warnings": [],
    }
    try:
        db = get_db()
        db.execute("SELECT 1")
    except Exception as exc:
        report["critical"].append(
            f"无法连接 MySQL {config.MYSQL_HOST}:{config.MYSQL_PORT}/{database.MYSQL_DATABASE}: "
            f"{type(exc).__name__}"
        )
        report["error_hint"] = (
            "请检查 MYSQL_HOST / MYSQL_PORT / MYSQL_DATABASE / MYSQL_USER / MYSQL_PASSWORD 配置"
        )
        report["ok"] = not report["critical"]
        return report

    try:
        report["server"] = get_db_status()
        _check_charset(report)
        _check_tables(db, report)
        _check_versions(db, report)
        _check_data(db, report)
        if deep:
            _check_deep(db, report)
    finally:
        from app.database import close_db

        close_db()

    _check_paths(report)
    report["ok"] = not report["critical"]
    return report


def main():
    parser = argparse.ArgumentParser(description="AMIG 数据库健康检查（MySQL，只读）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出报告")
    parser.add_argument("--deep", action="store_true",
                        help="额外执行 CHECK TABLE（大数据库较慢）")
    args = parser.parse_args()

    try:
        report = collect(deep=args.deep)
    except Exception as exc:  # 工具本身出错也要给出可读结论
        print(f"[失败] 健康检查异常: {type(exc).__name__}: {exc}")
        return 1

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1

    print("=" * 60)
    print("AMIG 数据库健康检查")
    print("=" * 60)
    print(f"数据库: {report['database']} @ {report['host']}")
    server = report.get("server", {})
    print(f"MySQL 版本: {server.get('server_version', '-')}")
    print(f"连接池: 空闲 {server.get('pool_idle', '-')} 个")
    if server.get("charset"):
        print(f"字符集: {server.get('charset')} / {server.get('collation')}")
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

    if report["critical"]:
        print(f"\n[严重] {len(report['critical'])} 项")
        for item in report["critical"]:
            print(f"  - {item}")
    if report.get("error_hint"):
        print(f"  提示: {report['error_hint']}")
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
