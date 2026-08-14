---
name: engram-task-workflow
description: "저장소를 변경하는 개발 작업의 시작·종료 절차를 강제한다. 트리거: 코드 수정, 파일 수정, 구현, 버그 수정, 리팩토링, 빌드, 테스트, Git, commit, branch, merge, PR. 조회·설명만 하는 작업에는 실행하지 않는다."
argument-hint: "수행할 개발 작업"
---

# Engram task workflow

코드나 저장소 상태를 바꾸기 전에 선행 지식과 브랜치를 확인하고, 완료 후 활동을 기록한다.

## 작업 시작

1. `kg_wiki_reminder(query=<작업 내용 한 줄 요약>)`를 호출한다.
   - 관련 기록이 있으면 필요한 노드를 읽고 작업에 반영한다.
   - 단순히 hit가 있다는 이유만으로 사용자 승인을 다시 요구하지 않는다. 작업 방향을 바꿀 중요한 충돌이 있을 때만 질문한다.
2. Git 저장소라면 수정 전에 현재 브랜치, worktree 목록, 미커밋 상태를 확인한다.
3. 다음 중 하나면 현재 디렉토리에서 작업하지 말고 독립 branch+worktree를 만든다.
   - 여러 에이전트가 서로 독립적인 작업을 병렬 수행한다.
   - 현재 worktree에 다른 작업의 미커밋 변경이 있다.
   - 장시간 빌드·테스트 또는 실험이 기존 작업 상태를 방해할 수 있다.
4. `main`, `master`, `dev`에서 작업 중이면 `kg_read_note("git-branch-guide")`를 읽는다.
5. 작업이 단순 chore가 아니면 변경 전에 작업 성격에 맞는 브랜치를 생성한다.
   - 신규 기능: `feat/<slug>`
   - 버그 수정: `fix/<slug>`
   - 리팩토링: `refactor/<slug>`
   - 문서 전용: `docs/<slug>`
   - 테스트 전용: `test/<slug>`
6. 기존 미커밋 변경이 있으면 삭제·stash·reset하지 않는다.
   - 새 작업이 기존 변경과 같은 작업이면 새 브랜치가 변경을 그대로 승계할 수 있다.
   - 다른 작업이면 깨끗한 기준 브랜치에서 별도 worktree를 만든다.
   - dirty 변경을 새 worktree로 임의 복사하거나 patch 적용하지 않는다.

## 병렬 worktree

1. 에이전트마다 고유한 branch와 worktree를 할당한다.
2. worktree 경로는 저장소 밖의 형제 디렉토리를 사용한다.
   - 권장: `../<repo>-worktrees/<branch-slug>`
3. 에이전트에게 branch, 절대 worktree 경로, 담당 범위를 함께 전달한다.
4. 같은 파일을 강하게 공유하는 작업은 병렬화하지 않고 순차 처리한다.
5. 통합은 조정자 worktree에서 수행하고, 검증 후 깨끗한 worktree만 제거한다.
6. dirty worktree를 강제 제거하지 않는다.

## 작업 완료

1. 변경 범위를 커버하는 가장 작은 기존 테스트·빌드·lint를 실행한다.
2. 의미 있는 코드 수정·리서치·빌드 작업이면 `engram_log_activity`를 호출한다.
3. Wiki를 생성·수정해야 하면 별도로 `engram-wiki-workflow` skill을 실행한다.

## 금지

- `main`, `master`, `dev`에서 feat/fix/refactor/test 작업을 직접 수행하거나 커밋하지 않는다.
- 독립 작업을 기존 dirty worktree에 섞지 않는다.
- 같은 branch를 여러 worktree나 에이전트가 공동 소유하게 하지 않는다.
- Wiki 문서 경로만 보고 절차를 추측하지 않는다. 필요한 원문은 MCP 도구로 읽는다.
- 조회·설명만 하는 요청에는 브랜치를 만들거나 activity를 기록하지 않는다.
