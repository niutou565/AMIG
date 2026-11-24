@echo off
setlocal enabledelayedexpansion

:: ============================================================
::  Anonymous Image Sharing - One-Click Launcher (Windows)
::  Checks env, installs deps, inits DB, starts server, opens browser
:: ============================================================

title Anonymous Image Sharing

set "SCRIPT_DIR=%~dp0"
if not defined PORT set "PORT=8000"
if not defined WORKERS set "WORKERS=1"
set "PROJECT_URL=http://127.0.0.1:%PORT%"

echo %PORT%| findstr /r "^[0-9][0-9]*$" >nul
if %errorlevel% neq 0 (
    echo [ERROR] PORT must be an integer (current: %PORT%)
    pause
    exit /b 1
)
echo %WORKERS%| findstr /r "^[1-9][0-9]*$" >nul
if %errorlevel% neq 0 (
    echo [ERROR] WORKERS must be a positive integer (current: %WORKERS%)
    pause
    exit /b 1
)

echo.
echo   ==========================================
echo     Anonymous Image Sharing - Launcher
echo   ==========================================
echo.

:: ========== Step 1: Check Python ==========
echo [*] Checking Python environment...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found. Please install Python 3.10+
    echo         Download: https://www.python.org/downloads/
    pause
    exit /b 1
)
for /f "tokens=*" %%i in ('python --version 2^>^&1') do set PY_VERSION=%%i
echo [OK] Detected: %PY_VERSION%

:: Warn about Python version issues
python -c "import sys; exit(0 if (3,10) <= sys.version_info[:2] <= (3,13) else 1)" >nul 2>&1
if %errorlevel% neq 0 (
    echo [WARN] Your Python version may not have prebuilt Pillow wheels.
    echo        Recommended: Python 3.10 - 3.13 for best compatibility.
    echo.
)

:: ========== Step 2: Upgrade pip first ==========
echo [*] Upgrading pip...
python -m pip install --upgrade pip >nul 2>&1

:: ========== Step 3: Check/Install Dependencies ==========
echo [*] Checking Python dependencies...
if not exist "%SCRIPT_DIR%requirements.txt" (
    echo [ERROR] requirements.txt not found
    pause
    exit /b 1
)

python -c "import fastapi, uvicorn, multipart, PIL, bcrypt, aiofiles, jinja2, httpx, pymysql" >nul 2>&1
if %errorlevel% neq 0 (
    echo [WARN] Dependencies not installed or incomplete, installing now...
    echo        (Using prebuilt wheels, this may take a moment...)
    echo.
    :: Use --only-binary :all: first to avoid source compilation
    pip install --only-binary :all: -r "%SCRIPT_DIR%requirements.txt" 2>nul
    if %errorlevel% neq 0 (
        echo [INFO] Some packages may need source install, retrying...
        pip install -r "%SCRIPT_DIR%requirements.txt"
    )
    if %errorlevel% neq 0 (
        echo.
        echo [ERROR] Dependency installation failed.
        echo         Try running manually: pip install -r requirements.txt
        echo         If Pillow fails, install from: https://www.lfd.uci.edu/~gohlke/pythonlibs/#pillow
        echo         Or use Python 3.10-3.13 which have prebuilt wheels.
        pause
        exit /b 1
    )
    echo.
    echo [OK] Dependencies installed
) else (
    echo [OK] Core dependencies ready
)

:: ========== Step 4: Check .env Config ==========
echo.
echo [*] Checking .env configuration...
if not exist "%SCRIPT_DIR%.env" (
    if exist "%SCRIPT_DIR%.env.example" (
        copy "%SCRIPT_DIR%.env.example" "%SCRIPT_DIR%.env" >nul
        echo [OK] Created .env from .env.example
    ) else (
        echo [ERROR] .env.example not found
        pause
        exit /b 1
    )
) else (
    echo [OK] .env config file exists
)

:: ========== Step 6: Initialize Database (MySQL) ==========
echo.
echo [*] Initializing database...
if not exist "%SCRIPT_DIR%data" mkdir "%SCRIPT_DIR%data"
:: 建表与默认数据初始化是幂等的；MySQL 连接失败时脚本会给出配置提示
python "%SCRIPT_DIR%init_db.py"
if %errorlevel% neq 0 (
    echo [ERROR] Database initialization failed.
    echo         Please check MYSQL_HOST / MYSQL_PORT / MYSQL_DATABASE / MYSQL_USER / MYSQL_PASSWORD
    echo         in .env, and make sure MySQL 8.0+ is running.
    pause
    exit /b 1
)

:: ========== Step 7: Check Port ==========
echo.
echo [*] Checking port %PORT% ...
set "OCCUPYING_PID="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%PORT% " ^| findstr "LISTENING"') do set "OCCUPYING_PID=%%p"
if defined OCCUPYING_PID (
    echo [WARN] Port %PORT% is occupied by PID !OCCUPYING_PID! ^(likely a previous instance^).
    echo        Attempting to stop the old process...
    taskkill /F /PID !OCCUPYING_PID! >nul 2>&1
    if !errorlevel! equ 0 (
        echo [OK] Old process stopped, waiting for port to be released...
        timeout /t 2 /nobreak >nul
    ) else (
        echo [ERROR] Could not stop PID !OCCUPYING_PID!. Please close it manually.
        netstat -ano | findstr ":%PORT% " | findstr "LISTENING"
        pause
        exit /b 1
    )
) else (
    echo [OK] Port %PORT% is available
)

:: ========== Step 8: Start Server ==========
echo.
echo [*] Starting web server...
echo     URL: %PROJECT_URL%
echo     Workers: %WORKERS%
echo     Press Ctrl+C to stop
echo.

:: Open browser after a short delay
start /b cmd /c "timeout /t 3 /nobreak >nul && start "" "%PROJECT_URL%""

:: Start uvicorn
cd /d "%SCRIPT_DIR%"
python -m uvicorn app.main:app --host 127.0.0.1 --port %PORT% --workers %WORKERS% --log-level info

echo.
echo [*] Server stopped
pause