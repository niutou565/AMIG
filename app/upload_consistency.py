# -*- coding: utf-8 -*-
"""上传目录与数据库一致性检查（默认只读）。

用于人工运维/迁移后核对：
- 数据库有记录但原图/缩略图缺失（已通过图片查公开目录，待审/已拒绝图片查私有目录）；
- 公开/私有上传目录中存在没有任何数据库记录引用的孤立文件；
- 长期未清理的 .tmp 临时文件。

不会在应用启动时自动删除文件；只有显式调用 delete_orphans 且超过安全期限才删除。
"""

import logging
import os
import time
from pathlib import Path

from app.database import get_db
from app import image_utils

LOGGER = logging.getLogger(__name__)
LOGO_PREFIXES = ("logo_large_", "logo_small_")
_IGNORED_NAMES = {".gitkeep"}
_PUBLIC = "public"
_PRIVATE = "private"


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _resolve(root, relative):
    return image_utils.resolve_upload_path(
        relative, must_exist=True, uploads_dir=root
    )


def _find_file(relative, bases):
    """按顺序在多个上传根目录查找文件，返回 (path, base) 或 (None, None)。"""
    for base in bases:
        path = _resolve(base, relative)
        if path is not None:
            return path, base
    return None, None


def _base_name(base, public_root, private_root):
    """返回目录的可读名称，直接取目录名（uploads / private_uploads）。"""
    if base == public_root:
        return os.path.basename(os.path.normpath(str(public_root)))
    if base == private_root:
        return os.path.basename(os.path.normpath(str(private_root)))
    return str(base)


def _scan_root(root, referenced, current, older_than_days, include_logos):
    orphan_files = []
    temp_files = []
    logo_files = []
    root = Path(root).resolve()
    if not root.is_dir():
        return orphan_files, temp_files, logo_files

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [name for name in dirnames if not name.startswith(".")]
        directory = Path(dirpath)
        for name in filenames:
            if name in _IGNORED_NAMES:
                continue
            path = directory / name
            if path.is_symlink():
                continue
            relative = _relative(root, path)
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue

            if name.endswith(".tmp"):
                temp_files.append({
                    "path": relative,
                    "age_days": round((current - mtime) / 86400, 1),
                })
                continue
            if relative in referenced:
                continue
            if include_logos and "/" not in relative and relative.startswith(LOGO_PREFIXES):
                logo_files.append(relative)
                continue
            orphan_files.append({
                "path": relative,
                "age_days": round((current - mtime) / 86400, 1),
                "old_enough": (current - mtime) >= older_than_days * 86400,
            })
    return orphan_files, temp_files, logo_files


def scan_uploads(older_than_days=7, now=None):
    """扫描公开与私有上传目录，返回只读一致性报告。"""
    public_root = Path(image_utils.UPLOADS_DIR).resolve()
    private_root = Path(image_utils.PRIVATE_UPLOADS_DIR).resolve()
    rows = get_db().execute(
        "SELECT id, filename, thumb_filename, status FROM images"
    ).fetchall()

    referenced = set()
    # 站点 Logo 也属于有效文件，避免清理时误删（即使将来 Logo 命名规则变化）
    for key in ("logo_large", "logo_small"):
        row = get_db().execute(
            "SELECT value FROM settings WHERE `key`=?", (key,)
        ).fetchone()
        if row and row["value"]:
            referenced.add(str(row["value"]).replace("\\", "/"))

    missing_original = []
    missing_thumbnail = []
    misplaced_files = []
    for row in rows:
        status = row["status"] or "pending"
        # 已通过图片应位于公开目录，待审/已拒绝应位于私有目录。
        # 优先查期望目录；另一目录仅用于兼容尚未执行迁移的历史数据，
        # 命中时记入 misplaced_files —— 否则「状态与文件位置不一致」会被
        # 静默忽略，而这正是图片审核状态机最容易出问题的地方。
        if status == "approved":
            expected, other = public_root, private_root
        else:
            expected, other = private_root, public_root

        for field, target in (("filename", missing_original),
                              ("thumb_filename", missing_thumbnail)):
            relative = row[field] or ""
            if not relative:
                continue
            referenced.add(relative)
            path, base = _find_file(relative, (expected, other))
            if path is None:
                target.append({"id": row["id"], "path": relative})
                continue
            if base == other:
                misplaced_files.append({
                    "id": row["id"],
                    "path": relative,
                    "status": status,
                    "expected": _base_name(expected, public_root, private_root),
                    "actual": _base_name(other, public_root, private_root),
                })

    public_orphans, public_temps, logo_files = _scan_root(
        public_root, referenced, now if now is not None else time.time(),
        older_than_days, include_logos=True,
    )
    private_orphans, private_temps, _ = _scan_root(
        private_root, referenced, now if now is not None else time.time(),
        older_than_days, include_logos=False,
    )
    for item in public_orphans:
        item["base"] = _PUBLIC
    for item in public_temps:
        item["base"] = _PUBLIC
    for item in private_orphans:
        item["base"] = _PRIVATE
    for item in private_temps:
        item["base"] = _PRIVATE

    return {
        "root": str(public_root),
        "private_root": str(private_root),
        "image_rows": len(rows),
        "referenced_files": len(referenced),
        "missing_original": missing_original,
        "missing_thumbnail": missing_thumbnail,
        "misplaced_files": misplaced_files,
        "orphan_files": public_orphans + private_orphans,
        "temp_files": public_temps + private_temps,
        "logo_files": logo_files,
        "older_than_days": older_than_days,
    }


def _root_for_item(item, report):
    if item.get("base") == _PRIVATE:
        return report.get("private_root") or image_utils.PRIVATE_UPLOADS_DIR
    return report.get("root") or image_utils.UPLOADS_DIR


def fix_misplaced_files(report):
    """把报告中“状态与目录不一致”的文件移动到状态期望的目录。

    与 delete_orphans 不同，本操作只移动被数据库引用的文件，不删除任何文件；
    但仍需显式调用（tools/check_uploads.py --fix-misplaced）才会执行。
    按图片 id 分组，保证原图与缩略图一起移动；任一文件移动失败则整体回滚。
    返回 (fixed, failed)。
    """
    fixed = []
    failed = []
    public_root = Path(image_utils.UPLOADS_DIR).resolve()
    private_root = Path(image_utils.PRIVATE_UPLOADS_DIR).resolve()

    by_id = {}
    for item in report.get("misplaced_files", []):
        by_id.setdefault(item["id"], []).append(item)

    for image_id, items in sorted(by_id.items()):
        row = get_db().execute(
            "SELECT filename, thumb_filename, status FROM images WHERE id=?",
            (image_id,),
        ).fetchone()
        if row is None:
            for item in items:
                failed.append({"path": item["path"], "error": "数据库记录已不存在"})
            continue
        if row["status"] == "approved":
            expected, other = public_root, private_root
        else:
            expected, other = private_root, public_root
        failures = image_utils.promote_image_files(
            row["filename"], row["thumb_filename"],
            source_dir=other, target_dir=expected,
        )
        if failures:
            # promote_image_files 失败时自动回滚，文件保持原状
            for item in items:
                failed.append({
                    "path": item["path"],
                    "error": "移动失败，已回滚保持原状",
                    "detail": failures,
                })
            LOGGER.warning("修复 misplaced 图片 #%s 失败: %s", image_id, failures)
        else:
            for item in items:
                fixed.append(item["path"])
            LOGGER.info(
                "已修复 misplaced 图片 #%s（%d 个文件移动到期望目录）",
                image_id, len(items),
            )
    return fixed, failed


def delete_temp_files(report, older_than_days=None):
    """删除超过安全期限的 .tmp 临时文件；返回 (deleted, failed)。"""
    if older_than_days is None:
        older_than_days = report.get("older_than_days", 7)
    deleted = []
    failed = []
    for item in report.get("temp_files", []):
        if item.get("age_days", 0) < older_than_days:
            continue
        relative = item.get("path", "")
        path = _resolve(_root_for_item(item, report), relative)
        if path is None:
            failed.append({"path": relative, "error": "路径无效或文件已不存在"})
            continue
        try:
            os.remove(path)
            deleted.append(relative)
            LOGGER.info("已删除过期临时上传文件: %s (%s)", relative, item.get("base"))
        except OSError as exc:
            failed.append({"path": relative, "error": str(exc)})
            LOGGER.warning("删除临时上传文件失败 %s: %s", relative, exc)
    return deleted, failed


def delete_orphans(report, older_than_days=None):
    """删除报告中足够陈旧且仍未被数据库引用的孤立文件；返回 (deleted, failed)。"""
    if older_than_days is None:
        older_than_days = report.get("older_than_days", 7)
    deleted = []
    failed = []
    for item in report.get("orphan_files", []):
        if not item.get("old_enough"):
            continue
        relative = item.get("path", "")
        root = _root_for_item(item, report)
        # 删除前再次确认路径仍在对应上传目录内，且数据库没有引用
        path = _resolve(root, relative)
        if path is None:
            failed.append({"path": relative, "error": "路径无效或文件已不存在"})
            continue
        row = get_db().execute(
            "SELECT 1 FROM images WHERE filename=? OR thumb_filename=? LIMIT 1",
            (relative, relative),
        ).fetchone()
        if row:
            failed.append({"path": relative, "error": "文件已被数据库引用，拒绝删除"})
            continue
        try:
            os.remove(path)
            deleted.append(relative)
            LOGGER.info("已删除孤立上传文件: %s (%s)", relative, item.get("base"))
        except OSError as exc:
            failed.append({"path": relative, "error": str(exc)})
            LOGGER.warning("删除孤立上传文件失败 %s: %s", relative, exc)
    return deleted, failed
