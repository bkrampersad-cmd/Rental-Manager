@echo off
REM Builds RentalManager.exe (and its support files) into dist\RentalManager\
REM Run this on a Windows machine with Python 3.9+ installed.
cd /d "%~dp0"

if not exist venv (
    echo Creating virtual environment...
    python -m venv venv
)
call venv\Scripts\activate.bat

echo Checking for pip updates...
python -m pip install -q --upgrade pip

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

REM Optional bundled Tesseract OCR: if you installed Tesseract once and
REM copied its Program Files\Tesseract-OCR folder into this project as
REM tesseract-bin\ (see BUILD INSTRUCTIONS.txt), it gets folded into the
REM build automatically here so people you hand the installer to don't need
REM to install Tesseract separately. If tesseract-bin\ isn't present, this
REM is skipped and OCR falls back to a separate system-wide Tesseract
REM install on the machine running the app, exactly as before.
set TESSERACT_FLAG=
if exist "tesseract-bin\tesseract.exe" (
    echo Found tesseract-bin\ - bundling Tesseract OCR into this build.
    set TESSERACT_FLAG=--add-data "tesseract-bin;tesseract-bin"
) else (
    echo No tesseract-bin\ folder found - OCR will rely on a separate
    echo Tesseract install on the machine running the app ^(see BUILD
    echo INSTRUCTIONS.txt if you'd like to bundle it instead^).
)

echo Building RentalManager.exe...
pyinstaller --noconfirm --name RentalManager ^
    --icon "assets\app_icon.ico" ^
    --add-data "templates;templates" ^
    --add-data "static;static" ^
    --add-data "LICENSE;." ^
    --add-data "migrations;migrations" ^
    --add-data "assets;assets" ^
    --hidden-import "logging.config" ^
    --hidden-import "win32com.client" ^
    --hidden-import "win32timezone" ^
    --hidden-import "pythoncom" ^
    --hidden-import "pywintypes" ^
    --hidden-import "win32api" ^
    --hidden-import "win32event" ^
    --hidden-import "winerror" ^
    --hidden-import "pystray._win32" ^
    %TESSERACT_FLAG% ^
    launcher.py

echo.
echo Build complete: dist\RentalManager\RentalManager.exe
echo Next: run build_installer.bat to create a Setup.exe you can hand out.
pause
