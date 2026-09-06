@echo off
REM GovAuctions Automated Login Launcher
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    py -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -r requirements.txt
)
.\.venv\Scripts\python.exe govauction_login.py %*
pause
