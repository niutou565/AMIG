#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AMIG 图形化安装向导入口（仅用于全新安装）。

用法：
    python install.py                 # 本机安装，自动打开浏览器
    python install.py --remote        # 监听 0.0.0.0，用另一台电脑的浏览器访问
    python install.py --port 9000     # 指定安装向导端口（默认 8765）
    python install.py --no-browser    # 不自动打开浏览器

老版本（1.1.x）升级不需要运行本脚本：直接启动应用（run.py / start.sh）即可
自动补齐表结构并沿用原数据库；数据库切换请登录后台管理操作。

安装向导是临时服务：安装完成后按 Ctrl+C 结束即可，随后用 run.py 启动应用。
"""
import argparse
import secrets
import sys
import threading
import webbrowser
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

try:
    import uvicorn  # noqa: E402
    from installer.app import create_app, lan_addresses  # noqa: E402
except ImportError as _exc:  # pragma: no cover - 环境缺少依赖时的明确提示
    raise SystemExit(
        f"[错误] 安装向导缺少依赖：{_exc}\n"
        "       请先执行 pip install -r requirements.txt 后重试"
        "（或直接使用 start.bat / start.sh，它们会自动安装依赖）"
    )


def _parse_args():
    parser = argparse.ArgumentParser(description="AMIG 图形化安装向导")
    parser.add_argument("--host", default="127.0.0.1",
                        help="监听地址（默认 127.0.0.1；远程安装用 0.0.0.0）")
    parser.add_argument("--port", type=int, default=8765, help="安装向导端口（默认 8765）")
    parser.add_argument("--remote", action="store_true",
                        help="监听 0.0.0.0，允许从其他电脑访问安装向导")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()
    if args.remote:
        args.host = "0.0.0.0"
    if not 1 <= args.port <= 65535:
        parser.error("--port 必须在 1-65535 之间")
    return args


def main():
    args = _parse_args()
    token = secrets.token_urlsafe(32)
    app = create_app(token, BASE_DIR)

    local_url = f"http://127.0.0.1:{args.port}/install/{token}"
    print("=" * 68)
    print("  AMIG 1.2.5 图形化安装向导")
    print("=" * 68)
    print(f"  安装地址（本机）：{local_url}")
    if args.host == "0.0.0.0":
        for address in lan_addresses():
            print(f"  安装地址（远程）：http://{address}:{args.port}/install/{token}")
        print("  提示：远程访问需在服务器防火墙/安全组放行该端口；")
        print("        安装完成后请结束本进程（Ctrl+C），改用 run.py 启动应用。")
    print("  " + "-" * 64)
    print("  检测到已有安装时，向导会提示：老版本直接启动即可完成升级。")
    print("  按 Ctrl+C 可随时退出安装向导。")
    print("=" * 68)

    if not args.no_browser and args.host in ("127.0.0.1", "localhost"):
        threading.Timer(1.2, lambda: webbrowser.open(local_url)).start()

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
