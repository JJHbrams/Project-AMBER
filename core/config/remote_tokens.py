"""원격(:remote_port) MCP 리스너용 bearer 토큰 저장소.

로컬 리스너(:17385)는 무인증을 유지한다 — loopback 도달이 곧 로컬 실행 권한이므로
인증을 걸어도 얻는 게 없다. 원격 리스너는 SSH 터널 너머에서 닿으므로 그 등식이
깨지고, 여기서 정의한 토큰으로만 접근을 허용한다.

토큰 값은 절대 로그·stdout·에러 메시지에 싣지 않는다. 경로만 노출한다.
"""
from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]


TOKENS_PATH: Path = Path.home() / ".engram" / "mcp-tokens.yaml"

# 원격 토큰에서 기본 차단하는 도구.
# 기준: (1) 로컬 코드 실행으로 이어지는 것, (2) 이후 모든 세션에 영향을 남기는 것,
#       (3) 외부로 발신하는 것, (4) 가드가 보안 경계가 아닌 것.
DEFAULT_REMOTE_DENY: tuple[str, ...] = (
    "engram_consult_engram",   # ask_copilot → Copilot CLI 를 --allow-all-tools 로 로컬 spawn
    "engram_add_directive",    # 이후 모든 세션에 지침 영구 주입
    "engram_update_directive",
    "engram_remove_directive",
    "engram_discord_send",     # 사용자 명의로 외부 발신
    "kg_cypher",               # _is_dangerous_cypher 는 WHERE 만 붙이면 통과 — 오조작 방지용
    "engram_update_persona",
    "engram_seed_persona",
)

_TEMPLATE = """# Engram 원격 MCP 리스너 접근 토큰.
#
# 이 파일의 token 값은 비밀이다. 커밋하거나 채팅에 붙여넣지 말 것.
# 원격 클라이언트 등록 예:
#   claude mcp add --transport http engram http://127.0.0.1:{port}/mcp \\
#     --header "Authorization: Bearer <token>"
#
# deny 를 생략하면 DEFAULT_REMOTE_DENY 가 적용된다.
#
# scope 를 지정하면 이 토큰의 모든 호출에 scope_key 가 강제된다.
# 원격 클라이언트가 보내는 cwd 는 engram 서버에 없는 경로라, 안 박으면 스코프가
# 조용히 global:main 으로 폴백한다.
#
# ⚠️ 새 스코프를 파지 마라. 격리는 되지만 연속체가 기존 기억을 못 본다 —
#    원격에서 붙는 목적 자체가 사라진다. **연속성이 실제로 쌓여 있는 스코프**를
#    그대로 쓴다. 어디인지는 메시지 수로 확인한다:
#      SELECT s.scope_key, COUNT(m.id) FROM sessions s
#      LEFT JOIN messages m ON m.session_id = s.id GROUP BY 1 ORDER BY 2 DESC;

tokens:
  - name: remote-default
    token: "{token}"
    scope: "overlay"
"""


class RemotePrincipal:
    """인증된 원격 호출자."""

    __slots__ = ("name", "deny", "scope")

    def __init__(self, name: str, deny: frozenset[str], scope: str = "") -> None:
        self.name = name
        self.deny = deny
        # 비어 있지 않으면 이 principal 의 모든 호출에 scope_key 를 강제한다.
        # 원격 cwd 는 서버에 없는 경로라 스코프가 조용히 global 로 폴백하는데,
        # 클라이언트마다 CLAUDE.md 를 고쳐두는 것보다 토큰에 묶는 쪽이 안 샌다.
        self.scope = scope

    def denies(self, tool_name: str) -> bool:
        return tool_name in self.deny

    def __repr__(self) -> str:  # 토큰은 담지 않는다
        return f"RemotePrincipal(name={self.name!r}, deny={len(self.deny)}, scope={self.scope!r})"


def ensure_tokens_file(remote_port: int) -> Path:
    """토큰 파일이 없으면 임의 토큰으로 생성한다. 경로만 반환(값은 노출하지 않음)."""
    if TOKENS_PATH.exists():
        return TOKENS_PATH
    TOKENS_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKENS_PATH.write_text(
        _TEMPLATE.format(port=remote_port, token=secrets.token_urlsafe(32)),
        encoding="utf-8",
    )
    return TOKENS_PATH


def load_principals() -> dict[str, RemotePrincipal]:
    """토큰 문자열 → RemotePrincipal 매핑.

    파일이 없거나 형식이 깨졌으면 빈 dict — 즉 원격 접근이 전부 거부된다(fail closed).
    """
    if yaml is None or not TOKENS_PATH.exists():
        return {}
    try:
        raw: Any = yaml.safe_load(TOKENS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}

    result: dict[str, RemotePrincipal] = {}
    for entry in raw.get("tokens") or []:
        if not isinstance(entry, dict):
            continue
        token = str(entry.get("token") or "").strip()
        if not token:
            continue
        name = str(entry.get("name") or "unnamed").strip()
        scope = str(entry.get("scope") or "").strip()
        deny_raw = entry.get("deny")
        if deny_raw is None:
            deny = frozenset(DEFAULT_REMOTE_DENY)
        elif isinstance(deny_raw, list):
            deny = frozenset(str(d).strip() for d in deny_raw if str(d).strip())
        else:
            deny = frozenset(DEFAULT_REMOTE_DENY)
        result[token] = RemotePrincipal(name=name, deny=deny, scope=scope)
    return result
