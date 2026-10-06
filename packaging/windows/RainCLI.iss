; RainCLI Windows app installer (protocol §15.5, amended by §15.8 H5, H6, H7, M6, M10, L2 and §15.9).
; Built by build.py:  ISCC /DAppVersion=X.Y.Z /DDistDir=<PyInstaller dist> RainCLI.iss
;
; A full install (no /UPDATE) goes to %LOCALAPPDATA%\Programs\RainCLI (no admin rights) and:
;   - records any existing RainCLI Run value, Startup-folder entry or Scheduled Task that starts
;     raincli in installer-record.log BEFORE writing anything (H7), and never overwrites an
;     unrecorded value: it writes the Run value only when none exists or it already names our stub;
;     otherwise the app's migration takes it over (§15.6, H6, §15.9);
;   - stops a running app with `RainCLI.exe --quit` and continues only if that exits 0 (§15.9);
;   - installs versions\X.Y.Z, and the stable stub RainCLI.exe and the PATH shim bin\raincli.exe,
;     both onedir with their own _internal (§15.9);
;   - writes install.json {current, previous, probation, stub: 2, install_stamp} atomically (M6, §16.15);
;   - puts <root>\bin first on the user PATH, adds the Start menu entries and starts the tray,
;     whose first run signs in or migrates an older install (§15.6, H6);
;   - checks for the WebView2 Runtime the window needs, and offers Microsoft's download page (§16.10).
; /UPDATE /DIR=<root>\versions\X.Y.Z (the app's updater) installs only that version folder: no
; uninstaller or uninstall key, no stub, shim, Run value, PATH, shortcuts, install.json or launch (H5).
; The uninstaller stops the app (it refuses while the app keeps running), removes the raincli-marked
; hooks, then signs out if asked (/SIGNOUT=yes, or Yes to its question; default no), and removes what
; M10 lists. What it removes and keeps is listed in docs/windows-client.md (Uninstall).

#ifndef AppVersion
  #error Define AppVersion (X.Y.Z), as build.py does
#endif
#ifndef DistDir
  #error Define DistDir (the PyInstaller dist directory), as build.py does
#endif

[Setup]
AppId={{6F1E3B52-8C4D-4A7B-9E21-5D0C7A9B3F14}
AppName=RainCLI
AppVersion={#AppVersion}
AppVerName=RainCLI {#AppVersion}
AppPublisher=RainCLI
AppPublisherURL=https://raincli.com
DefaultDirName={localappdata}\Programs\RainCLI
DisableDirPage=yes
DisableProgramGroupPage=yes
UsePreviousAppDir=no
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputBaseFilename=RainCLI-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes
; H5: update mode leaves no uninstaller and no uninstall key, and never closes or restarts anything.
Uninstallable=not IsUpdate
CreateUninstallRegKey=not IsUpdate
CloseApplications=no
RestartApplications=no
ChangesEnvironment=yes
UninstallDisplayName=RainCLI
SetupIconFile=app.ico
UninstallDisplayIcon={app}\RainCLI.exe

[Files]
Source: "{#DistDir}\RainCLI-{#AppVersion}\*"; DestDir: "{code:VersionDir}"; Flags: ignoreversion recursesubdirs createallsubdirs
; The stub and the shim: full installs only, after a successful --quit (PrepareToInstall).
Source: "{#DistDir}\stub\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Check: not IsUpdate
Source: "{#DistDir}\bin\*"; DestDir: "{app}\bin"; Flags: ignoreversion recursesubdirs createallsubdirs; Check: not IsUpdate

[Icons]
; No arguments: the v0.5 stub starts the app if needed and shows its window (§16.15).
Name: "{userprograms}\RainCLI\RainCLI"; Filename: "{app}\RainCLI.exe"; Check: not IsUpdate
Name: "{userprograms}\RainCLI\Uninstall RainCLI"; Filename: "{uninstallexe}"; Check: not IsUpdate

[Run]
Filename: "{app}\RainCLI.exe"; Parameters: "--background"; Flags: nowait; Check: not IsUpdate

[UninstallDelete]
; M10, removed: every version, the stub, the shim, install.json, and the app's transient state.
Type: filesandordirs; Name: "{app}\versions"
Type: filesandordirs; Name: "{app}\_internal"
Type: files; Name: "{app}\RainCLI.exe"
Type: filesandordirs; Name: "{app}\bin"
Type: files; Name: "{app}\install.json"
Type: files; Name: "{app}\install.json.new"
Type: files; Name: "{app}\heartbeat.json"
Type: files; Name: "{app}\update-state.json"
Type: filesandordirs; Name: "{app}\update-lock"
Type: filesandordirs; Name: "{app}\app-lock"
Type: filesandordirs; Name: "{app}\state\downloads"
Type: dirifempty; Name: "{userprograms}\RainCLI"

[Code]
const
  RunKey = 'Software\Microsoft\Windows\CurrentVersion\Run';
  RunName = 'RainCLI';
  EnvKey = 'Environment';
  MOVEFILE_REPLACE_EXISTING = 1;
  MOVEFILE_WRITE_THROUGH = 8;

var
  SignOutOnUninstall: Boolean;

function MoveFileEx(ExistingName, NewName: String; Flags: DWORD): Boolean;
  external 'MoveFileExW@kernel32.dll stdcall';
function GetTickCount: DWORD;
  external 'GetTickCount@kernel32.dll stdcall';
function GetCurrentProcessId: DWORD;
  external 'GetCurrentProcessId@kernel32.dll stdcall';

function IsUpdate: Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), '/UPDATE') = 0 then
      Result := True;
end;

{ The value of a /NAME=value parameter, lowercased, or ''. }
function ParamValue(Name: String): String;
var
  I: Integer;
begin
  Result := '';
  for I := 1 to ParamCount do
    if CompareText(Copy(ParamStr(I), 1, Length(Name) + 2), '/' + Name + '=') = 0 then
      Result := Lowercase(Copy(ParamStr(I), Length(Name) + 3, Length(ParamStr(I))));
end;

function VersionDir(Param: String): String;
begin
  if IsUpdate then
    Result := ExpandConstant('{app}')
  else
    Result := ExpandConstant('{app}\versions\{#AppVersion}');
end;

function RootDir: String;
begin
  if IsUpdate then
    Result := ExtractFileDir(ExtractFileDir(ExpandConstant('{app}')))
  else
    Result := ExpandConstant('{app}');
end;

function StubCommand: String;
begin
  Result := '"' + RootDir + '\RainCLI.exe" --background';
end;

{ -- small helpers ------------------------------------------------------------------- }

function Contains(Haystack, Needle: String): Boolean;
begin
  Result := Pos(Lowercase(Needle), Lowercase(Haystack)) > 0;
end;

function StripNuls(S: String): String;
var
  I: Integer;
begin
  Result := '';
  for I := 1 to Length(S) do
    if S[I] <> #0 then
      Result := Result + S[I];
end;

function IsVersion(S: String): Boolean;
var
  I, Dots: Integer;
begin
  Result := (Length(S) >= 5) and (Length(S) <= 14);
  Dots := 0;
  for I := 1 to Length(S) do
    if S[I] = '.' then
      Dots := Dots + 1
    else if (S[I] < '0') or (S[I] > '9') then
      Result := False;
  Result := Result and (Dots = 2);
end;

{ The string value of "Key" in a flat JSON object written by the app or this installer, with the
  escapes \\, \/ and \" undone; '' when absent, not a string, or using any other escape. }
function JsonText(Json, Key: String): String;
var
  P: Integer;
  C: String;
begin
  Result := '';
  P := Pos('"' + Key + '"', Json);
  if P = 0 then
    Exit;
  Json := Copy(Json, P + Length(Key) + 2, Length(Json));
  P := Pos(':', Json);
  if P = 0 then
    Exit;
  Json := Trim(Copy(Json, P + 1, Length(Json)));
  if (Length(Json) = 0) or (Json[1] <> '"') then
    Exit;
  P := 2;
  while P <= Length(Json) do
  begin
    C := Json[P];
    if C = '"' then
      Exit;
    if C = '\' then
    begin
      P := P + 1;
      if P > Length(Json) then
        Break;
      C := Json[P];
      if (C <> '\') and (C <> '/') and (C <> '"') then
        Break;
    end;
    Result := Result + C;
    P := P + 1;
  end;
  Result := '';  { unterminated or an unsupported escape }
end;

function JsonVersion(Json, Key: String): String;
begin
  Result := JsonText(Json, Key);
  if not IsVersion(Result) then
    Result := '';
end;

function ReadRootFile(Name: String): String;
var
  Raw: AnsiString;
begin
  Result := '';
  if LoadStringFromFile(RootDir + '\' + Name, Raw) then
    Result := String(Raw);
end;

{ -- H7: record before overwrite --------------------------------------------------------- }

procedure AppendRecord(Lines: TArrayOfString);
var
  Path: String;
begin
  Path := RootDir + '\installer-record.log';
  if not ForceDirectories(RootDir) or not SaveStringsToUTF8File(Path, Lines, True) then
    RaiseException('could not write ' + Path);
end;

procedure AddLine(var Lines: TArrayOfString; Line: String);
begin
  SetArrayLength(Lines, GetArrayLength(Lines) + 1);
  Lines[GetArrayLength(Lines) - 1] := Line;
end;

procedure RecordStartupEntries(var Lines: TArrayOfString);
var
  Find: TFindRec;
  Dir: String;
  Raw: AnsiString;
begin
  Dir := ExpandConstant('{userstartup}');
  if FindFirst(Dir + '\*', Find) then
  try
    repeat
      if (Find.Attributes and FILE_ATTRIBUTE_DIRECTORY) = 0 then
        if Contains(Find.Name, 'raincli') or
           (LoadStringFromFile(Dir + '\' + Find.Name, Raw) and Contains(StripNuls(String(Raw)), 'raincli')) then
          AddLine(Lines, 'startup-folder entry: ' + Dir + '\' + Find.Name);
    until not FindNext(Find);
  finally
    FindClose(Find);
  end;
end;

{ Scheduled Tasks whose action mentions raincli, through Get-ScheduledTask: locale-independent (B9). }
procedure RecordScheduledTasks(var Lines: TArrayOfString);
var
  Output: TArrayOfString;
  Script, Listing: String;
  I, Code: Integer;
begin
  Script := ExpandConstant('{tmp}\raincli-tasks.ps1');
  Listing := ExpandConstant('{tmp}\raincli-tasks.txt');
  if not SaveStringToFile(Script,
      '$ErrorActionPreference = "Stop"' + #13#10 +
      '$found = foreach ($t in Get-ScheduledTask) { foreach ($a in $t.Actions) {' + #13#10 +
      '  $line = "$($a.Execute) $($a.Arguments)"' + #13#10 +
      '  if ($line -match "raincli") { "scheduled task: $($t.TaskPath)$($t.TaskName) runs $line" } } }' + #13#10 +
      'Set-Content -LiteralPath $args[0] -Value (@("ok") + @($found)) -Encoding UTF8' + #13#10, False) or
     not Exec('powershell.exe', '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + Script + '" "' +
              Listing + '"', '', SW_HIDE, ewWaitUntilTerminated, Code) or (Code <> 0) or
     not LoadStringsFromFile(Listing, Output) or (GetArrayLength(Output) = 0) then
  begin
    AddLine(Lines, 'scheduled tasks: could not be listed');
    Exit;
  end;
  for I := 1 to GetArrayLength(Output) - 1 do
    AddLine(Lines, Output[I]);
end;

procedure RecordExistingStartup;
var
  Lines: TArrayOfString;
  Existing: String;
begin
  AddLine(Lines, '== RainCLI {#AppVersion} installer, ' + GetDateTimeString('yyyy-mm-dd hh:nn:ss', '-', ':'));
  if RegQueryStringValue(HKCU, RunKey, RunName, Existing) then
    AddLine(Lines, 'run value HKCU\' + RunKey + '\' + RunName + ' = ' + Existing)
  else
    AddLine(Lines, 'run value HKCU\' + RunKey + '\' + RunName + ': none');
  RecordStartupEntries(Lines);
  RecordScheduledTasks(Lines);
  AppendRecord(Lines);
end;

procedure WriteRunValue;
var
  Existing: String;
  Lines: TArrayOfString;
begin
  if RegQueryStringValue(HKCU, RunKey, RunName, Existing) and (CompareText(Existing, StubCommand) <> 0) then
  begin
    AddLine(Lines, 'run value left for the app''s migration to take over: ' + Existing);
    AppendRecord(Lines);
    Exit;
  end;
  if not RegWriteStringValue(HKCU, RunKey, RunName, StubCommand) then
    RaiseException('could not write the Run value');
end;

{ -- M6: install.json ------------------------------------------------------------------- }

function InstallStamp: String;
begin
  { §16.15: new on every full install; the client rotates its app install token when it changes. }
  Result := GetDateTimeString('yyyymmdd"T"hhnnss', #0, #0) + '-'
            + Copy(GetSHA256OfString(GetDateTimeString('yyyymmddhhnnsszzz', #0, #0) + '|'
                   + IntToStr(GetTickCount) + '|' + IntToStr(GetCurrentProcessId) + '|'
                   + ExpandConstant('{app}')), 1, 16);
end;

procedure WriteInstallJson;
var
  Old, Current, Previous, Json, Path, Extra: String;
begin
  Old := ReadRootFile('install.json');
  Current := JsonVersion(Old, 'current');
  Previous := JsonVersion(Old, 'previous');
  if (Current <> '') and (Current <> '{#AppVersion}') then
    Previous := Current;
  if Previous = '{#AppVersion}' then
    Previous := '';
  { §16.15: "stub": 2 marks the v0.5 stub, which opens the window when run without arguments. }
  Extra := ', "stub": 2, "install_stamp": "' + InstallStamp + '"}';
  if Previous = '' then
    Json := '{"current": "{#AppVersion}", "previous": null, "probation": null' + Extra
  else
    Json := '{"current": "{#AppVersion}", "previous": "' + Previous + '", "probation": null' + Extra;
  Path := RootDir + '\install.json';
  if not SaveStringToFile(Path + '.new', Json + #13#10, False) or
     not MoveFileEx(Path + '.new', Path, MOVEFILE_REPLACE_EXISTING or MOVEFILE_WRITE_THROUGH) then
    RaiseException('could not write ' + Path);
  Log('install.json: ' + Json);
end;

{ -- L2 and H6: the shim first on the user PATH ----------------------------------------- }

function WithoutEntry(PathValue, Entry: String): String;
var
  Part: String;
  P: Integer;
begin
  Result := '';
  PathValue := PathValue + ';';
  while PathValue <> '' do
  begin
    P := Pos(';', PathValue);
    Part := Copy(PathValue, 1, P - 1);
    PathValue := Copy(PathValue, P + 1, Length(PathValue));
    if (Trim(Part) <> '') and (CompareText(RemoveBackslashUnlessRoot(Trim(Part)), Entry) <> 0) then
    begin
      if Result <> '' then
        Result := Result + ';';
      Result := Result + Part;
    end;
  end;
end;

procedure PutBinFirstOnPath;
var
  Value, Bin: String;
begin
  Bin := RootDir + '\bin';
  if not RegQueryStringValue(HKCU, EnvKey, 'Path', Value) then
    Value := '';
  Value := WithoutEntry(Value, Bin);
  if Value = '' then
    Value := Bin
  else
    Value := Bin + ';' + Value;
  if not RegWriteExpandStringValue(HKCU, EnvKey, 'Path', Value) then
    RaiseException('could not update the user PATH');
end;

procedure RemoveBinFromPath;
var
  Value: String;
begin
  if RegQueryStringValue(HKCU, EnvKey, 'Path', Value) then
    RegWriteExpandStringValue(HKCU, EnvKey, 'Path', WithoutEntry(Value, ExpandConstant('{app}') + '\bin'));
end;

{ -- stopping the app (§15.9) ------------------------------------------------------------- }

{ True when no app is installed here, or `RainCLI.exe --quit` stopped it (exit code 0). }
function StopRunningApp: Boolean;
var
  Code: Integer;
begin
  Result := True;
  if FileExists(RootDir + '\RainCLI.exe') then
    Result := Exec(RootDir + '\RainCLI.exe', '--quit', '', SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0);
end;

{ Why --quit failed, for the refusal message (review 3 N2): the executables it waited on, one per
  line of <root>\app-lock\quit-blockers.txt, at most ten; or the tray hint when it wrote none. }
function QuitRefusal(Retry: String): String;
var
  Lines: TArrayOfString;
  List: String;
  I, Shown: Integer;
begin
  List := '';
  Shown := 0;
  if LoadStringsFromFile(RootDir + '\app-lock\quit-blockers.txt', Lines) then
    for I := 0 to GetArrayLength(Lines) - 1 do
      if (Trim(Lines[I]) <> '') and (Shown < 10) then
      begin
        List := List + #13#10 + '  ' + Trim(Lines[I]);
        Shown := Shown + 1;
      end;
  if List = '' then
    Result := 'RainCLI is still running and did not stop. Quit it from its tray icon, then ' + Retry + '.'
  else
    Result := 'RainCLI could not stop because these programs are still running from its folder:' + List +
              '' + #13#10 + #13#10 + 'Close them (for example a raincli command still running in a terminal ' +
              'window), then ' + Retry + '.';
end;

{ -- install ------------------------------------------------------------------------------ }

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if IsUpdate then
  begin
    { The updater names exactly <root>\versions\<this version>. }
    if (CompareText(ExtractFileName(ExpandConstant('{app}')), '{#AppVersion}') <> 0) or
       (CompareText(ExtractFileName(ExtractFileDir(ExpandConstant('{app}'))), 'versions') <> 0) then
      Result := '/UPDATE needs /DIR=<root>\versions\{#AppVersion}';
    Exit;
  end;
  try
    RecordExistingStartup;
  except
    Result := 'RainCLI could not record the existing startup entries: ' + GetExceptionMessage;
    Exit;
  end;
  if not StopRunningApp then
    Result := QuitRefusal('run Setup again');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and not IsUpdate then
  begin
    WriteInstallJson;
    PutBinFirstOnPath;
    WriteRunValue;
  end;
end;

{ -- the WebView2 Runtime (protocol §16.10) ------------------------------------------------------- }
{ The app window needs the Microsoft Edge WebView2 Runtime. Windows 11 ships it; some Windows 10
  machines lack it. Without it the app still delivers messages and its tray says how to get it. }

const
  WebView2Client = 'Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  WebView2Download = 'https://go.microsoft.com/fwlink/p/?LinkId=2124703';

function WebView2At(RootKey: Integer; Key: String): String;
begin
  if not RegQueryStringValue(RootKey, Key, 'pv', Result) then
    Result := '';
  if Result = '0.0.0.0' then
    Result := '';
end;

function WebView2Version: String;
begin
  Result := WebView2At(HKLM32, 'SOFTWARE\' + WebView2Client);
  if Result = '' then
    Result := WebView2At(HKLM64, 'SOFTWARE\' + WebView2Client);
  if Result = '' then
    Result := WebView2At(HKCU, 'Software\' + WebView2Client);
end;

function InitializeSetup: Boolean;
var
  Version: String;
  Code: Integer;
begin
  Result := True;
  if IsUpdate then
    Exit;
  Version := WebView2Version;
  if Version <> '' then
  begin
    Log('WebView2 Runtime: ' + Version);
    Exit;
  end;
  Log('WebView2 Runtime: missing');
  if WizardSilent then
    Exit;
  if MsgBox('The RainCLI window needs the Microsoft Edge WebView2 Runtime, which is not installed on this computer.'
            + #13#10#13#10 + 'Yes: open Microsoft''s download page. Install the runtime, then run this setup again.'
            + #13#10 + 'No: install RainCLI now. Messages are delivered, and the window opens once the runtime is installed.',
            mbConfirmation, MB_YESNO) = IDYES then
  begin
    ShellExec('open', WebView2Download, '', '', SW_SHOWNORMAL, ewNoWait, Code);
    Result := False;
  end;
end;

{ -- uninstall ---------------------------------------------------------------------------- }

function InitializeUninstall: Boolean;
var
  Choice: String;
begin
  Result := StopRunningApp;
  if not Result then
  begin
    SuppressibleMsgBox(QuitRefusal('uninstall again'), mbError, MB_OK, IDOK);
    Exit;
  end;
  Choice := ParamValue('SIGNOUT');
  if Choice = 'yes' then
    SignOutOnUninstall := True
  else if Choice = 'no' then
    SignOutOnUninstall := False
  else
    SignOutOnUninstall := SuppressibleMsgBox(
      'Also sign this computer out of RainCLI?' + #13#10#13#10 +
      'Yes revokes this machine''s credential on the server and deletes it here. ' +
      'No keeps it, so a reinstall picks up where you left off.',
      mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES;
end;

procedure RunCli(Cli, Params: String; var Code: Integer);
begin
  if not Exec(Cli, Params, '', SW_HIDE, ewWaitUntilTerminated, Code) then
    Code := -1;
end;

{ The runtime config the app runs: app.json's runtime_config, else the default (B5). }
function RuntimeConfig: String;
begin
  Result := JsonText(ReadRootFile('app.json'), 'runtime_config');
  if Result = '' then
    Result := ExpandConstant('{%USERPROFILE}\.config\raincli\runtime.json');
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Current, Cli, Runtime, Existing: String;
  Code: Integer;
begin
  if CurUninstallStep <> usUninstall then
    Exit;
  Current := JsonVersion(ReadRootFile('install.json'), 'current');
  Cli := ExpandConstant('{app}\versions\') + Current + '\raincli.exe';
  if (Current <> '') and FileExists(Cli) then
  begin
    { Hooks first: signing out deletes a machine-mode runtime.json, which names the hooks' state. }
    Runtime := RuntimeConfig;
    if FileExists(Runtime) then
    begin
      RunCli(Cli, 'hooks install --remove --claude --config "' + Runtime + '"', Code);
      RunCli(Cli, 'hooks install --remove --codex --config "' + Runtime + '"', Code);
    end;
    if SignOutOnUninstall then
    begin
      RunCli(Cli, 'logout --yes', Code);
      if Code <> 0 then
        SuppressibleMsgBox('Sign-out did not complete, so this machine''s credential was kept. ' +
                           'Revoke the machine on the RainCLI website if you no longer need it.',
                           mbError, MB_OK, IDOK);
    end;
  end;
  if RegQueryStringValue(HKCU, RunKey, RunName, Existing) and (CompareText(Existing, StubCommand) = 0) then
    RegDeleteValue(HKCU, RunKey, RunName);
  RemoveBinFromPath;
end;
