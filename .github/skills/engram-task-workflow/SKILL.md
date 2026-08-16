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
5. 다음의 **단순 유지보수**는 현재 브랜치를 유지하고 새 branch/worktree를 만들지 않는다.
   - `docs/**`, 루트 `README*.md`, `CHANGELOG.md`의 오탈자·표현·링크 같은 작은 문서 수정
   - 런타임 동작, 테스트, 빌드, 의존성, CI, 배포, agent 정책에 영향을 주지 않는 소규모 chore
   - `AGENTS.md`, `.github/**`, workflow/skill, 설정·의존성·빌드 파일은 이름이 docs/chore여도 단순 유지보수로 보지 않는다.
6. 단순 유지보수가 아닌 작업은 변경 전에 작업 성격에 맞는 브랜치를 생성한다.
   - 신규 기능: `feat/<slug>`
   - 버그 수정: `fix/<slug>`
   - 리팩토링: `refactor/<slug>`
   - 문서 전용: `docs/<slug>`
   - 테스트 전용: `test/<slug>`
7. 기존 미커밋 변경이 있으면 삭제·stash·reset하지 않는다.
   - 새 작업이 기존 변경과 같은 작업이면 새 브랜치가 변경을 그대로 승계할 수 있다.
   - 다른 작업이면 깨끗한 기준 브랜치에서 별도 worktree를 만든다.
   - dirty 변경을 새 worktree로 임의 복사하거나 patch 적용하지 않는다.

## Repository policy advisor

세션 bootstrap은 현재 cwd가 Git 저장소이고 정책 가이드가 켜져 있으면 Engram Git advisor를
멱등 설치하고 repo-local `merge.ff=false`를 관리한다.

- frozen: `engram-overlay.exe --role git-hook install --repo <path>`
- source: `python engram_overlay_entry.py --role git-hook install --repo <path>`
- 조회·제거: 같은 명령의 `install`을 `status` 또는 `uninstall`로 교체한다.
- `uninstall`은 공용 Git 디렉터리에 opt-out marker를 남긴다. 명시적 `install`만 marker를
  제거하므로 다음 세션에 원치 않게 재설치되지 않는다.
- 정책 가이드 OFF 또는 uninstall 시 Engram이 적용하기 전의 repo-local `merge.ff`를 복원한다.
- Agent의 branch 통합은 `git merge --no-ff <branch>`를 사용한다. `--ff-only`는 금지한다.
- 기존 사용자 `pre-commit`이나 custom `core.hooksPath`가 있으면 덮어쓰지 않고 중단한다.
- managed wrapper는 Git Bash·WSL·Linux에서 동작하며 runtime/backend 오류가 나도 commit을 허용한다.
- 보호 브랜치에서도 경고·추천·audit만 제공하고 사람이나 agent의 commit을 차단하지 않는다.
- 명시적 maintenance 맥락을 기록하려면 해당 호출에 `ENGRAM_CHORE_INTENT=1`과 선택적
  `ENGRAM_CHORE_REASON`을 전달한다. commit message만으로 맥락을 추론하지 않는다.

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
- 단순 유지보수 예외를 코드·설정·CI·빌드·의존성·agent 정책 변경에 확대 적용하지 않는다.
- 독립 작업을 기존 dirty worktree에 섞지 않는다.
- 같은 branch를 여러 worktree나 에이전트가 공동 소유하게 하지 않는다.
- Wiki 문서 경로만 보고 절차를 추측하지 않는다. 필요한 원문은 MCP 도구로 읽는다.
- 조회·설명만 하는 요청에는 브랜치를 만들거나 activity를 기록하지 않는다.
