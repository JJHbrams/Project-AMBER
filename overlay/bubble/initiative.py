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


# 결과 판정에 쓰이는 세 가지 값 — 무엇을 "참여"로 볼지가 곧 학습 신호의 정의다.
ENGAGED = "engaged"                     # 답장까지 실제로 도달
ACKNOWLEDGED = "acknowledged_no_reply"  # 열어는 봤으나 응답 없음(중간 신호)
IGNORED = "ignored"                     # dwell 만료 페이드 또는 "나중에"
# 판정이 끝난 뒤(대개 페이드로 ignored 처리된 뒤) 사용자가 캐릭터를 눌러 지난 발화를
# 다시 보고 그때 답한 경우. 이미 기록된 결과는 건드리지 않고 별도로 남긴다 —
# "발화 1건 = 결과 1건" 불변식을 지키면서도 "결국 응했다"는 신호를 잃지 않기 위함.
LATE_ENGAGED = "late_engaged"

# 결과별 백오프 가중치 — ignored 는 1스텝, acknowledged 는 그 절반만 민다.
# ("열어는 봤다"는 완전한 무시보다 약한 부정 신호라 간격을 덜 벌린다.)
_BACKOFF_WEIGHT = {ENGAGED: 0.0, ACKNOWLEDGED: 0.5, IGNORED: 1.0}


@dataclass
class Nudge:
    """능동 발화 후보 하나.

    - source_key: 어느 소스에서 나왔는지(쿨다운/백오프 키).
    - fallback_text: LLM 없이도 성립하는 완성 문구(말풍선용, 마크다운 허용).
    - context: 프레이징 LLM 에게 줄 상황 설명(없으면 fallback 을 그대로 프레이징 소스로).
    - topic: 무엇에 대한 발화였는지 짧은 라벨 — 결과 로그에 남겨 소재별 반응을 집계한다.
    - ref_id: 소재의 원본 레코드 id(예: curiosity id). 참여했을 때 그 레코드를 해소
      처리하는 데 쓴다 — 이게 없으면 "발화했다"는 사실만 남고 지식이 갱신되지 않는다.

    topic/ref_id 가 없으면 발화 결과를 어디에도 귀속시킬 수 없어 후처리 자체가
    불가능하다. 그래서 이 두 필드는 발화 시점이 아니라 **결과가 확정될 때까지**
    엔진이 _active_nudge 로 들고 있는다."""
    source_key: str
    fallback_text: str
    context: str = ""
    topic: str = ""
    ref_id: Optional[int] = None


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
                topic=f"{branch} 미커밋 변경",
            )
        if ahead_n:
            return Nudge(
                self.key,
                fallback_text=f"`{branch}` 에 아직 push 안 한 커밋 {ahead_n}개가 남아있어.",
                context=f"현재 git 브랜치 '{branch}' 가 원격보다 {ahead_n}개 커밋 앞서 있다(미push).",
                topic=f"{branch} 미push 커밋",
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
            topic=_clip(intents, 60),
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
        # ref_id 를 실어 보내야 사용자가 실제로 이 화제에 응했을 때 해당 호기심을
        # 해소 처리할 수 있다 — 안 그러면 이미 다룬 주제로 계속 다시 말을 건다.
        return Nudge(
            self.key,
            fallback_text=f"문득 궁금한 게 있는데 — {_clip(topic)}",
            context=ctx,
            topic=topic,
            ref_id=top.get("id"),
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
            topic="혼잣말",
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
        on_outcome: Optional[Callable[[Nudge, str, str], None]] = None,
    ):
        """
        - is_screen_clear(): 입력창 닫힘·턴 진행 안 함·뜬 풍선 없음 → True 일 때만 발화.
        - seconds_since_activity(): 마지막 사용자 상호작용 이후 경과 초.
        - show_nudge(text, on_click): 메인스레드에서 능동 발화 풍선을 렌더한다.
        - phrase(context, fallback): 프레이징 결과(없으면 None). 워커 스레드에서 불려도
          되게 만들어 두고, 결과 렌더는 엔진이 root.after 로 메인스레드에 넘긴다.
        - on_outcome(nudge, outcome, shown_text, latency_sec): 발화 하나의 결과가 확정될
          때 1회 호출. latency_sec 은 화면에 뜬 순간부터 결과가 정해질 때까지의 초 —
          같은 ignored 라도 "3초 만에 물림"과 "25초 dwell 을 다 채우고 페이드"는 정반대
          신호(전자는 관심 없음, 후자는 자리에 없었을 가능성)라 이 값 없이는 구분이 안
          되고, 로그에 안 남기면 나중에 소급 복원할 방법도 없다.
          엔진은 백오프 회계까지만 책임지고, "그래서 무엇을 남길지"(활동 로그·호기심
          해소·KG)는 전부 이 콜백 바깥에서 처리한다 — 엔진이 DB 를 직접 알면 tkinter
          없는 단위 테스트가 불가능해지기 때문.
        """
        self._root = root
        self._cfg = {**_DEFAULTS, **(cfg_initiative or {})}
        self._cfg["sources"] = {**_DEFAULTS["sources"], **(self._cfg.get("sources") or {})}
        self._is_screen_clear = is_screen_clear
        self._seconds_since_activity = seconds_since_activity
        self._show_nudge = show_nudge
        self._phrase = phrase
        self._on_outcome = on_outcome
        self._sources = {s.key: s for s in (sources or [])}
        self._source_state: dict[str, _SourceState] = {}
        self._last_spoke = 0.0
        # 결과 가중치가 0.5 단위라 float — 간격 배수 계산에서만 쓰이므로 정수일 필요 없다.
        self._ignore_streak = 0.0
        # 결과 대기 중인 발화 — 렌더된 순간부터 결과가 확정될 때까지만 살아있다.
        # 이게 None 이면 지금 판정할 발화가 없다는 뜻이라 모든 notify_* 가 무시된다
        # (같은 발화가 두 번 집계되는 걸 막는 유일한 장치).
        self._active_nudge: Optional[Nudge] = None
        self._active_text = ""
        self._active_since = 0.0  # 화면에 뜬 시각 — 결과까지의 지연을 재는 기준점
        # 결과가 확정된 뒤에도 남겨두는 "가장 마지막 발화" — 사용자가 아무 때나 캐릭터를
        # 눌러 "마지막에 뭐라고 했더라" 를 확인하고 그때 답할 수 있어야 하기 때문.
        # 25초 dwell 은 결과를 판정하는 창일 뿐이고, 답할 수 있는 창이 아니다.
        self._last_nudge: Optional[Nudge] = None
        self._last_nudge_text = ""
        # 폐기 시 되돌릴 값 — _speak 이 간격/쿨다운을 선지불하기 때문에 필요하다.
        self._rollback: Optional[tuple[float, str, float]] = None
        self._after_id = None
        self._phrasing_inflight = False
        self._stopped = False
        self._last_state_key = ""  # 같은 보류 이유를 반복해서 찍지 않기 위한 직전 상태 key

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
        """사용자가 능동 발화를 받아 실제로 답장까지 했다 — 백오프를 리셋한다."""
        self._resolve(ENGAGED)

    def notify_acknowledged(self) -> None:
        """풍선을 열어보긴 했으나 응답 없이 닫았다 — 중간 신호(백오프 절반 스텝)."""
        self._resolve(ACKNOWLEDGED)

    def notify_ignored(self) -> None:
        """dwell 이 만료돼 페이드됐거나 사용자가 "나중에"로 물렸다 — 백오프 한 스텝."""
        self._resolve(IGNORED)

    def notify_late_engaged(self) -> None:
        """판정이 끝난 지난 발화에 사용자가 뒤늦게 답했다.

        이미 기록된 결과(대개 ignored)는 고치지 않는다 — 그 시점의 관측은 사실이었다.
        대신 별도 신호로 남기고 백오프는 리셋한다: 결국 응했다는 건 그 소재가 쓸모
        있었다는 뜻이고, 그걸 무시 취급해 발화 간격을 계속 벌리면 안 된다."""
        nudge, self._last_nudge = self._last_nudge, None
        text, self._last_nudge_text = self._last_nudge_text, ""
        if nudge is None:
            return
        self._ignore_streak = 0.0
        if self._on_outcome is not None:
            try:
                self._on_outcome(nudge, LATE_ENGAGED, text, 0.0)
            except Exception:
                logger.exception("[initiative] 늦은 참여 후처리 실패")

    def has_pending_outcome(self) -> bool:
        """지금 결과 판정을 기다리는 발화가 있는지 — 호출부가 "이 사용자 입력이 자율발화에
        대한 답인가, 그냥 평소 대화인가"를 구분하는 데 쓴다."""
        return self._active_nudge is not None

    def _resolve(self, outcome: str) -> None:
        """발화 하나의 결과를 확정한다 — 백오프를 조정하고 on_outcome 을 1회 호출한다.

        대기 중인 발화가 없으면 조용히 무시한다. 평범한 대화(자율발화와 무관)가
        백오프를 리셋해버리던 문제를 여기서 막는다 — 호출부는 마음 놓고 부를 수 있고,
        "자율발화에 대한 반응일 때만" 실제로 반영된다."""
        nudge, self._active_nudge = self._active_nudge, None
        if nudge is None:
            return
        text, self._active_text = self._active_text, ""
        latency = max(0.0, time.monotonic() - self._active_since) if self._active_since else 0.0
        self._active_since = 0.0
        self._rollback = None  # 결과가 나온 이상 되돌릴 일은 없다
        if outcome == ENGAGED:
            self._ignore_streak = 0.0
            # 이미 답장까지 갔으면 "되살려서 답할" 대상이 없다.
            self._last_nudge, self._last_nudge_text = None, ""
        else:
            # 답하지 않은 발화는 슬롯에 남긴다 — 나중에 캐릭터를 눌러 답할 수 있어야 한다.
            self._ignore_streak += _BACKOFF_WEIGHT.get(outcome, 1.0)
        if self._on_outcome is not None:
            try:
                self._on_outcome(nudge, outcome, text, latency)
            except Exception:
                logger.exception("[initiative] 결과 후처리 실패 outcome=%s", outcome)

    def active_nudge_text(self) -> str:
        """대화를 이을 때 첫 프롬프트에 얹을 발화 문구 — 프레이징을 거쳤다면 템플릿이
        아니라 화면에 실제로 보인 그 문장이다.

        결과가 확정된 뒤에도 마지막 발화 문구를 돌려준다. 안 그러면 "지난 발화를 보고
        뒤늦게 답하는" 경로에서 연속성(prepend)이 통째로 빠진다."""
        return self._active_text or self._last_nudge_text

    # ── 내부 루프 ──────────────────────────────────────────────────────

    def _schedule_next(self) -> None:
        if self._stopped:
            return
        tick_ms = max(5_000, int(self._cfg.get("tick_sec", 45)) * 1000)
        self._after_id = self._root.after(tick_ms, self._tick)

    def _tick(self) -> None:
        self._after_id = None
        try:
            key, detail = self._blocking_reason()
            if key is None:
                nudge = self._select_nudge()
                if nudge is None:
                    key, detail = "dry", "소재 없음(모든 소스가 None 또는 쿨다운)"
                else:
                    self._log_state("spoke", f"발화 → source={nudge.source_key} topic={nudge.topic or '-'}")
                    self._speak(nudge)
                    return
            self._log_state(key, f"보류 — {detail}")
        except Exception:
            logger.exception("[initiative] tick 처리 실패")
        finally:
            self._schedule_next()

    def _log_state(self, key: str, msg: str) -> None:
        """상태가 **바뀔 때만** 한 줄 남긴다.

        tick 이 45초마다 도는데 매번 찍으면 로그가 쓸려나가고, 아무것도 안 찍으면
        "왜 안 뜨는지"를 밖에서 알 방법이 없다(이 침묵 때문에 실제로 디버깅이 막혔다).

        중복 판정은 **key** 로만 한다 — 메시지에 경과 초 같은 값이 들어가면 매 tick
        문자열이 달라져서 "상태 변화 시 1회"가 무력화된다(실제로 그렇게 도배됐다)."""
        if key == self._last_state_key:
            return
        self._last_state_key = key
        logger.info("[initiative] %s", msg)

    def _should_speak(self) -> bool:
        """게이트를 전부 통과했는지 — 이유가 필요하면 _blocking_reason() 을 쓴다."""
        return self._blocking_reason()[0] is None

    def _blocking_reason(self) -> tuple[Optional[str], str]:
        """발화를 막고 있는 첫 조건을 (중복판정 key, 사람이 읽을 문구)로. 통과하면 (None, "").

        key 는 조건의 종류만 나타낸다 — 문구에 든 경과 초는 매 tick 바뀌므로
        중복 판정에 쓰면 로그가 도배된다."""
        if not self._cfg.get("enabled"):
            return "disabled", "enabled=false"
        if self._phrasing_inflight:
            return "phrasing", "직전 프레이징이 아직 진행 중"
        if self._in_quiet_hours():
            return "quiet", (f"조용한 시간대 {self._cfg.get('quiet_start_hour')}~"
                             f"{self._cfg.get('quiet_end_hour')}시")
        try:
            if not self._is_screen_clear():
                return "screen", "화면이 비어있지 않음(입력창/턴 진행/떠 있는 풍선)"
            idle = self._seconds_since_activity()
            idle_min = float(self._cfg.get("idle_min_sec", 600))
            if idle < idle_min:
                return "idle", f"유휴 부족 {idle:.0f}s < {idle_min:.0f}s"
        except Exception:
            logger.debug("[initiative] 유휴 판정 콜백 실패", exc_info=True)
            return "callback", "유휴 판정 콜백 실패"
        gap, need = self._gap_status()
        if gap < need:
            return "gap", f"발화 간격 부족 {gap:.0f}s < {need:.0f}s (streak={self._ignore_streak:g})"
        return None, ""

    def _gap_status(self) -> tuple[float, float]:
        """(마지막 발화 후 경과초, 필요한 간격초) — 무시가 쌓이면 필요 간격이 늘어난다."""
        need = float(self._cfg.get("min_gap_sec", 1800))
        streak = min(self._ignore_streak, int(self._cfg.get("ignore_backoff_max", 4)))
        need *= (1 + streak)
        return (time.monotonic() - self._last_spoke), need

    def _seconds_since_activity_ok_gap(self) -> bool:
        gap, need = self._gap_status()
        return gap >= need

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
        # 간격/쿨다운은 여기서 선지불한다 — 프레이징이 도는 몇 초 사이에 다음 tick 이
        # 또 발화하는 걸 막으려면 렌더를 기다릴 수 없다. 다만 결국 렌더되지 못하면
        # _discard() 가 이 값들을 되돌린다.
        #
        # 백오프(ignore_streak)는 선지불하지 않는다. 예전에는 여기서 미리 +1 하고
        # 참여 시 리셋하는 방식이었는데, 그러면 (1) 화면에 뜨지도 않고 폐기된 발화가
        # 무시로 집계되고 (2) 실제 결과를 관측할 필요 자체가 없어져서 학습 신호가
        # 생기지 않는다. 이제는 결과가 확정될 때(_resolve) 한 번만 움직인다.
        st = self._source_state.setdefault(nudge.source_key, _SourceState())
        self._rollback = (self._last_spoke, nudge.source_key, st.last_fired)
        now = time.monotonic()
        self._last_spoke = now
        st.last_fired = now

        if self._phrase is None or not self._cfg.get("phrasing", True):
            self._render(nudge, nudge.fallback_text)
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
            self._root.after(0, lambda: self._finish_phrasing(nudge, text))

        threading.Thread(target=_worker, daemon=True, name="initiative-phrase").start()

    def _finish_phrasing(self, nudge: Nudge, text: str) -> None:
        self._phrasing_inflight = False
        # 프레이징(수 초)이 도는 사이 사용자가 입력창을 열거나 대화를 시작했을 수 있다 —
        # 그러면 뒤늦게 뜨는 nudge 가 방해가 되므로 조용히 버린다.
        try:
            if not self._is_screen_clear():
                self._discard()
                return
        except Exception:
            pass
        self._render(nudge, text)

    def _discard(self) -> None:
        """렌더되지 못한 발화의 선지불을 환불한다.

        예전에는 그냥 return 해서 간격·쿨다운이 소비된 채로 남았다 — 사용자 눈에는
        아무 일도 없었는데 다음 발화는 30분 밀리고, 그 소재는 쿨다운에 걸려 한동안
        다시 안 나왔다. 보이지 않은 발화는 없던 일로 되돌리는 게 맞다."""
        if self._rollback is None:
            return
        last_spoke, source_key, last_fired = self._rollback
        self._rollback = None
        self._last_spoke = last_spoke
        st = self._source_state.get(source_key)
        if st is not None:
            st.last_fired = last_fired
        self._log_state("discard", f"폐기 — 프레이징 완료 시점에 화면이 바빠짐 (source={source_key}, 간격·쿨다운 환불)")

    def _render(self, nudge: Nudge, text: str) -> None:
        try:
            self._show_nudge(text, self._on_nudge_click)
        except Exception:
            logger.exception("[initiative] nudge 렌더 실패")
            self._discard()  # 화면에 못 띄웠으면 폐기와 같은 취급
            return
        # 여기부터가 "결과를 기다리는" 상태 — 렌더에 성공한 발화만 판정 대상이 된다.
        self._active_nudge = nudge
        self._active_text = text
        self._active_since = time.monotonic()
        self._rollback = None
        # 판정이 끝난 뒤에도 남는 슬롯 — 다음 발화가 오면 덮인다.
        self._last_nudge = nudge
        self._last_nudge_text = text

    def _on_nudge_click(self) -> None:
        """풍선/답장 버튼 클릭 — 아직 engaged 가 아니다. 입력창이 열렸을 뿐이고,
        실제로 답장을 보냈는지(engaged) 그냥 닫았는지(acknowledged)는 main 이 판정한다."""
        return
