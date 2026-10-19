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
rem Do not use for /f or sys.argv[1]: cmd treats parentheses as block closers and
rem expands %1 as this script's first argument. Write the URL to a temp file instead.
if defined CLIPPY_OLLAMA_URL goto :clippy_ollama_url_done
if defined OLLAMA_HOST goto :clippy_ollama_url_done
set "CLIPPY_OLLAMA_URL=http://127.0.0.1:11434"
if not exist "%CLIPPY_DATA_DIR%\llm_config.json" goto :clippy_ollama_url_done
set "CLIPPY_OLLAMA_URL_TMP=%TEMP%\clippy-ollama-url.%RANDOM%.txt"
"%CLIPPY_PYTHON%" -c "import json,os,sys; p=os.path.join(os.environ.get('CLIPPY_DATA_DIR',''),'llm_config.json'); sys.stdout.write(str(json.load(open(p,encoding='utf-8')).get('base_url') or '').strip())" >"%CLIPPY_OLLAMA_URL_TMP%" 2>nul
if exist "%CLIPPY_OLLAMA_URL_TMP%" <"%CLIPPY_OLLAMA_URL_TMP%" set /p CLIPPY_OLLAMA_URL_READ=
if exist "%CLIPPY_OLLAMA_URL_TMP%" del "%CLIPPY_OLLAMA_URL_TMP%" >nul 2>&1
if defined CLIPPY_OLLAMA_URL_READ if not "%CLIPPY_OLLAMA_URL_READ%"=="" set "CLIPPY_OLLAMA_URL=%CLIPPY_OLLAMA_URL_READ%"
set "CLIPPY_OLLAMA_URL_READ="
set "CLIPPY_OLLAMA_URL_TMP="
:clippy_ollama_url_done

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
