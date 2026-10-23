@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    where uv >nul 2>nul
    if errorlevel 1 (
        python -m venv .venv
        if errorlevel 1 exit /b 1
        .venv\Scripts\python.exe -m pip install -r requirements.txt
    ) else (
        uv sync
    )
    if errorlevel 1 exit /b 1
)
set "FARM_PORT=8000"
if not "%~1"=="" set "FARM_PORT=%~1"
echo Innovation Farming: http://127.0.0.1:%FARM_PORT%
.venv\Scripts\python.exe -m uvicorn farm.app:app --host 127.0.0.1 --port %FARM_PORT%
