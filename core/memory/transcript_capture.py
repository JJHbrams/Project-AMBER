"""
Claude Code JSONL transcript를 LLM 호출 없이 tail하여 STM에 자동 반영하기 위한 순수 함수 모듈.

설계 배경: MCP 경유 대화(Claude Code/Copilot)에서는 engram_save_message가 모델의
자발적 tool-call에 의존해 불안정하다(docs/memory-tiering.md 참고). Claude Code가
디스크에 남기는 세션 transcript(.jsonl)는 매 턴마다 append되므로, 이를 incremental하게
읽어 user/assistant의 "최종 텍스트"만 룰 기반으로 추출하면 LLM 호출 없이 채울 수 있다.

주의: 여기서 하는 건 의미 요약이 아니라 노이즈 제거(툴 호출/thinking/시스템 알림 배제)다.
"""

import json
import re
from pathlib import Path
from typing import List, Optional, Tuple

# ── transcript 파일 경로 ─────────────────────────────────────────────


def claude_project_dir(cwd: str, claude_home: Optional[Path] = None) -> Path:
    """Claude Code가 세션을 기록하는 프로젝트 디렉터리 경로를 계산한다.

    Claude Code는 cwd의 구분자(':', '\\', '/')를 '-'로 치환한 이름의
    디렉터리 아래에 세션별 <sessionId>.jsonl을 남긴다.
    """
    home = claude_home or (Path.home() / ".claude")
    encoded = re.sub(r"[:\\/]", "-", cwd)
    return home / "projects" / encoded


def find_active_transcript(project_dir: Path) -> Optional[Path]:
    """가장 최근에 수정된 .jsonl(현재 활성 세션으로 추정)을 반환한다."""
    if not project_dir.is_dir():
        return None
    candidates = list(project_dir.glob("*.jsonl"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


# ── incremental tail ─────────────────────────────────────────────────


def read_new_lines(path: Path, offset: int) -> Tuple[int, List[str]]:
    """offset부터 이어 읽어 완전한 줄만 반환하고, 다음에 이어 읽을 offset을 함께 준다.

    파일 끝에 개행 없이 걸쳐있는 미완성 줄은 버리지 않고 offset을 그 줄 시작
    지점으로 되돌려 다음 poll에서 이어받는다(중간에 잘린 JSON 파싱 방지).
    파일이 offset보다 작아졌으면(로테이션/삭제) offset을 0으로 리셋한다.
    """
    size = path.stat().st_size
    if size < offset:
        offset = 0

    with path.open("r", encoding="utf-8", errors="replace") as f:
        f.seek(offset)
        chunk = f.read()

    if not chunk:
        return offset, []

    ends_with_newline = chunk.endswith("\n")
    lines = chunk.split("\n")
    if not ends_with_newline:
        # 마지막 조각은 미완성 줄일 수 있으니 보류
        incomplete = lines.pop()
        new_offset = offset + len(chunk.encode("utf-8")) - len(incomplete.encode("utf-8"))
    else:
        if lines and lines[-1] == "":
            lines.pop()
        new_offset = offset + len(chunk.encode("utf-8"))

    return new_offset, [ln for ln in lines if ln.strip()]


# ── 라인 파싱: 노이즈 제거 후 (role, text)만 추출 ──────────────────────


def extract_turn(line: str) -> Optional[Tuple[str, str]]:
    """transcript 한 줄에서 실제 human 입력 또는 assistant 최종 텍스트만 뽑는다.

    제외 대상: tool_use/tool_result/thinking 블록, task-notification,
    isMeta 리마인더, sidechain(서브에이전트) 라인, mode/system 메타 라인.
    """
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None

    if obj.get("isSidechain"):
        return None

    obj_type = obj.get("type")
    message = obj.get("message") or {}

    if obj_type == "user":
        content = message.get("content")
        if not isinstance(content, str):
            return None  # list content == tool_result 등, 실제 사람 입력 아님
        if obj.get("origin", {}).get("kind") != "human":
            return None  # task-notification, hook 주입 등
        if obj.get("isMeta"):
            return None
        text = content.strip()
        return ("user", text) if text else None

    if obj_type == "assistant":
        content = message.get("content")
        if not isinstance(content, list):
            return None
        texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        text = "".join(texts).strip()
        return ("assistant", text) if text else None

    return None


def extract_turns(lines: List[str]) -> List[Tuple[str, str]]:
    turns = []
    for line in lines:
        turn = extract_turn(line)
        if turn:
            turns.append(turn)
    return turns


# ── 민감정보 마스킹 (규칙 기반 — 문맥 판단 아님, 알려진 패턴만) ─────────

_SECRET_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9_-]{20,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._-]{20,}"), "Bearer [REDACTED_TOKEN]"),
    (re.compile(r"(?i)(api[_-]?key|token|secret|password|passwd)\s*[:=]\s*['\"]?[^\s'\",]{6,}"), r"\1=[REDACTED]"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[REDACTED_EMAIL]"),
    (re.compile(r"\b01[016789]-?\d{3,4}-?\d{4}\b"), "[REDACTED_PHONE]"),
]

_SENSITIVE_PATH_MARKERS = (".env", "credentials.json", "id_rsa", ".pem", ".pfx")


def redact_secrets(text: str) -> str:
    """알려진 패턴(API 키/토큰/이메일/전화번호)만 마스킹한다.

    문맥적으로만 민감한 내용(예: '이건 회사 기밀이야')은 걸러내지 못한다 —
    이건 규칙 기반 필터의 근본적 한계이며 LLM 판단을 대체하지 않는다.
    """
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def looks_like_sensitive_file_reference(text: str) -> bool:
    return any(marker in text for marker in _SENSITIVE_PATH_MARKERS)
