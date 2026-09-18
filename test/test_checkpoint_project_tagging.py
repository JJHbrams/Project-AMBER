"""체크포인트의 프로젝트 판정 회귀 — 실제 cwd 기반.

프로젝트를 overlay 의 고정 workdir 에서 유도하던 탓에, 하루에 여러 프로젝트를
오가도 체크포인트가 전부 한 곳으로 기록됐다. 2026-09-17 실측: transcript 의
30분 버킷 56개 중 35개가 2개 이상의 cwd 를 담고 있었고, 가장 많은 프로젝트는
session-agent-orchestration(11,730줄)인데 기록은 전부 ProjectIntelContunuum
이었다.

사실은 transcript 에 줄마다 있고, 캡처가 messages.source_cwd 로 실어 나른다.
"""

import json
import unittest
from datetime import datetime

from core.memory.transcript_capture import extract_turn_record
from core.memory.daily_checkpoint import _checkpoint_block
from core.graph.semantic.stm_promoter import _observed_project_keys


def _line(cwd=None, text="안녕"):
    obj = {
        "type": "user",
        "uuid": "u-1",
        "timestamp": "2026-09-17T05:42:26.269Z",
        "sessionId": "s-1",
        "promptSource": "typed",
        "message": {"content": text},
    }
    if cwd is not None:
        obj["cwd"] = cwd
    return json.dumps(obj)


class _Row(dict):
    """sqlite3.Row 처럼 키 접근만 지원하는 최소 대역."""

    def __getitem__(self, key):
        if key not in self:
            raise KeyError(key)
        return super().__getitem__(key)


class TranscriptCwdTests(unittest.TestCase):
    def test_record_carries_cwd(self):
        rec = extract_turn_record(_line(cwd=r"C:\work\projects\session-agent-orchestration"))
        self.assertEqual(rec["cwd"], r"C:\work\projects\session-agent-orchestration")

    def test_missing_cwd_is_none(self):
        self.assertIsNone(extract_turn_record(_line())["cwd"])


class ObservedProjectKeyTests(unittest.TestCase):
    """detect_project_root 는 .git 같은 마커가 실재해야 프로젝트로 인정하므로,
    가짜 경로가 아니라 임시 디렉터리를 만들어 쓴다."""

    @classmethod
    def setUpClass(cls):
        import tempfile
        from pathlib import Path

        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cls.alpha, cls.beta = root / "alpha", root / "beta"
        for d in (cls.alpha, cls.beta):
            (d / ".git").mkdir(parents=True)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_orders_by_frequency(self):
        rows = [_Row(source_cwd=str(self.alpha)) for _ in range(3)]
        rows += [_Row(source_cwd=str(self.beta)) for _ in range(5)]
        keys = _observed_project_keys(rows)
        self.assertEqual(len(keys), 2)
        self.assertTrue(keys[0].startswith("beta"), keys)

    def test_single_project_returns_one(self):
        rows = [_Row(source_cwd=str(self.alpha)) for _ in range(4)]
        self.assertEqual(len(_observed_project_keys(rows)), 1)

    def test_blank_cwd_is_skipped_when_coverage_holds(self):
        rows = [_Row(source_cwd="")] + [_Row(source_cwd=str(self.alpha)) for _ in range(3)]
        self.assertEqual(len(_observed_project_keys(rows)), 1)

    def test_minority_sample_does_not_decide_the_span(self):
        """2026-09-17 실측: 옛 메시지 1,497건 + 새 메시지 119건인 구간에서
        뒤의 119건만으로 판정해 다른 창의 작업이 구간 전체를 대표했다."""
        rows = [_Row(source_cwd="") for _ in range(1497)]
        rows += [_Row(source_cwd=str(self.beta)) for _ in range(119)]
        self.assertEqual(_observed_project_keys(rows), [])

    def test_majority_coverage_is_enough(self):
        rows = [_Row(source_cwd="") for _ in range(4)]
        rows += [_Row(source_cwd=str(self.alpha)) for _ in range(6)]
        self.assertEqual(len(_observed_project_keys(rows)), 1)

    def test_rows_without_the_column_yield_nothing(self):
        """옛 메시지만 있는 구간은 빈 목록 — 호출부가 기존 경로로 떨어진다."""
        self.assertEqual(_observed_project_keys([_Row(role="user")]), [])

    def test_no_rows(self):
        self.assertEqual(_observed_project_keys([]), [])


class DisplayNameTests(unittest.TestCase):
    def test_path_digest_is_stripped(self):
        from core.memory.daily_checkpoint import _display_project

        self.assertEqual(_display_project("session-agent-orchestration-88eff352"),
                         "session-agent-orchestration")

    def test_name_without_digest_is_untouched(self):
        from core.memory.daily_checkpoint import _display_project

        self.assertEqual(_display_project("truviewcadmom-standalone"), "truviewcadmom-standalone")

    def test_blank_is_blank(self):
        from core.memory.daily_checkpoint import _display_project

        self.assertEqual(_display_project(""), "")


class HeadingLabelTests(unittest.TestCase):
    def _h(self, label):
        from core.memory.daily_checkpoint import _heading_label

        return _heading_label(label)

    def test_two_or_fewer_are_listed_in_full(self):
        self.assertEqual(self._h("alpha, beta"), "alpha, beta")

    def test_more_than_two_are_folded(self):
        self.assertEqual(self._h("alpha, beta, gamma, delta"), "alpha, beta 외 2개")

    def test_empty_falls_back(self):
        self.assertEqual(self._h(""), "general")


class CheckpointBlockLabelTests(unittest.TestCase):
    def _block(self, **kw):
        return _checkpoint_block("cp-1", datetime(2026, 9, 17, 17, 53), "요약", "", **kw)

    def test_multiple_projects_are_all_recorded(self):
        """헤딩은 접고 본문에 전부 남긴다 — 제목이 목록이 되면 시각이 안 보인다."""
        label = "session-agent-orchestration, projectintelcontunuum, truviewcadmom"
        out = self._block(project_label=label, project_node_id="")
        self.assertIn("### 17:53 — session-agent-orchestration, projectintelcontunuum 외 1개", out)
        self.assertIn(f"- 프로젝트: {label}", out)
        self.assertNotIn("[[", out)

    def test_single_known_project_keeps_the_wiki_link(self):
        out = self._block(project_label="engram", project_node_id="project-amber-project-engram")
        self.assertIn("- 프로젝트: [[project-amber-project-engram]]", out)


if __name__ == "__main__":
    unittest.main()
