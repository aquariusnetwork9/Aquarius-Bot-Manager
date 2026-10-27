; Inno Setup script for Aquarius Bot Manager.
;
; Produces ONE installer exe: service registration (NSSM, bundled -- no
; separate download/winget step during setup) and a Start Menu shortcut for
; the native control panel (the only shortcut this installer creates, per
; the locked "single shortcut, no separate web-dashboard entry" decision).
; This is the thing a user actually double-clicks -- replaces every prior
; "run these exes by hand in the right order" / hand-edited-shortcut path,
; which is exactly what broke last time (a malformed quote in a manually
; edited Shortcut Target field).
;
; No tray icon: AquariusTray.exe was dropped from the distributed product
; (got Defender-quarantined as a false positive; "nice to have," not worth
; the AV friction -- see docs/windows-exe-build.md). tray_win.py still
; exists in the repo for anyone running from source.
;
; Build prerequisites:
;  1. Run build_windows_exes.ps1 first so dist\ contains AquariusManager.exe,
;     AquariusControlPanel\ (onedir), and pyrt\ (the bundled helper-process
;     Python runtime) -- this script only packages what's already in dist\,
;     it doesn't build the exes itself.
;  2. Copy a real nssm.exe (win64) to this same directory, next to
;     installer.iss -- not checked into the repo (it's not ours to commit).
;     Get it via: winget install NSSM.NSSM
;     then copy from %LOCALAPPDATA%\Microsoft\WinGet\Packages\NSSM.NSSM_*\
;     nssm-*\win64\nssm.exe

#define MyAppName "Aquarius Bot Manager"
#define MyAppVersion "1.0"
#define MyAppPublisher "aquariusnetwork9"
#define ServiceName "AquariusBotManager"

[Setup]
AppId={{B7C1A5F0-7A3E-4B9A-9D2C-6E1F8A4C2D90}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=installer_output
OutputBaseFilename=AquariusBotManagerSetup
Compression=lzma
SolidCompression=yes
WizardStyle=classic
; Service install/uninstall and writing under Program Files both need admin.
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\AquariusManager.exe

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Types]
Name: "full"; Description: "Full installation (recommended)"
Name: "custom"; Description: "Custom"; Flags: iscustom

[Components]
Name: "app"; Description: "Aquarius Bot Manager"; Types: full custom; Flags: fixed

[Tasks]
Name: "bootwaitforlogin"; Description: "Start the manager after the first user logs in (recommended)"; GroupDescription: "When should the manager start?"; Flags: exclusive
Name: "bootalways"; Description: "Start the manager at boot, before any user logs in"; GroupDescription: "When should the manager start?"; Flags: exclusive unchecked

[Files]
Source: "dist\AquariusManager.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\AquariusControlPanel\*"; DestDir: "{app}\AquariusControlPanel"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "dist\pyrt\*"; DestDir: "{app}\pyrt"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "nssm.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; Single Start Menu shortcut, per the locked decision -- opens the native
; control panel directly, never a browser/web-dashboard entry.
Name: "{group}\{#MyAppName}"; Filename: "{app}\AquariusControlPanel\AquariusControlPanel.exe"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"

[Run]
; instances.json is created self-initializing by manager.py itself on first
; run now (see manager.py's load_config() IS_WINDOWS branch) -- nothing to
; seed here.
Filename: "{app}\nssm.exe"; Parameters: "install {#ServiceName} ""{app}\AquariusManager.exe"" ""--config \""{app}\instances.json\"" serve --host 127.0.0.1 --port 8765"""; Flags: runhidden; StatusMsg: "Registering the manager service..."
Filename: "{app}\nssm.exe"; Parameters: "set {#ServiceName} AppDirectory ""{app}"""; Flags: runhidden
Filename: "{app}\nssm.exe"; Parameters: "set {#ServiceName} AppStdout ""{app}\service-stdout.log"""; Flags: runhidden
Filename: "{app}\nssm.exe"; Parameters: "set {#ServiceName} AppStderr ""{app}\service-stderr.log"""; Flags: runhidden
Filename: "{app}\nssm.exe"; Parameters: "set {#ServiceName} AppRestartDelay 2000"; Flags: runhidden
Filename: "{app}\nssm.exe"; Parameters: "set {#ServiceName} AppThrottle 1500"; Flags: runhidden
; Boot mode: exactly one of these two tasks is checked (Tasks section above
; is "exclusive"), selecting which NSSM Start value + optional login-trigger
; scheduled task gets applied -- mirrors install_windows_service.ps1's own
; -BootMode Always/WaitForLogin branch, just expressed as installer Tasks
; instead of a script parameter since there's no terminal step anymore.
Filename: "{app}\nssm.exe"; Parameters: "set {#ServiceName} Start SERVICE_AUTO_START"; Flags: runhidden; Tasks: bootalways
Filename: "{app}\nssm.exe"; Parameters: "set {#ServiceName} Start SERVICE_DEMAND_START"; Flags: runhidden; Tasks: bootwaitforlogin
Filename: "{sys}\schtasks.exe"; Parameters: "/Create /TN ""{#ServiceName}-LoginStart"" /TR ""sc.exe start {#ServiceName}"" /SC ONLOGON /RL HIGHEST /F"; Flags: runhidden; Tasks: bootwaitforlogin
Filename: "{sys}\net.exe"; Parameters: "start {#ServiceName}"; Flags: runhidden; StatusMsg: "Starting the manager service..."
Filename: "{app}\AquariusControlPanel\AquariusControlPanel.exe"; Description: "Open Aquarius Bot Manager"; Flags: postinstall nowait skipifsilent shellexec

[UninstallRun]
; Inno Setup's auto-generated uninstall removes files/shortcuts/registry
; entries IT created -- it has no idea a Windows service or scheduled task
; exists unless told explicitly, so both are stopped/unregistered here
; before the files themselves (including nssm.exe) are deleted.
Filename: "{sys}\net.exe"; Parameters: "stop {#ServiceName}"; Flags: runhidden; RunOnceId: "StopService"
Filename: "{app}\nssm.exe"; Parameters: "remove {#ServiceName} confirm"; Flags: runhidden; RunOnceId: "RemoveService"
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /TN ""{#ServiceName}-LoginStart"" /F"; Flags: runhidden; RunOnceId: "RemoveLoginTask"
