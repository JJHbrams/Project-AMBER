; ============================================================
;  Engram Overlay — Inno Setup 통짜 installer (Model B / frozen)
;
;  frozen 번들(dist\engram-overlay\*)을 설치하고, GUI 로 수집한 옵션을
;  configure.ps1 에 넘겨 config/MCP/환경변수/바로가기를 구성한다.
;  사용자 머신에 conda/python 불필요.
;
;  빌드: build-installer.ps1 (ISCC 컴파일)
; ============================================================

#define AppName "AMBER (ENGRAM)"
#ifndef AppVersion
  #define AppVersion "0.0.0.0"
#endif
#define AppPublisher "DRTECH"
#define AppExeName "engram-overlay.exe"
#ifndef BuildCompression
  #define BuildCompression "zip"
#endif
#ifndef BuildSolidCompression
  #define BuildSolidCompression "no"
#endif
#ifndef BuildOutputSuffix
  #define BuildOutputSuffix "-dev"
#endif

[Setup]
AppId={{A7E3C1D2-9B4F-4E6A-8C11-5D2F1A0B3E64}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\EngramOverlay
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
; drt-notebookLM 처럼 setup.exe 를 프로젝트 루트에 생성 (.iss 기준 상위 = repo 루트)
OutputDir=..
OutputBaseFilename=AMBER_{#AppVersion}{#BuildOutputSuffix}_x64-setup
Compression={#BuildCompression}
SolidCompression={#BuildSolidCompression}
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\dist\engram-overlay\{#AppExeName}

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
Name: "autostart"; Description: "Windows 시작 시 자동 실행 (Startup 등록)"; Flags: unchecked

[Files]
; install 전 실행 중인 동일 설치본을 종료하기 위한 공통 helper (PrepareToInstall에서 추출)
Source: "stop-engram-processes.ps1"; Flags: dontcopy
; frozen 번들 전체 (dist\engram-overlay\*)
Source: "..\dist\engram-overlay\*"; DestDir: "{app}\dist\engram-overlay"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\config\overlay.yaml"; DestDir: "{app}\dist\engram-overlay\config"; Flags: ignoreversion
Source: "..\config\config.yaml"; DestDir: "{app}\dist\engram-overlay\config"; Flags: ignoreversion
Source: "..\config\clients\copilot.md"; DestDir: "{app}\config\clients"; Flags: ignoreversion
Source: "templates\*"; DestDir: "{app}\installer\templates"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\.github\skills\engram\SKILL.md"; DestDir: "{app}\.github\skills\engram"; Flags: ignoreversion
Source: "..\.github\skills\orchestrate\SKILL.md"; DestDir: "{app}\.github\skills\orchestrate"; Flags: ignoreversion
Source: "..\.github\skills\engram-new-session\SKILL.md"; DestDir: "{app}\.github\skills\engram-new-session"; Flags: ignoreversion
Source: "..\.github\skills\engram-task-workflow\SKILL.md"; DestDir: "{app}\.github\skills\engram-task-workflow"; Flags: ignoreversion
Source: "..\.github\skills\engram-wiki-workflow\SKILL.md"; DestDir: "{app}\.github\skills\engram-wiki-workflow"; Flags: ignoreversion
Source: "..\.github\skills\engram-close-session\SKILL.md"; DestDir: "{app}\.github\skills\engram-close-session"; Flags: ignoreversion
; 설치타임 구성기
Source: "configure.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion

[UninstallRun]
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\installer\configure.ps1"" -Uninstall -InstallDir ""{app}"""; \
  Flags: runhidden; RunOnceId: "EngramConfigureUninstall"

[Code]
var
  DirPage: TInputDirWizardPage;
  ProviderPage: TInputOptionWizardPage;
  ExternalOverlayPage: TInputOptionWizardPage;
  QueryPage: TInputQueryWizardPage;

function DefaultDbDir(): String;
begin
  if DirExists('D:\') then
    Result := 'D:\intel_engram'
  else
    Result := ExpandConstant('{sd}') + '\intel_engram';
end;

function DecodeYamlScalar(Value: String): String;
var
  L: Integer;
begin
  Result := Trim(Value);
  L := Length(Result);
  if L >= 2 then
  begin
    if ((Result[1] = '"') and (Result[L] = '"')) or
       ((Result[1] = '''') and (Result[L] = '''')) then
      Result := Copy(Result, 2, L - 2);
  end;
end;

procedure LoadExistingUserPaths(var DbDir: String; var WorkDir: String);
var
  ConfigPath: String;
  Lines: TArrayOfString;
  I: Integer;
  Line: String;
  Clean: String;
  InDb: Boolean;
begin
  ConfigPath := ExpandConstant('{%USERPROFILE}\.engram\user.config.yaml');
  if not LoadStringsFromFile(ConfigPath, Lines) then
    Exit;

  InDb := False;
  for I := 0 to GetArrayLength(Lines) - 1 do
  begin
    Line := Lines[I];
    Clean := Trim(Line);
    if (Clean = '') or (Copy(Clean, 1, 1) = '#') then
      Continue;

    if Line = Clean then
    begin
      InDb := Clean = 'db:';
      if Pos('workdir:', Clean) = 1 then
      begin
        Line := DecodeYamlScalar(Copy(Clean, Length('workdir:') + 1, MaxInt));
        if Line <> '' then
          WorkDir := Line;
      end;
    end
    else if InDb and (Pos('root_dir:', Clean) = 1) then
    begin
      Line := DecodeYamlScalar(Copy(Clean, Length('root_dir:') + 1, MaxInt));
      if Line <> '' then
        DbDir := Line;
    end;
  end;
end;

procedure InitializeWizard();
var
  InitialDbDir: String;
  InitialWorkDir: String;
begin
  InitialDbDir := DefaultDbDir();
  InitialWorkDir := InitialDbDir;
  LoadExistingUserPaths(InitialDbDir, InitialWorkDir);

  { 1) DB / 작업 디렉토리 }
  DirPage := CreateInputDirPage(wpSelectDir,
    'AMBER (ENGRAM) 데이터 위치', 'DB 와 작업 디렉토리를 선택하세요',
    'engram 의 지식 그래프 DB 와 위키가 저장될 위치입니다.', False, '');
  DirPage.Add('DB / 위키 디렉토리');
  DirPage.Add('작업 디렉토리 (engram 실행 시 기준 경로)');
  DirPage.Values[0] := InitialDbDir;
  DirPage.Values[1] := InitialWorkDir;

  { 2) CLI provider (라디오) }
  ProviderPage := CreateInputOptionPage(DirPage.ID,
    '기본 CLI 공급자', '오버레이가 기본으로 사용할 AI 공급자를 선택하세요',
    '나중에 트레이 메뉴에서 변경할 수 있습니다.', True, False);
  ProviderPage.Add('Copilot CLI');
  ProviderPage.Add('Antigravity (agy)');
  ProviderPage.Add('Codex CLI');
  ProviderPage.Add('Claude Code (직접)');
  ProviderPage.Add('Claude Code (Ollama 라우팅)');
  ProviderPage.Add('Ollama (로컬)');
  ProviderPage.SelectedValueIndex := 3;

  { 3) External overlay contract scaffold. v1.1.0.89 has no installable assets. }
  ExternalOverlayPage := CreateInputOptionPage(ProviderPage.ID,
    '외부 오버레이 (선택)', '외부 렌더러 구성 방식을 선택하세요',
    '현재 고정 공개 릴리스 v1.1.0.89에는 self-contained preset/SDK asset이 없습니다. 선택 시 AMBER core만 설치됩니다.', True, False);
  ExternalOverlayPage.Add('설치 안 함 (권장)');
  ExternalOverlayPage.Add('Preset provider — 현재 릴리스에서 사용 불가');
  ExternalOverlayPage.Add('Renderer SDK — 현재 릴리스에서 사용 불가');
  ExternalOverlayPage.SelectedValueIndex := 0;

  { 4) Ollama 모델 / Identity 이름 (선택) }
  QueryPage := CreateInputQueryPage(ExternalOverlayPage.ID,
    '추가 설정 (선택)', '비워두면 기본값을 사용합니다',
    'Ollama 를 쓰는 경우 모델명을, engram 이름을 미리 정하려면 입력하세요.');
  QueryPage.Add('Ollama 모델명 (예: qwen3.5:4b) — 선택', False);
  QueryPage.Add('Engram Identity 이름 — 선택', False);
end;

function ExternalOverlayModeCode(): String;
begin
  case ExternalOverlayPage.SelectedValueIndex of
    1: Result := 'presets';
    2: Result := 'sdk';
  else
    Result := 'none';
  end;
end;

function ProviderCode(): String;
begin
  case ProviderPage.SelectedValueIndex of
    0: Result := 'copilot';
    1: Result := 'antigravity';
    2: Result := 'codex';
    3: Result := 'claude-code';
    4: Result := 'claude-code-ollama';
    5: Result := 'ollama';
  else
    Result := 'claude-code';
  end;
end;

function GetConfigureParams(Param: String): String;
var
  Q: String;
  R: String;
begin
  Q := '"';
  R := '-InstallDir ' + Q + ExpandConstant('{app}') + Q;
  R := R + ' -DbDir ' + Q + DirPage.Values[0] + Q;
  R := R + ' -WorkDir ' + Q + DirPage.Values[1] + Q;
  R := R + ' -CliProvider ' + ProviderCode();
  R := R + ' -ExternalOverlayMode ' + ExternalOverlayModeCode();
  if Trim(QueryPage.Values[0]) <> '' then
    R := R + ' -OllamaModel ' + Q + Trim(QueryPage.Values[0]) + Q;
  if Trim(QueryPage.Values[1]) <> '' then
    R := R + ' -IdentityName ' + Q + Trim(QueryPage.Values[1]) + Q;
  if WizardIsTaskSelected('autostart') then
    R := R + ' -EnableAutoStart';
  R := R + ' -LaunchNow';
  Result := R;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = ExternalOverlayPage.ID) and
     (ExternalOverlayPage.SelectedValueIndex <> 0) then
    MsgBox('선택한 외부 오버레이 구성은 고정 공개 릴리스 v1.1.0.89에서 사용할 수 없습니다.' + #13#10 +
      'AMBER core 설치는 계속되지만 외부 오버레이는 설치되지 않습니다.', mbInformation, MB_OK);
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if (CurPageID = wpFinished) and (ExternalOverlayPage.SelectedValueIndex <> 0) then
    WizardForm.FinishedLabel.Caption := WizardForm.FinishedLabel.Caption + #13#10 + #13#10 +
      '외부 오버레이: 선택한 구성은 현재 릴리스에서 사용할 수 없어 설치되지 않았습니다.';
end;

procedure RunConfigure;
var
  ResultCode: Integer;
  Params: String;
begin
  WizardForm.StatusLabel.Caption := 'AMBER (ENGRAM) 구성 중 (config · MCP · 바로가기)...';
  Params := '-NoProfile -ExecutionPolicy Bypass -File "' +
    ExpandConstant('{app}\installer\configure.ps1') + '" ' + GetConfigureParams('');
  if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Params,
    ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    RaiseException('AMBER (ENGRAM) 구성기를 실행하지 못했습니다.');
  if ResultCode <> 0 then
    RaiseException('AMBER (ENGRAM) 구성에 실패했습니다. 로그: ' +
      ExpandConstant('{%USERPROFILE}\.engram\logs') +
      ' (exit=' + IntToStr(ResultCode) + ')');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    RunConfigure;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
  HelperPath: String;
  Params: String;
begin
  ExtractTemporaryFile('stop-engram-processes.ps1');
  HelperPath := ExpandConstant('{tmp}\stop-engram-processes.ps1');
  Params := '-NoProfile -ExecutionPolicy Bypass -File "' + HelperPath +
    '" -ArtifactDir "' + ExpandConstant('{app}\dist\engram-overlay') + '"';
  if (not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Params,
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode)) then
  begin
    Result := '실행 중인 AMBER (ENGRAM) 프로세스를 종료하지 못했습니다.';
    Exit;
  end;
  if ResultCode <> 0 then
    Result := '실행 중인 AMBER (ENGRAM) 프로세스를 종료하지 못했습니다. (exit=' + IntToStr(ResultCode) + ')';
end;
