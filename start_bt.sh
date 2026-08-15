#!/usr/bin/env bash
# ============================================================
#  AMIG - One-Click Launcher (宝塔面板 / 系统 Python)
#  使用系统 python3 直接运行，适配宝塔面板环境（无需 venv）
#  与 Windows 版 start.bat 功能一致
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${PORT:-8000}"
PROJECT_URL="http://127.0.0.1:${PORT}"

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
if ! python3 -c "import fastapi, PIL, bcrypt, jinja2" >/dev/null 2>&1; then
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

# ========== Step 3: 检查 ip2region.xdb ==========
echo ""
echo "[*] Checking ip2region offline database..."
if [ ! -f "$SCRIPT_DIR/data/ip2region.xdb" ]; then
    echo "[WARN] data/ip2region.xdb not found, IP geo-location will be disabled"
    echo "       Download: https://github.com/lionsoul2014/ip2region/releases"
else
    echo "[OK] ip2region.xdb ready"
fi

# ========== Step 4: 检查 .env ==========
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

# ========== Step 5: 初始化数据库 ==========
echo ""
echo "[*] Initializing database..."
mkdir -p "$SCRIPT_DIR/data"
if [ -f "$SCRIPT_DIR/data/app.db" ]; then
    echo "[OK] Database already exists, skipping init"
else
    python3 "$SCRIPT_DIR/init_db.py" || {
        echo "[ERROR] Database initialization failed"
        exit 1
    }
    echo "[OK] Database initialized"
fi

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
echo "    Press Ctrl+C to stop"
echo ""
cd "$SCRIPT_DIR"
exec python3 -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" --workers 1 --log-level info
