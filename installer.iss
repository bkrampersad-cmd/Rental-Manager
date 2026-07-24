; Inno Setup script for Rental Property Manager.
; Compile with Inno Setup (https://jrsoftware.org/isinfo.php) — either open
; this file in the Inno Setup Compiler GUI and click Build, or run
; build_installer.bat, which invokes ISCC.exe for you.

#define MyAppName "Rental Property Manager"
#define MyAppVersion "2.0"
#define MyAppExeName "RentalManager.exe"

[Setup]
AppId={{6C6E4C2E-6E7B-4B0C-9A1D-8E7F2E9A9B10}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
; Per-user install to a location the user already owns — no admin rights
; required, which matters if you're handing this .exe to someone who isn't
; a local administrator on their machine.
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
OutputDir=installer_output
OutputBaseFilename=RentalManagerSetup
Compression=lzma
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
; Branded house-mark icon: shows on Setup.exe itself, the installer window,
; and Add/Remove Programs (via UninstallDisplayIcon above, which points at
; the .exe — already carries this same icon since build_exe.bat embeds it).
SetupIconFile=assets\app_icon.ico
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
Source: "dist\RentalManager\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
; Password-recovery scripts (see the User Manual, Settings section) — plain
; .py files, not part of the PyInstaller bundle, but worth having on hand
; without needing the source repo. reset_access_password.py only needs a
; plain Python 3 install; reset_admin_password.py additionally needs
; psycopg2 + werkzeug (see requirements-server.txt) since it's a Network
; mode / Postgres tool.
Source: "reset_access_password.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "reset_admin_password.py"; DestDir: "{app}"; Flags: ignoreversion
; License and documentation — bundled so they're on hand without needing the
; source repo, and so the Start Menu shortcuts below have something to open.
Source: "LICENSE.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "docs\RentalManagerInstallGuide.pdf"; DestDir: "{app}"; Flags: ignoreversion
Source: "docs\RentalManagerUserManual.pdf"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\User Manual"; Filename: "{app}\RentalManagerUserManual.pdf"
Name: "{group}\Install Guide"; Filename: "{app}\RentalManagerInstallGuide.pdf"
; LICENSE.md has no reliable default file association on a fresh Windows
; install, so it's opened explicitly with Notepad rather than left to
; whatever (if anything) Windows would otherwise prompt to pick.
Name: "{group}\License"; Filename: "{win}\notepad.exe"; Parameters: """{app}\LICENSE.md"""
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName} now"; Flags: nowait postinstall skipifsilent
Filename: "{app}\RentalManagerUserManual.pdf"; Description: "Open the User Manual"; Flags: nowait postinstall skipifsilent unchecked shellexec
