@echo off
REM Build single-file Windows executable via PyInstaller.
REM Output: dist\SlammerWiz.exe + dist\config.json (ship both).
setlocal

if not exist .venv (
  call install.bat
)
call .venv\Scripts\activate.bat
pip install pyinstaller==6.10.0

pyinstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --windowed ^
  --name SlammerWiz ^
  --hidden-import PyQt6 ^
  --hidden-import pynput.mouse._win32 ^
  --hidden-import pynput.keyboard._win32 ^
  --collect-submodules keyboard ^
  --collect-all rapidocr_onnxruntime ^
  --collect-all onnxruntime ^
  --add-data "data;data" ^
  --add-data "assets;assets" ^
  src\main.py

if exist dist\SlammerWiz.exe (
  copy /Y config.json dist\config.json >nul
  echo.
  echo [+] Built dist\SlammerWiz.exe
  echo     Ship dist\SlammerWiz.exe + dist\config.json together.
) else (
  echo [!] Build failed.
  exit /b 1
)

endlocal
