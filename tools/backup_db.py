#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据库备份工具（MySQL / mysqldump，命令行，只写备份目录，不改动生产库）。

用法：
    python tools/backup_db.py                       # 备份数据库
    python tools/backup_db.py --with-uploads        # 同时打包 uploads/ 与 private_uploads/
    python tools/backup_db.py --keep 14             # 覆盖保留份数（默认取 BACKUP_KEEP）
    python tools/backup_db.py --json                # 机器可读输出

备份完成后可检查：
    python tools/restore_db.py --list
    python tools/restore_db.py --backup backups/backup_xxx.sql --check

设计要点（不是简单复制数据目录）：
1. 使用 mysqldump --single-transaction 取得 InnoDB 一致性快照：不阻塞业务读写，
   快照内容与备份开始时已提交的事务一致。
2. 备份先写入 .part 临时文件，校验通过（Dump completed 尾标 + 全部业务表的
   CREATE TABLE 均存在）后才原子改名为最终文件；失败则删除临时文件并返回
   非零退出码，绝不留下看似可用的坏备份。
3. 备份同时生成同名 .manifest.json 行数清单，恢复工具据此核对数据完整性。
4. 数据库密码通过 MYSQL_PWD 环境变量传给 mysqldump，不出现在命令行参数中。
5. 备份目录不在 Web 可访问路径下（见 app/config.py 的 BACKUP_DIR 默认值）。
6. 保留策略按文件名时间戳排序，仅删除超出 keep 数量的历史备份；备份失败时
   不清理旧备份，保证"最坏情况还有上一份"。
"""
import argparse
import json
import os
import sys
import tarfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config
from app import database
from app.database import EXPECTED_TABLES
from tools import mysql_cli

PREFIX = "backup_"


def _stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _list_backups(backup_dir):
    try:
        names = [
            n for n in os.listdir(backup_dir)
            if n.startswith(PREFIX) and n.endswith(".sql")
        ]
    except OSError:
        return []
    # 文件名内嵌 UTC 时间戳，按名字排序即为时间序
    names.sort()
    return [os.path.join(backup_dir, n) for n in names]


def _manifest_path(backup_path):
    return str(backup_path) + ".manifest.json"


def _collect_row_counts():
    """备份前统计各业务表行数，写入清单供恢复后校验。"""
    from app.database import get_db

    db = get_db()
    counts = {}
    for table in EXPECTED_TABLES:
        counts[table] = db.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
    return counts


def _dump_database(target_path):
    """用 mysqldump 把当前数据库导出到 target_path，返回 (ok, detail)。"""
    dump_path = mysql_cli.find_mysqldump()
    if not dump_path:
        return False, "未找到 mysqldump（请安装 MySQL 客户端或设置 MYSQLDUMP_PATH）"
    base_args, env = mysql_cli.build_password_args(dump_path)
    args = base_args + [
        "--single-transaction",
        "--quick",
        "--no-tablespaces",
        f"--result-file={target_path}",
        str(database.MYSQL_DATABASE),
    ]
    proc = mysql_cli.run_client(dump_path, args, env)
    if proc.returncode != 0:
        # stderr 可能包含连接串，只保留错误消息本身，不回显完整命令
        detail = (proc.stderr or "").strip().splitlines()
        detail = detail[-1] if detail else f"exit code {proc.returncode}"
        return False, f"mysqldump 执行失败: {detail}"
    return True, "mysqldump 完成"


def _verify_dump(path):
    """校验备份文件可用性，返回 (ok, detail)。

    检查两项：文件以 "Dump completed" 尾标结束（mysqldump 成功完成的标记）；
    每张业务表的 CREATE TABLE 都存在（防止导出中途截断或表缺失）。
    """
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return False, "备份文件不存在或为空"
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 200))
            tail = fh.read().decode("utf-8", errors="replace")
        if "Dump completed" not in tail:
            return False, "备份缺少 Dump completed 尾标，导出可能中途失败"
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except OSError as exc:
        return False, f"无法读取备份文件: {exc}"
    missing = [
        table for table in EXPECTED_TABLES
        if f"CREATE TABLE `{table}`" not in content
    ]
    if missing:
        return False, f"备份缺少业务表结构: {', '.join(missing)}"
    return True, f"dump 校验通过, tables={len(EXPECTED_TABLES)}, size={size}B"


def _unique_backup_path(path):
    """避免同一秒内多次备份互相覆盖：已存在时追加序号。"""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    for index in range(2, 1000):
        candidate = f"{base}_{index}{ext}"
        if not os.path.exists(candidate):
            return candidate
    raise RuntimeError(f"无法生成唯一备份文件名: {path}")


def _archive_uploads(target_path):
    """把上传目录打包为 tar.gz，便于与数据库快照一并恢复。

    先写 .part，重新打开校验可读且文件数一致后才原子改名；
    失败时删除 .part，绝不覆盖已有的有效归档。返回 (ok, detail)。
    """
    dirs = [d for d in (config.UPLOADS_DIR, config.PRIVATE_UPLOADS_DIR) if os.path.isdir(d)]
    if not dirs:
        return True, "跳过：上传目录不存在"
    part_path = target_path + ".part"
    count = 0
    try:
        with tarfile.open(part_path, "w:gz") as tar:
            for directory in dirs:
                base = os.path.basename(os.path.normpath(directory))
                for root, _, files in os.walk(directory):
                    for name in files:
                        full = os.path.join(root, name)
                        rel = os.path.join(base, os.path.relpath(full, directory))
                        tar.add(full, arcname=rel)
                        count += 1
        with tarfile.open(part_path, "r:gz") as tar:
            readable = sum(1 for m in tar.getmembers() if m.isfile())
        if readable != count:
            raise OSError(f"归档文件数不一致: 写入 {count}，读取 {readable}")
        os.replace(part_path, target_path)
    except (OSError, tarfile.TarError) as exc:
        _safe_remove(part_path)
        return False, f"打包上传文件失败: {exc}"
    return True, f"已打包 {count} 个文件"


def _prune(backup_dir, keep):
    """按保留份数删除最旧的备份及其清单，返回已删除路径列表。"""
    if keep <= 0:
        return []
    existing = _list_backups(backup_dir)
    extra = existing[:-keep] if len(existing) > keep else []
    removed = []
    for path in extra:
        manifest = _manifest_path(path)
        try:
            os.remove(path)
            if os.path.exists(manifest):
                os.remove(manifest)
            removed.append(path)
            # 与之配对的 uploads 归档一并清理
            archive = path[: -len(".sql")] + "_uploads.tar.gz"
            if os.path.exists(archive):
                os.remove(archive)
                removed.append(archive)
        except OSError:
            pass
    return removed


def _safe_remove(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _emit(result, as_json):
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if result["error"]:
        print(f"[失败] {result['error']}")
    if result["backup_file"]:
        size_mb = result["size_bytes"] / 1024 / 1024
        print("=" * 60)
        print("AMIG 数据库备份完成")
        print("=" * 60)
        print(f"源数据库: {result['database']}")
        print(f"备份文件: {result['backup_file']}")
        print(f"备份大小: {size_mb:.2f} MB")
        print(f"校验结果: {result['verify']}")
        if result.get("row_counts"):
            counts = ", ".join(f"{k}={v}" for k, v in result["row_counts"].items())
            print(f"备份时行数: {counts}")
        if result["uploads_archive"]:
            print(f"上传文件归档: {result['uploads_archive']}")
        if result["pruned"]:
            print(f"已清理历史备份: {len(result['pruned'])} 个")
            for item in result["pruned"]:
                print(f"  {item}")
    elif not result["error"]:
        print("[失败] 备份未生成")


def main():
    parser = argparse.ArgumentParser(description="AMIG 数据库备份（MySQL）")
    parser.add_argument("--keep", type=int, default=config.BACKUP_KEEP,
                        help=f"保留份数（默认 {config.BACKUP_KEEP}）")
    parser.add_argument("--with-uploads", action="store_true",
                        help="同时打包 uploads/ 与 private_uploads/（文件体量较大）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = parser.parse_args()

    result = {
        "ok": False,
        "database": str(database.MYSQL_DATABASE),
        "backup_dir": str(config.BACKUP_DIR),
        "backup_file": None,
        "uploads_archive": None,
        "size_bytes": 0,
        "row_counts": None,
        "verify": None,
        "pruned": [],
        "error": None,
    }

    try:
        os.makedirs(str(config.BACKUP_DIR), exist_ok=True)
    except OSError as exc:
        result["error"] = f"无法创建备份目录: {exc}"
        _emit(result, args.json)
        return 1

    stamp = _stamp()
    final_path = _unique_backup_path(
        os.path.join(str(config.BACKUP_DIR), f"{PREFIX}{stamp}.sql")
    )
    part_path = final_path + ".part"

    try:
        result["row_counts"] = _collect_row_counts()
    except Exception as exc:
        result["error"] = f"无法连接数据库统计行数（请检查 MySQL 配置）: {type(exc).__name__}"
        _emit(result, args.json)
        return 1

    try:
        # 先写到 .part，校验通过再原子改名：避免出现"看起来正常但内容不完整"的备份
        ok, detail = _dump_database(part_path)
        if not ok:
            result["error"] = f"备份失败: {detail}"
            _safe_remove(part_path)
            _emit(result, args.json)
            return 1
        ok, detail = _verify_dump(part_path)
        result["verify"] = detail
        if not ok:
            result["error"] = f"备份校验失败，已丢弃本次备份: {detail}"
            _safe_remove(part_path)
            _emit(result, args.json)
            return 1
        os.replace(part_path, final_path)
        result["backup_file"] = final_path
        result["size_bytes"] = os.path.getsize(final_path)
        # 行数清单与备份同生共死：清单写失败视为备份失败并回滚文件
        manifest = {
            "created_at": stamp,
            "database": str(database.MYSQL_DATABASE),
            "row_counts": result["row_counts"],
        }
        with open(_manifest_path(final_path), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2)
    except (OSError, Exception) as exc:
        _safe_remove(part_path)
        _safe_remove(_manifest_path(final_path))
        result["error"] = f"备份失败: {type(exc).__name__}: {exc}"
        _emit(result, args.json)
        return 1

    if args.with_uploads:
        archive = final_path[: -len(".sql")] + "_uploads.tar.gz"
        ok, detail = _archive_uploads(archive)
        if ok:
            result["uploads_archive"] = archive
        else:
            # 数据库备份已经成功，文件打包失败不删除数据库备份，但要让退出码可见
            result["verify"] = f"{result['verify']}; {detail}"
            result["error"] = detail
            _emit(result, args.json)
            return 1

    result["pruned"] = _prune(str(config.BACKUP_DIR), args.keep)
    result["ok"] = True
    if result.get("uploads_archive"):
        result["notes"] = (
            "数据库快照与上传归档非同一时刻生成：快照之后新上传的文件会进入归档"
            "但不在数据库记录中，恢复后可能产生少量孤儿文件（无害，"
            "可用 tools/check_uploads.py 识别）"
        )
    _emit(result, args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
