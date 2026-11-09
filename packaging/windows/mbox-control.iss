; Mbox Control for Windows — Inno Setup script. Built by .github/workflows/windows-app.yml.
#ifndef AppVersion
  #define AppVersion "0.0"
#endif

[Setup]
AppId={{E41A046E-5093-4EE6-AA60-F99C5D795D54}
AppName=Mbox Control
AppVersion={#AppVersion}
AppVerName=Mbox Control {#AppVersion}
AppPublisher=Fancy Butler
AppPublisherURL=https://github.com/fancy-butler/mbox-control
AppSupportURL=https://github.com/fancy-butler/mbox-control/issues
DefaultDirName={autopf}\Mbox Control
DefaultGroupName=Mbox Control
DisableProgramGroupPage=yes
LicenseFile=..\..\LICENSE
OutputDir=..\..\dist
OutputBaseFilename=MboxControl-Setup
SetupIconFile=mbox-control.ico
UninstallDisplayIcon={app}\MboxControl.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "autostart"; Description: "Start Mbox Control when I sign in (it waits in the tray)"; GroupDescription: "Options:"
Name: "desktopicon"; Description: "Desktop shortcut"; GroupDescription: "Options:"; Flags: unchecked

[Files]
Source: "..\..\dist\MboxControl\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\Mbox Control"; Filename: "{app}\MboxControl.exe"
Name: "{autodesktop}\Mbox Control"; Filename: "{app}\MboxControl.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Mbox Control"; \
  ValueData: """{app}\MboxControl.exe"" --tray"; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\MboxControl.exe"; Description: "Start Mbox Control now"; Flags: nowait postinstall skipifsilent

[Code]
function DriverInstalled(): Boolean;
begin
  Result := FileExists(ExpandConstant('{commonpf32}\Avid\Mbox Pro\DriverInterface.exe')) or
            FileExists(ExpandConstant('{commonpf64}\Avid\Mbox Pro\DriverInterface.exe'));
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  if not DriverInstalled() then
    Result := MsgBox('Avid''s Mbox Pro driver (1.1.10) doesn''t seem to be installed.' + #13#10 +
                     'Mbox Control talks to the Mbox through it. Install anyway?',
                     mbQuestion, MB_YESNO) = IDYES;
end;
