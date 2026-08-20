"""능동적 상주(initiative) 엔진의 가드/선택 로직 단위 테스트.

tkinter·claude·DB 없이 검증한다 — root 는 after/after_cancel 만 흉내내는 가짜를,
소스는 고정 결과를 내는 스텁을 쓴다. 실제 소재 소스 중 core import 가 없는
GitStatusSource 만 subprocess 를 모킹해 직접 검증한다.
"""

import threading
import time
import unittest
from unittest.mock import patch

from overlay.bubble.initiative import (
    ACKNOWLEDGED,
    ENGAGED,
    IGNORED,
    LATE_ENGAGED,
    GitStatusSource,
    InitiativeEngine,
    MemoryEventSource,
    Nudge,
    _clip,
)


class FakeRoot:
    """root.after 는 콜백을 즉시 실행하지 않고 토큰만 돌려준다 — tick 은 테스트가
    직접 호출한다. after_cancel 은 무시."""

    def __init__(self):
        self.scheduled = []

    def after(self, ms, cb=None):
        self.scheduled.append((ms, cb))
        return f"after#{len(self.scheduled)}"

    def after_cancel(self, _token):
        pass


class StubSource:
    def __init__(self, key, nudge):
        self.key = key
        self._nudge = nudge
        self.polls = 0

    def poll(self):
        self.polls += 1
        return self._nudge


def make_engine(root=None, cfg=None, sources=None, screen_clear=True, idle=9999.0,
                show_nudge=None, phrase=None):
    """screen_clear 는 bool 또는 무인자 callable(중간에 값이 바뀌는 시나리오용)."""
    root = root or FakeRoot()
    base = {
        "enabled": True,
        "idle_min_sec": 0,
        "min_gap_sec": 0,
        "quiet_start_hour": 0,
        "quiet_end_hour": 0,  # start==end → 조용시간 없음
        "phrasing": False,
    }
    base.update(cfg or {})
    captured = []
    outcomes = []
    clear_fn = screen_clear if callable(screen_clear) else (lambda: screen_clear)
    engine = InitiativeEngine(
        root,
        base,
        is_screen_clear=clear_fn,
        seconds_since_activity=lambda: idle,
        show_nudge=show_nudge or (lambda text, cb: captured.append((text, cb))),
        phrase=phrase,
        sources=sources or [],
        on_outcome=lambda n, o, t, lat: outcomes.append((n, o, t, lat)),
    )
    engine._captured = captured  # 테스트 편의
    engine._outcomes = outcomes
    engine._root_obj = root
    return engine


class ClipTests(unittest.TestCase):
    def test_collapses_whitespace_and_truncates(self):
        self.assertEqual(_clip("a   b\n c"), "a b c")
        out = _clip("x" * 200, limit=10)
        self.assertEqual(len(out), 10)
        self.assertTrue(out.endswith("…"))


class SelectionTests(unittest.TestCase):
    def test_memory_event_has_highest_priority(self):
        n_memory = Nudge("memory", "memory")
        n_unf = Nudge("unfinished", "unf")
        engine = make_engine(sources=[
            StubSource("unfinished", n_unf),
            StubSource("memory", n_memory),
        ])
        self.assertIs(engine._select_nudge(), n_memory)

    def test_priority_order_unfinished_before_git(self):
        n_unf = Nudge("unfinished", "unf")
        n_git = Nudge("git", "git")
        engine = make_engine(sources=[
            StubSource("git", n_git),
            StubSource("unfinished", n_unf),
        ])
        self.assertIs(engine._select_nudge(), n_unf)

    def test_disabled_source_skipped(self):
        n_unf = Nudge("unfinished", "unf")
        n_git = Nudge("git", "git")
        engine = make_engine(
            cfg={"sources": {"unfinished": {"enabled": False}}},
            sources=[StubSource("unfinished", n_unf), StubSource("git", n_git)],
        )
        self.assertIs(engine._select_nudge(), n_git)

    def test_cooldown_skips_recently_fired_source(self):
        n_unf = Nudge("unfinished", "unf")
        n_git = Nudge("git", "git")
        engine = make_engine(
            cfg={"sources": {"unfinished": {"enabled": True, "cooldown_sec": 9999}}},
            sources=[StubSource("unfinished", n_unf), StubSource("git", n_git)],
        )
        # unfinished 를 방금 발화한 것으로 표시 → 쿨다운에 걸려 git 이 뽑혀야 한다.
        engine._speak(n_unf)
        engine._captured.clear()
        self.assertIs(engine._select_nudge(), n_git)

    def test_returns_none_when_all_sources_dry(self):
        engine = make_engine(sources=[StubSource("git", None)])
        self.assertIsNone(engine._select_nudge())


class MemoryEventSourceTests(unittest.TestCase):
    def test_non_memory_events_are_ignored(self):
        source = MemoryEventSource()
        source.feed_event({"kind": "thought", "tool_name": "mcp__engram__kg_search"})
        source.feed_event({"kind": "tool_use", "tool_name": "web_search"})
        source.feed_event({"kind": "tool_result", "tool_name": "mcp__engram__kg_search"})
        self.assertIsNone(source.poll())

    def test_memory_events_coalesce_and_candidate_is_consumed_once(self):
        source = MemoryEventSource()
        source.feed_event({
            "kind": "tool_use",
            "tool_name": "mcp__engram__kg_search",
            "tool_input": {"secret": "ignored"},
        })
        source.feed_event({"kind": "tool_use", "tool_name": "memory_lookup", "tool_output": "ignored"})
        candidate = source.poll()
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.source_key, "memory")
        self.assertIsNone(source.poll())

    def test_disabled_source_does_not_queue_events(self):
        source = MemoryEventSource()
        engine = make_engine(
            cfg={"sources": {"memory": {"enabled": False}}},
            sources=[source],
        )
        engine.feed_event({"kind": "tool_use", "tool_name": "mcp__engram__kg_search"})
        self.assertIsNone(source.poll())

    def test_master_disabled_does_not_leave_stale_candidate_after_enable(self):
        source = MemoryEventSource()
        engine = make_engine(cfg={"enabled": False}, sources=[source])
        engine.feed_event({"kind": "tool_use", "tool_name": "mcp__engram__kg_search"})
        engine.update_cfg({
            "enabled": True,
            "idle_min_sec": 0,
            "min_gap_sec": 0,
            "quiet_start_hour": 0,
            "quiet_end_hour": 0,
            "phrasing": False,
        })
        self.assertIsNone(source.poll())
        self.assertIsNone(engine._select_nudge())

    def test_source_cooldown_blocks_next_queued_candidate(self):
        source = MemoryEventSource()
        engine = make_engine(
            cfg={"sources": {"memory": {"enabled": True, "cooldown_sec": 9999}}},
            sources=[source],
        )
        event = {"kind": "tool_use", "tool_name": "mcp__engram__kg_search"}
        # The source state starts at monotonic zero.  Pin time beyond the
        # configured cooldown so this test is independent of host uptime
        # (notably immediately after a reboot).
        with patch("overlay.bubble.initiative.time.monotonic", return_value=10_000.0):
            engine.feed_event(event)
            first = engine._select_nudge()
            self.assertIsNotNone(first)
            engine._speak(first)
        with patch("overlay.bubble.initiative.time.monotonic", return_value=10_001.0):
            engine.feed_event(event)
            self.assertIsNone(engine._select_nudge())


class GuardTests(unittest.TestCase):
    def test_disabled_never_speaks(self):
        engine = make_engine(cfg={"enabled": False})
        self.assertFalse(engine._should_speak())

    def test_screen_not_clear_blocks(self):
        engine = make_engine(screen_clear=False)
        self.assertFalse(engine._should_speak())

    def test_not_idle_long_enough_blocks(self):
        engine = make_engine(cfg={"idle_min_sec": 600}, idle=10.0)
        self.assertFalse(engine._should_speak())

    def test_all_conditions_met_allows(self):
        engine = make_engine()
        self.assertTrue(engine._should_speak())

    def test_min_gap_blocks_second_speak(self):
        engine = make_engine(cfg={"min_gap_sec": 99999})
        self.assertTrue(engine._should_speak())
        engine._speak(Nudge("git", "hi"))
        self.assertFalse(engine._should_speak())  # 방금 발화 → 간격 미충족

    def test_speaking_alone_does_not_touch_backoff(self):
        """발화만으로는 백오프가 움직이지 않는다 — 결과를 봐야 안다.

        예전엔 _speak 이 미리 +1 하고 참여 시 리셋했는데, 그러면 화면에 뜨지도 못하고
        폐기된 발화까지 무시로 집계됐다."""
        engine = make_engine(cfg={"min_gap_sec": 10})
        engine._speak(Nudge("git", "1"))
        self.assertEqual(engine._ignore_streak, 0.0)
        self.assertTrue(engine.has_pending_outcome())

    def test_ignore_backoff_widens_gap(self):
        engine = make_engine(cfg={"min_gap_sec": 10, "ignore_backoff_max": 3})
        engine._speak(Nudge("git", "1"))
        engine.notify_ignored()
        self.assertEqual(engine._ignore_streak, 1.0)
        engine._speak(Nudge("git", "2"))
        engine.notify_engaged()
        self.assertEqual(engine._ignore_streak, 0.0)


class QuietHoursTests(unittest.TestCase):
    def _hour(self, engine, h):
        with patch("overlay.bubble.initiative.datetime") as dt:
            dt.now.return_value.hour = h
            return engine._in_quiet_hours()

    def test_overnight_range_22_to_8(self):
        engine = make_engine(cfg={"quiet_start_hour": 22, "quiet_end_hour": 8})
        self.assertTrue(self._hour(engine, 23))
        self.assertTrue(self._hour(engine, 2))
        self.assertFalse(self._hour(engine, 12))

    def test_same_start_end_means_no_quiet(self):
        engine = make_engine(cfg={"quiet_start_hour": 9, "quiet_end_hour": 9})
        self.assertFalse(self._hour(engine, 9))


class SpeakRenderTests(unittest.TestCase):
    def test_speak_without_phrasing_renders_fallback(self):
        engine = make_engine()  # phrase=None, phrasing False
        engine._speak(Nudge("git", "fallback text"))
        self.assertEqual(len(engine._captured), 1)
        self.assertEqual(engine._captured[0][0], "fallback text")
        self.assertEqual(engine.active_nudge_text(), "fallback text")


class OutcomeTests(unittest.TestCase):
    """결과 3분화 — 발화 하나당 정확히 한 번, 대기 중일 때만 집계된다."""

    def test_engaged_reports_meta_and_shown_text(self):
        engine = make_engine()
        nudge = Nudge("curiosity", "fb", topic="쿼터니언", ref_id=42)
        engine._speak(nudge)
        engine.notify_engaged()
        self.assertEqual(len(engine._outcomes), 1)
        got, outcome, shown, latency = engine._outcomes[0]
        self.assertEqual(outcome, ENGAGED)
        self.assertEqual(got.ref_id, 42)          # 호기심 해소에 쓰인다
        self.assertEqual(got.topic, "쿼터니언")
        self.assertEqual(shown, "fb")             # 화면에 실제로 뜬 문구
        self.assertGreaterEqual(latency, 0.0)     # 반응까지 걸린 시간

    def test_latency_measured_from_render(self):
        """같은 ignored 라도 즉시 물린 것과 dwell 을 다 채운 것은 정반대 신호다 —
        로그에 안 남기면 나중에 소급 복원할 방법이 없어서 여기서 반드시 재야 한다."""
        engine = make_engine()
        engine._speak(Nudge("git", "x"))
        engine._active_since -= 12.0   # 12초 전에 떴던 것으로 되돌림
        engine.notify_ignored()
        latency = engine._outcomes[0][3]
        self.assertGreaterEqual(latency, 12.0)
        self.assertLess(latency, 13.0)

    def test_acknowledged_is_half_step(self):
        engine = make_engine()
        engine._speak(Nudge("git", "x"))
        engine.notify_acknowledged()
        self.assertEqual(engine._ignore_streak, 0.5)
        self.assertEqual(engine._outcomes[0][1], ACKNOWLEDGED)

    def test_latency_not_carried_between_nudges(self):
        """앞 발화의 기준점이 남아 다음 발화의 지연이 부풀지 않아야 한다."""
        engine = make_engine()
        engine._speak(Nudge("git", "1"))
        engine._active_since -= 30.0
        engine.notify_ignored()
        engine._speak(Nudge("git", "2"))
        engine.notify_engaged()
        self.assertLess(engine._outcomes[1][3], 1.0)

    def test_ignored_is_full_step(self):
        engine = make_engine()
        engine._speak(Nudge("git", "x"))
        engine.notify_ignored()
        self.assertEqual(engine._ignore_streak, 1.0)
        self.assertEqual(engine._outcomes[0][1], IGNORED)

    def test_outcome_counted_only_once(self):
        """페이드와 클릭이 겹쳐 들어와도 한 발화는 한 번만 집계된다."""
        engine = make_engine()
        engine._speak(Nudge("git", "x"))
        engine.notify_ignored()
        engine.notify_ignored()
        engine.notify_engaged()
        self.assertEqual(len(engine._outcomes), 1)
        self.assertEqual(engine._ignore_streak, 1.0)
        self.assertFalse(engine.has_pending_outcome())

    def test_notify_without_active_nudge_is_noop(self):
        """F2 회귀 — 자율발화와 무관한 평소 대화가 백오프를 리셋하면 안 된다."""
        engine = make_engine()
        engine._speak(Nudge("git", "x"))
        engine.notify_ignored()          # streak 1.0
        engine.notify_engaged()          # 대기 중인 발화 없음 → 무시돼야 함
        engine.notify_engaged()
        self.assertEqual(engine._ignore_streak, 1.0)
        self.assertEqual(len(engine._outcomes), 1)


class StateLogTests(unittest.TestCase):
    """보류 이유 로깅 — 같은 조건이 계속되면 딱 한 번만 찍혀야 한다."""

    def test_same_reason_logs_once_even_though_numbers_change(self):
        """경과 초가 메시지에 들어가서 매 tick 문자열이 달라져도 중복 로그가 나면 안 된다.
        (실제로 45초마다 '발화 간격 부족 2655s < 3600s' 가 도배됐다.)"""
        clock = {"t": 0.0}
        engine = make_engine(cfg={"min_gap_sec": 99999})
        engine._speak(Nudge("git", "x"))
        engine._captured.clear()
        with patch("overlay.bubble.initiative.logger") as log:
            for _ in range(5):
                clock["t"] += 45
                engine._tick()
            reasons = [c.args[1] for c in log.info.call_args_list]
        self.assertEqual(len(reasons), 1, f"중복 로그: {reasons}")
        self.assertIn("발화 간격 부족", reasons[0])

    def test_reason_change_logs_again(self):
        engine = make_engine(cfg={"enabled": False})
        with patch("overlay.bubble.initiative.logger") as log:
            engine._tick()                      # disabled
            engine._cfg["enabled"] = True
            engine._cfg["idle_min_sec"] = 99999
            engine._tick()                      # idle 부족 — 이유가 바뀌었으니 다시 찍힘
            engine._tick()                      # 같은 이유 — 안 찍힘
            reasons = [c.args[1] for c in log.info.call_args_list]
        self.assertEqual(len(reasons), 2, f"{reasons}")


class LateEngageTests(unittest.TestCase):
    """판정이 끝난 뒤 캐릭터를 눌러 지난 발화를 되살려 답하는 경로.

    dwell 25초는 **결과를 판정하는 창**이고 답할 수 있는 창이 아니다 — 사용자는 아무
    때나 "마지막에 뭐라고 했더라"를 확인하고 그때 답할 수 있어야 한다."""

    def _spoke_and_ignored(self):
        engine = make_engine()
        engine._speak(Nudge("curiosity", "fb", topic="쿼터니언", ref_id=7))
        engine.notify_ignored()
        return engine

    def test_engage_payload_survives_outcome(self):
        engine = self._spoke_and_ignored()
        # 연속성(prepend)에 쓸 발화 문구가 판정 후에도 남아있어야 한다.
        self.assertEqual(engine.active_nudge_text(), "fb")

    def test_late_engage_is_separate_signal(self):
        engine = self._spoke_and_ignored()
        self.assertEqual(engine._ignore_streak, 1.0)
        engine.notify_late_engaged()
        # 이미 기록된 ignored 는 그대로 두고 별도 항목으로 남는다.
        self.assertEqual([o[1] for o in engine._outcomes], [IGNORED, LATE_ENGAGED])
        nudge = engine._outcomes[1][0]
        self.assertEqual(nudge.ref_id, 7)          # 호기심 해소까지 이어진다
        self.assertEqual(engine._ignore_streak, 0.0)  # 결국 응했으니 백오프 리셋

    def test_late_engage_only_once(self):
        engine = self._spoke_and_ignored()
        engine.notify_late_engaged()
        engine.notify_late_engaged()
        self.assertEqual(len(engine._outcomes), 2)  # ignored + late 1건

    def test_new_nudge_replaces_late_target(self):
        """지난 발화 슬롯은 새 발화가 덮는다 — 오래된 프롬프트가 되살아나면 안 된다."""
        engine = self._spoke_and_ignored()
        engine._speak(Nudge("git", "새 발화", topic="dev"))
        engine.notify_ignored()
        self.assertEqual(engine.active_nudge_text(), "새 발화")
        engine.notify_late_engaged()
        self.assertEqual(engine._outcomes[-1][0].source_key, "git")


class DiscardTests(unittest.TestCase):
    """프레이징이 도는 사이 화면이 바빠져 렌더를 못 한 경우 — 선지불 환불."""

    def _speak_then_finish(self, clear_at_finish):
        clear = {"v": True}
        engine = make_engine(
            cfg={"phrasing": True, "min_gap_sec": 1000,
                 "sources": {"git": {"enabled": True, "cooldown_sec": 1000}}},
            screen_clear=lambda: clear["v"],
            phrase=lambda ctx, fb: "프레이징된 한마디",
        )
        engine._speak(Nudge("git", "fb"))
        # 워커 스레드가 root.after(0, ...) 로 넘긴 콜백을 직접 돌린다(FakeRoot 는 실행 안 함).
        deadline = time.monotonic() + 3.0
        while not engine._root_obj.scheduled and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(engine._root_obj.scheduled, "프레이징 완료 콜백이 예약되지 않음")
        clear["v"] = clear_at_finish
        engine._root_obj.scheduled[-1][1]()
        engine._clear_flag = clear
        return engine

    def test_discard_refunds_gap_and_cooldown(self):
        engine = self._speak_then_finish(clear_at_finish=False)
        self.assertEqual(engine._captured, [])            # 화면에 안 뜸
        self.assertFalse(engine.has_pending_outcome())    # 판정 대상 아님
        self.assertEqual(engine._outcomes, [])            # 무시로 집계되지 않음
        self.assertEqual(engine._ignore_streak, 0.0)
        self.assertEqual(engine._last_spoke, 0.0)         # 간격 환불
        self.assertEqual(engine._source_state["git"].last_fired, 0.0)  # 쿨다운 환불
        # 사용자가 하던 걸 끝내 화면이 다시 비면, 밀리지 않고 바로 재시도할 수 있어야 한다.
        engine._clear_flag["v"] = True
        self.assertTrue(engine._should_speak())

    def test_rendered_nudge_keeps_payment_and_awaits_outcome(self):
        engine = self._speak_then_finish(clear_at_finish=True)
        self.assertEqual(len(engine._captured), 1)
        self.assertEqual(engine._captured[0][0], "프레이징된 한마디")
        self.assertTrue(engine.has_pending_outcome())
        self.assertEqual(engine.active_nudge_text(), "프레이징된 한마디")
        self.assertNotEqual(engine._last_spoke, 0.0)      # 간격은 소비된 채 유지
        self.assertFalse(engine._should_speak())

    def test_discard_restores_consumed_memory_candidate(self):
        clear = {"v": True}
        source = MemoryEventSource()
        engine = make_engine(
            cfg={"phrasing": True, "min_gap_sec": 1000,
                 "sources": {"memory": {"enabled": True, "cooldown_sec": 1000}}},
            sources=[source],
            screen_clear=lambda: clear["v"],
            phrase=lambda ctx, fb: "프레이징된 기억 발화",
        )
        engine.feed_event({"kind": "tool_use", "tool_name": "mcp__engram__kg_search"})
        candidate = engine._select_nudge()
        self.assertIsNotNone(candidate)
        engine._speak(candidate)
        deadline = time.monotonic() + 3.0
        while not engine._root_obj.scheduled and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(engine._root_obj.scheduled)
        clear["v"] = False
        engine._root_obj.scheduled[-1][1]()
        self.assertEqual(engine._last_spoke, 0.0)
        self.assertEqual(engine._source_state["memory"].last_fired, 0.0)
        self.assertIs(source.poll(), candidate)


class GitSourceTests(unittest.TestCase):
    def _source_with_git(self, outputs):
        src = GitStatusSource(lambda: ".")
        src._git = lambda *args: outputs.get(args, "")
        return src

    def test_dirty_working_tree(self):
        src = self._source_with_git({
            ("rev-parse", "--abbrev-ref", "HEAD"): "dev\n",
            ("status", "--porcelain"): " M a.py\n?? b.py\n",
            ("rev-list", "--count", "@{upstream}..HEAD"): "0\n",
        })
        nudge = src.poll()
        self.assertIsNotNone(nudge)
        self.assertEqual(nudge.source_key, "git")
        self.assertIn("2", nudge.fallback_text)

    def test_ahead_but_clean(self):
        src = self._source_with_git({
            ("rev-parse", "--abbrev-ref", "HEAD"): "dev\n",
            ("status", "--porcelain"): "",
            ("rev-list", "--count", "@{upstream}..HEAD"): "3\n",
        })
        nudge = src.poll()
        self.assertIsNotNone(nudge)
        self.assertIn("3", nudge.fallback_text)

    def test_clean_and_synced_returns_none(self):
        src = self._source_with_git({
            ("rev-parse", "--abbrev-ref", "HEAD"): "dev\n",
            ("status", "--porcelain"): "",
            ("rev-list", "--count", "@{upstream}..HEAD"): "0\n",
        })
        self.assertIsNone(src.poll())

    def test_not_a_git_repo_returns_none(self):
        src = self._source_with_git({})  # 모든 git 호출이 "" → 브랜치 없음
        self.assertIsNone(src.poll())


if __name__ == "__main__":
    unittest.main()
