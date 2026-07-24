---
title: 사용자 작성 to do list
tags:
  - todo
  - roadmap
  - development
  - tracking
  - engram
created: 2026-04-19
updated: 2026-05-01
status: active
description: copilot cli todo list
project: ICCC
related:
  - "[architecture](obsidian://open?vault=Workspace&file=LLM_project%2FICCC_for_ARONA%2Fdocs%2Farchitecture)"
  - "[readme](obsidian://open?vault=Workspace&file=LLM_project%2FICCC_for_ARONA%2FREADME)"
---

## Todo list

### core 리팩토링

- core 라는 디렉토리아래 너무 한덩어리로 뭉쳐있음
- [ ] 기능단위로 분류
- [ ] 너무 긴 단일 모듈은 패키지화

### 튜토리얼 추가

- [ ] 최초 설치 시 튜토리얼 제공
  - 튜토리얼 단계별 flag 둬서 세션 연속성 테스트도 가능하도록
  - 튜토리얼 시나리오
    1. 이름이 안정해졌으면 이름 입력
    2. 설정>페르소나 설정 안내
    3. 위키 튜토리얼
       1. Project-AMBER 분석해서 위키화하는 튜토리얼
       2. llm wiki 조사 & 보고서 작성 튜토리얼
       3. 마지막에 사용자에게 위키 작성 팁 제공
    4. 세션 연속성 튜토리얼
       1. 튜토리얼 세션 내용정리 & 메모리화 안내
       2. 세션 끝내고 다음 세션 시작 시 이전 세션 내용 파악 질문해서 연속성 시연
       3. 마지막에 사용자에게 세션 끝, 혹은 작업 끝 단위마다 메모리화 및 위키화 추천

### Setting 개선

- [x] 전역 설정
  - engram 이 임의의 cli 공급자(선택)가 시작될 때 무조건 오버라이드되도록할 지 여부
  - 단 최초 context 소모량에 대한 경고 필수
- [x] 재부팅 시 자동 실행 여부
- [ ] claude code (ollama) 선택지 추가(설치할 때도 마찬가지)

### Discord 봇

- [x] 기본은 동일 채널에서 동일 세션 지속 사용
- [x] 사용자가 명시적으로 "새 세션"을 요청하면 새 세션 생성 후 그 세션을 이어서 사용 (2026-05-01 적용)
- [ ] 세션 연속성 고도화(장기 세션 context rot 완화, 롤오버/compact 정책)
  - 추후 논의
- [ ] 입력 큐처리
- [ ] 사용자가 봇 응답에 이모티콘 반응 보냈을 때 응답

### UI/UX 개선

- [ ] 이미지 입력 기능 추가
  - [ ] 이미지 첨부
  - [ ] 이미지 클립보드

### 메모리

- [ ] 오래된 메모리 가소성
  - 필요한가?
- [ ] 최신 내용과 위배되는 노드 정리
- [ ] 노드가 너무 많아지면 검색 질에 악영향을 미칠까?
