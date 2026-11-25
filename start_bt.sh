#!/usr/bin/env bash
# ============================================================
#  AMIG - One-Click Launcher (宝塔面板 / 系统 Python)
#  使用系统 python3 直接运行，适配宝塔面板环境（无需 venv）
#  与 Windows 版 start.bat 功能一致
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${PORT:-8000}"
WORKERS="${WORKERS:-1}"
PROJECT_URL="http://127.0.0.1:${PORT}"

if ! [[ "$PORT" =~ ^[0-9]+$ ]] || [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
    echo "[ERROR] PORT must be an integer between 1 and 65535 (current: $PORT)"
    exit 1
fi
if ! [[ "$WORKERS" =~ ^[0-9]+$ ]] || [ "$WORKERS" -lt 1 ]; then
    echo "[ERROR] WORKERS must be a positive integer (current: $WORKERS)"
    exit 1
fi

echo ""
echo "  =========================================="
echo "    AMIG - Launcher (宝塔面板)"
echo "  =========================================="
echo ""

# ========== Step 1: 检查 Python ==========
echo "[*] Checking Python environment..."
if ! command -v python3 >/dev/null 2>&1; then
    echo "[ERROR] python3 not found. Please install Python 3.10+"
    exit 1
fi
PY_VERSION=$(python3 --version 2>&1)
echo "[OK] Detected: $PY_VERSION"

if ! python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>/dev/null; then
    echo "[ERROR] Python 3.10+ is required."
    exit 1
fi

# ========== Step 2: 升级 pip + 安装依赖（系统 python3） ==========
echo ""
echo "[*] Upgrading pip..."
python3 -m pip install --upgrade pip >/dev/null 2>&1 || true

echo "[*] Checking dependencies..."
# 注意：PyMySQL 仅在 DB_TYPE=mysql 时需要，SQLite 用户无需安装
if ! python3 -c "import fastapi, uvicorn, multipart, PIL, bcrypt, aiofiles, jinja2, httpx" >/dev/null 2>&1; then
    echo "[*] Installing dependencies (system python3)..."
    # Debian/Ubuntu PEP 668 限制时需 --break-system-packages
    python3 -m pip install --break-system-packages -r "$SCRIPT_DIR/requirements.txt" 2>/dev/null \
        || python3 -m pip install -r "$SCRIPT_DIR/requirements.txt" \
        || {
            echo "[ERROR] Dependency installation failed."
            echo "        Try manually: pip3 install --break-system-packages -r requirements.txt"
            exit 1
        }
fi
echo "[OK] Dependencies ready"

# ========== Step 3: 检查 .env ==========
echo ""
echo "[*] Checking .env configuration..."
if [ ! -f "$SCRIPT_DIR/.env" ]; then
    if [ -f "$SCRIPT_DIR/.env.example" ]; then
        cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
        echo "[OK] Created .env from .env.example"
    else
        echo "[ERROR] .env.example not found"
        exit 1
    fi
else
    echo "[OK] .env config file exists"
fi

# ========== Step 5: 初始化数据库（SQLite / MySQL，幂等） ==========
echo ""
echo "[*] Initializing database..."
mkdir -p "$SCRIPT_DIR/data"
# 老版本升级同样走这里：自动补齐表结构与索引，原数据保持不变
python3 "$SCRIPT_DIR/init_db.py" || {
    echo "[ERROR] Database initialization failed."
    echo "        DB_TYPE=sqlite: check DATABASE_PATH and directory permissions in .env"
    echo "        DB_TYPE=mysql : check MYSQL_HOST / MYSQL_PORT / MYSQL_DATABASE / MYSQL_USER / MYSQL_PASSWORD,"
    echo "        make sure MySQL 8.0+ is running and PyMySQL is installed."
    exit 1
}

# ========== Step 6: 检查端口 ==========
echo ""
echo "[*] Checking port $PORT ..."
PORT_PID=""
if command -v fuser >/dev/null 2>&1; then
    PORT_PID=$(fuser "$PORT/tcp" 2>/dev/null | grep -o '[0-9]*' | head -1 || true)
fi
if [ -n "$PORT_PID" ]; then
    echo "[WARN] Port $PORT is occupied by PID $PORT_PID (likely a previous instance)."
    echo "       Attempting to stop the old process..."
    kill "$PORT_PID" 2>/dev/null || true
    sleep 1
    echo "[OK] Old process stopped"
else
    echo "[OK] Port $PORT is available"
fi

# ========== Step 7: 启动服务 ==========
echo ""
echo "[*] Starting web server..."
echo "    URL: $PROJECT_URL"
echo "    Workers: $WORKERS"
echo "    Press Ctrl+C to stop"
echo ""
cd "$SCRIPT_DIR"
exec python3 -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" --workers "$WORKERS" --log-level info
