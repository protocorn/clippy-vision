@echo off
REM Clippy Vision Setup Script for Windows (Batch Alternative)
REM Use this if setup.ps1 fails due to PowerShell execution policy

echo ========================================
echo    Clippy Vision Setup - Windows
echo ========================================
echo.

REM Check Python
echo [1/6] Checking Python installation...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo   X Python not found!
    echo     Please install Python 3.8+ from https://www.python.org/downloads/
    echo     Make sure to check 'Add Python to PATH' during installation.
    pause
    exit /b 1
)
echo   OK Python is installed
python --version

REM Check Ollama
echo.
echo [2/6] Checking Ollama installation...
ollama --version >nul 2>&1
if %errorlevel% neq 0 (
    echo   X Ollama not found!
    echo     Please install Ollama from https://ollama.com/download
    echo     After installation, restart your terminal and run this script again.
    pause
    exit /b 1
)
echo   OK Ollama is installed

REM Start Ollama if not running
echo   Checking if Ollama is running...
tasklist /FI "IMAGENAME eq ollama.exe" 2>NUL | find /I /N "ollama.exe">NUL
if %errorlevel% neq 0 (
    echo   Starting Ollama service...
    start /B ollama serve
    timeout /t 3 /nobreak >nul
    echo   OK Ollama started
) else (
    echo   OK Ollama is already running
)

REM Install Python dependencies
echo.
echo [3/6] Installing Python dependencies...
if not exist "requirements.txt" (
    echo   X requirements.txt not found!
    pause
    exit /b 1
)
python -m pip install --upgrade pip --quiet
python -m pip install -r requirements.txt --quiet
if %errorlevel% neq 0 (
    echo   X Failed to install Python packages
    pause
    exit /b 1
)
echo   OK Python packages installed

REM Pull Ollama models
echo.
echo [4/6] Downloading required AI models...
echo   This may take 15-45 minutes depending on your internet speed.
echo.

echo   Checking qwen3:8b (~4.7 GB) - Main reasoning engine
ollama list | findstr "qwen3:8b" >nul 2>&1
if %errorlevel% neq 0 (
    echo   Downloading qwen3:8b... (this may take a while)
    ollama pull qwen3:8b
    echo   OK Downloaded qwen3:8b
) else (
    echo   OK Model already exists: qwen3:8b
)

REM Create directories
echo.
echo [5/6] Setting up project directories...
if not exist "core\data" mkdir "core\data"
if not exist "core\data\screenshots" mkdir "core\data\screenshots"
if not exist "logs" mkdir "logs"
echo   OK Directories created

REM Connectivity test
echo.
echo [6/6] Running connectivity test...
python -c "from core.local_embeddings import embed_text; assert embed_text('test'); print('  OK local embeddings available')" 2>nul
if %errorlevel% neq 0 (
    echo   ! Local embedding test failed - check Python dependencies
)

REM Final summary
echo.
echo ========================================
echo    Setup Complete!
echo ========================================
echo.
echo Verify Installation:
echo   Run the test script to check everything:
echo   python test_installation.py
echo.
echo Quick Start Commands:
echo   1. Start capturing your activity:
echo      python core\screen_capture.py
echo.
echo   2. Chat with Clippy (in a new terminal):
echo      python agent\react_agent.py
echo.
echo   3. Run MCP server (optional):
echo      python mcp_server.py
echo.
echo Troubleshooting:
echo   - If Ollama commands fail, restart the terminal or run: ollama serve
echo   - Models location: C:\Users\%USERNAME%\.ollama\models
echo   - Database location: core\data\events.db
echo   - Screenshots location: core\data\screenshots\
echo.
echo For more info, see README.md
echo.
pause
