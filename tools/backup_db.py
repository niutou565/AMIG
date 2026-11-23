#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据库备份工具（命令行，只写备份目录，不改动生产库）。

用法：
    python tools/backup_db.py                       # 备份数据库
    python tools/backup_db.py --with-uploads        # 同时打包 uploads/ 与 private_uploads/
    python tools/backup_db.py --keep 14             # 覆盖保留份数（默认取 BACKUP_KEEP）
    python tools/backup_db.py --json                # 机器可读输出

备份完成后可检查：
    python tools/restore_db.py --list
    python tools/restore_db.py --backup backups/backup_xxx.db --check

设计要点（不是简单复制 .db 文件）：
1. 使用 SQLite Online Backup API（Connection.backup）在 WAL 模式下取得一致性
   快照。直接复制 .db 会漏掉尚未 checkpoint 到主库的 -wal 内容。
2. 备份先写入 .part 临时文件，完整性校验通过后才原子改名为最终文件；校验失败
   则删除临时文件并返回非零退出码，绝不留下看似可用的坏备份。
3. 备份完成后对副本执行 PRAGMA integrity_check 与 foreign_key_check。
4. 备份目录不在 Web 可访问路径下（见 app/config.py 的 BACKUP_DIR 默认值）。
5. 保留策略按文件名时间戳排序，仅删除超出 keep 数量的历史备份，删除前不触碰
   最新一份。备份失败时不清理旧备份，保证"最坏情况还有上一份"。
"""
import argparse
import json
import os
import sqlite3
import sys
import tarfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import (
    BACKUP_DIR,
    BACKUP_KEEP,
    DATABASE_PATH,
    PRIVATE_UPLOADS_DIR,
    UPLOADS_DIR,
)

PREFIX = "backup_"


def _stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _list_backups(backup_dir):
    try:
        names = [
            n for n in os.listdir(backup_dir)
            if n.startswith(PREFIX) and n.endswith(".db")
        ]
    except OSError:
        return []
    # 文件名内嵌 UTC 时间戳，按名字排序即为时间序
    names.sort()
    return [os.path.join(backup_dir, n) for n in names]


def _verify_copy(path):
    """对备份副本执行完整性校验，返回 (ok, detail)。

    以只读 URI 打开，避免校验本身修改副本；WAL 副本内可能有 -wal 文件，
    mode=ro 会读取它，因此校验的是"这份备份实际包含的数据"。
    """
    if not os.path.exists(path):
        return False, "备份文件不存在"
    uri = "file:" + str(path).replace("\\", "/") + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        return False, f"无法打开备份: {exc}"
    try:
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.Error as exc:
            return False, f"integrity_check 执行失败: {exc}"
        integrity = (row[0] if row else "") or ""
        if integrity.lower() != "ok":
            return False, f"integrity_check 失败: {integrity[:200]}"
        try:
            fk_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
        except sqlite3.Error as exc:
            return False, f"foreign_key_check 执行失败: {exc}"
        if fk_rows:
            return False, f"外键约束损坏: {len(fk_rows)} 条违规"
        try:
            tables = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            ).fetchone()[0]
        except sqlite3.Error as exc:
            return False, f"读取表结构失败: {exc}"
        return True, f"integrity=ok, tables={tables}"
    finally:
        conn.close()


def _backup_database(source, target):
    """用 Online Backup API 把 source 备份到 target。

    在 WAL 模式下，backup() 会把主库与 -wal 中已提交的内容一并写入目标，
    从而得到一致快照；直接 copy 主库文件做不到这一点。
    """
    src_uri = "file:" + str(source).replace("\\", "/") + "?mode=ro"
    src = sqlite3.connect(src_uri, uri=True)
    try:
        dest = sqlite3.connect(target)
        try:
            src.backup(dest)
        finally:
            dest.close()
    finally:
        src.close()


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
    dirs = [d for d in (UPLOADS_DIR, PRIVATE_UPLOADS_DIR) if os.path.isdir(d)]
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
    """按保留份数删除最旧的备份，返回已删除路径列表。"""
    if keep <= 0:
        return []
    existing = _list_backups(backup_dir)
    extra = existing[:-keep] if len(existing) > keep else []
    removed = []
    for path in extra:
        try:
            os.remove(path)
            if os.path.exists(path + "-wal"):
                os.remove(path + "-wal")
            if os.path.exists(path + "-shm"):
                os.remove(path + "-shm")
            removed.append(path)
            # 与之配对的 uploads 归档一并清理
            archive = path[: -len(".db")] + "_uploads.tar.gz"
            if os.path.exists(archive):
                os.remove(archive)
                removed.append(archive)
        except OSError:
            pass
    return removed


def main():
    parser = argparse.ArgumentParser(description="AMIG 数据库备份")
    parser.add_argument("--keep", type=int, default=BACKUP_KEEP,
                        help=f"保留份数（默认 {BACKUP_KEEP}）")
    parser.add_argument("--with-uploads", action="store_true",
                        help="同时打包 uploads/ 与 private_uploads/（文件体量较大）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = parser.parse_args()

    result = {
        "ok": False,
        "database": str(DATABASE_PATH),
        "backup_dir": str(BACKUP_DIR),
        "backup_file": None,
        "uploads_archive": None,
        "size_bytes": 0,
        "verify": None,
        "pruned": [],
        "error": None,
    }

    if not os.path.exists(str(DATABASE_PATH)):
        result["error"] = f"数据库不存在: {DATABASE_PATH}"
        _emit(result, args.json)
        return 1
    try:
        os.makedirs(str(BACKUP_DIR), exist_ok=True)
    except OSError as exc:
        result["error"] = f"无法创建备份目录: {exc}"
        _emit(result, args.json)
        return 1

    stamp = _stamp()
    final_path = _unique_backup_path(
        os.path.join(str(BACKUP_DIR), f"{PREFIX}{stamp}.db")
    )
    part_path = final_path + ".part"

    try:
        # 先写到 .part，校验通过再原子改名：避免出现"看起来正常但内容不完整"的备份
        _backup_database(str(DATABASE_PATH), part_path)
        ok, detail = _verify_copy(part_path)
        result["verify"] = detail
        if not ok:
            result["error"] = f"备份校验失败，已丢弃本次备份: {detail}"
            _safe_remove(part_path)
            _emit(result, args.json)
            return 1
        os.replace(part_path, final_path)
        # 清理 .part 期间可能残留的伴生文件，保持备份目录整洁
        _safe_remove(part_path)
        result["backup_file"] = final_path
        result["size_bytes"] = os.path.getsize(final_path)
    except (sqlite3.Error, OSError) as exc:
        _safe_remove(part_path)
        result["error"] = f"备份失败: {exc}"
        _emit(result, args.json)
        return 1

    if args.with_uploads:
        archive = final_path[: -len(".db")] + "_uploads.tar.gz"
        ok, detail = _archive_uploads(archive)
        if ok:
            result["uploads_archive"] = archive
        else:
            # 数据库备份已经成功，文件打包失败不删除数据库备份，但要让退出码可见
            result["verify"] = f"{result['verify']}; {detail}"
            result["error"] = detail
            _emit(result, args.json)
            return 1

    result["pruned"] = _prune(str(BACKUP_DIR), args.keep)
    result["ok"] = True
    if result.get("uploads_archive"):
        result["notes"] = (
            "数据库快照与上传归档非同一时刻生成：快照之后新上传的文件会进入归档"
            "但不在数据库记录中，恢复后可能产生少量孤儿文件（无害，"
            "可用 tools/check_uploads.py 识别）"
        )
    _emit(result, args.json)
    return 0


def _safe_remove(path):
    """删除临时产物，同时清理 SQLite 可能留下的伴生文件。"""
    for suffix in ("", "-journal", "-wal", "-shm"):
        target = path + suffix
        try:
            if os.path.exists(target):
                os.remove(target)
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
        if result["uploads_archive"]:
            print(f"上传文件归档: {result['uploads_archive']}")
        if result["pruned"]:
            print(f"已清理历史备份: {len(result['pruned'])} 个")
            for item in result["pruned"]:
                print(f"  {item}")
    elif not result["error"]:
        print("[失败] 备份未生成")


if __name__ == "__main__":
    sys.exit(main())
