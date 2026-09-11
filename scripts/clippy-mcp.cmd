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
