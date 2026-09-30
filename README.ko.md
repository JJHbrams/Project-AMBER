<p align="center"><img src="resource/icon.png" width="120" alt="호박 속에 보존된 디지털 정체성 칩을 표현한 AMBER 아이콘" /></p>

<h1 align="center">AMBER</h1>

<p align="center"><strong>경험이 쌓여, 정체성이 됩니다.</strong></p>

<p align="center">AI 에이전트의 기억과 정체성을 다음 세션으로 이어갑니다.</p>

<p align="center"><sub>Agent Memory Backend with Episodic Recall · AMBER는 Engram의 배포 이름입니다.</sub></p>

<p align="center"><a href="https://github.com/JJHbrams/Project-AMBER/releases/latest"><strong>Windows 다운로드</strong></a> · <a href="README.md">English</a> · <a href="#빠른-시작">빠른 시작</a></p>

<p align="center"><sub>Windows · 로컬 저장소 · MCP 연결 · MIT</sub></p>

AMBER는 기억·페르소나·지식을 세션과 설정된 AI 도구 사이에서 이어가는 로컬 런타임 **Engram의 배포명**입니다. 호박석 안에 담긴 칩은 경험이 쌓여 형성되는 디지털 정체성을 상징합니다.

## 이어지는 것

- **정체성** — 저장된 경험과 반성을 바탕으로 이어지는 자기 서술과 페르소나.
- **기억** — 다음 세션에서 다시 찾을 수 있는 세션 요약, 결정, 미완료 작업.
- **지식** — 로컬 지식 그래프로 연결하고 검색하는 Markdown 노트와 프로젝트 문서.

## 빠른 시작

1. [최신 AMBER Windows 설치 프로그램](https://github.com/JJHbrams/Project-AMBER/releases/latest)을 내려받아 실행합니다.
2. 시작 메뉴에서 **AMBER (ENGRAM)**을 열고 로컬 설정을 마칩니다.
3. 설정한 코딩 도구로 프로젝트 세션을 시작합니다. 지원되는 연결에는 같은 로컬 프로젝트 맥락이 제공됩니다.

일반 사용자는 Windows 설치 프로그램을 사용하면 됩니다. 설치 파일에 필요한 런타임이 포함되어 있어 Python이나 Conda를 별도로 설치할 필요가 없습니다.

## 코딩 도구 사이의 연속성

AMBER는 설정된 로컬 클라이언트를 하나의 프로젝트 기억 서비스에 연결합니다. 예를 들면 다음과 같습니다.

- **Claude Code**에서 구현한 뒤 **Codex CLI**에서 같은 저장 프로젝트 맥락으로 검토합니다.
- **GitHub Copilot CLI**에서 이전 세션의 결정을 다시 찾아 특정 작업을 이어 갑니다.
- 호환되는 다른 MCP 클라이언트도, 제공하는 lifecycle 신호 범위에서 연동할 수 있습니다.

클라이언트마다 지원 범위와 lifecycle 정보는 다릅니다. AMBER의 지속 프로젝트 맥락은 특정 터미널·모델·UI와 분리되어 있습니다.

## 바탕화면에서 보는 AMBER

Windows 오버레이는 선택한 세션 활동을 보여 주고, 캐릭터 옆에 간결한 대화 화면을 제공합니다.

<p align="center"><img src="resource/asset/readme/bubble-mode-history-20260912.png" width="850" alt="AMBER 세션 모니터, 컴팩트 대화 말풍선, 캐릭터를 함께 보여 주는 화면." /></p>

합성 QA 텍스트와 커스텀 캐릭터 아트를 사용한 네이티브 UI 미리보기입니다. [캡처 정보](resource/asset/readme/trickcal-demo-v1.5.15.provenance.json).

<details>
<summary>캐릭터 애니메이션 보기</summary>

<p align="center"><img src="resource/asset/readme/native-bolttagu-trickcal-v1.5.15.gif" width="220" alt="AMBER v1.5.15.736에서 촬영한 커스텀 캐릭터 애니메이션" /></p>

커스텀 아트·매핑과 합성 이벤트로 촬영한 v1.5.15.736 예시입니다. [캡처 정보](resource/asset/readme/trickcal-demo-v1.5.15.provenance.json).

</details>

## 로컬 저장소와 공급자

AMBER는 작업 기억과 프로젝트 지식을 로컬에 저장하고 관련 맥락을 검색합니다. 모든 과거 내용을 빠짐없이 회상하는 것은 아닙니다. 이 저장소는 AI 공급자와 별개입니다. 사용자가 자신의 클라이언트에서 선택해 보낸 맥락은 해당 클라이언트가 설정한 공급자에게 전달될 수 있습니다. 외부 renderer에는 Event API를 통해 표시용 메타데이터만 전달되며, 원문 프롬프트·도구 payload·비공개 기억 본문은 전달하지 않습니다.

## 문서

- [한국어 사용자 가이드](docs/user-guide.ko.md): 오버레이, Obsidian 지식, 캐릭터 매핑, 소스 개발 설정
- [아키텍처](docs/architecture.md): 저장소·서비스·검색 경계
- [캐릭터 팩](docs/character-packs.md), [외부 오버레이 Event API](docs/overlay-event-api-v2.md)
- [이슈와 피드백](https://github.com/JJHbrams/Project-AMBER/issues/new/choose)

<details>
<summary>AMBER 자체를 개발하려면</summary>

AMBER 자체를 개발할 때만 저장소를 복제하고 루트 설치 진입점을 사용합니다.

```powershell
git clone https://github.com/JJHbrams/Project-AMBER.git
cd Project-AMBER
powershell -ExecutionPolicy Bypass -File .\INSTALL.ps1
```

소스 개발 환경과 제거 방법은 [사용자 가이드](docs/user-guide.ko.md)를 참고하세요. 일반 사용자는 위의 Windows 설치 프로그램으로 시작하면 됩니다.

</details>

## 라이선스

AMBER는 [MIT License](LICENSE)로 제공됩니다.
