; DOT Download Manager installer (Inno Setup 6). Built by build.ps1:
;   ISCC.exe /DAppVersion=0.4.0 installer\DOT.iss
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "DOT Download Manager"

[Setup]
AppId={{4070C5F9-106D-4D78-B862-7E2703D29E67}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=DOT
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}
; Per-user by default (no admin prompt); the wizard also offers "all users" (Program Files).
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\DOT
DefaultGroupName=DOT
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist
OutputBaseFilename=DOT-Setup-{#AppVersion}
SetupIconFile=..\assets\dot.ico
UninstallDisplayIcon={app}\DOT.exe
UninstallDisplayName={#AppName}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
; DOT is closed by [Code] below: a tray app ignores the usual close requests.
CloseApplications=no
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "startup"; Description: "Start DOT in the tray when I sign in to Windows (recommended: browser downloads always reach DOT)"; GroupDescription: "Startup:"

[InstallDelete]
; Upgrades replace these folders as a whole, so no stale files from an older version remain.
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\tools"
Type: filesandordirs; Name: "{app}\browser-extension"

[Files]
Source: "..\build\app\DOT\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\build\tools\bin\*"; DestDir: "{app}\tools"; Flags: ignoreversion
Source: "..\build\tools\licenses\*"; DestDir: "{app}\licenses"; Flags: ignoreversion
Source: "..\browser-extension\*"; DestDir: "{app}\browser-extension"; Flags: ignoreversion recursesubdirs
Source: "..\README.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\DOT.exe"; Comment: "Download manager with browser integration"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\DOT.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "DOT Download Manager"; ValueData: """{app}\DOT.exe"" --minimized"; Tasks: startup; Flags: uninsdeletevalue

[Run]
Filename: "{app}\DOT.exe"; Parameters: "--first-run"; Description: "Start DOT and connect your browser"; Flags: nowait postinstall skipifsilent

[Code]
const
  WM_DOT_EXIT = $8003;  { dotdm/tray.py WM_EXIT: the running DOT saves its work and exits. }
  RUN_KEY = 'Software\Microsoft\Windows\CurrentVersion\Run';
  RUN_VALUE = 'DOT Download Manager';

function DotRunning(): Boolean;
begin
  { The tray window (0.4 and later) or the main window title of any version. }
  Result := (FindWindowByClassName('DOT.Tray') <> 0) or
            (FindWindowByWindowName('DOT ' + #183 + ' Download Manager') <> 0);
end;

function CloseRunningDot(): Boolean;
var
  Tray: HWND;
  Waited: Integer;
begin
  Tray := FindWindowByClassName('DOT.Tray');
  if Tray <> 0 then
  begin
    PostMessage(Tray, WM_DOT_EXIT, 0, 0);
    Waited := 0;
    while DotRunning() and (Waited < 20000) do
    begin
      Sleep(250);
      Waited := Waited + 250;
    end;
  end;
  Result := not DotRunning();
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if not CloseRunningDot() then
    Result := 'DOT is still open. Close it (right-click its tray icon and choose Exit DOT, or close its window), then click Back and Next.';
end;

function InitializeUninstall(): Boolean;
begin
  Result := CloseRunningDot();
  if not Result then
    MsgBox('DOT is still open. Close it, then run the uninstaller again.', mbError, MB_OK);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Value, Data: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    { Also remove a Start-with-Windows entry that was turned on later in DOT's Settings. }
    if RegQueryStringValue(HKCU, RUN_KEY, RUN_VALUE, Value) then
      if Pos(Lowercase(ExpandConstant('{app}')), Lowercase(Value)) > 0 then
        RegDeleteValue(HKCU, RUN_KEY, RUN_VALUE);
  end;
  if CurUninstallStep = usPostUninstall then
  begin
    Data := ExpandConstant('{localappdata}\DOT');
    if DirExists(Data) then
      if SuppressibleMsgBox('Also delete your DOT download list and settings?' + #13#10 +
           'Files you downloaded are kept.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES then
        DelTree(Data, True, True, True);
  end;
end;
