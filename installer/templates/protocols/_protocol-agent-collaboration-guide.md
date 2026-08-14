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
| Planner | 실행 가능한 Task Spec 작성 |
| Coder | 할당된 worktree와 branch에서만 구현 |
| Reviewer | 변경 검토와 통합 전 검증 |

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
