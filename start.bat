@echo off
setlocal enabledelayedexpansion

:: ============================================================
::  Anonymous Image Sharing - One-Click Launcher (Windows)
::  Checks env, installs deps, inits DB, starts server, opens browser
:: ============================================================

title Anonymous Image Sharing

set "SCRIPT_DIR=%~dp0"
set "PORT=8000"
set "PROJECT_URL=http://127.0.0.1:%PORT%"

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

python -c "import fastapi, PIL, bcrypt, jinja2" >nul 2>&1
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

:: ========== Step 4: Check ip2region.xdb ==========
echo.
echo [*] Checking ip2region offline database...
if not exist "%SCRIPT_DIR%data\ip2region.xdb" (
    echo [WARN] data\ip2region.xdb not found, IP geo-location will be disabled
    echo         Download: https://github.com/lionsoul2014/ip2region/releases
    echo         Place ip2region.xdb into the data\ folder and restart
    echo [OK] Continuing without ip2region.xdb
) else (
    echo [OK] ip2region.xdb ready
)

:: ========== Step 5: Check .env Config ==========
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

findstr /C:"SECRET_KEY=change-me" "%SCRIPT_DIR%.env" >nul 2>&1
if %errorlevel% equ 0 (
    echo [WARN] SECRET_KEY is still the default value, consider changing it
)

:: ========== Step 6: Initialize Database ==========
echo.
echo [*] Initializing database...
if not exist "%SCRIPT_DIR%data" mkdir "%SCRIPT_DIR%data"
if exist "%SCRIPT_DIR%data\app.db" (
    echo [OK] Database already exists, skipping init
) else (
    python "%SCRIPT_DIR%init_db.py"
    if %errorlevel% neq 0 (
        echo [ERROR] Database initialization failed
        pause
        exit /b 1
    )
    echo [OK] Database initialized
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
echo     Press Ctrl+C to stop
echo.

:: Open browser after a short delay
start /b cmd /c "timeout /t 3 /nobreak >nul && start "" "%PROJECT_URL%""

:: Start uvicorn
cd /d "%SCRIPT_DIR%"
python -m uvicorn app.main:app --host 127.0.0.1 --port %PORT% --workers 1 --log-level info

echo.
echo [*] Server stopped
pause