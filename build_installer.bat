@echo off
REM Compiles installer.iss into installer_output\RentalManagerSetup.exe
REM Requires Inno Setup: https://jrsoftware.org/isdl.php (free)
cd /d "%~dp0"

if not exist "dist\RentalManager\RentalManager.exe" (
    echo dist\RentalManager\RentalManager.exe not found.
    echo Run build_exe.bat first to build the app, then re-run this script.
    pause
    exit /b 1
)

REM Inno Setup 7 (64-bit edition is the recommended default install) takes
REM priority if present, then Inno Setup 7 32-bit, then Inno Setup 6 — all
REM produce a working RentalManagerSetup.exe from the same installer.iss.
set ISCC="C:\Program Files\Inno Setup 7\ISCC.exe"
if not exist %ISCC% set ISCC="C:\Program Files (x86)\Inno Setup 7\ISCC.exe"
if not exist %ISCC% set ISCC="C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if not exist %ISCC% set ISCC="C:\Program Files\Inno Setup 6\ISCC.exe"

if not exist %ISCC% (
    echo Inno Setup was not found in the usual install locations.
    echo Install it from https://jrsoftware.org/isdl.php, then re-run this script.
    echo ^(Or open installer.iss directly in the Inno Setup Compiler and click Build.^)
    pause
    exit /b 1
)

%ISCC% installer.iss

echo.
echo Installer built: installer_output\RentalManagerSetup.exe
echo Hand that single file to anyone — running it installs the app and adds
echo a desktop shortcut, no admin rights or Python required on their end.
pause
