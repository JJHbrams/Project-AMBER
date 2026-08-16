---
id: git-branch-guide
title: Git 브랜치 관리 규칙
note_type: concept
tags:
  - git
  - workflow
  - branch
created: __DATE__
updated: __DATE__
summary: 단순 유지보수 예외와 main/dev 보호 정책. feat/fix/refactor prefix 브랜치 및 worktree 사용 기준.
---

# Git 브랜치 관리 규칙

> 실행 진입점은 `engram-task-workflow` skill이다. 이 문서는 브랜치 판단의 상세 정책을 제공한다.

## 기본 규칙

1. `main`/`master`, `dev` 브랜치에 **기능·코드 변경 직접 커밋 금지**.
   - 예외: 아래 범위의 단순 유지보수는 현재 브랜치에서 처리한다.
     - `docs/**`, 루트 `README*.md`, `CHANGELOG.md`의 오탈자·표현·링크 수정
     - 런타임 동작, 테스트, 빌드, 의존성, CI, 배포, agent 정책에 영향이 없는 소규모 chore
   - `AGENTS.md`, `.github/**`, workflow/skill, 설정·의존성·빌드 파일은 이름이 docs/chore여도 예외가 아니다.

2. 새 작업 시작 시 작업 성격을 반영한 브랜치 생성:

   | prefix      | 용도      |
   | ----------- | --------- |
   | `feat/`     | 신규 기능 |
   | `fix/`      | 버그 수정 |
   | `refactor/` | 리팩토링  |
   | `docs/`     | 구조 변경·대규모 작성 등 단순 유지보수를 넘는 문서 작업 |
   - 기준 브랜치: `dev` (또는 명시된 브랜치)

3. 현재 브랜치와 성격이 크게 다른 작업 → 파생 브랜치 생성:
   - 예: `feat/overlay-gui` → `feat/overlay-gui-crash-fix`
   - 파생 브랜치에서 충분히 검증 후 원 브랜치에 병합.

4. 작업 완료 후 `dev`에 병합. `main`/`master` 병합은 안정 확인 후 별도 진행.

## 실행 절차

1. 파일을 수정하기 전에 현재 브랜치와 worktree 상태를 확인한다.
2. 보호 브랜치에서 단순 유지보수가 아닌 작업이면 변경 전에 작업 브랜치를 생성한다.
3. 기존 미커밋 변경은 삭제하거나 자동 stash하지 않고 새 브랜치로 그대로 승계한다.
4. 완료 후 검증과 activity 기록은 `engram-task-workflow`에서 처리한다.

## Worktree 사용 기준

worktree는 작업 종류가 아니라 격리가 실제로 필요한지를 기준으로 사용한다. 다음 상황에서는 단순 브랜치 전환 대신 독립 branch+worktree를 사용한다.

- 여러 에이전트가 독립 작업을 병렬 수행할 때
- 현재 worktree에 다른 작업의 미커밋 변경이 있을 때
- 장시간 빌드·테스트·실험이 기존 작업 상태를 방해할 수 있을 때

### 규칙

1. 에이전트마다 고유한 branch와 worktree를 할당한다.
2. worktree는 저장소 밖의 형제 경로 `../<repo>-worktrees/<branch-slug>`를 권장한다.
3. 같은 파일을 강하게 공유하는 작업은 병렬화하지 않는다.
4. dirty 변경을 다른 worktree로 임의 복사하거나 patch 적용하지 않는다.
5. 통합·최종 검증은 조정자 worktree에서 수행한다.
6. dirty worktree는 강제 제거하지 않는다.

## 예외

사용자가 직접 브랜치 없이 커밋하도록 명시한 경우.
