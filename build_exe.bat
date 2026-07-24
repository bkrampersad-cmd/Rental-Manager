@echo off
REM Builds RentalManager.exe (and its support files) into dist\RentalManager\
REM Run this on a Windows machine with Python 3.9+ installed.
cd /d "%~dp0"

if not exist venv (
    echo Creating virtual environment...
    python -m venv venv
)
call venv\Scripts\activate.bat

echo Installing dependencies...
pip install -q -r requirements.txt
pip install -q -r requirements-server.txt
if errorlevel 1 (
    echo.
    echo Could not install requirements-server.txt ^(the PostgreSQL driver^).
    echo This usually means this machine's Python version is too new for the
    echo current psycopg2-binary release on PyPI - see the comments in
    echo requirements-server.txt for what to do about it.
    echo The build cannot continue: without this, the .exe you hand out
    echo would not support "Shared server for a team" mode.
    pause
    exit /b 1
)
pip install -q pyinstaller

echo Cleaning previous build...
rmdir /s /q build 2>nul
rmdir /s /q dist 2>nul

echo Building RentalManager.exe...
pyinstaller --noconfirm --name RentalManager ^
    --icon "assets\app_icon.ico" ^
    --add-data "templates;templates" ^
    --add-data "static;static" ^
    --add-data "LICENSE;." ^
    --add-data "migrations;migrations" ^
    --hidden-import "logging.config" ^
    launcher.py

echo.
echo Build complete: dist\RentalManager\RentalManager.exe
echo Next: run build_installer.bat to create a Setup.exe you can hand out.
pause
