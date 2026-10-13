; Inno Setup script for Seekarr.
; Build with scripts/build_windows.ps1, which passes /DMyAppVersion from VERSION.
;
; AppId stays the same as the Lidarr Soulseek installer so this setup upgrades
; that install in place. AppMutex stays LidarrSoulseekMutex so an open 1.9.4
; window is closed by the upgrade. UsePreviousAppDir (the default) keeps the
; existing program folder.

#ifndef MyAppVersion
#define MyAppVersion "1.10.0"
#endif

#define MyAppName "Seekarr"
#define MyAppPublisher "T2112"
#define MyAppExeName "Seekarr.exe"

[Setup]
AppId={{7C3E9A2B-5F14-4D8E-9A6C-2B1F0E4D8A77}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\Seekarr
DefaultGroupName=Seekarr
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=Seekarr-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
AppMutex=LidarrSoulseekMutex
CloseApplications=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
LicenseFile=..\LICENSE.txt
; Settings, state.sqlite3, and the log now live in {localappdata}\Seekarr.
; The app copies them from {localappdata}\Lidarr Soulseek on first launch
; when the new folder has no config.toml yet.

[Tasks]
Name: startup; Description: "Start Seekarr when I sign in to Windows"; Flags: unchecked

[InstallDelete]
Type: files; Name: "{app}\LidarrSoulseek.exe"
Type: files; Name: "{autoprograms}\Lidarr Soulseek.lnk"
Type: files; Name: "{userstartup}\Lidarr Soulseek.lnk"
Type: files; Name: "{userdesktop}\Lidarr Soulseek.lnk"

[Files]
Source: "..\dist\Seekarr\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Seekarr"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\Seekarr"; Filename: "{app}\{#MyAppExeName}"
Name: "{userstartup}\Seekarr"; Filename: "{app}\{#MyAppExeName}"; Tasks: startup

[Run]
; Interactive setup still offers the launch checkbox. A silent upgrade from the
; app (Update now) must start the new version on its own.
Filename: "{app}\{#MyAppExeName}"; Description: "Launch Seekarr"; Flags: nowait postinstall skipifsilent
Filename: "{app}\{#MyAppExeName}"; Flags: nowait skipifnotsilent
