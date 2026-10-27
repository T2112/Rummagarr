; Inno Setup script for Rummagarr.
; Build with scripts/build_windows.ps1, which passes /DMyAppVersion from VERSION.
;
; AppId stays the same as the Lidarr Soulseek installer so this setup upgrades
; that install in place. AppMutex stays LidarrSoulseekMutex so an open 1.9.4
; window is closed by the upgrade. UsePreviousAppDir (the default) keeps the
; existing program folder.

#ifndef MyAppVersion
#define MyAppVersion "1.11.0"
#endif

#define MyAppName "Rummagarr"
#define MyAppPublisher "T2112"
#define MyAppExeName "Rummagarr.exe"

[Setup]
AppId={{7C3E9A2B-5F14-4D8E-9A6C-2B1F0E4D8A77}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\Rummagarr
DefaultGroupName=Rummagarr
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=Rummagarr-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=rummagarr.ico
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
AppMutex=LidarrSoulseekMutex
CloseApplications=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
LicenseFile=..\LICENSE.txt
; Settings, state.sqlite3, and the log now live in {localappdata}\Rummagarr.
; The app copies them from {localappdata}\Lidarr Soulseek on first launch
; when the new folder has no config.toml yet.

[Tasks]
Name: startup; Description: "Start Rummagarr when I sign in to Windows"; Flags: unchecked
Name: firewall; Description: "Allow Rummagarr through Windows Firewall so Lidarr on another PC can connect"; Flags: unchecked

[InstallDelete]
Type: files; Name: "{app}\LidarrSoulseek.exe"
Type: files; Name: "{autoprograms}\Lidarr Soulseek.lnk"
Type: files; Name: "{userstartup}\Lidarr Soulseek.lnk"
Type: files; Name: "{userdesktop}\Lidarr Soulseek.lnk"

[Files]
Source: "..\dist\Rummagarr\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\open-firewall.ps1"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Rummagarr"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\Rummagarr"; Filename: "{app}\{#MyAppExeName}"
Name: "{userstartup}\Rummagarr"; Filename: "{app}\{#MyAppExeName}"; Tasks: startup

[Run]
; Interactive setup still offers the launch checkbox. A silent upgrade from the
; app (Update now) must start the new version on its own.
Filename: "{app}\{#MyAppExeName}"; Description: "Launch Rummagarr"; Flags: nowait postinstall skipifsilent
Filename: "{app}\{#MyAppExeName}"; Flags: nowait skipifnotsilent

[Code]
procedure CurStepChanged(CurStep: TSetupStep);
var
  ErrorCode: Integer;
  Script: String;
begin
  if CurStep <> ssPostInstall then
    Exit;
  if not WizardIsTaskSelected('firewall') then
    Exit;
  if MsgBox('Windows will ask for approval to allow Rummagarr through the firewall so Lidarr on another PC can connect.', mbConfirmation, MB_YESNO) <> IDYES then
    Exit;
  Script := ExpandConstant('{app}\open-firewall.ps1');
  if not ShellExec('runas', 'powershell.exe', '-NoProfile -ExecutionPolicy Bypass -File "' + Script + '" -BridgePort 8788', '', SW_SHOWNORMAL, ewWaitUntilTerminated, ErrorCode) then
    MsgBox('The firewall prompt did not open. Use Settings, Bridge, Allow in Windows Firewall after Rummagarr starts.', mbInformation, MB_OK);
end;
