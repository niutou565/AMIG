#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把待审核/已拒绝图片从公开上传目录迁移到私有上传目录（默认只预演）。

第三阶段起，待审核与已拒绝图片物理存放在 PRIVATE_UPLOADS_DIR，避免
Web 服务器误将 /uploads 目录整段静态暴露时泄露未审核内容。历史版本的
待审文件仍在公开目录，运行本工具完成一次性迁移。

用法：
    python tools/migrate_private_uploads.py            # 预演，只输出计划
    python tools/migrate_private_uploads.py --apply    # 真正移动文件
    python tools/migrate_private_uploads.py --apply --status pending

迁移是幂等的：重复执行时，已位于私有目录的文件会被跳过。
"""

import argparse
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import image_utils
from app.config import DATABASE_PATH
from app.database import get_db


def _plan_moves(statuses):
    rows = get_db().execute(
        "SELECT id, filename, thumb_filename, status FROM images "
        "WHERE status IN ({})".format(",".join("?" for _ in statuses)),
        tuple(statuses),
    ).fetchall()

    moves = []
    skipped = []
    missing = []
    conflicts = []

    for row in rows:
        for field in ("filename", "thumb_filename"):
            relative = row[field] or ""
            if not relative:
                continue
            src = image_utils.resolve_upload_path(
                relative, must_exist=False, uploads_dir=image_utils.UPLOADS_DIR
            )
            dst = image_utils.resolve_upload_path(
                relative, must_exist=False,
                uploads_dir=image_utils.PRIVATE_UPLOADS_DIR,
            )
            if src is None or dst is None:
                conflicts.append({
                    "id": row["id"], "path": relative,
                    "error": "路径无效（越界或非法文件名）",
                })
                continue
            if src.exists() and dst.exists():
                conflicts.append({
                    "id": row["id"], "path": relative,
                    "error": "公开与私有目录同时存在同名文件，拒绝覆盖",
                })
                continue
            if dst.exists():
                skipped.append({"id": row["id"], "path": relative, "reason": "已在私有目录"})
                continue
            if not src.exists():
                missing.append({"id": row["id"], "path": relative, "reason": "公开目录中不存在"})
                continue
            moves.append({
                "id": row["id"], "status": row["status"],
                "path": relative, "src": str(src), "dst": str(dst),
            })
    return moves, skipped, missing, conflicts


def _apply_moves(moves):
    moved = []
    failed = []
    for item in moves:
        try:
            os.makedirs(os.path.dirname(item["dst"]), exist_ok=True)
            shutil.move(item["src"], item["dst"])
            moved.append(item)
        except OSError as exc:
            failed.append({**item, "error": str(exc)})
    return moved, failed


def main():
    parser = argparse.ArgumentParser(description="AMIG 待审图片私有目录迁移")
    parser.add_argument("--apply", action="store_true",
                        help="真正移动文件；默认只输出迁移计划（预演）")
    parser.add_argument("--status", default="pending,rejected",
                        help="需要迁移的状态，逗号分隔（默认 pending,rejected）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出报告")
    args = parser.parse_args()

    if not os.path.exists(DATABASE_PATH):
        print(f"[失败] 数据库不存在: {DATABASE_PATH}（请先启动应用或执行 init_db.py）")
        return 1

    statuses = [s.strip() for s in args.status.split(",") if s.strip()]
    allowed = {"pending", "rejected"}
    if not statuses or any(s not in allowed for s in statuses):
        print("[失败] --status 仅支持 pending,rejected")
        return 1

    try:
        moves, skipped, missing, conflicts = _plan_moves(statuses)
    except Exception as exc:
        print(f"[失败] 无法读取数据库: {exc}")
        return 1

    report = {
        "mode": "apply" if args.apply else "dry-run",
        "public_dir": image_utils.UPLOADS_DIR,
        "private_dir": image_utils.PRIVATE_UPLOADS_DIR,
        "statuses": statuses,
        "planned": len(moves),
        "moved": 0,
        "failed": [],
        "skipped": skipped,
        "missing": missing,
        "conflicts": conflicts,
    }

    if args.apply:
        moved, failed = _apply_moves(moves)
        report["moved"] = len(moved)
        report["failed"] = failed

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("=" * 60)
        print("AMIG 待审图片私有目录迁移" + ("（执行）" if args.apply else "（预演）"))
        print("=" * 60)
        print(f"公开目录: {report['public_dir']}")
        print(f"私有目录: {report['private_dir']}")
        print(f"待迁移状态: {', '.join(statuses)}")
        print(f"计划移动: {report['planned']}")
        if args.apply:
            print(f"实际移动: {report['moved']}")
            print(f"移动失败: {len(report['failed'])}")
        print(f"已跳过（已在私有目录）: {len(skipped)}")
        print(f"源文件缺失: {len(missing)}")
        print(f"冲突/非法路径: {len(conflicts)}")
        for key in ("failed", "missing", "conflicts"):
            for item in report[key]:
                print(f"  [{key}] {item}")

    if not args.apply:
        print("\n当前为预演模式；确认无误后加 --apply 执行迁移。")
    if report["failed"] or report["conflicts"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
