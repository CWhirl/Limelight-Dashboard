@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel% equ 0 (set "PY=py") else (set "PY=python")
if not exist ".venv\Scripts\python.exe" (
    echo Creating local Python environment...
    %PY% -m venv .venv
    if errorlevel 1 goto :error
)
echo Installing/updating dashboard dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :error
echo Opening Limelight dashboard at http://127.0.0.1:8501
".venv\Scripts\python.exe" -m streamlit run app.py --server.address 127.0.0.1
exit /b %errorlevel%
:error
echo.
echo Setup failed. Ensure Python 3.10+ is installed and your PC can reach PyPI.
pause
exit /b 1
