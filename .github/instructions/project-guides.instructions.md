---
description: Project coding guidelines, todo monitoring rules, and context for AI agents working on Project Engram
# applyTo: 'Describe when these instructions should be loaded by the agent based on task context' # when provided, instructions will automatically be added to the request context when the pattern matches an attached file
---

<!-- Tip: Use /create-instructions in chat to generate content with agent assistance -->

Provide project context and coding guidelines that AI should follow when generating code, answering questions, or reviewing changes.

## Todo List 관리 지침

- `docs/todo/User Todo list.md`와 `docs/todo/todo.md` 를 주기적으로 확인하고, 내용이 변경되었으면 `docs/todo/todo.md` 에 반영하여 갱신한다.
- 사용자가 todo와 무관한 작업을 지시했을 때, 미결 항목이 있으면 작업 전후 한 줄로 remind 한다.
- 완료된 항목은 완료 후 7일이 지나면 `todo.md` 에서 삭제한다.

## 커밋 지침

- 개발 repo 커밋 할 때는 change log 를 업데이트할 것
- 배포 repo 커밋할 때는 release note 를 업데이트할 것
