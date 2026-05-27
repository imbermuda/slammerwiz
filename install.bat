@echo off
REM PoEWiz Slammer - dev install
setlocal

where python >nul 2>&1
if errorlevel 1 (
  echo [!] Python not found on PATH. Install Python 3.10+ from https://www.python.org/downloads/
  exit /b 1
)

if not exist .venv (
  echo [+] Creating virtualenv...
  python -m venv .venv
)

call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt

echo.
echo [+] Install complete. Run with:  .venv\Scripts\python -m src.main
endlocal
