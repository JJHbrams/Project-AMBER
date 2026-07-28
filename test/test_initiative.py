"""능동적 상주(initiative) 엔진의 가드/선택 로직 단위 테스트.

tkinter·claude·DB 없이 검증한다 — root 는 after/after_cancel 만 흉내내는 가짜를,
소스는 고정 결과를 내는 스텁을 쓴다. 실제 소재 소스 중 core import 가 없는
GitStatusSource 만 subprocess 를 모킹해 직접 검증한다.
"""

import unittest
from unittest.mock import patch

from overlay.bubble.initiative import (
    GitStatusSource,
    InitiativeEngine,
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
    engine = InitiativeEngine(
        root,
        base,
        is_screen_clear=lambda: screen_clear,
        seconds_since_activity=lambda: idle,
        show_nudge=show_nudge or (lambda text, cb: captured.append((text, cb))),
        phrase=phrase,
        sources=sources or [],
    )
    engine._captured = captured  # 테스트 편의
    return engine


class ClipTests(unittest.TestCase):
    def test_collapses_whitespace_and_truncates(self):
        self.assertEqual(_clip("a   b\n c"), "a b c")
        out = _clip("x" * 200, limit=10)
        self.assertEqual(len(out), 10)
        self.assertTrue(out.endswith("…"))


class SelectionTests(unittest.TestCase):
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

    def test_ignore_backoff_widens_gap(self):
        engine = make_engine(cfg={"min_gap_sec": 10, "ignore_backoff_max": 3})
        engine._speak(Nudge("git", "1"))
        self.assertEqual(engine._ignore_streak, 1)
        engine.notify_engaged()
        self.assertEqual(engine._ignore_streak, 0)


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
        engine._speak(Nudge("git", "fallback text", engage_prompt="do it"))
        self.assertEqual(len(engine._captured), 1)
        self.assertEqual(engine._captured[0][0], "fallback text")
        self.assertEqual(engine.take_pending_engage(), "do it")
        self.assertIsNone(engine.take_pending_engage())  # 한 번만 꺼내진다


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
