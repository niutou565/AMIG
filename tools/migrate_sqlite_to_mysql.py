#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQLite → MySQL 完整数据迁移工具。

把已有 AMIG SQLite 数据库（含图片关联、点赞、管理员、设置、日志、统计、
封禁记录等）完整迁移到 MySQL，保留原始记录 ID 与表间关联关系。

安全设计：
- SQLite 源库以只读模式（mode=ro）打开，迁移过程绝不写入、修改或删除源库；
- 迁移前打印数据统计并要求确认；
- 目标库任何业务表非空时拒绝执行，避免重复导入或覆盖现有数据；
- 迁移在每表一个事务内进行，出错时给出具体表名、行号与原因并中止；
- 迁移完成后逐表对比行数，并对每张表做逐行内容级校验。

用法：
    python tools/migrate_sqlite_to_mysql.py            # 交互确认
    python tools/migrate_sqlite_to_mysql.py --yes      # 跳过确认（自动化）
    python tools/migrate_sqlite_to_mysql.py --source data/app.db --yes
前置条件：
- 已按 README 配置好 MYSQL_* 环境变量；
- 目标 MySQL 库中不存在业务数据（全新库，或仅执行过 init_db.py）。
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import DATABASE_PATH
import app.database as database
from app.database import DatabaseError, MYSQL_DATABASE

# 表清单按外键依赖排序（likes 引用 images）
TABLES = [
    "images", "likes", "ban_list", "admins", "sessions", "login_rate_limits",
    "settings", "upload_reservations", "pages", "friend_links", "daily_stats",
    "admin_logs", "view_sessions", "cooling",
]
ALL_TABLES = TABLES + ["schema_migrations"]
# 各表用于稳定排序/校验的列；未列出的表用自增 id
ORDER_COLUMN = {
    "sessions": "session_id",
    "settings": "`key`",
    "login_rate_limits": "`key`",
    "daily_stats": "date",
    "schema_migrations": "version",
}


def order_column_for(table, columns):
    """返回该表的排序/比对列：优先用 ORDER_COLUMN，其次 id，最后第一列。"""
    column = ORDER_COLUMN.get(table)
    if column:
        return column
    if "id" in columns:
        return "id"
    return columns[0]
BATCH_SIZE = 200


def open_sqlite_readonly(path):
    uri = "file:" + path.replace("\\", "/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def file_digest(path):
    """计算源库文件 SHA-256，用于证明迁移前后源文件未被修改。"""
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sqlite_table_exists(src, table):
    row = src.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def sqlite_existing_tables(src):
    """源库实际存在的业务表（早期版本可能缺少较新的表）。"""
    return [t for t in ALL_TABLES if sqlite_table_exists(src, t)]


def sqlite_table_columns(src, table):
    cur = src.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in cur.fetchall()]


def sqlite_row_count(src, table):
    return src.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def mysql_row_count(table):
    return database.get_db().execute(f"SELECT COUNT(*) AS c FROM `{table}`").fetchone()["c"]


def precheck_target_empty():
    """目标库业务表必须为空，防止重复导入/覆盖。schema_migrations 允许非空（init_db 写入版本号）。"""
    problems = []
    for table in TABLES:
        cnt = mysql_row_count(table)
        if cnt:
            problems.append(f"{table}: {cnt} 行")
    return problems


def migrate_table(src, table, columns):
    """把一张表从 SQLite 迁入 MySQL（每表一个事务），返回行数。"""
    db = database.get_db()
    db.execute("SET FOREIGN_KEY_CHECKS=0")
    order_col = order_column_for(table, columns)
    rows = src.execute(
        f"SELECT {', '.join(columns)} FROM {table} ORDER BY {order_col}"
    ).fetchall()
    col_sql = ", ".join(f"`{c}`" for c in columns)
    ph = ", ".join("?" for _ in columns)
    insert_sql = f"INSERT INTO `{table}` ({col_sql}) VALUES ({ph})"
    pk_index = columns.index("id") if "id" in columns else 0
    total = 0
    current_no = 0
    current_pk = None
    try:
        db.begin()
        batch = []
        for idx, row in enumerate(rows, start=1):
            current_no = idx
            current_pk = row[pk_index]
            batch.append(tuple(row[i] for i in range(len(columns))))
            if len(batch) >= BATCH_SIZE:
                for values in batch:
                    db.execute(insert_sql, values)
                total += len(batch)
                batch = []
        for values in batch:
            db.execute(insert_sql, values)
        total += len(batch)
        # 恢复自增序列（有自增 id 的表）
        if table not in ORDER_COLUMN and any(c.lower() == "id" for c in columns):
            max_id = src.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()[0]
            if max_id:
                db.execute(f"ALTER TABLE `{table}` AUTO_INCREMENT = {int(max_id) + 1}")
        db.commit()
    except Exception as exc:
        db.rollback()
        raise SystemExit(
            f"[失败] 迁移表 {table} 第 {current_no} 行（主键 {current_pk}）出错: "
            f"{type(exc).__name__}: {exc}\n"
            "       该表在目标库中已回滚；已迁移完成的表保持不变。"
            "修复源数据后请先清空目标库再重新运行本工具。"
        ) from exc
    finally:
        db.execute("SET FOREIGN_KEY_CHECKS=1")
    return total


def precheck_lengths(src, tables):
    """迁移前检查文本长度是否会超出目标 VARCHAR 限制，返回问题列表。

    旧库中 TEXT 字段可能存有超过新库 VARCHAR 上限的内容（如超长描述/UA）。
    提前发现并给出表名、字段与超限行，避免插入到一半才失败。
    """
    db = database.get_db()
    targets = {}
    rows = db.execute(
        "SELECT TABLE_NAME AS table_name, COLUMN_NAME AS column_name, "
        "CHARACTER_MAXIMUM_LENGTH AS max_len "
        "FROM information_schema.columns "
        "WHERE table_schema=? AND data_type IN ('varchar','char') "
        "AND CHARACTER_MAXIMUM_LENGTH IS NOT NULL",
        (str(MYSQL_DATABASE),),
    ).fetchall()
    for row in rows:
        targets.setdefault(row["table_name"], {})[row["column_name"]] = int(row["max_len"])
    problems = []
    for table in tables:
        if table not in targets:
            continue
        src_cols = set(sqlite_table_columns(src, table))
        for column, limit in targets[table].items():
            if column not in src_cols:
                continue
            row = src.execute(
                f"SELECT COUNT(*) AS c, COALESCE(MAX(LENGTH(`{column}`)), 0) AS m "
                f"FROM {table}"
            ).fetchone()
            if row["m"] and row["m"] > limit:
                problems.append(
                    f"{table}.{column}: 源库最长 {row['m']} 字符，目标上限 {limit} 字符"
                    f"（{row['c']} 行受影响风险）"
                )
    return problems


def migrate_schema_migrations(src, columns):
    """结构迁移版本表：清空 init_db 写入的版本号，保留源库版本历史。"""
    db = database.get_db()
    rows = src.execute(
        f"SELECT {', '.join(columns)} FROM schema_migrations ORDER BY version"
    ).fetchall()
    db.begin()
    try:
        db.execute("DELETE FROM schema_migrations")
        ph = ", ".join("?" for _ in columns)
        for row in rows:
            db.execute(
                f"INSERT INTO schema_migrations ({', '.join('`%s`' % c for c in columns)}) VALUES ({ph})",
                tuple(row[i] for i in range(len(columns))),
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return len(rows)


def _values_equal(src_value, dst_value):
    """内容级比对：NULL 与 NULL 相等；数值按数值比；其余按字符串比。"""
    if src_value is None or dst_value is None:
        return src_value is None and dst_value is None
    if isinstance(src_value, bool) or isinstance(dst_value, bool):
        return bool(src_value) == bool(dst_value)
    if isinstance(src_value, (int, float)) and isinstance(dst_value, (int, float)):
        return float(src_value) == float(dst_value)
    return str(src_value) == str(dst_value)


def verify_tables(src, tables):
    """逐表行数对比 + 逐行内容级校验。返回 (失败列表, 校验行数总计)。"""
    failures = []
    verified = 0
    db = database.get_db()
    for table in tables:
        src_count = sqlite_row_count(src, table)
        dst_count = mysql_row_count(table)
        if src_count != dst_count:
            failures.append(f"{table}: 行数不一致 源={src_count} 目标={dst_count}")
            continue
        if src_count == 0:
            continue
        columns = sqlite_table_columns(src, table)
        order_col = order_column_for(table, columns)
        src_rows = src.execute(
            f"SELECT {', '.join(columns)} FROM {table} ORDER BY {order_col}"
        ).fetchall()
        col_sql = ", ".join(f"`{c}`" for c in columns)
        dst_rows = db.execute(
            f"SELECT {col_sql} FROM `{table}` ORDER BY {order_col}"
        ).fetchall()
        for idx, (srow, drow) in enumerate(zip(src_rows, dst_rows)):
            for col_idx, col in enumerate(columns):
                sv = srow[col_idx]
                dv = drow[col]
                if not _values_equal(sv, dv):
                    failures.append(
                        f"{table} 第 {idx + 1} 行 字段 {col}: 源={sv!r} 目标={dv!r}"
                    )
                    if len(failures) > 20:
                        failures.append("...更多差异已省略")
                        return failures, verified
        verified += src_count
    return failures, verified


def main():
    args = sys.argv[1:]
    skip_confirm = "--yes" in args
    source = DATABASE_PATH
    if "--source" in args:
        source = args[args.index("--source") + 1]

    print("=" * 60)
    print("  AMIG SQLite → MySQL 数据迁移工具")
    print("=" * 60)

    if not os.path.exists(source):
        print(f"[失败] 未找到 SQLite 源数据库文件: {source}")
        print("       可通过 --source 参数或 DATABASE_PATH 环境变量指定路径")
        sys.exit(1)

    source_digest = file_digest(source)
    src = open_sqlite_readonly(source)
    try:
        result = src.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            print(f"[失败] SQLite 源库完整性校验未通过: {result}")
            sys.exit(1)
        print(f"[OK] 源库完整性校验通过: {source}")
        existing = sqlite_existing_tables(src)
        skipped = [t for t in ALL_TABLES if t not in existing]
        if skipped:
            print(f"[提示] 源库缺少以下表（旧版本库正常现象，将跳过）: {', '.join(skipped)}")

        # 迁移前统计
        print("\n── 迁移前数据统计 ──")
        total_rows = 0
        for table in existing:
            cnt = sqlite_row_count(src, table)
            total_rows += cnt
            print(f"  {table:<22} {cnt:>8} 行")
        print(f"  {'合计':<22} {total_rows:>8} 行")
        print("\n  注意：迁移不会修改源 SQLite 文件；建议先对 uploads/ 与")
        print("  private_uploads/ 目录做好备份。")

        if not skip_confirm:
            try:
                answer = input("\n确认开始迁移？输入 y/yes 继续: ").strip().lower()
            except EOFError:
                print("[失败] 非交互环境请加 --yes 参数")
                sys.exit(1)
            if answer not in ("y", "yes"):
                print("[已取消] 未执行任何修改")
                sys.exit(0)

        # 目标库初始化与空库检查
        database.init_db()
        problems = precheck_target_empty()
        if problems:
            print("\n[失败] 目标 MySQL 库存在业务数据，拒绝迁移（防止重复导入/覆盖）：")
            for p in problems:
                print(f"       - {p}")
            print("       如需迁移到新库，请清空目标库或在配置中指定新的 MYSQL_DATABASE。")
            sys.exit(1)

        # 字段长度预检（TEXT → VARCHAR 转换风险）
        length_problems = precheck_lengths(src, existing)
        if length_problems:
            print("\n[失败] 源库存在超出目标字段长度的数据，请先处理后再迁移：")
            for p in length_problems:
                print(f"       - {p}")
            sys.exit(1)

        # 迁移
        print("\n── 开始迁移 ──")
        migrated = {}
        for table in TABLES:
            if table not in existing:
                continue
            columns = sqlite_table_columns(src, table)
            migrated[table] = migrate_table(src, table, columns)
            print(f"  [OK] {table}: {migrated[table]} 行")
        if "schema_migrations" in existing:
            columns = sqlite_table_columns(src, "schema_migrations")
            migrated["schema_migrations"] = migrate_schema_migrations(src, columns)
            print(f"  [OK] schema_migrations: {migrated['schema_migrations']} 行")

        # 迁移后校验
        print("\n── 迁移后校验 ──")
        failures, verified = verify_tables(src, existing)
        for table in existing:
            s = sqlite_row_count(src, table)
            d = mysql_row_count(table)
            mark = "OK" if s == d else "不一致"
            print(f"  {table:<22} 源 {s:>8} 行 | 目标 {d:>8} 行 | {mark}")
        if failures:
            print("\n[失败] 内容级校验发现差异：")
            for f in failures:
                print(f"       - {f}")
            sys.exit(1)

        print(f"\n[成功] 迁移完成：共 {sum(migrated.values())} 行，内容级校验 {verified} 行全部一致")
        # 证明源库未被修改（工具以 mode=ro 打开，正常情况下校验和必然一致）
        if file_digest(source) != source_digest:
            print("[警告] 源 SQLite 文件校验和发生变化，请立即检查是否有其他进程写入了旧库！")
        else:
            print("[OK] 源 SQLite 文件校验和未变化（源库未被修改）")
        print("  后续步骤：")
        print("  1. 确认 .env 中 MYSQL_* 配置指向本库；")
        print("  2. 启动应用并验证图片、点赞、后台功能；")
        print("  3. 原 SQLite 文件保留作为存档，请勿在迁移后继续往旧库写入。")
    finally:
        src.close()


if __name__ == "__main__":
    try:
        main()
    except DatabaseError as e:
        print(f"[失败] MySQL 连接或操作出错: {type(e).__name__}")
        print("       请检查 MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD / MYSQL_DATABASE 配置")
        print("       （详细错误已记录在应用日志中，不会显示数据库密码）")
        sys.exit(1)
