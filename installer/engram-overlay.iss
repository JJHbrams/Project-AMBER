; ============================================================
;  Engram Overlay — Inno Setup 통짜 installer (Model B / frozen)
;
;  frozen 번들(dist\engram-overlay\*)을 설치하고, GUI 로 수집한 옵션을
;  configure.ps1 에 넘겨 config/MCP/환경변수/바로가기를 구성한다.
;  사용자 머신에 conda/python 불필요.
;
;  빌드: build-installer.ps1 (ISCC 컴파일)
; ============================================================

#define AppName "Engram Overlay"
#define AppVersion "0.2.1"
#define AppPublisher "DRTECH"
#define AppExeName "engram-overlay.exe"

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
OutputBaseFilename=EngramOverlay_{#AppVersion}_x64-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\dist\engram-overlay\{#AppExeName}

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
Name: "autostart"; Description: "Windows 시작 시 자동 실행 (Startup 등록)"; Flags: unchecked

[Files]
; frozen 번들 전체 (dist\engram-overlay\*)
Source: "..\dist\engram-overlay\*"; DestDir: "{app}\dist\engram-overlay"; Flags: recursesubdirs createallsubdirs ignoreversion
; 설치타임 구성기
Source: "configure.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion

[Run]
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\installer\configure.ps1"" {code:GetConfigureParams}"; \
  StatusMsg: "Engram 구성 중 (config · MCP · 바로가기)..."; \
  Flags: runhidden waituntilterminated

[UninstallRun]
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\installer\configure.ps1"" -Uninstall -InstallDir ""{app}"""; \
  Flags: runhidden; RunOnceId: "EngramConfigureUninstall"

[Code]
var
  DirPage: TInputDirWizardPage;
  ProviderPage: TInputOptionWizardPage;
  QueryPage: TInputQueryWizardPage;

function DefaultDbDir(): String;
begin
  if DirExists('D:\') then
    Result := 'D:\intel_engram'
  else
    Result := ExpandConstant('{sd}') + '\intel_engram';
end;

procedure InitializeWizard();
begin
  { 1) DB / 작업 디렉토리 }
  DirPage := CreateInputDirPage(wpSelectDir,
    'Engram 데이터 위치', 'DB 와 작업 디렉토리를 선택하세요',
    'engram 의 지식 그래프 DB 와 위키가 저장될 위치입니다.', False, '');
  DirPage.Add('DB / 위키 디렉토리');
  DirPage.Add('작업 디렉토리 (engram 실행 시 기준 경로)');
  DirPage.Values[0] := DefaultDbDir();
  DirPage.Values[1] := DefaultDbDir();

  { 2) CLI provider (라디오) }
  ProviderPage := CreateInputOptionPage(DirPage.ID,
    '기본 CLI 공급자', '오버레이가 기본으로 사용할 AI 공급자를 선택하세요',
    '나중에 트레이 메뉴에서 변경할 수 있습니다.', True, False);
  ProviderPage.Add('Copilot CLI');
  ProviderPage.Add('Gemini CLI');
  ProviderPage.Add('Claude Code (직접)');
  ProviderPage.Add('Claude Code (Ollama 라우팅)');
  ProviderPage.Add('Ollama (로컬)');
  ProviderPage.SelectedValueIndex := 2;

  { 3) Ollama 모델 / Identity 이름 (선택) }
  QueryPage := CreateInputQueryPage(ProviderPage.ID,
    '추가 설정 (선택)', '비워두면 기본값을 사용합니다',
    'Ollama 를 쓰는 경우 모델명을, engram 이름을 미리 정하려면 입력하세요.');
  QueryPage.Add('Ollama 모델명 (예: qwen3.5:4b) — 선택', False);
  QueryPage.Add('Engram Identity 이름 — 선택', False);
end;

function ProviderCode(): String;
begin
  case ProviderPage.SelectedValueIndex of
    0: Result := 'copilot';
    1: Result := 'gemini';
    2: Result := 'claude-code';
    3: Result := 'claude-code-ollama';
    4: Result := 'ollama';
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
  if Trim(QueryPage.Values[0]) <> '' then
    R := R + ' -OllamaModel ' + Q + Trim(QueryPage.Values[0]) + Q;
  if Trim(QueryPage.Values[1]) <> '' then
    R := R + ' -IdentityName ' + Q + Trim(QueryPage.Values[1]) + Q;
  if WizardIsTaskSelected('autostart') then
    R := R + ' -EnableAutoStart';
  R := R + ' -LaunchNow';
  Result := R;
end;
