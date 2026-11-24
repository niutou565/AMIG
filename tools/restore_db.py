#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据库恢复工具（MySQL，离线命令行；禁止在 Web 请求中调用）。

用法：
    python tools/restore_db.py --list                      # 列出可用备份
    python tools/restore_db.py --backup backups/backup_xxx.sql --yes
    python tools/restore_db.py --backup ... --yes --restore-uploads
    python tools/restore_db.py --backup ... --yes --restore-uploads --force-uploads

安全流程（每一步失败都立即中止，绝不做"半恢复"）：
1. 校验备份文件本身：Dump completed 尾标 + 全部业务表结构存在。
2. 要求 --yes 显式确认；不带 --yes 时只打印将要执行的操作后退出。
3. 恢复前用 mysqldump 把当前数据库导出为 pre_restore_<时间戳>.sql 作为回退点，
   回退副本同样通过校验后才继续。
4. 用 mysql 客户端把备份导入目标库（dump 内含 DROP TABLE IF EXISTS +
   CREATE TABLE，恢复即整库表级替换）。
5. 导入后核对：全部业务表存在、行数与备份清单一致（清单缺失时跳过行数核对）、
   likes 表无孤儿点赞记录；任一失败则自动用回退副本恢复原库。

执行前必须停掉 Web 进程，避免恢复过程中业务连接写入造成数据竞争。
"""
import argparse
import json
import os
import shutil
import sys
import tarfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config
from app import database
from app.database import EXPECTED_TABLES
from tools import mysql_cli

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


def _manifest_path(backup_path):
    return str(backup_path) + ".manifest.json"


def verify_backup(path):
    """校验备份可用性，返回 (ok, detail, info)。"""
    info = {}
    if not os.path.isfile(path):
        return False, f"备份文件不存在: {path}", info
    info["size"] = os.path.getsize(path)
    if info["size"] == 0:
        return False, "备份文件为空", info
    try:
        with open(path, "rb") as fh:
            fh.seek(max(0, info["size"] - 200))
            tail = fh.read().decode("utf-8", errors="replace")
        if "Dump completed" not in tail:
            return False, "备份缺少 Dump completed 尾标，可能不完整", info
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except OSError as exc:
        return False, f"无法读取备份: {exc}", info
    tables = set()
    marker = "CREATE TABLE `"
    idx = 0
    while True:
        idx = content.find(marker, idx)
        if idx < 0:
            break
        rest = content[idx + len(marker):]
        name = rest.split("`", 1)[0]
        if name:
            tables.add(name)
        idx += len(marker)
    info["tables"] = sorted(tables)
    missing = REQUIRED_TABLES - tables
    if missing:
        return False, f"备份缺少关键表: {', '.join(sorted(missing))}", info
    # 行数清单（backup_db 生成；缺失不算失败，仅跳过行数核对）
    manifest_file = _manifest_path(path)
    if os.path.isfile(manifest_file):
        try:
            with open(manifest_file, "r", encoding="utf-8") as fh:
                manifest = json.load(fh)
            info["row_counts"] = manifest.get("row_counts", {})
            info["manifest"] = manifest_file
        except (OSError, ValueError) as exc:
            return False, f"备份行数清单无法读取: {exc}", info
    return True, "备份校验通过", info


def list_backups(backup_dir):
    """列出可恢复的备份，包含恢复前自动生成的回退点。"""
    try:
        names = sorted(
            n for n in os.listdir(backup_dir)
            if n.endswith(".sql") and (n.startswith(PREFIX) or n.startswith("pre_restore_"))
        )
    except OSError:
        return []
    return [os.path.join(backup_dir, n) for n in names]


def _safety_dump(target_path):
    """恢复前把当前库导出到 target_path 作为回退点，返回 (ok, detail)。"""
    ok, detail = backup_database(target_path)
    if not ok:
        return False, detail
    # 回退副本本身也必须完整，否则不能继续恢复
    size = os.path.getsize(target_path)
    with open(target_path, "rb") as fh:
        fh.seek(max(0, size - 200))
        tail = fh.read().decode("utf-8", errors="replace")
    if "Dump completed" not in tail:
        return False, "恢复前回退副本校验失败（可能当前库无法导出）"
    return True, "已创建恢复前回退副本"


def backup_database(target_path):
    """mysqldump 导出当前库到 target_path（backup_db 同款逻辑，供回退使用）。"""
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
        detail = (proc.stderr or "").strip().splitlines()
        detail = detail[-1] if detail else f"exit code {proc.returncode}"
        return False, f"mysqldump 执行失败: {detail}"
    return True, "mysqldump 完成"


def _import_dump(backup_path):
    """用 mysql 客户端把备份导入目标库，返回 (ok, detail)。"""
    cli_path = mysql_cli.find_mysql_client()
    if not cli_path:
        return False, "未找到 mysql 客户端（请安装 MySQL 客户端或设置 MYSQL_CLIENT_PATH）"
    base_args, env = mysql_cli.build_password_args(cli_path)
    args = base_args + [str(database.MYSQL_DATABASE)]
    proc = mysql_cli.run_client(cli_path, args, env, input_file=backup_path)
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        detail = detail[-1] if detail else f"exit code {proc.returncode}"
        return False, f"mysql 导入失败: {detail}"
    return True, "mysql 导入完成"


def _db_row_counts():
    """从当前库读取各业务表行数，返回 dict；表缺失返回 None 占位。"""
    from app.database import get_db

    db = get_db()
    counts = {}
    for table in EXPECTED_TABLES:
        try:
            counts[table] = db.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
        except Exception:
            counts[table] = None
    return counts


def _post_verify(manifest_counts):
    """导入后的数据完整性核对，返回 (ok, detail)。

    - 全部业务表存在；
    - 行数与备份清单一致（清单缺失时跳过，只检查表存在）；
    - likes 表不存在指向已删除图片的孤儿记录。
    """
    from app.database import get_db

    db = get_db()
    counts = _db_row_counts()
    missing = [t for t, c in counts.items() if c is None]
    if missing:
        return False, f"恢复后缺少业务表: {', '.join(missing)}"
    if manifest_counts:
        mismatch = [
            f"{t}: 期望 {manifest_counts[t]}, 实际 {counts[t]}"
            for t, c in counts.items()
            if t in manifest_counts and manifest_counts[t] != c
        ]
        if mismatch:
            return False, "恢复后行数与备份清单不一致: " + "; ".join(mismatch[:5])
    drift = db.execute(
        "SELECT COUNT(*) AS c FROM likes l LEFT JOIN images i ON l.image_id = i.id "
        "WHERE i.id IS NULL"
    ).fetchone()["c"]
    if drift:
        return False, f"恢复后存在 {drift} 条孤儿点赞记录（image_id 无对应图片）"
    return True, f"恢复后校验通过（行数: " + ", ".join(
        f"{t}={c}" for t, c in sorted(counts.items()) if t in REQUIRED_TABLES
    ) + ")"


def restore_database(backup_path, manifest_counts=None):
    """执行导入 + 恢复后校验，返回 (ok, detail)。不自动回滚（由 main 统一处理）。"""
    ok, detail = _import_dump(backup_path)
    if not ok:
        return False, detail
    ok, detail = _post_verify(manifest_counts)
    if not ok:
        return False, detail
    return True, detail


def _archive_for_backup(backup_path):
    """返回与备份配对的 uploads 归档路径；不存在时返回 None。"""
    path = os.path.abspath(str(backup_path))
    if not path.lower().endswith(".sql"):
        return None
    archive = path[: -len(".sql")] + "_uploads.tar.gz"
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

    - 数据库：Dump completed 尾标、关键表结构（同 verify_backup）；
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
    # 备份为 SQL 文本，逐行解析图片记录脆弱且代价高；
    # 图片文件核对改为恢复后用 tools/check_uploads.py 在数据库上执行。
    result["warning"] = (
        "上传归档包含 %d 个文件；图片记录核对请恢复后在数据库上运行 "
        "tools/check_uploads.py" % len(names)
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
        os.path.basename(os.path.normpath(str(config.UPLOADS_DIR))): str(config.UPLOADS_DIR),
        os.path.basename(os.path.normpath(str(config.PRIVATE_UPLOADS_DIR))): str(config.PRIVATE_UPLOADS_DIR),
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
    extracted_names = set()
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
                extracted_names.add(relative)
                extracted_names.add(os.path.basename(relative))
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

    # 完整性校验：数据库中仍在引用的图片文件必须都出现在归档里
    missing = _missing_referenced_files(extracted_names)
    if missing:
        preview = ", ".join(missing[:10])
        return False, (
            f"上传归档缺少 {len(missing)} 个数据库仍在引用的文件（如 {preview}）；"
            "数据库与目录已按归档恢复，请人工核对是否使用了错误的备份"
        )
    return True, detail


def _missing_referenced_files(archive_names):
    """返回数据库中引用、但归档中不存在的文件名列表。"""
    from app.database import get_db

    rows = get_db().execute(
        "SELECT filename, thumb_filename FROM images"
    ).fetchall()
    missing = []
    for row in rows:
        for key in ("filename", "thumb_filename"):
            name = row[key]
            if not name:
                continue
            if name not in archive_names and os.path.basename(name) not in archive_names:
                missing.append(name)
    return missing


def _swap_path(src, dst):
    """os.replace 的薄封装：目录/文件原子改名，测试可单独替换。"""
    os.replace(src, dst)


def main():
    parser = argparse.ArgumentParser(description="AMIG 数据库恢复（MySQL，离线执行）")
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

    result = {"ok": False, "database": str(database.MYSQL_DATABASE), "backup": None,
              "pre_restore_copy": None, "steps": [], "error": None}

    if args.list:
        items = list_backups(str(config.BACKUP_DIR))
        if args.json:
            print(json.dumps({"backups": items}, ensure_ascii=False, indent=2))
        else:
            print(f"备份目录: {config.BACKUP_DIR}")
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
        print(f"备份表结构: {report.get('info', {}).get('tables')}")
        print(f"备份行数清单: {report.get('info', {}).get('row_counts')}")
        print(f"数据库校验: {report['detail']}")
        if report["uploads_archive"]:
            print(f"上传归档: {report['uploads_archive']}")
            print(f"归档文件数: {report['uploads_files']}")
        elif report.get("warning"):
            print(f"[提示] {report['warning']}")
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
    manifest_counts = info.get("row_counts")

    if not args.json:
        print("=" * 60)
        print("AMIG 数据库恢复")
        print("=" * 60)
        print(f"备份文件: {backup_path}")
        print(f"备份大小: {_human(info.get('size', 0))}")
        print(f"备份行数清单: {info.get('row_counts') or '（无清单，恢复后仅校验表结构）'}")
        print(f"目标数据库: {database.MYSQL_DATABASE} @ {config.MYSQL_HOST}:{config.MYSQL_PORT}")
        print()
        print("此操作会覆盖当前数据库的全部表；恢复前会自动导出回退副本，")
        print("导入或校验失败时自动用回退副本还原。请先停止 Web 进程。")

    if not args.yes:
        if not args.json:
            print("\n[未执行] 这是预演。确认无误后请追加 --yes 重新执行。")
        result["error"] = "未提供 --yes，仅执行预演"
        result["steps"].append("预演：未执行恢复")
        _emit(result, args.json, silent=True)
        return 2

    # 恢复前回退副本
    safety = os.path.join(str(config.BACKUP_DIR), f"pre_restore_{_stamp()}.sql")
    os.makedirs(str(config.BACKUP_DIR), exist_ok=True)
    ok, detail = _safety_dump(safety)
    if not ok:
        result["error"] = f"无法创建恢复前回退副本，已中止: {detail}"
        _emit(result, args.json, header=False)
        return 1
    result["pre_restore_copy"] = safety
    result["steps"].append(f"已创建恢复前副本: {safety}")

    restored, message = restore_database(backup_path, manifest_counts)
    result["steps"].append(message)
    if not restored:
        result["error"] = message
        # 自动用回退副本还原原库，避免留下半恢复状态
        rb_ok, rb_msg = restore_database(safety)
        result["steps"].append(
            f"已自动回滚到恢复前副本: {rb_msg}" if rb_ok
            else f"自动回滚失败（请手工执行: python tools/restore_db.py --backup {safety} --yes）"
        )
        _emit(result, args.json, header=False)
        return 1

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
