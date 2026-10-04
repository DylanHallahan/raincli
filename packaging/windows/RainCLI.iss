; RainCLI Windows app installer (protocol §15.5, amended by §15.8 H5, H6, H7, M6, M10 and L2).
; Built by build.py:  ISCC /DAppVersion=X.Y.Z /DDistDir=<PyInstaller dist> RainCLI.iss
;
; A full install (no /UPDATE) goes to %LOCALAPPDATA%\Programs\RainCLI (no admin rights) and:
;   - records any existing RainCLI Run value, Startup-folder entry or Scheduled Task that starts
;     raincli in installer-record.log BEFORE writing anything (H7), and never overwrites an
;     unrecorded value: it writes the Run value only when none exists or it already names our stub;
;     otherwise the app's migration (H6 step 5) takes it over once the new runtime is ready;
;   - installs versions\X.Y.Z, the stable stub RainCLI.exe and the PATH shim bin\raincli.exe (the
;     stub and the shim only if absent: they never change in place);
;   - writes install.json {current, previous, probation} atomically (M6);
;   - puts <root>\bin first on the user PATH, adds the Start menu entries and starts the tray,
;     whose first run signs in or migrates an older install (§15.6, H6).
; /UPDATE /DIR=<root>\versions\X.Y.Z (the app's updater) installs only that version folder: no
; uninstaller or uninstall key, no Run value, PATH, shortcuts, install.json or launch (H5).
; The uninstaller stops the app through the stub, optionally signs out (default no), removes the
; raincli-marked hooks, our Run value, the PATH entry, the shortcuts, versions\*, the stub, the shim
; and install.json, and keeps agent.json, the queues, machine-salt and the logs (M10).

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
UninstallDisplayIcon={app}\RainCLI.exe

[Files]
Source: "{#DistDir}\RainCLI-{#AppVersion}\*"; DestDir: "{code:VersionDir}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#DistDir}\RainCLI.exe"; DestDir: "{app}"; Flags: onlyifdoesntexist; Check: not IsUpdate
Source: "{#DistDir}\bin\raincli.exe"; DestDir: "{app}\bin"; Flags: onlyifdoesntexist; Check: not IsUpdate

[Icons]
Name: "{userprograms}\RainCLI\RainCLI"; Filename: "{app}\RainCLI.exe"; Check: not IsUpdate
Name: "{userprograms}\RainCLI\Uninstall RainCLI"; Filename: "{uninstallexe}"; Check: not IsUpdate

[Run]
Filename: "{app}\RainCLI.exe"; Parameters: "--background"; Flags: nowait; Check: not IsUpdate

[UninstallDelete]
Type: filesandordirs; Name: "{app}\versions"
Type: files; Name: "{app}\RainCLI.exe"
Type: files; Name: "{app}\bin\raincli.exe"
Type: dirifempty; Name: "{app}\bin"
Type: files; Name: "{app}\install.json"
Type: files; Name: "{app}\install.json.new"
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

function MoveFileEx(ExistingName, NewName: String; Flags: DWORD): BOOL;
  external 'MoveFileExW@kernel32.dll stdcall';

function IsUpdate: Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), '/UPDATE') = 0 then
      Result := True;
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

{ The string value of "Key" in a flat JSON object written by the app or this installer, or ''. }
function JsonString(Json, Key: String): String;
var
  P: Integer;
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
  Json := Copy(Json, 2, Length(Json));
  P := Pos('"', Json);
  if P > 0 then
    Result := Copy(Json, 1, P - 1);
  if not IsVersion(Result) then
    Result := '';
end;

function ReadInstallJson: String;
var
  Raw: AnsiString;
begin
  Result := '';
  if LoadStringFromFile(RootDir + '\install.json', Raw) then
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

procedure RecordScheduledTasks(var Lines: TArrayOfString);
var
  Output: TArrayOfString;
  Tmp, Name: String;
  I, Code: Integer;
begin
  Tmp := ExpandConstant('{tmp}\schtasks.txt');
  if not Exec(ExpandConstant('{cmd}'), '/c schtasks /query /fo LIST /v > "' + Tmp + '" 2>nul', '',
              SW_HIDE, ewWaitUntilTerminated, Code) or not LoadStringsFromFile(Tmp, Output) then
  begin
    AddLine(Lines, 'scheduled tasks: could not be listed');
    Exit;
  end;
  Name := '';
  for I := 0 to GetArrayLength(Output) - 1 do
  begin
    if Pos('TaskName:', Output[I]) = 1 then
      Name := Trim(Copy(Output[I], 10, Length(Output[I])))
    else if (Pos('Task To Run:', Output[I]) = 1) and Contains(Output[I], 'raincli') then
      AddLine(Lines, 'scheduled task: ' + Name + ' runs ' + Trim(Copy(Output[I], 13, Length(Output[I]))));
  end;
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

procedure WriteInstallJson;
var
  Old, Current, Previous, Json, Path: String;
begin
  Old := ReadInstallJson;
  Current := JsonString(Old, 'current');
  Previous := JsonString(Old, 'previous');
  if (Current <> '') and (Current <> '{#AppVersion}') then
    Previous := Current;
  if Previous = '{#AppVersion}' then
    Previous := '';
  if Previous = '' then
    Json := '{"current": "{#AppVersion}", "previous": null, "probation": null}'
  else
    Json := '{"current": "{#AppVersion}", "previous": "' + Previous + '", "probation": null}';
  Path := RootDir + '\install.json';
  if not SaveStringToFile(Path + '.new', Json + #13#10, False) or
     not MoveFileEx(Path + '.new', Path, MOVEFILE_REPLACE_EXISTING or MOVEFILE_WRITE_THROUGH) then
    RaiseException('could not write ' + Path);
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

{ -- install ------------------------------------------------------------------------------ }

procedure StopRunningApp;
var
  Code: Integer;
begin
  if FileExists(RootDir + '\RainCLI.exe') then
    Exec(RootDir + '\RainCLI.exe', '--quit', '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

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
  StopRunningApp;
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

{ -- uninstall ---------------------------------------------------------------------------- }

function InitializeUninstall: Boolean;
begin
  SignOutOnUninstall := SuppressibleMsgBox(
    'Also sign this computer out of RainCLI?' + #13#10#13#10 +
    'Yes revokes this machine''s credential on the server and deletes it here. ' +
    'No keeps it, so a reinstall picks up where you left off.',
    mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES;
  Result := True;
end;

procedure RunCli(Cli, Params: String; var Code: Integer);
begin
  if not Exec(Cli, Params, '', SW_HIDE, ewWaitUntilTerminated, Code) then
    Code := -1;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Current, Cli, Runtime, Existing: String;
  Code: Integer;
begin
  if CurUninstallStep <> usUninstall then
    Exit;
  StopRunningApp;
  Current := JsonString(ReadInstallJson, 'current');
  Cli := ExpandConstant('{app}\versions\') + Current + '\raincli.exe';
  if (Current <> '') and FileExists(Cli) then
  begin
    if SignOutOnUninstall then
    begin
      RunCli(Cli, 'logout --yes', Code);
      if Code <> 0 then
        SuppressibleMsgBox('Sign-out did not complete, so this machine''s credential was kept. ' +
                           'Revoke the machine on the RainCLI website if you no longer need it.',
                           mbError, MB_OK, IDOK);
    end;
    Runtime := ExpandConstant('{%USERPROFILE}\.config\raincli\runtime.json');
    if FileExists(Runtime) then
    begin
      RunCli(Cli, 'hooks install --remove --claude --config "' + Runtime + '"', Code);
      RunCli(Cli, 'hooks install --remove --codex --config "' + Runtime + '"', Code);
    end;
  end;
  if RegQueryStringValue(HKCU, RunKey, RunName, Existing) and (CompareText(Existing, StubCommand) = 0) then
    RegDeleteValue(HKCU, RunKey, RunName);
  RemoveBinFromPath;
end;
