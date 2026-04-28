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
summary: main/dev 직접 커밋 금지 정책. feat/fix/refactor prefix 브랜치 생성 규칙 및 병합 절차.
---

# Git 브랜치 관리 규칙

## 기본 규칙

1. `main`/`master`, `dev` 브랜치에 **직접 커밋 금지**.
   - 예외: chore (의존성 업데이트, 문서 오탈자 등 단순 유지보수)

2. 새 작업 시작 시 작업 성격을 반영한 브랜치 생성:

   | prefix      | 용도      |
   | ----------- | --------- |
   | `feat/`     | 신규 기능 |
   | `fix/`      | 버그 수정 |
   | `refactor/` | 리팩토링  |
   | `docs/`     | 문서 작업 |
   - 기준 브랜치: `dev` (또는 명시된 브랜치)

3. 현재 브랜치와 성격이 크게 다른 작업 → 파생 브랜치 생성:
   - 예: `feat/overlay-gui` → `feat/overlay-gui-crash-fix`
   - 파생 브랜치에서 충분히 검증 후 원 브랜치에 병합.

4. 작업 완료 후 `dev`에 병합. `main`/`master` 병합은 안정 확인 후 별도 진행.

## 예외

사용자가 직접 브랜치 없이 커밋하도록 명시한 경우.
