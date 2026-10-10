; Inno Setup script for Lidarr Soulseek.
; Build with scripts/build_windows.ps1, which passes /DMyAppVersion from VERSION.

#ifndef MyAppVersion
#define MyAppVersion "1.3.0"
#endif

#define MyAppName "Lidarr Soulseek"
#define MyAppPublisher "T2112"
#define MyAppExeName "LidarrSoulseek.exe"

[Setup]
AppId={{7C3E9A2B-5F14-4D8E-9A6C-2B1F0E4D8A77}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\Lidarr Soulseek
DefaultGroupName=Lidarr Soulseek
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=LidarrSoulseek-Setup-{#MyAppVersion}
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

[Tasks]
Name: startup; Description: "Start Lidarr Soulseek when I sign in to Windows"; Flags: unchecked

[Files]
Source: "..\dist\LidarrSoulseek\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Lidarr Soulseek"; Filename: "{app}\{#MyAppExeName}"
Name: "{userstartup}\Lidarr Soulseek"; Filename: "{app}\{#MyAppExeName}"; Tasks: startup

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch Lidarr Soulseek"; Flags: nowait postinstall skipifsilent
