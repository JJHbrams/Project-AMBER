"""Deterministic, bounded persona-behavior modules.

The evaluator keeps its ephemeral cadence separately from persona persistence:
the effective scalar remains owned by ``identity.service``.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Callable


PERSONALITY_MODULES = ("situational_humor",)
_LIGHT_MIN_HUMOR = 0.35
_LIGHT_COOLDOWN_SECONDS = 120.0
_STATE_TTL_SECONDS = 1800.0
_SENSITIVE_TERMS = (
    "suicide", "self-harm", "emergency", "urgent", "death", "funeral",
    "자살", "자해", "응급", "긴급", "사망", "장례",
)


@dataclass(frozen=True)
class SituationalHumorSignal:
    mode: str
    text: str


@dataclass
class _CadenceState:
    last_light_at: float = 0.0
    expires_at: float = 0.0


_SESSION_STATE: dict[int, _CadenceState] = {}


def _signal(mode: str) -> SituationalHumorSignal:
    text = {
        "off": "situational_humor: off; keep the response plain and direct.",
        "adaptive": "situational_humor: adaptive; keep humor optional and follow the moment.",
        "light": "situational_humor: light; a brief dry aside may fit if it helps; never force a joke.",
    }[mode]
    return SituationalHumorSignal(mode=mode, text=text)


def _is_sensitive(query: str) -> bool:
    folded = (query or "").casefold()
    return any(term.casefold() in folded for term in _SENSITIVE_TERMS)


def clear_situational_humor_state(session_key: int) -> None:
    """Remove cadence at the same lifecycle boundary as MCP session bindings."""
    _SESSION_STATE.pop(int(session_key), None)


def evaluate_situational_humor(
    humor: object,
    *,
    user_query: str = "",
    session_key: int | None = None,
    is_bootstrap: bool = False,
    now: float | None = None,
) -> SituationalHumorSignal:
    """Return one compact, non-mandatory signal without persisting persona data."""
    moment = time.monotonic() if now is None else float(now)
    try:
        humor_value = float(humor)
    except (TypeError, ValueError):
        humor_value = 0.0

    if session_key is not None:
        state = _SESSION_STATE.get(int(session_key))
        if state and state.expires_at <= moment:
            _SESSION_STATE.pop(int(session_key), None)

    if _is_sensitive(user_query):
        mode = "off"
    elif is_bootstrap or not (user_query or "").strip():
        mode = "adaptive"
    elif humor_value < _LIGHT_MIN_HUMOR:
        mode = "adaptive"
    else:
        state = _SESSION_STATE.get(int(session_key)) if session_key is not None else None
        mode = "adaptive" if state and moment - state.last_light_at < _LIGHT_COOLDOWN_SECONDS else "light"

    if session_key is not None:
        state = _SESSION_STATE.setdefault(int(session_key), _CadenceState())
        state.expires_at = moment + _STATE_TTL_SECONDS
        if mode == "light":
            state.last_light_at = moment
    return _signal(mode)
