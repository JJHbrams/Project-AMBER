# 입력풍선 압축 피드백 — 2026-09-12

기존 0004 명세에 대한 사용자 승인 변경이다. 전체 말풍선 기능의 미검증 항목을 대체하지 않는다.

## 이번 수용기준

### 추가 복원 계약: 설정 글꼴·자동 크기·상대 위치

사용자가 승인한 기존 기능 복원이며 아래 항목 모두 중요하다. 기존 입력/큐 UX, private IPC, provider 상태는 유지한다.

| ID | 사용자에게 보이는 결과 | 대상 런타임 / 증거 |
| --- | --- | --- |
| R1 | 짧은 응답은 작게, 긴 응답은 설정 최대 폭/높이까지 자동 확장하고 이후 스크롤 | 실제 native Windows WebView의 짧음→길음→짧음 크기, 최대치/overflow 측정 |
| R2 | 설정 GUI의 font_family/font_size 및 0 자동 크기를 재시작 없이 반영 | 기존 GUI 저장 경로 연결 테스트, 실제 native computed font/크기 재측정, 사용자 입력/큐 보존 |
| R3 | 수동 배치한 입력·응답·생각 풍선이 캐릭터 이동/크기 변경을 상대 위치로 추종 | 실제 native user_drag 뒤 anchor 이동/확대, negative 좌표와 passive/DPI 회귀 |
| R4 | 수동 크기 조절은 자동 크기와 독립적으로 유지되고 기존 Flip·전송 동작은 보존 | 실제 native 수동 resize 및 기존 full smoke |

확정 사실: 기존 Tk는 모니터 작업영역 높이 비율 및 character width 비율로 상한을 정하고 설정 글꼴을 사용한다. 네이티브는 현재 고정 치수·CSS 글꼴·절대 수동 좌표라 이를 연결하는 작업이다. 미검증 항목은 완료로 표시하지 않는다.

복원 구현 및 한정 검증 결과:

- R1: 실제 native 짧은 응답 220×175 → 긴 응답 312×422 → 짧은 응답 복귀 PASS. 생각풍선 185×179 → 298×316 및 overflow PASS. 0 높이 비율은 설정 상한 없음으로 해석하되 모니터 작업영역은 안전 상한으로 유지한다.
- R2: GUI와 같은 `bubble.font_family/font_size`를 private snapshot으로 전달. Tk 양수 font size는 pt라 CSS px=pt×96/72 변환; Arial 18pt → computed 24px PASS. 실제 `_reload_config` 콜백을 모킹한 통합 테스트에서 폰트만 변경할 때 provider/session 유지 PASS. 실제 GUI를 마우스로 저장하는 경로는 아직 별도 미검증.
- R3: 실제 OS 헤더 드래그 후 합성 캐릭터 anchor +100,+50 이동 시 native HWND 위치도 같은 만큼 이동 PASS. 입력/응답/생각의 앵커 확대·음수좌표·DPI는 host 회귀 테스트로 보완. bundled drag 및 replace geometry 이벤트와 기존 100ms loop의 앵커 변경 감지까지 연결했다.
- R4: 실제 OS 수동 크기 220×223 → 280×263 PASS. 수동 크기는 내용 자동 측정과 독립적. 설정 글꼴이 커지면 입력풍선도 내용에 필요한 높이로 늘지만 Flip 양면은 같은 크기 유지.
- host 26, settings reload integration 3, JS 6 PASS. Debug native build 및 `--exercise --restoration --os-resize` 전체 PASS, dispatch 3/held/owned child reaped 확인. 캡처 `.tmp/bubble-restoration-final/`.
- 이번 한정 검증은 전체 16 AC, 실제 provider 이미지/승인, 150/200% 다중모니터 시각 검증, clean installer 완료를 뜻하지 않는다.

| ID | 사용자에게 보이는 결과 | 대상 런타임 / 증거 |
| --- | --- | --- |
| C1 | 상단 제목과 하단 단축키 설명 없이 작은 입력풍선 | Windows native 실제 캡처, 높이 측정 |
| C2 | 최근 입력 슬롯 오른쪽 아이콘 Flip, 입력칸 오른쪽 작은 전송 버튼 | Windows native 실제 캡처 및 DOM 좌표 |
| C3 | 버튼·입력·읽기 영역 이외 배경 드래그 | 실제 Windows 마우스 드래그, 버튼/입력 회귀 |
| C4 | 입력풍선 본체와 꼬리의 이어진 외곽선과 채색 | Windows native 캡처 시각 확인 |
| C5 | 닫았다 연 뒤 새 입력은 idle일 때 실행하며 기존 보류 요청은 유지 | 실제 native UI + 합성 provider 순서 검증, host 회귀 테스트 |
| C6 | 말풍선 작업표시줄 제외, 제목줄·최소화·최대화 제거 | 실제 Windows 창 스타일 및 작업표시줄 시각 확인 |

기존 active/unknown 요청은 새 제출만으로 중단하거나 우회하지 않는다. 보류한 기존 요청을 재개하려면 명시적 대기열 재개를 사용한다.

## 검증 상태

### 추가 피드백: 포커스·툴팁·동일 크기 Flip

- 후속 실제 Windows 마우스 검증: `--exercise --os-resize` PASS. 테스트 소유 응답창 손잡이를 드래그해 460×220에서 520×260으로 변경하고 host의 manual_size 반영 확인. 포인터 복원 및 테스트 자식 종료 확인. 아래의 OS 리사이즈 미검증 기록은 이 증거로 보완됐다.

- textarea 노란 outline 대신 editor 전체의 은은한 focus-within 테두리 적용.
- 버튼별 잘리는 pseudo tooltip 대신 body fixed 레이어에 안전한 textContent 및 풍선 안쪽 좌표 제한 적용.
- Flip의 310px 확장 제거. 동일 크기를 유지하며 전체 큐 목록은 세로 스크롤, 선택 항목 가시성 유지.
- 응답/생각 창 크기 조절 손잡이를 둥근 모서리 안쪽으로 이동. 실제 OS 마우스 리사이즈는 미검증.
- release build PASS, JS 6 / host 22 PASS. 격리 실제 Windows WebView smoke PASS: 입력 outline 없음, Flip/응답 닫기 tooltip 사각형 경계, Flip 전후 동일 크기, queue overflow 및 기존 3 dispatch 시나리오. hover는 DOM PointerEvent로 구동했고 OS 마우스 검증을 대체하지 않는다.
- 캡처: `.tmp/bubble-tooltip-qa/input-tooltip.png`, `speech-tooltip.png`, `queue-actions.png`. 실행 중 사용자 창은 교체하지 않았다.

구현 반영. host 22개, JS 6개 테스트 PASS. Debug native 빌드 PASS.

격리 Windows native smoke PASS: 세 창, 합성 provider 3회 dispatch, 닫기/재열기 후 새 입력 실행, 이전 대기열 보류 유지, 테스트 자식 종료 확인. 입력 높이 220px 이하 및 전송 버튼 우측 DOM 좌표 확인. 실제 캡처는 185px 높이이며 `.tmp/bubble-compact-qa/compact-input.png`에 저장했다.

Win32 실제 세 창의 caption/min/max 비트 및 EX_APPWINDOW 제거를 확인했다. Tauri 설정만으로 스타일이 남는 경로가 있어 geometry 적용 뒤 TOOLWINDOW 스타일을 재적용한다. 기존 사용자 release 창은 파일이 잠겨 있어 교체하거나 종료하지 않았다.

남은 검증: 실제 마우스 드래그(C3), 작업표시줄 시각 확인(C6), 현재 실행본 재시작 후 적용, 실제 provider 응답. 전체 완료로 간주하지 않는다.
