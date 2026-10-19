@echo off
setlocal EnableExtensions

rem Stable stdio entrypoint for MCP clients (Cursor, Claude Desktop, etc.).
rem Clients should launch THIS file, not raw python mcp_server.py.

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "CLIPPY_ROOT=%%~fI"

if not exist "%CLIPPY_ROOT%\mcp_server.py" (
    echo clippy-mcp: mcp_server.py not found under "%CLIPPY_ROOT%" 1>&2
    exit /b 1
)

if not defined CLIPPY_PYTHON (
    where python >nul 2>&1 && set "CLIPPY_PYTHON=python"
)
if not defined CLIPPY_PYTHON (
    where python3 >nul 2>&1 && set "CLIPPY_PYTHON=python3"
)
if not defined CLIPPY_PYTHON (
    echo clippy-mcp: Python not found on PATH. Set CLIPPY_PYTHON to your python.exe. 1>&2
    exit /b 1
)

if not defined CLIPPY_DATA_DIR (
    if exist "%APPDATA%\Clippy Vision\data\" (
        set "CLIPPY_DATA_DIR=%APPDATA%\Clippy Vision\data"
    ) else (
        set "CLIPPY_DATA_DIR=%CLIPPY_ROOT%\core\data"
    )
)

if not defined CLIPPY_EMBEDDING_MODEL_DIR (
    if /I "%CLIPPY_DATA_DIR%"=="%CLIPPY_ROOT%\core\data" set "CLIPPY_EMBEDDING_MODEL_DIR=%CLIPPY_ROOT%\models\embeddings\all-MiniLM-L6-v2"
    if /I "%CLIPPY_DATA_DIR%"=="%CLIPPY_ROOT%\core\data\" set "CLIPPY_EMBEDDING_MODEL_DIR=%CLIPPY_ROOT%\models\embeddings\all-MiniLM-L6-v2"
)
if not defined CLIPPY_EMBEDDING_MODEL_DIR (
    for %%I in ("%CLIPPY_DATA_DIR%") do (
        if /I "%%~nxI"=="data" (
            for %%J in ("%%~dpI.") do set "CLIPPY_EMBEDDING_MODEL_DIR=%%~fJ\models\embeddings\all-MiniLM-L6-v2"
        )
    )
)
if not defined CLIPPY_EMBEDDING_MODEL_DIR (
    set "CLIPPY_EMBEDDING_MODEL_DIR=%CLIPPY_DATA_DIR%\models\embeddings\all-MiniLM-L6-v2"
)

rem Match Python: CLIPPY_OLLAMA_URL, then OLLAMA_HOST, then llm_config, then default.
rem Read the path from CLIPPY_DATA_DIR in the child env. Do not use sys.argv[1]
rem here: cmd expands %1 as this script's first argument.
if not defined CLIPPY_OLLAMA_URL if not defined OLLAMA_HOST (
    if exist "%CLIPPY_DATA_DIR%\llm_config.json" (
        for /f "usebackq delims=" %%U in (`"%CLIPPY_PYTHON%" -c "import json,os; p=os.path.join(os.environ.get('CLIPPY_DATA_DIR',''),'llm_config.json'); print((json.load(open(p, encoding='utf-8')).get('base_url') or '').strip())" 2^>nul`) do set "CLIPPY_OLLAMA_URL=%%U"
    )
)
if not defined CLIPPY_OLLAMA_URL if not defined OLLAMA_HOST (
    set "CLIPPY_OLLAMA_URL=http://127.0.0.1:11434"
)

if defined PYTHONPATH (
    set "PYTHONPATH=%CLIPPY_ROOT%;%PYTHONPATH%"
) else (
    set "PYTHONPATH=%CLIPPY_ROOT%"
)
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

rem All diagnostics must go to stderr — stdout is the MCP protocol stream.
"%CLIPPY_PYTHON%" "%CLIPPY_ROOT%\mcp_server.py"
exit /b %ERRORLEVEL%
