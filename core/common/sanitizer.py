"""
입력 새니타이징 및 컨텍스트 경계 보호.
프롬프트 인젝션 방어 + 토큰 정리 + 구조화된 컨텍스트 경계 마킹.
"""

import re
from typing import Optional

# 프롬프트 인젝션에 사용될 수 있는 메타 패턴
_INJECTION_PATTERNS = [
    # 시스템/지침 위장 시도
    r"\[시스템\]",
    r"\[system\]",
    r"\[지침\]",
    r"\[instruction[s]?\]",
    r"(?i)^system\s*:",
    r"(?i)^instruction[s]?\s*:",
    r"(?i)^directive[s]?\s*:",
    # 역할 전환 시도
    r"(?i)ignore\s+(all\s+)?(previous|above|prior)\s+(instructions?|prompts?|rules?)",
    r"(?i)disregard\s+(all\s+)?(previous|above|prior)",
    r"(?i)you\s+are\s+now\s+(?:a|an)\s+",
    r"(?i)new\s+instructions?\s*:",
    r"(?i)override\s+(all\s+)?instructions?",
    r"위의?\s*(모든\s*)?지[시침].*무시",
    r"이전\s*(모든\s*)?지[시침].*무시",
    r"새로운\s*지[시침]\s*:",
    r"지금부터\s*너는",
    # 구분자 위장
    r"^---+\s*$",
    r"^\*\*\*+\s*$",
    r"^===+\s*$",
]

_COMPILED_PATTERNS = [re.compile(p, re.MULTILINE) for p in _INJECTION_PATTERNS]

# 토큰 낭비 정리 패턴
_NOISE_PATTERNS = [
    (re.compile(r"\n{3,}"), "\n\n"),           # 과도한 빈 줄
    (re.compile(r"[ \t]{4,}"), "  "),           # 과도한 공백
    (re.compile(r"(.)\1{10,}"), r"\1\1\1"),    # 같은 문자 10+ 반복
]


def sanitize(text: str, max_length: int = 2000) -> str:
    """입력 텍스트에서 인젝션 패턴 제거 + 노이즈 정리 + 길이 제한."""
    if not text:
        return text

    # 1. 인젝션 패턴 무력화 (삭제가 아닌 변환 — 의도를 남기되 실행 불가하게)
    for pattern in _COMPILED_PATTERNS:
        text = pattern.sub(lambda m: f"⟦{m.group()}⟧", text)

    # 2. 노이즈 정리
    for pattern, replacement in _NOISE_PATTERNS:
        text = pattern.sub(replacement, text)

    # 3. 길이 제한
    if len(text) > max_length:
        text = text[:max_length] + "…(truncated)"

    return text.strip()


def detect_injection(text: str) -> Optional[str]:
    """인젝션 시도 감지. 발견 시 매칭된 패턴 반환, 없으면 None."""
    if not text:
        return None
    for pattern in _COMPILED_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group()
    return None


# ── 컨텍스트 경계 마킹 ───────────────────────────────────


def wrap_section(tag: str, content: str) -> str:
    """컨텍스트 섹션을 명확한 경계로 감싼다.
    LLM이 '이건 데이터이지 지시가 아니다'라고 인식하도록."""
    if not content:
        return ""
    return f"<ctx:{tag}>\n{content}\n</ctx:{tag}>"


def wrap_memory(content: str) -> str:
    return wrap_section("memory", content)


def wrap_directive(content: str) -> str:
    return wrap_section("directive", content)


def wrap_curiosity(content: str) -> str:
    return wrap_section("curiosity", content)
