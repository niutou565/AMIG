"""启动入口"""
import os
import sys

import uvicorn

from app.main import app


def _env_int(name, default):
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit(f"[ERROR] 环境变量 {name} 必须是整数，当前值: {raw!r}")
    return value


if __name__ == "__main__":
    host = os.getenv("HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = _env_int("PORT", 8000)
    workers = _env_int("WORKERS", 1)
    if not 1 <= port <= 65535:
        raise SystemExit(f"[ERROR] PORT 必须在 1-65535 之间，当前值: {port}")
    if workers < 1:
        raise SystemExit(f"[ERROR] WORKERS 必须大于 0，当前值: {workers}")
    if host not in ("127.0.0.1", "::1", "localhost"):
        print(
            f"[WARN] HOST={host} 会直接暴露应用端口；生产环境请保持 127.0.0.1 "
            "并由 Nginx 反向代理对外提供服务",
            file=sys.stderr,
        )

    print(f"[INFO] Starting AMIG on {host}:{port} with {workers} worker(s)")
    uvicorn.run("app.main:app", host=host, port=port, reload=False, workers=workers)
