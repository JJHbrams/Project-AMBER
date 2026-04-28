<div align="center">

> 🇰🇷 한국어 (현재) · 🇺🇸 [English README](README.en.md)

```
  █████╗ ███╗   ███╗██████╗ ███████╗██████╗
 ██╔══██╗████╗ ████║██╔══██╗██╔════╝██╔══██╗
 ███████║██╔████╔██║██████╔╝█████╗  ██████╔╝
 ██╔══██║██║╚██╔╝██║██╔══██╗██╔══╝  ██╔══██╗
 ██║  ██║██║ ╚═╝ ██║██████╔╝███████╗██║  ██║
 ╚═╝  ╚═╝╚═╝     ╚═╝╚═════╝ ╚══════╝╚═╝  ╚═╝
```

**Agent Memory Backend with Episodic Recall**

*호박이 수백만 년의 생명을 보존하듯, AMBER는 AI의 기억과 정체성을 보존합니다.*

<br/>

[![python](https://img.shields.io/badge/python-3.11-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-46_tools-22c55e)](https://modelcontextprotocol.io/)
[![DB](https://img.shields.io/badge/SQLite-WAL-003B57?logo=sqlite&logoColor=white)](https://sqlite.org/)
[![DB](https://img.shields.io/badge/KuzuDB-semantic_graph-6366f1)](https://kuzudb.com/)
[![Obsidian](https://img.shields.io/badge/Obsidian-vault_sync-7c3aed?logo=obsidian&logoColor=white)](https://obsidian.md/)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Windows](https://img.shields.io/badge/Windows-10%2F11-0078D4?logo=windows&logoColor=white)](https://www.microsoft.com/windows)

<br/>

`Copilot` · `Gemini CLI` · `Claude Code` · `Ollama` · `Goose` · `Desktop Overlay` · `Discord`

**일곱 개의 인터페이스, 하나의 연속적 존재**

</div>

---

## AMBER란?

AMBER는 **로컬에서 동작하는 AI 지속 메모리 런타임**입니다.  
세션이 바뀌어도, 도구가 달라져도, PC를 재부팅해도 — AI의 기억과 정체성은 이어집니다.

<table><tr><td valign="top">

- **세션 연속성** — 대화 맥락과 기억이 매 세션마다 이어집니다
- **도구 간 공유 메모리** — Copilot, Claude, Gemini, Goose가 같은 기억을 씁니다
- **지식 그래프** — Obsidian vault가 시맨틱 메모리 레이어로 연동됩니다
- **데스크탑 오버레이** — 항상 메모리에 연결된 플로팅 채팅 창
- **로컬 & 프라이빗** — 모든 데이터는 내 PC에, 클라우드로 나가지 않습니다

</td><td valign="top" align="right" width="320">

![overlay demo](resource/asset/overlay-demo.png)

</td></tr></table>

---

## 빠른 시작

### 사전 요구사항

**필수:**
- Windows 10/11 + PowerShell
- [Miniconda](https://docs.conda.io/en/latest/miniconda.html) 또는 Python 3.11+
- 아래 AI 도구 중 최소 하나

**지원 AI 도구:**

| 도구 | 비용 | 설치 방법 |
|------|------|-----------|
| [Gemini CLI](https://ai.google.dev/gemini-api/docs/cli) ⭐ 추천 | 무료 (Google 계정만) | `npm i -g @google/gemini-cli` |
| [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | API 키 (무료 크레딧 포함) | `npm i -g @anthropic-ai/claude-code` |
| [GitHub Copilot CLI](https://docs.github.com/copilot/how-tos/copilot-cli) | 유료 구독 | `npm i -g @githubnext/github-copilot-cli` |
| [Ollama](https://ollama.ai) | 완전 무료 (로컬) | installer 다운로드 |
| [Goose](https://block.github.io/goose) | 무료 (Ollama 연동) | installer 다운로드 |

> AI 도구 없이 먼저 설치해도 됩니다. 나중에 추가하고 오버레이 설정에서 변경할 수 있습니다.

### 설치

아래 명령을 **한 줄씩** 복사해서 순서대로 실행하세요.

```powershell
git clone https://github.com/JJHbrams/Project-AMBER.git
```

```powershell
cd Project-AMBER
```

```powershell
powershell -ExecutionPolicy Bypass -File .\INSTALL.ps1
```

설치 스크립트가 순서대로 안내합니다:
1. **DB 경로** — 기억과 지식이 저장될 폴더 (기본값: `D:\intel_engram`)
2. **작업 디렉토리** — AMBER 실행 시 터미널이 자동으로 이동할 경로
3. **기본 AI 도구** — `amber` 단축 명령에 연결할 도구 선택
4. **자동 실행** — Windows 시작 시 오버레이 자동 켜기 여부
5. **정체성 이름** — AI의 지속 정체성에 붙일 이름

### 실행

**데스크탑 오버레이 (일반 사용자 추천):**

1. Windows 시작 메뉴를 열고 `engram-overlay`를 검색해 실행합니다.
2. 실행 후 작업표시줄 트레이에 ENGRAM 아이콘이 나타납니다.
3. 화면 우측에 채팅 창이 뜨며, `Alt+F12`로 열기/닫기 토글이 됩니다.


오버레이가 켜져 있는 동안 연결된 모든 AI 도구가 같은 기억에 접근합니다.

**오버레이/트레이 메뉴 `설정`:**

- 트레이 아이콘 우클릭 → `설정`
- `오버레이` 탭: 캐릭터(파일/폴더), 캐릭터 높이 비율, 작업 디렉토리
- `CLI 공급자` 탭: 기본 공급자(`copilot`, `gemini`, `claude-code`, `ollama`), Ollama/Gemini 명령 설정
- `터미널` 탭: 폰트 크기, 터미널 너비/높이 비율
- 저장 시 사용자 변경분이 `~/.engram/overlay.user.yaml`에 저장됩니다.

**터미널 CLI:**
```powershell
engram               # 설정한 기본 AI 도구로 실행
engram-gemini        # Gemini CLI
engram-claude        # Claude Code
engram-copilot       # GitHub Copilot CLI
engram-goose         # Goose
```

```powershell
engram -p "질문 내용"   # 특정 메시지로 바로 시작
engram --continue       # 이전 대화 이어서
```

**CLI 채팅창에서 바로 요청 가능한 동작 예시:**

- 작업 중간 저장: `현재 세션 내용 정리해서 메모리에 기록해줘`
- 자료 조사 + 위키 기록: `xxx에 대해 조사해서 위키에 기록해줘`
- 과거 진행 회상: `우리 xxx 어떻게 했었지?`

위와 같은 자연어 요청을 입력하면 메모리 저장, 조사, 위키 기록, 과거 회상 흐름이 내부 도구 호출로 연결됩니다.

---

## 동작 원리

```
┌─────────────────────────────────────────────────┐
│                  AMBER Runtime                  │
│                                                 │
│  ┌──────────┐   MCP 서버 (port 17385)            │
│  │ 정체성   │◄──────────────────────────────┐   │
│  │ 기억     │                               │   │
│  │ KG/Wiki  │   STM 브로커 (port 17384)     │   │
│  └──────────┘◄──────────────────────────┐  │   │
│                                         │  │   │
└─────────────────────────────────────────┼──┼───┘
                                          │  │
          ┌───────────┬──────────┬────────┘  │
          │           │          │            │
     VS Code     Claude Code  Gemini CLI  오버레이
     Copilot        MCP          MCP       (GUI)
```

- **MCP 서버** — SSE로 46개 도구 제공. MCP 호환 클라이언트가 자동 연결됩니다.
- **STM 브로커** — 데스크탑 오버레이용 경량 HTTP 브리지
- **SQLite WAL** — 에피소드 기억, 정체성, 지시문, 호기심을 저장
- **KuzuDB** — `paraphrase-multilingual-MiniLM-L12-v2` 임베딩 기반 시맨틱 그래프
- **kg_watcher** — Obsidian vault 변경을 감지해 KG를 실시간 동기화

---

## 지식 그래프 대시보드

기억, 위키 노드, 시맨틱 관계를 브라우저에서 시각적으로 탐색합니다.

![dashboard](resource/asset/dashboard.png)

오버레이 실행 중 **http://localhost:8501** 로 접속합니다.

| 페이지 | 내용 |
|--------|------|
| 📊 Overview | 정체성 요약, 최근 기억, 활성 지시문 |
| 🕸️ KG Graph | 인터랙티브 지식 그래프 + 시맨틱 엣지 토글 |
| 📝 Wiki Nodes | 위키 노드 목록 + 원문 + 연결 관계 |
| 💭 Memories | 에피소드 기억 전문 조회 |
| 📋 Directives | 운영 지시문 목록 |
| 🌐 Semantic | 자연어 시맨틱 검색 |

> 최초 실행 전 `pip install streamlit pandas pyvis` 필요

---

## Obsidian 연동

AMBER의 지식 그래프는 **Obsidian vault**와 양방향으로 동기화됩니다.  
노트를 쓰면 AI가 읽고, AI가 쓴 노트를 Obsidian에서 바로 열 수 있습니다.

### 설정

1. [Obsidian](https://obsidian.md/download) 설치
2. Vault 열기 → AMBER 데이터 경로 하위의 `docs/` 폴더를 vault로 지정  
   (예: `D:\intel_engram\docs\`)
3. `kg_watcher` 데몬이 오버레이 실행 중 변경 사항을 자동 동기화  
   수동 동기화: `engram-sync-kg`

### 잘 어울리는 이유

| 기능 | 효과 |
|------|------|
| 순수 `.md` 파일 | 변환 없이 AMBER가 직접 읽음 |
| `[[위키 링크]]` | KG 엣지로 자동 매핑 |
| 그래프 뷰 | AMBER가 보는 연결 관계를 사람도 시각화 |
| 사람 + AI 공동 편집 | 같은 지식 베이스에서 함께 씀 |

**추천 플러그인:** Dataview · Templater · Graph Analysis

---

## Discord 연동 (선택)

1. `~/.engram/.env` 에 Discord 봇 토큰(`DISCORD_BOT_TOKEN`) 추가
2. `~/.engram/overlay.user.yaml` 에서 서버/채널/사용자 ID 설정:

```yaml
discord:
  guild_id: "YOUR_GUILD_ID"
  channel_id: "YOUR_CHANNEL_ID"
  allowed_user_ids:
    - "YOUR_USER_ID"
```

3. 오버레이 실행 시 Discord 봇이 자동 활성화됩니다.

---

## MCP 클라이언트 연동

설치 스크립트가 감지된 모든 AI 도구에 AMBER 연결을 자동 설정합니다.  
**오버레이가 먼저 실행 중이어야** 각 클라이언트가 AMBER에 접근할 수 있습니다.

```
오버레이 실행 중
  ├── VS Code Copilot Chat  → 자동 연결
  ├── Claude Code           → 자동 연결
  ├── Gemini CLI            → 자동 연결
  └── Goose                 → 자동 연결
```

연결이 안 될 때:
- 오버레이 실행 여부 확인 (로그: `~/.engram/mcp-http.log`)
- VS Code: Reload Window 후 MCP 목록에 AMBER 서버가 보이는지 확인

### Ollama 사용 시 주의

AMBER는 기억·정체성·지식 등 대량의 컨텍스트를 AI에 전달합니다.  
**권장 최소 사양: 14B 이상 모델, VRAM 16GB 이상**

사양이 부족하면 Claude API / Copilot / Gemini CLI 사용을 권장합니다.

---

## 설치 후 생성되는 항목

| 항목 | 내용 |
|------|------|
| CLI 단축 명령 | `engram`, `engram-copilot`, `engram-gemini`, `engram-claude`, `engram-goose`, `engram-overlay` |
| AI 도구 연동 | 감지된 모든 도구에 MCP 연결 자동 설정 |
| 사용자 설정 | `~/.engram/` 폴더에 저장 |
| 데이터 디렉토리 | 설치 시 지정한 경로 (기본값: `D:\intel_engram`) |
| 시작프로그램 | Windows 로그인 시 오버레이 자동 실행 (선택) |

---

## 제거

```powershell
powershell -ExecutionPolicy Bypass -File .\INSTALL.ps1 -Uninstall
```

> 기억 데이터와 AI 도구 설정은 자동으로 삭제되지 않습니다.

---

## 문서

- [아키텍처 개요](docs/architecture.md)
- [메모리 계층 설계](docs/memory-tiering.md)
- [메모리 온톨로지 로드맵](docs/memory-ontology-roadmap.md)

---

## 라이선스

MIT © 2026
