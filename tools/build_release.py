#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布包构建与内容扫描工具。

从指定 Git 提交（默认 HEAD）按白名单打包，确保发布包不包含：
- 测试代码（tests/）与测试数据
- 本地配置与密钥（.env 等）
- 运行时数据（data/、backups/、uploads/、private_uploads/、日志）
- 开发残留（__pycache__、*.pyc、.pytest_cache、.screenshots、临时脚本）

命中禁入项时以非 0 退出码终止，禁止发布。

用法：
    python tools/build_release.py --scan-only          # 只扫描，不写文件
    python tools/build_release.py                      # 生成 dist/amig-<版本>.zip
    python tools/build_release.py --ref 1.2.0 --out dist/amig-1.2.0.zip
"""
import argparse
import fnmatch
import io
import os
import re
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.config import APP_NAME, APP_VERSION  # noqa: E402

# 允许进入发布包的顶层条目（目录或文件）
ALLOWED_TOP_LEVEL = {
    "app", "tools", "docs",
    "README.md", "CHANGELOG.md", "requirements.txt", ".env.example",
    ".gitignore", ".gitattributes",
    "init_db.py", "reset_password.py", "run.py",
    "start.bat", "start.sh", "start_bt.sh",
}

# 禁止出现的路径模式（在允许列表之后再兜底扫描一次）
FORBIDDEN_PATTERNS = [
    "tests/*", "*/tests/*",
    ".env", "*.env",
    "data/*", "backups/*", "uploads/*", "private_uploads/*",
    "__pycache__/*", "*/__pycache__/*", "*.pyc", "*.pyo",
    ".pytest_cache/*", ".screenshots/*", "dist/*",
    "*.log", "logs/*",
    "*.sql", "*.db", "*.db-wal", "*.db-shm", "*.sqlite", "*.sqlite3",
    "*_tmp.py", "tmp_*.py", "seed_tmp.py",
    # 开发辅助样例图（仓库内保留，发布包不携带）
    "docs/watermark_samples/*",
]

# 这些目录永远不打包（即便在 ALLOWED_TOP_LEVEL 内）
FORBIDDEN_DIRS = {
    "tests", "data", "backups", "uploads", "private_uploads",
    "__pycache__", ".pytest_cache", ".screenshots", "dist", "logs",
    ".git", ".vscode", ".idea", "venv", "node_modules",
}


def _git(*args):
    proc = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"[失败] git {' '.join(args)} 出错: "
            f"{proc.stderr.decode('utf-8', errors='replace').strip()}"
        )
    return proc.stdout


def _is_forbidden(path):
    if any(part in FORBIDDEN_DIRS for part in path.split("/")[:-1]):
        return True
    if path.split("/", 1)[0] in FORBIDDEN_DIRS:
        return True
    return any(fnmatch.fnmatch(path, pattern) for pattern in FORBIDDEN_PATTERNS)


def select_files(ref):
    """返回 (要打包的文件列表, 被排除的文件列表)。"""
    raw = _git("ls-tree", "-r", "--name-only", ref).decode("utf-8")
    tracked = [line for line in raw.splitlines() if line]
    included, excluded = [], []
    for path in tracked:
        top = path.split("/", 1)[0]
        if top not in ALLOWED_TOP_LEVEL:
            excluded.append(path)
            continue
        if _is_forbidden(path):
            excluded.append(path)
            continue
        included.append(path)
    return included, excluded


def scan(names):
    """扫描最终包内文件名，返回违规项列表。"""
    return [name for name in names if _is_forbidden(name)]


def _read_blob(ref, path):
    return _git("show", f"{ref}:{path}")


def build_zip(ref, version, out_path):
    included, excluded = select_files(ref)
    violations = scan(included)
    if violations:
        print("[失败] 发布包包含禁止条目，已中止：")
        for item in violations[:50]:
            print(f"  - {item}")
        return 1

    prefix = f"amig-{version}"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in included:
            zf.writestr(f"{prefix}/{path}", _read_blob(ref, path))

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "wb") as fh:
        fh.write(buffer.getvalue())

    size_mb = len(buffer.getvalue()) / 1024 / 1024
    print("=" * 60)
    print(f"AMIG {version} 发布包已生成")
    print("=" * 60)
    print(f"来源提交: {ref}")
    print(f"输出文件: {os.path.abspath(out_path)}")
    print(f"文件数量: {len(included)}")
    print(f"包体积  : {size_mb:.2f} MB")
    print(f"已排除  : {len(excluded)} 个条目（测试/运行时数据/本地配置等）")
    for item in excluded:
        if not item.startswith("docs/watermark_samples"):
            print(f"  - {item}")
    return 0


def main():
    parser = argparse.ArgumentParser(description="AMIG 发布包构建与内容扫描")
    parser.add_argument("--version", default=APP_VERSION, help=f"版本号（默认 {APP_VERSION}）")
    parser.add_argument("--ref", default="HEAD", help="Git 提交/标签（默认 HEAD）")
    parser.add_argument("--out", default=None,
                        help="输出路径（默认 dist/amig-<版本>.zip）")
    parser.add_argument("--scan-only", action="store_true",
                        help="只扫描待发布内容，不生成文件")
    args = parser.parse_args()

    if not re.match(r"^[0-9]+\.[0-9]+\.[0-9]+$", args.version):
        print(f"[失败] 版本号格式非法: {args.version}（应为 MAJOR.MINOR.PATCH）")
        return 1

    included, excluded = select_files(args.ref)
    violations = scan(included)
    if violations:
        print("[失败] 待发布内容包含禁止条目：")
        for item in violations[:50]:
            print(f"  - {item}")
        if args.scan_only:
            return 1

    print("=" * 60)
    print(f"{APP_NAME} 发布内容扫描")
    print("=" * 60)
    print(f"版本: {args.version}  来源: {args.ref}")
    print(f"打包文件: {len(included)} 个")
    print(f"排除条目: {len(excluded)} 个")
    print(f"禁止项  : {len(violations)} 个")
    if violations:
        return 1

    if args.scan_only:
        print("\n[通过] 扫描未发现测试/临时/本地配置文件")
        return 0

    out_path = args.out or os.path.join(ROOT, "dist", f"amig-{args.version}.zip")
    return build_zip(args.ref, args.version, out_path)


if __name__ == "__main__":
    sys.exit(main())
