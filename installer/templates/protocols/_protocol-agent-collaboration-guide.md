---
id: agent-collaboration-guide
title: 에이전트 협업 및 세션 분리 가이드
note_type: concept
tags:
  - workflow
  - multi-agent
  - worktree
created: __DATE__
updated: __DATE__
summary: 다중 에이전트의 역할 분리, 작업별 branch+worktree 격리, 통합 및 정리 절차.
---

# 에이전트 협업 및 세션 분리 가이드

> 병렬 에이전트는 대화만 분리하는 것이 아니라 파일시스템과 Git 상태도 작업별로 격리한다.

## 역할

| 역할 | 책임 |
| --- | --- |
| Orchestrator | 작업 분해, 의존성 판단, worktree 할당, 결과 통합 |
| Planner | 실행 가능한 Task Spec 작성, fresh acceptance audit (읽기 전용) |
| Coder | 할당된 worktree와 branch에서만 구현 |
| Fresh planner acceptance reviewer | 구현과 분리되어 criterion별 증거를 판정 |

## 완료 판정

1. 조정자는 원 사용자 요청을 criterion별 acceptance contract로 만든다. 각 criterion에는
   사용자에게 보이는 결과, intended runtime, 검증 방법, 증거, critical 여부를 적는다.
2. criterion을 삭제·축소·대체하거나 검증을 생략하려면 `[SCOPE_DELTA]`로 영향과 이유를
   공개한다. material delta는 사용자 승인 전 실행하지 않는다.
3. 구현 후에는 구현자가 아닌 fresh Planner가 `[ACCEPTANCE_AUDIT]`에서 각 criterion을
   `PASS` / `FAIL` / `UNVERIFIED`로 판정한다. 구현자와 조정자가 자기검수로 대체하지 않는다.
4. UI, CLI, installer, integration은 intended runtime에서 실제 실행하지 않았다면
   `UNVERIFIED`다. 파일 존재, diff, AST, unit test는 사용자 경로의 대체 증거가 아니다.
5. critical criterion이 모두 `PASS`이고 통합 후 재검증했을 때만 완료로 보고한다. 그 외에는
   `partial`과 남은 검증을 보고한다.

## Worktree 판단

다음 조건이면 작업별 branch+worktree를 만든다.

- 둘 이상의 에이전트가 독립 작업을 동시에 수행한다.
- 현재 worktree가 다른 작업의 변경으로 dirty 상태다.
- 빌드·테스트·실험이 다른 작업 상태와 충돌할 수 있다.

같은 파일을 강하게 공유하거나 선행 작업 결과가 필요한 작업은 병렬화하지 않는다.

## 생성 및 할당

1. 깨끗한 통합 기준 브랜치를 선택한다.
2. 작업 성격에 맞는 고유 branch를 만든다.
3. 저장소 밖의 형제 경로에 worktree를 생성한다.
   - 권장: `../<repo>-worktrees/<branch-slug>`
4. 에이전트에게 절대 경로, branch, 담당 범위, 완료 조건을 전달한다.
5. 하나의 branch와 worktree는 한 에이전트만 소유한다.

## 통합 및 정리

1. 각 worktree에서 담당 검증을 완료한다.
2. 조정자 worktree에서 변경을 검토하고 통합한다.
3. 통합 결과를 다시 검증한다.
4. clean 상태를 확인한 worktree만 제거한다.
5. dirty worktree를 강제로 제거하거나 사용자 변경을 폐기하지 않는다.

## 관련 노트

- [[git-branch-guide]]
- [[directive-workflow]]
