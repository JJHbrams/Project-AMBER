---
id: activity-log-guide
title: Activity Log 기록 규칙
note_type: concept
tags:
  - activity
  - log
  - workflow
created: __DATE__
updated: __DATE__
summary: engram_log_activity 호출 시점과 형식. 코드수정·리서치·빌드 완료 시 반드시 기록. actor=현재 CLI명 필수.
---

# Activity Log 기록 규칙

> `activity_log`는 engram 부재 중 외부 협력자가 수행한 작업을 3인칭으로 기록하는 보고서.
> `engram_prepare_reflection` 단계에서 소비된다.

## 호출 시점

다음 작업 완료 시 **반드시** `engram_log_activity` 호출:

- 코드 수정 / 버그 수정 / 리팩토링 완료
- 리서치 / 설계 결정 완료
- 빌드·테스트·배포 등 환경 작업 완료
- 세션 종료(`close_session`) 직전

**제외**: 단순 파일 조회, 검색, 질의응답.

## 파라미터

| 파라미터  | 필수 | 설명                                                                        |
| --------- | ---- | --------------------------------------------------------------------------- |
| `actor`   | ✅   | 현재 CLI 공급자명 (예: `"copilot"`, `"claude-code"`, `"gemini"`, `"goose"`) |
| `action`  | ✅   | 수행한 작업 한 줄 요약 (3인칭, 과거형)                                      |
| `detail`  | 선택 | 변경 내용, 파일, 이유 등 상세                                               |
| `project` | 선택 | 작업한 프로젝트명 (레포명 등)                                               |

## 예시

```python
engram_log_activity(
    actor="copilot",
    action="core/directives.py sanitize 한도 500→1500으로 상향, protocol 문서 6개 생성",
    detail="directive 내용 truncated 문제 해결 — 상세 규칙을 protocols/ 문서로 분리",
    project="Project_Engram"
)
```
