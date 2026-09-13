---
name: sage
description: >
  현자(sage) — orchestrator 가 풀지 못하는 문제를 각 provider 최상위 모델(Claude Fable /
  Codex GPT-6-Astra)에게 1회성으로 위임한다. 트리거: 현자, sage, 깊은 고찰, 깊이 생각,
  난제, 설계 판단이 갈릴 때, 통상 디버깅으로 원인이 안 잡힐 때, PC 제어·컴퓨터 조작이
  필요할 때. 일상적인 구현·조사·단순 버그에는 실행하지 않는다.
---

# Sage (현자)

Orchestrator 는 계속 중간 티어(Opus / Sol)로 돈다. 현자는 **상시 티어가 아니라 예외 호출**이다.
한 번의 호출로 끝낼 질문 하나를 들려 보내고, 답을 받아 orchestrator 가 실행한다.

## Phase 0 — 승급 판정

다음 중 **하나라도** 해당하면 승급한다.

| 승급 사유 | 예 |
|---|---|
| 설계 갈림길 | 되돌리기 비싼 구조 결정, 상호 배타적 아키텍처 후보 |
| 난치 원인 규명 | 통상 디버깅·로그·테스트로 2회 이상 못 잡은 버그, 재현이 불안정한 결함 |
| 교차 시스템 추론 | 여러 런타임·프로세스·경계를 동시에 물고 있는 실패 |
| 판단 검증 | orchestrator 자신의 계획을 스스로 검증할 수 없을 때 |
| PC 제어 | 화면·GUI·데스크톱 조작이 필요한 작업 (Phase 3) |

다음은 **승급하지 않는다.** 그냥 평소대로 처리한다.

- 파일 몇 개 수정, 리팩토링, 테스트 추가
- 코드 위치 찾기·읽기·설명
- 이미 원인을 아는 버그의 수정
- "어렵다"가 아니라 "길다"인 작업 — 그건 분할 대상이지 승급 대상이 아니다

승급 판정이 애매하면 승급하지 않는다. 현자는 비싸다.

## Phase 1 — 브리프 작성

현자는 이 세션의 맥락을 **모른다.** 호출 하나로 닫히는 자립 브리프를 만든다.

```
[SAGE_BRIEF]
question: <현자가 답해야 할 단 하나의 질문>
why_escalated: <위 표의 승급 사유 + 이미 시도해서 실패한 것>
context: <필요한 배경 사실만. 추측과 사실을 구분해 적는다>
artifacts: <읽어야 할 파일 경로, 로그 발췌, 실패한 명령과 그 출력>
constraints: <바꾸면 안 되는 것, 이미 확정된 결정, 런타임·플랫폼 제약>
deliverable: <판단만 | 판단+계획 | 원인규명>
```

`question` 이 둘 이상이면 브리프를 쪼개지 말고 **가장 비싼 질문 하나만** 남긴다.
나머지는 그 답이 나온 뒤 orchestrator 가 직접 처리한다.

## Phase 2 — 자문 호출 (읽기 전용, 기본 모드)

세션이 돌고 있는 provider 의 최상위 모델로 대칭 승급한다.

| 세션 | 호출 방법 | 모델 |
|---|---|---|
| Claude Code | `sage` 서브에이전트 | Fable |
| Codex | `sage` 서브에이전트 | GPT-6-Astra (`model_reasoning_effort = high`) |
| Copilot CLI | `sage` 서브에이전트 | 해당 CLI 최상위 |

현재 세션에 `sage` 서브에이전트가 없으면 Codex 로 직접 넘긴다.

```bash
codex exec -m gpt-6-astra -c model_reasoning_effort=high -s read-only "<[SAGE_BRIEF] 전문>"
```

현자는 읽기 전용이다. 파일을 고치거나 상태를 바꾸는 명령을 실행하지 않는다.
반드시 `[SAGE]` 형식(verdict / reasoning / evidence / risks / plan / confidence / unknowns)으로 답을 받는다.

## Phase 3 — 제어 모드 (PC 제어가 필요할 때만)

화면·GUI·데스크톱 조작은 Codex 의 `computer_use` 로만 수행한다. 이 경로는 샌드박스 밖에서
실제 기계를 건드리므로 **사용자의 명시적 승인 없이는 실행하지 않는다.**

승인 요청 시 다음을 먼저 보여준다.

```
[SAGE_ACT]
goal: <조작으로 달성할 것>
target: <대상 앱·창·경로>
actions: <수행할 조작의 순서>
blast_radius: <잘못되면 무엇이 바뀌는가 / 되돌릴 수 있는가>
```

사용자가 승인한 뒤에만 실행한다.

```bash
codex exec -m gpt-6-astra -c model_reasoning_effort=high \
  --enable computer_use -s workspace-write "<[SAGE_ACT] + 조작 지시>"
```

- 되돌릴 수 없는 조작(삭제, 결제, 전송, 설정 영구 변경)은 승인받은 항목만 수행한다.
- `--dangerously-bypass-approvals-and-sandbox` 는 사용하지 않는다.
- 조작 후 결과를 실제로 확인한다 — 명령이 성공했다는 사실은 목표 달성의 증거가 아니다.

## Phase 4 — 수용과 실행

1. `[SAGE]` 의 `verdict` 를 그대로 따르지 말고 `evidence` 로 검증한다. 근거 없는 판단은 재질의한다.
2. `confidence: low` 이거나 `unknowns` 가 결론을 뒤집을 수준이면, 그 미지수를 먼저 `servant` 로 해소한 뒤 판단을 확정한다.
3. `plan` 은 orchestrator 가 `planner`/`coder`/`servant` 로 분해해 실행한다. 현자를 구현 루프에 다시 넣지 않는다.
4. 같은 질문으로 현자를 두 번 호출하지 않는다. 두 번째가 필요하다면 그건 브리프가 부실했던 것이므로, 브리프를 고쳐서 한 번 더만 호출한다.

## 금지

- 승급 사유 없이 습관적으로 현자를 호출하지 않는다.
- 현자에게 파일 수정·커밋·빌드를 시키지 않는다.
- 맥락 없는 한 줄 질문을 던지지 않는다. 브리프 없는 호출은 비싼 추측을 살 뿐이다.
- 사용자 승인 없이 PC 제어 모드를 실행하지 않는다.
- 현자의 답을 검증 없이 사용자에게 결론으로 전달하지 않는다.
