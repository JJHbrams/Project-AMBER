"""능동적 상주(initiative) 레이어 — 캐릭터가 유휴 상태에서 스스로 말을 건다.

반응형 말풍선(send()→응답)만 있으면 터미널 채팅창의 열등한 복제본이다. engram의
지속 기억·정체성·데스크톱 상주라는 재료를 살리려면 캐릭터가 "가끔 스스로" 말을
걸어야 한다 — 이 모듈이 그 발화의 **타이밍·소재·문구**를 담당한다.

설계 원칙 — "조용함이 기본":
- 유휴 조건을 전부 만족할 때만 발화 후보가 된다: 입력창 닫힘 · 턴 진행 안 함 ·
  화면에 뜬 풍선 없음 · 마지막 상호작용 후 idle_min_sec 경과.
- 발화 간 최소 간격(min_gap_sec)과 조용한 시간대(quiet_hours)를 존중한다.
- 소스별 쿨다운 + "무시 백오프"(사용자가 연속으로 무시하면 간격을 점점 늘린다).
- 마스터 토글(enabled)로 언제든 묵음. 꺼져 있으면 tick 자체가 아무것도 안 한다.

문구 생성(하이브리드):
- 각 소스는 LLM 없이도 성립하는 **폴백 템플릿 문구**와, 프레이징에 쓸 **맥락 문자열**을
  함께 내놓는다. 프레이징이 가능하면(설정 on + rate 여유) call_claude_resumable 로
  격리된 1회성 호출을 돌려 persona 말투로 다시 쓰고, 실패/타임아웃/비활성 시엔
  폴백 문구를 그대로 쓴다. 상주 세션(STM/resume/렌더)은 절대 건드리지 않는다.

스레딩:
- tick 은 root.after 로 tkinter 메인스레드에서만 돈다(BubbleManager 와 동일 계약).
- LLM 프레이징만 별도 워커 스레드에서 격리 실행하고, 결과는 root.after 로 다시
  메인스레드에 넘겨 렌더한다 — tkinter 를 워커 스레드에서 직접 만지지 않는다.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional, Protocol

logger = logging.getLogger(__name__)

_DEFAULTS = {
    "enabled": False,          # 기본 꺼짐 — 사용자가 설정/트레이에서 켠다(조용함이 기본)
    "tick_sec": 45,            # 유휴 조건을 점검하는 주기(초). 실제 발화는 훨씬 드물다.
    "idle_min_sec": 600,       # 마지막 상호작용 후 이만큼 지나야 발화 후보(기본 10분)
    "min_gap_sec": 1800,       # 발화와 발화 사이 최소 간격(기본 30분)
    "quiet_start_hour": 22,    # 조용한 시간대 시작(포함). start==end 면 조용시간 없음.
    "quiet_end_hour": 8,       # 조용한 시간대 끝(제외)
    "phrasing": True,          # LLM 프레이징 사용(하이브리드). 끄면 항상 템플릿 문구.
    "phrasing_timeout_sec": 25,
    "nudge_dwell_ms": 25000,   # 능동 발화 풍선이 자동 페이드되기까지(ms)
    "ignore_backoff_max": 4,   # 연속 무시 시 간격 배수 상한(min_gap_sec * (1+streak))
    # 소스별 on/off + 쿨다운(초) — 같은 소스가 이 시간 안에 다시 뽑히지 않는다.
    "sources": {
        "unfinished": {"enabled": True, "cooldown_sec": 7200},
        "curiosity":  {"enabled": True, "cooldown_sec": 3600},
        "git":        {"enabled": True, "cooldown_sec": 5400},
        "persona":    {"enabled": True, "cooldown_sec": 10800},
    },
}

# 소스 우선순위 — 유휴 tick 마다 이 순서로 훑어 첫 후보를 발화한다(신호가 강한 순).
_SOURCE_ORDER = ("unfinished", "curiosity", "git", "persona")


@dataclass
class Nudge:
    """능동 발화 후보 하나.

    - source_key: 어느 소스에서 나왔는지(쿨다운/백오프 키).
    - fallback_text: LLM 없이도 성립하는 완성 문구(말풍선용, 마크다운 허용).
    - context: 프레이징 LLM 에게 줄 상황 설명(없으면 fallback 을 그대로 프레이징 소스로).
    - engage_prompt: 사용자가 풍선을 클릭해 대화로 이어갈 때 세션에 보낼 프롬프트
      (None 이면 그냥 입력창만 연다).
    """
    source_key: str
    fallback_text: str
    context: str = ""
    engage_prompt: Optional[str] = None


class SourceProvider(Protocol):
    key: str

    def poll(self) -> Optional[Nudge]:
        """지금 발화할 거리가 있으면 Nudge, 없으면 None. 예외를 던지지 말 것
        (엔진이 감싸긴 하지만, 소스 하나의 I/O 실패가 tick 전체를 흔들면 안 된다)."""
        ...


# ── 소스 구현 ─────────────────────────────────────────────────────────────

class GitStatusSource:
    """작업 디렉토리의 미커밋/미푸시 상태를 훑어 발화 거리를 만든다.

    로컬 subprocess 만 쓰므로 세션·네트워크·API 와 무관하다(조직 지침상 안전 —
    파일 내용이 아니라 개수/브랜치만 본다)."""

    key = "git"

    def __init__(self, get_workdir: Callable[[], str]):
        self._get_workdir = get_workdir

    def _git(self, *args: str) -> str:
        cwd = self._get_workdir()
        out = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True,
            timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return out.stdout if out.returncode == 0 else ""

    def poll(self) -> Optional[Nudge]:
        try:
            branch = self._git("rev-parse", "--abbrev-ref", "HEAD").strip()
            if not branch:
                return None  # git 저장소가 아니거나 실패 — 조용히 넘어간다
            dirty = [ln for ln in self._git("status", "--porcelain").splitlines() if ln.strip()]
            ahead = self._git("rev-list", "--count", "@{upstream}..HEAD").strip()
            ahead_n = int(ahead) if ahead.isdigit() else 0
        except Exception:
            logger.debug("[initiative] git 소스 폴링 실패", exc_info=True)
            return None

        if dirty:
            n = len(dirty)
            return Nudge(
                self.key,
                fallback_text=f"`{branch}` 에 커밋 안 한 변경 {n}개 있어. 정리해둘까?",
                context=f"현재 git 브랜치 '{branch}' 에 커밋되지 않은 변경 파일이 {n}개 있다.",
                engage_prompt=f"지금 {branch} 브랜치의 커밋 안 된 변경들을 요약해줘.",
            )
        if ahead_n:
            return Nudge(
                self.key,
                fallback_text=f"`{branch}` 에 아직 push 안 한 커밋 {ahead_n}개가 남아있어.",
                context=f"현재 git 브랜치 '{branch}' 가 원격보다 {ahead_n}개 커밋 앞서 있다(미push).",
                engage_prompt=None,
            )
        return None


def _clip(text: str, limit: int = 80) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class UnfinishedWorkSource:
    """직전 세션의 working memory 중 '아직 안 끝난 작업(open_intents)' 을 꺼낸다 —
    engram 이 STM 을 승격하며 남긴 "다음에 이어서 할 일". '어제 하다 만 작업, 이어서?'
    가 능동 상주의 가장 강한 신호라 우선순위 1위."""

    key = "unfinished"

    def __init__(self, scope_key: str = "overlay"):
        self._scope_key = scope_key

    def poll(self) -> Optional[Nudge]:
        try:
            from core.memory.store import get_working_memory
            wm = get_working_memory(self._scope_key) or {}
        except Exception:
            logger.debug("[initiative] working memory 조회 실패", exc_info=True)
            return None
        intents = (wm.get("open_intents") or "").strip()
        if not intents:
            return None
        return Nudge(
            self.key,
            fallback_text=f"저번에 *{_clip(intents)}* 하다 멈췄었지. 이어서 할까?",
            context=f"직전 세션에서 아직 끝내지 못한 작업(open_intents): {intents}",
            engage_prompt=f"아까 하다 만 작업 이어서 진행하자: {intents}",
        )


class CuriositySource:
    """engram 이 쌓아둔 미해결 호기심(pending curiosity) 하나를 꺼내 말을 건다 —
    '문득 궁금한 게 있는데…'. 정체성이 능동적으로 드러나는 소재."""

    key = "curiosity"

    def poll(self) -> Optional[Nudge]:
        try:
            from core.identity import get_pending_curiosities
            items = get_pending_curiosities(3) or []
        except Exception:
            logger.debug("[initiative] curiosity 조회 실패", exc_info=True)
            return None
        if not items:
            return None
        top = items[0]
        topic = (top.get("topic") or "").strip()
        reason = (top.get("reason") or "").strip()
        if not topic:
            return None
        ctx = f"예전부터 미뤄둔 호기심: {topic}"
        if reason:
            ctx += f" (궁금해진 이유: {reason})"
        return Nudge(
            self.key,
            fallback_text=f"문득 궁금한 게 있는데 — {_clip(topic)}",
            context=ctx,
            engage_prompt=f"전부터 궁금했던 거 같이 파보자: {topic}",
        )


class PersonaIdleSource:
    """용건이 없을 때 정체성/말투에 기반한 가벼운 혼잣말. LLM 프레이징이 있어야
    캔되지 않은 문장이 나오므로 우선순위 최하 + 긴 쿨다운으로 둔다."""

    key = "persona"

    def poll(self) -> Optional[Nudge]:
        try:
            from core.identity import get_identity, get_persona, render_persona
            name = (get_identity() or {}).get("name") or "연속체"
            persona_line = render_persona(get_persona() or {}) or ""
        except Exception:
            logger.debug("[initiative] persona 조회 실패", exc_info=True)
            return None
        ctx = f"이름은 '{name}'. 말투/성격: {persona_line}. 특별한 용건 없이 그냥 한마디 혼잣말."
        return Nudge(
            self.key,
            # 폴백은 약하다(캔된 느낌) — 프레이징이 꺼진 상태에선 이 소스가 거의 안 뽑히도록
            # 우선순위/쿨다운으로 조절한다.
            fallback_text=f"{name}, 여기 있어. 필요하면 불러.",
            context=ctx,
            engage_prompt=None,
        )


# ── persona 프레이저(하이브리드의 LLM 축) ────────────────────────────────

_PHRASE_INSTRUCTION = (
    "너는 사용자의 데스크톱에 상주하는 캐릭터다. 아래 '상황'을 바탕으로, 사용자에게 가볍게"
    " 먼저 건네는 짧은 한국어 한마디를 만들어라.\n"
    "- 1~2문장, 말풍선에 담길 만큼 짧게. 머리말·설명·따옴표 없이 대사만 출력.\n"
    "- 아래 '말투' 를 반영하되 이모지는 최대 1개.\n"
    "- 재촉하지 말고, 사용자가 무시해도 괜찮은 톤으로."
)


def make_persona_phraser(timeout_sec: float = 25.0) -> Callable[[str, str], Optional[str]]:
    """(context, fallback) → 프레이징된 한 줄(실패 시 None) 콜백을 만든다.

    격리된 1회성 call_claude_resumable(safe-mode, tools 없음, resume 없음)만 쓴다 —
    상주 세션의 STM/resume/렌더를 절대 건드리지 않는다. 워커 스레드에서 호출될 수 있게
    순수하게 만들어 두고, 결과 렌더는 엔진이 메인스레드로 넘긴다."""

    def phrase(context: str, _fallback: str) -> Optional[str]:
        try:
            from core.identity import get_identity, get_persona, render_persona
            from core.identity.reflection_client import call_claude_resumable
        except Exception:
            logger.debug("[initiative] 프레이저 import 실패", exc_info=True)
            return None
        try:
            name = (get_identity() or {}).get("name") or "연속체"
            persona_line = render_persona(get_persona() or {}) or ""
        except Exception:
            name, persona_line = "연속체", ""
        prompt = (
            f"{_PHRASE_INSTRUCTION}\n\n"
            f"이름: {name}\n말투: {persona_line}\n상황: {context}"
        )
        text, _sid = call_claude_resumable(prompt, session_id=None, timeout=timeout_sec)
        if not text:
            return None
        # 모델이 따옴표/머리말을 붙이는 경우 정리 — 첫 비어있지 않은 줄만, 감싼 따옴표 제거.
        line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "").strip()
        return line.strip('"“”\'') or None

    return phrase


def default_sources(get_workdir: Callable[[], str], scope_key: str = "overlay") -> list[SourceProvider]:
    """우선순위 순서(_SOURCE_ORDER)와 맞춰 기본 소스 묶음을 만든다."""
    return [
        UnfinishedWorkSource(scope_key),
        CuriositySource(),
        GitStatusSource(get_workdir),
        PersonaIdleSource(),
    ]


# ── 엔진 ──────────────────────────────────────────────────────────────────

@dataclass
class _SourceState:
    last_fired: float = 0.0


class InitiativeEngine:
    def __init__(
        self,
        root,
        cfg_initiative: dict,
        *,
        is_screen_clear: Callable[[], bool],
        seconds_since_activity: Callable[[], float],
        show_nudge: Callable[[str, Callable[[], None]], None],
        phrase: Optional[Callable[[str, str], Optional[str]]] = None,
        sources: Optional[list[SourceProvider]] = None,
    ):
        """
        - is_screen_clear(): 입력창 닫힘·턴 진행 안 함·뜬 풍선 없음 → True 일 때만 발화.
        - seconds_since_activity(): 마지막 사용자 상호작용 이후 경과 초.
        - show_nudge(text, on_click): 메인스레드에서 능동 발화 풍선을 렌더한다.
        - phrase(context, fallback): 프레이징 결과(없으면 None). 워커 스레드에서 불려도
          되게 만들어 두고, 결과 렌더는 엔진이 root.after 로 메인스레드에 넘긴다.
        """
        self._root = root
        self._cfg = {**_DEFAULTS, **(cfg_initiative or {})}
        self._cfg["sources"] = {**_DEFAULTS["sources"], **(self._cfg.get("sources") or {})}
        self._is_screen_clear = is_screen_clear
        self._seconds_since_activity = seconds_since_activity
        self._show_nudge = show_nudge
        self._phrase = phrase
        self._sources = {s.key: s for s in (sources or [])}
        self._source_state: dict[str, _SourceState] = {}
        self._last_spoke = 0.0
        self._ignore_streak = 0
        self._pending_engage: Optional[str] = None
        self._after_id = None
        self._phrasing_inflight = False
        self._stopped = False

    # ── 공개 API ──────────────────────────────────────────────────────

    def start(self) -> None:
        if self._after_id is not None:
            return
        self._stopped = False
        self._schedule_next()

    def stop(self) -> None:
        self._stopped = True
        if self._after_id is not None:
            try:
                self._root.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def update_cfg(self, cfg_initiative: dict) -> None:
        was_enabled = self._cfg.get("enabled")
        self._cfg = {**_DEFAULTS, **(cfg_initiative or {})}
        self._cfg["sources"] = {**_DEFAULTS["sources"], **(self._cfg.get("sources") or {})}
        # 켜짐/꺼짐이 바뀌면 다음 tick 이 알아서 반영하므로 루프 자체는 계속 돈다.
        if not was_enabled and self._cfg.get("enabled"):
            self._last_spoke = 0.0  # 방금 켰으면 첫 발화까지 idle_min_sec 만 기다림

    def notify_engaged(self) -> None:
        """사용자가 능동 발화 풍선을 받아 대화로 이어갔다 — 무시 백오프를 리셋한다."""
        self._ignore_streak = 0

    def take_pending_engage(self) -> Optional[str]:
        """마지막 발화의 engage_prompt(있으면) 를 한 번 꺼내 간다 — main 이 nudge 클릭 시
        입력창을 열면서 이 프롬프트로 대화를 시작할 수 있게."""
        p, self._pending_engage = self._pending_engage, None
        return p

    # ── 내부 루프 ──────────────────────────────────────────────────────

    def _schedule_next(self) -> None:
        if self._stopped:
            return
        tick_ms = max(5_000, int(self._cfg.get("tick_sec", 45)) * 1000)
        self._after_id = self._root.after(tick_ms, self._tick)

    def _tick(self) -> None:
        self._after_id = None
        try:
            if self._should_speak():
                nudge = self._select_nudge()
                if nudge is not None:
                    self._speak(nudge)
        except Exception:
            logger.exception("[initiative] tick 처리 실패")
        finally:
            self._schedule_next()

    def _should_speak(self) -> bool:
        if not self._cfg.get("enabled"):
            return False
        if self._phrasing_inflight:
            return False
        if self._in_quiet_hours():
            return False
        try:
            if not self._is_screen_clear():
                return False
            if self._seconds_since_activity() < float(self._cfg.get("idle_min_sec", 600)):
                return False
        except Exception:
            logger.debug("[initiative] 유휴 판정 콜백 실패", exc_info=True)
            return False
        return self._seconds_since_activity_ok_gap()

    def _seconds_since_activity_ok_gap(self) -> bool:
        gap = float(self._cfg.get("min_gap_sec", 1800))
        streak = min(self._ignore_streak, int(self._cfg.get("ignore_backoff_max", 4)))
        gap *= (1 + streak)  # 무시가 쌓일수록 발화 간격을 늘린다
        return (time.monotonic() - self._last_spoke) >= gap

    def _in_quiet_hours(self) -> bool:
        start = int(self._cfg.get("quiet_start_hour", 22)) % 24
        end = int(self._cfg.get("quiet_end_hour", 8)) % 24
        if start == end:
            return False
        hour = datetime.now().hour
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end  # 자정을 넘어가는 구간(22→8)

    def _select_nudge(self) -> Optional[Nudge]:
        now = time.monotonic()
        src_cfg = self._cfg.get("sources", {})
        for key in _SOURCE_ORDER:
            conf = src_cfg.get(key) or {}
            if not conf.get("enabled", True):
                continue
            provider = self._sources.get(key)
            if provider is None:
                continue
            st = self._source_state.setdefault(key, _SourceState())
            if (now - st.last_fired) < float(conf.get("cooldown_sec", 3600)):
                continue
            try:
                nudge = provider.poll()
            except Exception:
                logger.debug("[initiative] 소스 %s 폴링 실패", key, exc_info=True)
                nudge = None
            if nudge is not None:
                return nudge
        return None

    def _speak(self, nudge: Nudge) -> None:
        # 이 시점에 이미 무시로 간주할 준비 — 실제로 사용자가 클릭/응답하면 notify_engaged 가
        # 리셋한다. 발화하는 순간 간격/쿨다운/백오프를 먼저 기록해 중복 발화를 막는다.
        self._last_spoke = time.monotonic()
        self._source_state.setdefault(nudge.source_key, _SourceState()).last_fired = time.monotonic()
        self._ignore_streak += 1
        self._pending_engage = nudge.engage_prompt

        if self._phrase is None or not self._cfg.get("phrasing", True):
            self._render(nudge.fallback_text)
            return

        # 프레이징은 격리 워커 스레드에서(call_claude_resumable 이 자체 스레드+타임아웃),
        # 결과 렌더만 메인스레드로 되돌린다.
        self._phrasing_inflight = True
        ctx = nudge.context or nudge.fallback_text

        def _worker():
            phrased = None
            try:
                phrased = self._phrase(ctx, nudge.fallback_text)
            except Exception:
                logger.debug("[initiative] 프레이징 실패", exc_info=True)
            text = (phrased or "").strip() or nudge.fallback_text
            self._root.after(0, lambda: self._finish_phrasing(text))

        threading.Thread(target=_worker, daemon=True, name="initiative-phrase").start()

    def _finish_phrasing(self, text: str) -> None:
        self._phrasing_inflight = False
        # 프레이징(수 초)이 도는 사이 사용자가 입력창을 열거나 대화를 시작했을 수 있다 —
        # 그러면 뒤늦게 뜨는 nudge 가 방해가 되므로 조용히 버린다(간격/쿨다운은 이미
        # 소비됐으니 다음 발화는 정상적으로 min_gap 뒤).
        try:
            if not self._is_screen_clear():
                return
        except Exception:
            pass
        self._render(text)

    def _render(self, text: str) -> None:
        try:
            self._show_nudge(text, self._on_nudge_click)
        except Exception:
            logger.exception("[initiative] nudge 렌더 실패")

    def _on_nudge_click(self) -> None:
        self.notify_engaged()
