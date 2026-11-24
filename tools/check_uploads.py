#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上传目录与数据库一致性检查工具（默认只读）。

用法：
    python tools/check_uploads.py                      # 只检查，输出报告
    python tools/check_uploads.py --older-than-days 7  # 标记/删除 7 天前的孤儿文件
    python tools/check_uploads.py --delete-orphans     # 真正删除符合条件的孤儿文件
    python tools/check_uploads.py --fix-misplaced      # 修复状态与目录不一致的文件

删除只会针对“上传目录中没有任何数据库记录引用”的文件，并且有安全期限；
数据库有记录但文件缺失的情况只报告，不会自动删除数据库记录。
--fix-misplaced 只移动被数据库引用的文件到期望目录（按图片整体移动，
失败自动回滚），不删除任何文件。
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import get_db
from app.upload_consistency import (
    scan_uploads, delete_orphans, delete_temp_files, fix_misplaced_files,
)


def main():
    parser = argparse.ArgumentParser(description="AMIG 上传目录一致性检查")
    parser.add_argument("--older-than-days", type=int, default=7,
                        help="孤立文件达到多少天后才允许删除（默认 7）")
    parser.add_argument("--delete-orphans", action="store_true",
                        help="真正删除符合条件的孤立文件；默认只输出报告")
    parser.add_argument("--delete-temp", action="store_true",
                        help="删除超过安全期限的 .tmp 临时文件")
    parser.add_argument("--fix-misplaced", action="store_true",
                        help="把状态与目录不一致的图片文件移动到期望目录；默认只输出报告")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出报告")
    args = parser.parse_args()

    # 数据库连通性检查（MySQL 后端不再以文件存在性判断）
    try:
        get_db().execute("SELECT 1")
    except Exception:
        print("[失败] 无法连接数据库（请检查 MySQL 配置，或先执行 init_db.py 初始化）")
        return 1
    try:
        report = scan_uploads(older_than_days=max(0, args.older_than_days))
    except Exception as exc:
        print(f"[失败] 无法读取数据库或上传目录: {exc}")
        return 1
    if not args.json:
        print("=" * 60)
        print("AMIG 上传目录一致性检查")
        print("=" * 60)
        print(f"上传目录: {report['root']}")
        print(f"私有目录: {report.get('private_root', '-')}")
        print(f"数据库图片记录: {report['image_rows']}")
        print(f"被引用文件数: {report['referenced_files']}")
        print(f"原图缺失: {len(report['missing_original'])}")
        print(f"缩略图缺失: {len(report['missing_thumbnail'])}")
        print(f"状态与目录不一致: {len(report.get('misplaced_files', []))}")
        print(f"孤立文件: {len(report['orphan_files'])}")
        print(f"临时文件: {len(report['temp_files'])}")
        print(f"Logo 文件: {len(report['logo_files'])}")
        for key in ("missing_original", "missing_thumbnail", "orphan_files", "temp_files"):
            for item in report[key]:
                print(f"  [{key}] {item}")
        for item in report.get("misplaced_files", []):
            print(
                "  [misplaced] image#%s %s 状态=%s 期望目录=%s 实际目录=%s"
                % (item["id"], item["path"], item["status"],
                   item["expected"], item["actual"])
            )

    failed = []
    if args.fix_misplaced:
        fixed, misplaced_failed = fix_misplaced_files(report)
        report["fixed_misplaced"] = fixed
        failed.extend(misplaced_failed)
        if not args.json:
            print(f"已修复 misplaced 文件: {len(fixed)}")
            for item in fixed:
                print(f"  {item}")
    if args.delete_orphans:
        deleted, orphan_failed = delete_orphans(report, args.older_than_days)
        report["deleted_orphans"] = deleted
        failed.extend(orphan_failed)
        if not args.json:
            print(f"已删除孤立文件: {len(deleted)}")
            for item in deleted:
                print(f"  {item}")
    if args.delete_temp:
        deleted, temp_failed = delete_temp_files(report, args.older_than_days)
        report["deleted_temp"] = deleted
        failed.extend(temp_failed)
        if not args.json:
            print(f"已删除临时文件: {len(deleted)}")
            for item in deleted:
                print(f"  {item}")
    if failed:
        report["delete_failed"] = failed

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif failed:
        print(f"删除失败: {len(failed)}")
        for item in failed:
            print(f"  {item}")
    elif not args.delete_orphans and not args.delete_temp and not args.fix_misplaced:
        print("\n当前为只读模式；确认无误后可加 --fix-misplaced / --delete-orphans / --delete-temp 执行处理。")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
