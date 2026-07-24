"""말풍선 배경(몸통+꼬리)을 Pillow RGBA 이미지로 그려서 크로마키 창에 얹는다.

character.py가 캐릭터 스프라이트에 쓰는 것과 동일한 방식(PIL로 RGBA 합성 →
Image.new("RGB", size, (1,1,1)) 위에 alpha 마스크로 paste → ImageTk.PhotoImage →
-transparentcolor #010101 창)을 말풍선 도형에도 적용한다. Canvas 폴리곤으로 둥근
사각형+삼각형 꼬리를 따로 그리던 이전 방식은 (1) 크로마키 이진 투명이라 가장자리
계단이 남고 (2) 꼬리가 몸통과 이음새로 분리돼 보이는 "급조 형상"이었다. 여기서는
몸통과 꼬리를 하나의 마스크에 그려 합집합으로 매끄럽게 잇고, 슈퍼샘플 후 LANCZOS
다운스케일로 안티에일리어싱한다.

이 모듈은 PIL만 의존한다(shapes.py를 import 하지 않음 → 순환 방지). 색·기하 헬퍼는
작고 순수해서 여기에 자체 사본을 둔다. margin(=shapes.TAIL_REACH)은 호출부가 넘긴다.
"""

from __future__ import annotations

import math

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageTk


def _hex_to_rgb(hex_color: str) -> tuple:
    hex_color = hex_color.lstrip("#")
    return int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)


def _lighten_rgb(rgb: tuple, factor: float) -> tuple:
    r, g, b = rgb
    return (
        int(r + (255 - r) * factor),
        int(g + (255 - g) * factor),
        int(b + (255 - b) * factor),
    )


def _tail_exit_point(body_x0, body_y0, body_w, body_h, angle_rad):
    """몸통 중심에서 angle_rad 방향 직선이 몸통 사각 경계를 뚫는 점(shapes와 동일 로직)."""
    dx, dy = math.cos(angle_rad), math.sin(angle_rad)
    cx, cy = body_x0 + body_w / 2, body_y0 + body_h / 2
    half_w, half_h = body_w / 2, body_h / 2
    candidates = []
    if dx > 1e-6:
        candidates.append(half_w / dx)
    elif dx < -1e-6:
        candidates.append(-half_w / dx)
    if dy > 1e-6:
        candidates.append(half_h / dy)
    elif dy < -1e-6:
        candidates.append(-half_h / dy)
    t = min(candidates) if candidates else 0
    return cx + t * dx, cy + t * dy, dx, dy


def _build_silhouette_mask(size_s, margin_s, body_w_s, body_h_s, radius_s, angle_rad, tail_base_s, tail_len_s):
    """몸통(둥근 사각형) + 꼬리를 한 L 마스크에 합집합으로 그린다.

    꼬리는 밑변을 몸통 경계 "안쪽"(-방향)으로 밀어넣어 삼각형 밑변이 몸통에 완전히
    파묻히게 한다 → 몸통과 이음새 없이 하나의 실루엣으로 이어진다(이전엔 밑변이 정확히
    경계에 얹혀 있고 끝에 큰 원까지 붙어서 "막대사탕 꼭지"처럼 어색했다). 끝은 살짝만
    둥근 뾰족점으로 마무리한다."""
    mask = Image.new("L", size_s, 0)
    d = ImageDraw.Draw(mask)
    bx0, by0 = margin_s, margin_s
    bx1, by1 = bx0 + body_w_s, by0 + body_h_s
    d.rounded_rectangle([bx0, by0, bx1, by1], radius=radius_s, fill=255)

    ex, ey, dx, dy = _tail_exit_point(margin_s, margin_s, body_w_s, body_h_s, angle_rad)
    perp_x, perp_y = -dy, dx
    # 밑변 중심을 몸통 안쪽으로 밀어넣어(파묻어) 이음새 제거.
    base_cx = ex - dx * tail_base_s
    base_cy = ey - dy * tail_base_s
    p1 = (base_cx + perp_x * tail_base_s, base_cy + perp_y * tail_base_s)
    p2 = (base_cx - perp_x * tail_base_s, base_cy - perp_y * tail_base_s)
    # 끝을 살짝 안쪽/옆으로 굽혀 부드러운 곡선 느낌(직선 삼각형보다 자연스러움).
    apex = (ex + dx * tail_len_s, ey + dy * tail_len_s)
    near_apex = (apex[0] - perp_x * tail_base_s * 0.25, apex[1] - perp_y * tail_base_s * 0.25)
    d.polygon([p1, near_apex, apex, p2], fill=255)
    tip_r = max(tail_base_s * 0.18, 1.5)
    d.ellipse([apex[0] - tip_r, apex[1] - tip_r, apex[0] + tip_r, apex[1] + tip_r], fill=255)
    return mask


def _erode(mask: Image.Image, px: float) -> Image.Image:
    """마스크를 px 픽셀만큼 침식(안쪽으로 줄임) — blur 후 고임계로 근사(MinFilter보다 빠름)."""
    if px <= 0:
        return mask
    return mask.filter(ImageFilter.GaussianBlur(px)).point(lambda v: 255 if v >= 170 else 0)


def build_bubble_flat(
    body_w: int,
    body_h: int,
    angle_rad: float,
    bg: str,
    outline: str,
    *,
    radius: int = 16,
    margin: int = 18,
    glow: bool = True,
    supersample: int = 3,
    tail_base: int = 8,
    tail_len: int = 16,
    outline_w: int = 2,
    glow_radius: int = 5,
    glow_alpha_floor: int = 34,
) -> "Image.Image":
    """크로마키(#010101=(1,1,1)) 배경으로 평탄화한 RGB PIL 이미지를 반환한다. Tk 없이
    픽셀 검사가 가능해서 build_bubble_photo와 분리(테스트용). 색은 전부 인자로 받아
    테마색을 그대로 쓴다(하드코딩 금지). 크로마키 fringing 완화 = 슈퍼샘플+LANCZOS로
    가장자리를 얇게 유지 + 최종 알파의 옅은 값(glow_alpha_floor 미만)을 0으로 스냅해
    바깥 헤일로를 완전 투명으로 떨군다."""
    total_w = body_w + margin * 2
    total_h = body_h + margin * 2
    # 슈퍼샘플은 안티에일리어싱 품질을 올리지만 비용이 면적²로 커진다 — 스트리밍 중
    # 매 델타마다 몸통이 커지며 재생성되므로, 큰 말풍선은 배율을 낮춰 렉을 막는다
    # (큰 이미지는 가장자리 대비 면적이 커서 낮은 배율로도 충분히 매끄럽다).
    S = max(1, int(supersample))
    area = total_w * total_h
    if area > 500_000:
        S = min(S, 1)
    elif area > 200_000:
        S = min(S, 2)
    size_s = (total_w * S, total_h * S)

    mask = _build_silhouette_mask(
        size_s, margin * S, body_w * S, body_h * S, radius * S, angle_rad, tail_base * S, tail_len * S
    )

    bg_rgb = _hex_to_rgb(bg)
    outline_rgb = _hex_to_rgb(outline)
    img = Image.new("RGBA", size_s, (0, 0, 0, 0))

    # 1) 글로우(후광) — 몸통 뒤에 깔아 가장자리 바깥으로만 은은하게 남게 한다.
    if glow:
        glow_alpha = mask.filter(ImageFilter.GaussianBlur(glow_radius * S)).point(lambda v: int(v * 0.7))
        glow_layer = Image.new("RGBA", size_s, (*_lighten_rgb(outline_rgb, 0.5), 0))
        glow_layer.putalpha(glow_alpha)
        img = Image.alpha_composite(img, glow_layer)

    # 2) 몸통 채움(평면 bg — 위 텍스트 위젯 배경과 정확히 일치시켜 이음새 제거) +
    #    가장자리 안쪽으로 살짝 어두운 비네트로 밋밋함만 덜어냄.
    fill = Image.new("RGBA", size_s, (*bg_rgb, 0))
    fill.putalpha(mask)
    img = Image.alpha_composite(img, fill)

    edge_only = ImageChops.subtract(mask, _erode(mask, outline_w * S * 1.5))
    vignette = Image.new("RGBA", size_s, (*_lighten_rgb(bg_rgb, -0.06), 0))
    vignette.putalpha(edge_only.point(lambda v: int(v * 0.5)))
    img = Image.alpha_composite(img, vignette)

    # 3) 외곽선 — 마스크에서 얇은 밴드를 추출해 몸통+꼬리를 하나의 연속 스트로크로.
    outline_band = ImageChops.subtract(mask, _erode(mask, outline_w * S))
    outline_layer = Image.new("RGBA", size_s, (*outline_rgb, 0))
    outline_layer.putalpha(outline_band)
    img = Image.alpha_composite(img, outline_layer)

    # 4) 다운스케일(여기서 안티에일리어싱) → 알파 셰이핑(fringing 완화) → 크로마 평탄화.
    img = img.resize((total_w, total_h), Image.LANCZOS)
    r, g, b, a = img.split()
    a = a.point(lambda v: 0 if v < glow_alpha_floor else v)
    flat = Image.new("RGB", (total_w, total_h), (1, 1, 1))
    flat.paste(Image.merge("RGB", (r, g, b)), mask=a)
    return flat


def build_bubble_photo(body_w: int, body_h: int, angle_rad: float, bg: str, outline: str, **kwargs) -> tuple:
    """말풍선 배경을 그려 (photo, total_w, total_h, body_x0, body_y0) 반환.
    뒤 4개는 shapes._draw_speech_shell과 동일 계약(total=body+2*margin, body 원점=margin)."""
    margin = kwargs.get("margin", 18)
    flat = build_bubble_flat(body_w, body_h, angle_rad, bg, outline, **kwargs)
    photo = ImageTk.PhotoImage(flat)
    return photo, body_w + margin * 2, body_h + margin * 2, margin, margin
