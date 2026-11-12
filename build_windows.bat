@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
echo =========================================================
echo FlowDesk - Windows x64 standalone build, Python 3.15.2
 echo =========================================================
where py >nul 2>nul
if errorlevel 1 (
  echo Python Launcher not found. Use the included GitHub Actions workflow
  echo to build remotely without installing Python on your computer.
  pause
  exit /b 1
)
py -3.15 -c "import sys; assert sys.version_info[:3] == (3,15,2), sys.version" >nul 2>nul
if errorlevel 1 (
  echo Requires Python exactly 3.15.2 to build locally.
  echo End users DO NOT need Python after the EXE is built.
  pause
  exit /b 1
)
py -3.15 -m venv .venv
if errorlevel 1 goto fail
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
if errorlevel 1 goto fail
python -m pip install -r requirements-build.txt
if errorlevel 1 goto fail
python -m unittest discover -s tests -v
if errorlevel 1 goto fail
python -m PyInstaller --clean --noconfirm --windowed --onedir --name FlowDesk --collect-all tzdata --collect-all websockets --add-data "flowdesk/data;flowdesk/data" main.py
if errorlevel 1 goto fail
 echo.
 echo SUCCESS: dist\FlowDesk\FlowDesk.exe
 echo Copy the ENTIRE dist\FlowDesk folder to other Windows PCs.
 pause
 exit /b 0
:fail
 echo BUILD FAILED - review errors above.
 pause
 exit /b 1

