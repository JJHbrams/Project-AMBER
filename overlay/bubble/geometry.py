"""캐릭터 기준 풍선 앵커/배치 계산.

character.py의 CharacterOverlay와 이 풍선들은 모두 순수 tkinter 창(같은 논리 좌표 공간)이라
chat_window.py의 wt(외부 프로세스) 전용 물리↔논리 DPI 변환은 필요 없다. 모니터 작업영역도
character.py._reload_image_for_current_monitor와 동일하게 win32api로 논리 좌표 그대로 얻는다.
"""

import math

import win32api


def angle_to_point(from_x: float, from_y: float, to_x: float, to_y: float) -> float:
    """(from_x, from_y)에서 (to_x, to_y)를 향하는 각도(라디안, 화면 좌표계 —
    0=오른쪽, 양수는 시계방향/아래쪽) — 말풍선 몸통 중심에서 대상 지점을 향한
    꼬리 방향을 매 렌더마다 새로 계산하는 데 쓴다."""
    return math.atan2(to_y - from_y, to_x - from_x)


def get_monitor_work_rect(x: int, y: int) -> tuple[int, int, int, int]:
    """주어진 논리 좌표를 포함하는 모니터의 작업영역(left, top, right, bottom)."""
    try:
        hmon = win32api.MonitorFromPoint((x, y), 2)
        wl, wt, wr, wb = win32api.GetMonitorInfo(hmon)["Work"]
        return wl, wt, wr, wb
    except Exception:
        return 0, 0, 1920, 1080


def get_monitor_rect(x: int, y: int) -> tuple[int, int, int, int]:
    """주어진 논리 좌표를 포함하는 모니터 전체 영역(left, top, right, bottom)."""
    try:
        hmon = win32api.MonitorFromPoint((x, y), 2)
        left, top, right, bottom = win32api.GetMonitorInfo(hmon)["Monitor"]
        return left, top, right, bottom
    except Exception:
        return 0, 0, 1920, 1080


def monitor_bottom_center_pixel(mon_rect: tuple[int, int, int, int]) -> tuple[int, int]:
    """모니터 전체 영역에서 중앙에 가장 가까운 마지막 픽셀 좌표."""
    left, _, right, bottom = mon_rect
    return (left + right - 1) // 2, bottom - 1


def clamp_rect(x: int, y: int, w: int, h: int, mon_rect: tuple[int, int, int, int]) -> tuple[int, int]:
    ml, mt, mr, mb = mon_rect
    x = int(max(ml, min(x, mr - w)))
    y = int(max(mt, min(y, mb - h)))
    return x, y


def default_bubble_width(char_x: int, char_y: int, char_w: int, cfg_bubble: dict) -> int:
    """말풍선/입력창의 기본 폭 — 모니터 폭 비율이 아니라 캐릭터(오버레이 이미지) 크기
    기준으로 정한다. char_w는 이미 해당 환경의 실제 스케일(모니터 크기/DPI 전부 반영된
    결과)이라, 여기서부터 비율만 곱하면 화면 크기와 무관하게 "캐릭터 대비 적당한 크기"가
    나온다 — 모니터 폭의 고정 비율(예: 28%)을 쓰면 4K 등 넓은 모니터에서 캐릭터와
    안 맞게 지나치게 커지는 문제가 있었다.
    """
    ratio = float(cfg_bubble.get("width_to_char_ratio", 2.6))
    min_width = int(cfg_bubble.get("min_width", 160))
    width = max(min_width, int(char_w * ratio))
    ml, _, mr, _ = get_monitor_work_rect(char_x, char_y)
    return min(width, int((mr - ml) * 0.9))  # 모니터 밖으로 나가지 않게 안전판


def tail_side_toward_char(bubble_x: int, bubble_w: int, char_x: int, char_w: int) -> str:
    """말풍선의 최종 위치가 어디든(자동 배치든, 사용자가 드래그해서 옮긴 자리든)
    꼬리가 항상 캐릭터 쪽을 향하도록 — 풍선 중심과 캐릭터 중심을 비교해서 결정한다.
    "left"면 풍선 왼쪽 끝에 꼬리(왼쪽을 가리킴), "right"면 오른쪽 끝(오른쪽을 가리킴)."""
    bubble_center = bubble_x + bubble_w / 2
    char_center = char_x + char_w / 2
    return "right" if char_center > bubble_center else "left"


def place_speech_bubble(
    char_x: int,
    char_y: int,
    char_w: int,
    char_h: int,
    bubble_w: int,
    bubble_h: int,
    cfg_bubble: dict,
) -> tuple[int, int, str, tuple[int, int, int, int]]:
    """대화/입력 풍선의 (x, y, tail_side, mon_rect) 계산.

    캐릭터 위가 아니라 옆(화면 중심의 반대쪽)에 배치한다 — 캐릭터가 모니터 오른쪽
    절반이면 왼쪽으로 펼치고(tail_side="right" — 꼬리가 캐릭터 쪽인 오른쪽을 향함),
    세로로는 캐릭터 상단에서 anchor_y_ratio만큼 내려온 지점을 풍선의 "바닥" 기준점으로
    고정하고 위로만 자라게 한다. side_gap만큼 캐릭터와 간격을 둬서 스프라이트를
    가리지 않는다.

    바닥을 고정점으로 쓰는 이유(세로 중심을 고정하던 이전 방식의 버그): 중심을
    고정하면 응답이 길어져 bubble_h가 커질 때 위/아래로 대칭으로 늘어나면서 바닥이
    계속 아래로 밀려 내려온다("응답이 길어질수록 풍선 하단이 점점 내려온다"는
    사용자 피드백) — _speech_manual_pos(드래그로 옮긴 뒤)가 이미 하단 코너를
    고정점으로 쓰는 것과 똑같은 방식으로 자동배치도 통일한다."""
    side_gap = int(cfg_bubble.get("side_gap", 10))
    anchor_y_ratio = cfg_bubble.get("anchor_y_ratio", 0.30)

    mon_rect = get_monitor_work_rect(char_x + char_w // 2, char_y + char_h // 2)
    ml, mt, mr, mb = mon_rect

    char_center = char_x + char_w // 2
    mon_center = (ml + mr) // 2
    flip_left = char_center > mon_center

    if flip_left:
        x = char_x - side_gap - bubble_w
        tail_side = "right"
    else:
        x = char_x + char_w + side_gap
        tail_side = "left"

    anchor_bottom_y = char_y + int(char_h * anchor_y_ratio)
    y = anchor_bottom_y - bubble_h
    if y < mt:
        y = mt

    x, y = clamp_rect(x, y, bubble_w, bubble_h, mon_rect)
    return x, y, tail_side, mon_rect


def place_input_default(
    char_x: int,
    char_y: int,
    char_w: int,
    char_h: int,
    bubble_w: int,
    bubble_h: int,
    cfg_bubble: dict,
) -> tuple[int, int, str]:
    """입력 풍선의 기본 위치 — 캐릭터 옆(화면 중심 반대쪽) '아래쪽'에 둔다.

    응답 풍선(place_speech_bubble)은 캐릭터 옆 '상단'(anchor_y_ratio≈0.30에서 위로
    자람)에 뜨므로, 입력을 캐릭터 하단 옆에 두면 기본 상태에서 둘이 서로 다른 세로
    영역을 쓴다 → "응답이 입력 자리에서 시작하는 것처럼" 보이던 문제가 사라진다(예전엔
    입력도 place_speech_bubble을 써서 응답과 기본 위치가 완전히 같았다). 입력의 바닥을
    캐릭터 바닥 근처에 맞춘다."""
    side_gap = int(cfg_bubble.get("side_gap", 10))
    mon_rect = get_monitor_work_rect(char_x + char_w // 2, char_y + char_h // 2)
    ml, mt, mr, mb = mon_rect

    char_center = char_x + char_w // 2
    mon_center = (ml + mr) // 2
    if char_center > mon_center:
        x = char_x - side_gap - bubble_w
        tail_side = "right"
    else:
        x = char_x + char_w + side_gap
        tail_side = "left"

    y = char_y + char_h - bubble_h  # 입력 바닥 ≈ 캐릭터 바닥
    x, y = clamp_rect(x, y, bubble_w, bubble_h, mon_rect)
    return x, y, tail_side


def place_thought_bubble(
    char_x: int,
    char_y: int,
    char_w: int,
    char_h: int,
    bubble_w: int,
    bubble_h: int,
    cfg_bubble: dict,
) -> tuple[int, int, tuple[int, int, int, int]]:
    """생각(+도구 상태) 풍선의 (x, y, mon_rect) 계산.

    대화 풍선과 완전히 독립적으로 항상 캐릭터 머리 바로 위 중앙에 배치한다(꼬리는
    항상 아래로 향해 캐릭터를 가리킴 — tail_side="down"). 좌우 반전이나 앵커 비율
    없이 캐릭터 폭 중앙 정렬만 하면 되므로 place_speech_bubble보다 단순하다.
    """
    gap = int(cfg_bubble.get("thought_gap", 6))
    mon_rect = get_monitor_work_rect(char_x + char_w // 2, char_y + char_h // 2)
    ml, mt, mr, mb = mon_rect

    x = char_x + char_w // 2 - bubble_w // 2
    y = char_y - bubble_h - gap
    if y < mt:
        y = mt

    x, y = clamp_rect(x, y, bubble_w, bubble_h, mon_rect)
    return x, y, mon_rect
