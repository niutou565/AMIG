#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据库恢复工具（离线命令行；禁止在 Web 请求中调用）。

用法：
    python tools/restore_db.py --list                      # 列出可用备份
    python tools/restore_db.py --backup backups/backup_xxx.db --yes
    python tools/restore_db.py --backup ... --yes --restore-uploads
    python tools/restore_db.py --backup ... --yes --restore-uploads --force-uploads

安全流程（每一步失败都立即中止，绝不做"半恢复"）：
1. 校验备份文件本身：integrity_check + foreign_key_check + 关键表存在。
2. 要求 --yes 显式确认；不带 --yes 时只打印将要执行的操作后退出。
3. 恢复前把当前生产库复制为 pre_restore_<时间戳>.db 作为回退点。
4. 把备份复制到目标目录下的临时文件并校验，再原子替换，避免出现"半个数据库"。
5. 替换前把现有库及 -wal/-shm 改名保留（不是直接删除）：替换失败时原样还原，
   不会因删除 WAL 而丢失旧库中尚未 checkpoint 的已提交数据。
6. 对替换后的数据库再次执行完整性校验，失败时用回退点自动还原。

必须停掉 Web 进程后再执行：Windows 上打开中的文件无法被 os.replace 覆盖。
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import BACKUP_DIR, DATABASE_PATH, PRIVATE_UPLOADS_DIR, UPLOADS_DIR

PREFIX = "backup_"
REQUIRED_TABLES = {"images", "likes", "admin_logs", "settings"}


def _stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.2f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return f"{n:.2f} GB"


def _open_ro(path):
    uri = "file:" + str(path).replace("\\", "/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def verify_backup(path):
    """校验备份可用性，返回 (ok, detail, info)。"""
    info = {}
    if not os.path.isfile(path):
        return False, f"备份文件不存在: {path}", info
    info["size"] = os.path.getsize(path)
    try:
        conn = _open_ro(path)
    except sqlite3.Error as exc:
        return False, f"无法打开备份: {exc}", info
    try:
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.Error as exc:
            return False, f"integrity_check 失败: {exc}", info
        integrity = (row[0] if row else "") or ""
        if integrity.lower() != "ok":
            return False, f"备份已损坏: {integrity[:200]}", info
        try:
            fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        except sqlite3.Error as exc:
            return False, f"foreign_key_check 失败: {exc}", info
        if fk:
            return False, f"备份存在 {len(fk)} 条外键违规", info
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        info["tables"] = len(tables)
        missing = REQUIRED_TABLES - tables
        if missing:
            return False, f"备份缺少关键表: {', '.join(sorted(missing))}", info
        counts = {}
        for table in sorted(REQUIRED_TABLES):
            try:
                counts[table] = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            except sqlite3.Error:
                counts[table] = None
        info["row_counts"] = counts
        try:
            info["schema_versions"] = [
                r[0] for r in conn.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
            ]
        except sqlite3.Error:
            info["schema_versions"] = []
        return True, "备份校验通过", info
    finally:
        conn.close()


def list_backups(backup_dir):
    """列出可恢复的备份，包含备份前自动生成的回退点。"""
    try:
        names = sorted(
            n for n in os.listdir(backup_dir)
            if n.endswith(".db") and (n.startswith(PREFIX) or n.startswith("pre_restore_"))
        )
    except OSError:
        return []
    return [os.path.join(backup_dir, n) for n in names]


def _safety_copy(db_path):
    """恢复前把当前库复制到备份目录，作为自动回退点。"""
    if not os.path.exists(db_path):
        return None
    stamp = _stamp()
    target = os.path.join(str(BACKUP_DIR), f"pre_restore_{stamp}.db")
    os.makedirs(str(BACKUP_DIR), exist_ok=True)
    # 用 Online Backup API 而不是 copy：当前库可能还有未 checkpoint 的 WAL 内容
    src = _open_ro(db_path)
    try:
        dest = sqlite3.connect(target)
        try:
            src.backup(dest)
        finally:
            dest.close()
    finally:
        src.close()
    return target


def _swap_path(src, dst):
    """os.replace 的薄封装：目录/文件原子改名，测试可单独替换。"""
    os.replace(src, dst)


def _preserve_live_files(db_path):
    """把现有库与 -wal/-shm/-journal 改名保留，返回 [(原路径, 保留路径)]。

    任何一步失败都撤销已改名文件并抛 RuntimeError，保证当前库完整可用。
    不能先删除 WAL：其中可能含尚未 checkpoint 的已提交事务，删除即丢失。
    """
    saved = []
    try:
        for suffix in ("-wal", "-shm", "-journal"):
            src = db_path + suffix
            if os.path.exists(src):
                dst = src + ".pre_restore"
                _swap_path(src, dst)
                saved.append((src, dst))
        if os.path.exists(db_path):
            dst = db_path + ".pre_restore"
            _swap_path(db_path, dst)
            saved.append((db_path, dst))
    except OSError as exc:
        _restore_saved_files(saved)
        raise RuntimeError(
            f"无法保留现有数据库文件（请先停止 Web 进程）: {exc}"
        )
    return saved


def _restore_saved_files(saved):
    """把改名保留的旧库文件还原回原路径（替换失败时使用）。"""
    for original, moved in reversed(saved):
        try:
            if os.path.exists(moved):
                _swap_path(moved, original)
        except OSError:
            pass


def _discard_saved_files(saved):
    for _, moved in saved:
        try:
            os.remove(moved)
        except OSError:
            pass


def restore_database(backup_path, db_path):
    """执行原子替换，返回 (ok, detail)。"""
    parent = os.path.dirname(os.path.abspath(db_path)) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".restore_", suffix=".db", dir=parent)
    os.close(fd)
    try:
        shutil.copyfile(backup_path, tmp_path)
        # 先在临时位置验证复制结果，确认无误后再替换正式文件
        ok, detail, _ = verify_backup(tmp_path)
        if not ok:
            return False, f"复制后的数据库校验失败: {detail}"
        try:
            saved = _preserve_live_files(db_path)
        except RuntimeError as exc:
            return False, str(exc)
        try:
            _swap_path(tmp_path, db_path)
        except OSError as exc:
            # 替换失败：把旧库与 WAL 原样放回，保持修复前的可用状态
            _restore_saved_files(saved)
            return False, f"替换数据库失败（Web 进程是否仍在运行？）: {exc}"
        # 新库已就位；旧库及旧 WAL 已由恢复前安全副本覆盖，可以直接清理
        _discard_saved_files(saved)
        return True, "已替换数据库文件"
    except OSError as exc:
        return False, f"替换数据库失败（Web 进程是否仍在运行？）: {exc}"
    finally:
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                if os.path.exists(tmp_path + suffix):
                    os.remove(tmp_path + suffix)
            except OSError:
                pass


def _archive_for_backup(backup_path):
    """返回与备份配对的 uploads 归档路径；不存在时返回 None。"""
    path = os.path.abspath(str(backup_path))
    if not path.lower().endswith(".db"):
        return None
    archive = path[: -len(".db")] + "_uploads.tar.gz"
    return archive if os.path.isfile(archive) else None


def _archive_relative_names(archive):
    """读取归档中的相对文件名（去掉 uploads/ 或 private_uploads/ 前缀）。"""
    names = set()
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            parts = member.name.replace("\\", "/").split("/", 1)
            if len(parts) == 2 and parts[1]:
                names.add(parts[1])
    return names


def check_backup_set(backup_path):
    """校验备份数据库与配对上传归档，返回结果字典。

    - 数据库：integrity_check、foreign_key_check、关键表（同 verify_backup）；
    - 上传归档：必须与备份文件同名配对，且包含数据库引用的全部图片文件。
    只有数据库、没有归档时给出警告但不算失败（允许仅备份数据库）。
    """
    ok, detail, info = verify_backup(backup_path)
    result = {
        "ok": ok,
        "backup": os.path.abspath(str(backup_path)),
        "detail": detail,
        "info": info,
        "uploads_archive": None,
        "uploads_files": 0,
        "expected_files": 0,
        "missing_files": [],
        "warning": None,
        "error": None,
    }
    if not ok:
        result["error"] = detail
        return result

    archive = _archive_for_backup(backup_path)
    if not archive:
        result["warning"] = "未找到配对的上传归档，该备份只能恢复数据库"
        return result
    result["uploads_archive"] = archive
    try:
        names = _archive_relative_names(archive)
    except (OSError, tarfile.TarError) as exc:
        result["ok"] = False
        result["error"] = f"上传归档无法读取: {exc}"
        return result
    result["uploads_files"] = len(names)

    try:
        conn = _open_ro(backup_path)
        try:
            rows = conn.execute(
                "SELECT filename, thumb_filename FROM images"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        result["ok"] = False
        result["error"] = f"读取备份图片记录失败: {exc}"
        return result

    expected = set()
    for row in rows:
        for name in (row["filename"], row["thumb_filename"]):
            if name:
                expected.add(str(name).replace("\\", "/"))
    result["expected_files"] = len(expected)
    missing = sorted(expected - names)
    if missing:
        result["ok"] = False
        result["missing_files"] = missing[:50]
        result["error"] = (
            f"{len(missing)} 个数据库引用的图片不在上传归档中"
            f"（示例: {', '.join(missing[:3])}）"
        )
    return result


def _prepare_staging(target, stamp):
    """在目标目录同盘位置创建空的临时目录，确保后续可以原子改名。"""
    path = os.path.abspath(target) + f".restore_new_{stamp}"
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)
    return path


def restore_uploads(backup_path, force):
    """用与备份配对的上传归档恢复上传目录，返回 (ok, detail)。

    先解包到同盘临时目录，再原子改名整体替换；旧目录改名为
    <目录>.pre_restore_<时间戳> 保留。任一步失败都会还原旧目录，不会留下
    解包到一半的混合状态。
    """
    archive = _archive_for_backup(backup_path)
    if not archive:
        return False, (
            f"未找到与备份配对的上传归档: {backup_path}"
            "（创建备份时需加 --with-uploads）"
        )

    mapping = {
        os.path.basename(os.path.normpath(str(UPLOADS_DIR))): str(UPLOADS_DIR),
        os.path.basename(os.path.normpath(str(PRIVATE_UPLOADS_DIR))): str(PRIVATE_UPLOADS_DIR),
    }
    if any(os.path.isdir(p) for p in mapping.values()) and not force:
        return False, "上传目录已存在；确认覆盖请加 --force-uploads（旧目录会改名保留）"

    # 归档里的根目录必须与当前配置一致，否则拒绝恢复，避免把错误归档解到
    # 其他目录或产生"恢复 0 个文件"却清空现有目录的假成功。
    try:
        with tarfile.open(archive, "r:gz") as tar:
            roots = {
                m.name.replace("\\", "/").split("/", 1)[0]
                for m in tar.getmembers() if m.isfile()
            }
    except (OSError, tarfile.TarError) as exc:
        return False, f"上传归档无法读取: {exc}"
    unknown = roots - set(mapping)
    if unknown:
        return False, (
            f"归档目录 {', '.join(sorted(unknown))} 与当前配置不一致，已中止恢复"
        )

    stamp = _stamp()
    staging = {name: _prepare_staging(target, stamp) for name, target in mapping.items()}
    extracted = 0
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                parts = member.name.replace("\\", "/").split("/", 1)
                if len(parts) != 2 or parts[0] not in staging:
                    continue
                relative = parts[1]
                # 拒绝越界路径，避免归档中的 ../ 写到目录外
                if not relative or relative.startswith("/") or ".." in relative.split("/"):
                    continue
                dest = os.path.join(staging[parts[0]], *relative.split("/"))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                src = tar.extractfile(member)
                if src is None:
                    continue
                with src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
                extracted += 1
    except (OSError, tarfile.TarError) as exc:
        for path in staging.values():
            shutil.rmtree(path, ignore_errors=True)
        return False, f"解包上传文件失败: {exc}"

    saved = []
    swapped = []
    try:
        for name, target in mapping.items():
            if os.path.exists(target):
                old = os.path.abspath(target) + f".pre_restore_{stamp}"
                _swap_path(target, old)
                saved.append((target, old))
            _swap_path(staging[name], target)
            swapped.append((staging[name], target))
    except OSError as exc:
        # 先撤掉已切换的新目录，再把旧目录改名回来
        for staging_path, target in reversed(swapped):
            try:
                _swap_path(target, staging_path)
            except OSError:
                pass
        for target, old in reversed(saved):
            try:
                if os.path.exists(old):
                    _swap_path(old, target)
            except OSError:
                pass
        for path in staging.values():
            shutil.rmtree(path, ignore_errors=True)
        return False, f"切换上传目录失败（Web 进程是否仍在运行？）: {exc}"

    for path in staging.values():
        shutil.rmtree(path, ignore_errors=True)
    kept = [old for _, old in saved]
    detail = f"已恢复 {extracted} 个上传文件（来自 {os.path.basename(archive)}）"
    if kept:
        detail += f"；旧目录已保留: {', '.join(kept)}"
    return True, detail


def main():
    parser = argparse.ArgumentParser(description="AMIG 数据库恢复（离线执行）")
    parser.add_argument("--backup", help="要恢复的备份文件路径")
    parser.add_argument("--list", action="store_true", help="列出可用备份")
    parser.add_argument("--check", action="store_true",
                        help="只校验备份及其配对上传归档，不执行恢复")
    parser.add_argument("--yes", action="store_true", help="确认执行恢复（必需）")
    parser.add_argument("--restore-uploads", action="store_true",
                        help="同时恢复上传文件归档")
    parser.add_argument("--force-uploads", action="store_true",
                        help="上传目录已存在时仍覆盖（危险）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args()

    db_path = str(DATABASE_PATH)
    result = {"ok": False, "database": db_path, "backup": None,
              "pre_restore_copy": None, "steps": [], "error": None}

    if args.list:
        items = list_backups(str(BACKUP_DIR))
        if args.json:
            print(json.dumps({"backups": items}, ensure_ascii=False, indent=2))
        else:
            print(f"备份目录: {BACKUP_DIR}")
            if not items:
                print("  （空）")
            for item in items:
                paired = "  [含上传归档]" if _archive_for_backup(item) else ""
                print(f"  {item}  {_human(os.path.getsize(item))}{paired}")
        return 0

    if args.check:
        if not args.backup:
            print("[失败] --check 必须配合 --backup 指定要检查的备份文件")
            return 1
        report = check_backup_set(os.path.abspath(args.backup))
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["ok"] else 1
        print("=" * 60)
        print("AMIG 备份完整性检查")
        print("=" * 60)
        print(f"备份文件: {report['backup']}")
        if report.get("info", {}).get("size") is not None:
            print(f"备份大小: {_human(report['info']['size'])}")
        print(f"备份记录数: {report.get('info', {}).get('row_counts')}")
        print(f"schema 版本: {report.get('info', {}).get('schema_versions')}")
        print(f"数据库校验: {report['detail']}")
        if report["uploads_archive"]:
            print(f"上传归档: {report['uploads_archive']}")
            print(f"归档文件数: {report['uploads_files']}")
            print(f"数据库引用文件数: {report['expected_files']}")
        elif report.get("warning"):
            print(f"[警告] {report['warning']}")
        if report["missing_files"]:
            print(f"缺失文件（前 {len(report['missing_files'])} 个）:")
            for name in report["missing_files"]:
                print(f"  - {name}")
        if report["ok"]:
            print("\n[通过] 备份可用")
        else:
            print(f"\n[失败] {report['error']}")
        return 0 if report["ok"] else 1

    if not args.backup:
        print("[失败] 必须用 --backup 指定备份文件，或用 --list 查看可用备份")
        return 1

    backup_path = os.path.abspath(args.backup)
    ok, detail, info = verify_backup(backup_path)
    result["backup"] = backup_path
    if not ok:
        result["error"] = detail
        _emit(result, args.json)
        return 1
    result["steps"].append(f"备份校验通过：{detail}")

    if not args.json:
        print("=" * 60)
        print("AMIG 数据库恢复")
        print("=" * 60)
        print(f"备份文件: {backup_path}")
        print(f"备份大小: {_human(info.get('size', 0))}")
        print(f"备份记录数: {info.get('row_counts')}")
        print(f"备份 schema 版本: {info.get('schema_versions')}")
        print(f"目标数据库: {db_path}")
        if os.path.exists(db_path):
            print(f"当前数据库大小: {_human(os.path.getsize(db_path))}")
            # 正常停服后通常没有 -wal；不能对不存在的文件调用 getsize
            wal_path = db_path + "-wal"
            wal_size = os.path.getsize(wal_path) if os.path.exists(wal_path) else 0
            print(f"当前数据库 WAL: {_human(wal_size)}")
        print()
        print("此操作会用备份覆盖当前数据库；旧的 -wal/-shm 会先改名保留，")
        print("替换成功后清理，失败时自动还原。")

    if not args.yes:
        if not args.json:
            print("\n[未执行] 这是预演。确认无误后请追加 --yes 重新执行。")
        result["error"] = "未提供 --yes，仅执行预演"
        result["steps"].append("预演：未执行恢复")
        _emit(result, args.json, silent=True)
        return 2

    try:
        safety = _safety_copy(db_path)
    except (OSError, sqlite3.Error) as exc:
        result["error"] = f"无法创建恢复前回退副本，已中止: {exc}"
        _emit(result, args.json, header=False)
        return 1
    result["pre_restore_copy"] = safety
    result["steps"].append(f"已创建恢复前副本: {safety}")

    restored, message = restore_database(backup_path, db_path)
    result["steps"].append(message)
    if not restored:
        result["error"] = message
        _emit(result, args.json, header=False)
        return 1

    # 替换后再校验一次；失败则用回退点自动还原，避免留下不可用数据库
    ok, detail, _ = verify_backup(db_path)
    if not ok:
        result["error"] = f"恢复后的数据库校验失败: {detail}"
        if safety and os.path.exists(safety):
            rollback_ok, rollback_msg = restore_database(safety, db_path)
            result["steps"].append(
                f"已尝试自动回滚: {rollback_msg}" if rollback_ok else "自动回滚失败，请手工恢复"
            )
        _emit(result, args.json, header=False)
        return 1
    result["steps"].append(f"恢复后校验通过：{detail}")

    if args.restore_uploads:
        up_ok, up_msg = restore_uploads(backup_path, args.force_uploads)
        result["steps"].append(up_msg)
        if not up_ok:
            # 数据库已恢复成功，上传文件恢复失败单独报告
            result["error"] = up_msg
            _emit(result, args.json, header=False)
            return 1

    result["ok"] = True
    _emit(result, args.json, header=False)
    return 0


def _emit(result, as_json, silent=False, header=True):
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if silent:
        return
    if header and result.get("steps"):
        print("执行步骤:")
        for step in result["steps"]:
            print(f"  - {step}")
    if result["ok"]:
        print("\n[成功] 数据库恢复完成")
        if result.get("pre_restore_copy"):
            print(f"恢复前副本保留在: {result['pre_restore_copy']}（可用于回滚）")
        print("请重新启动应用，并运行 python tools/check_db.py 与 tools/check_uploads.py 复核。")
    elif result.get("error"):
        print(f"\n[失败] {result['error']}")


if __name__ == "__main__":
    sys.exit(main())
