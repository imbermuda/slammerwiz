@echo off
setlocal
if not exist .venv call install.bat
call .venv\Scripts\activate.bat
python -m src.main
endlocal
