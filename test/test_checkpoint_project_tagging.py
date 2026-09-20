"""체크포인트의 프로젝트 판정 회귀 — 실제 cwd 기반.

프로젝트를 overlay 의 고정 workdir 에서 유도하던 탓에, 하루에 여러 프로젝트를
오가도 체크포인트가 전부 한 곳으로 기록됐다. 2026-09-17 실측: transcript 의
30분 버킷 56개 중 35개가 2개 이상의 cwd 를 담고 있었고, 가장 많은 프로젝트는
session-agent-orchestration(11,730줄)인데 기록은 전부 ProjectIntelContunuum
이었다.

사실은 transcript 에 줄마다 있고, 캡처가 messages.source_cwd 로 실어 나른다.
"""

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

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


class SectionHeadingTests(unittest.TestCase):
    def _h(self, project_key, project_keys=None):
        from core.memory.daily_checkpoint import _section_heading

        return _section_heading(project_key, project_keys)

    def test_dominant_project_becomes_the_section(self):
        """project_keys 는 많이 나온 순이라 앞이 대표다."""
        self.assertEqual(
            self._h("fallback", ["truviewcadmom-2586e5d8", "projectintelcontunuum-a323e1a1"]),
            "truviewcadmom",
        )

    def test_falls_back_to_the_project_key(self):
        self.assertEqual(self._h("session-agent-orchestration-88eff352", []), "session-agent-orchestration")

    def test_empty_falls_back(self):
        self.assertEqual(self._h("", None), "general")


class CheckpointBlockLabelTests(unittest.TestCase):
    def _block(self, **kw):
        return _checkpoint_block("cp-1", datetime(2026, 9, 17, 17, 53), "요약", "", **kw)

    def test_time_is_the_only_heading(self):
        """프로젝트는 바깥 섹션이 말한다. 항목 제목에는 시각만 남는다."""
        out = self._block(project_label="engram", project_node_id="")
        self.assertIn("## 17:53", out)
        self.assertNotIn("###", out)
        self.assertNotIn("—", out)

    def test_multiple_projects_are_all_recorded(self):
        """섹션은 대표 하나뿐이므로 나머지는 이 줄에서만 확인할 수 있다."""
        keys = ["session-agent-orchestration", "projectintelcontunuum", "truviewcadmom"]
        with mock.patch("core.memory.daily_checkpoint.resolve_kg_node_id", return_value=None):
            out = self._block(project_label="", project_node_id="", project_keys=keys)
        self.assertIn(f"- 프로젝트: {', '.join(keys)}", out)
        self.assertNotIn("[[", out)

    def test_each_resolved_project_is_linked(self):
        nodes = {"claude-image-forge": "claude-image-forge-overview", "mystery": None}
        with mock.patch("core.memory.daily_checkpoint.resolve_kg_node_id", side_effect=nodes.get):
            out = self._block(project_label="", project_node_id="",
                              project_keys=["claude-image-forge", "mystery"])
        self.assertIn("- 프로젝트: [[claude-image-forge-overview]], mystery", out)

    def test_single_known_project_keeps_the_wiki_link(self):
        out = self._block(project_label="engram", project_node_id="project-amber-project-engram")
        self.assertIn("- 프로젝트: [[project-amber-project-engram]]", out)


class ProjectSectionWriterTests(unittest.TestCase):
    """프로젝트가 바깥 축, 시각이 안쪽 축인 daily note 구조."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "2026-09-18.md"

    def _write(self, heading, time_text, checkpoint_id="cp"):
        from core.memory.daily_checkpoint import _upsert_project_section

        hour, minute = (int(part) for part in time_text.split(":"))
        block = f"<!-- engram-checkpoint:{checkpoint_id} -->\n## {time_text}\n- 요약: {checkpoint_id}"
        return _upsert_project_section(
            self.path, f"engram-checkpoint:{checkpoint_id}", "---\nid: daily\n---\n",
            heading, block, hour * 60 + minute,
        )

    def _headings(self):
        return [line for line in self.path.read_text(encoding="utf-8").splitlines()
                if line.startswith("#")]

    def test_project_is_the_outer_heading_and_time_the_inner_one(self):
        self._write("projA", "12:30", "a1")
        self.assertEqual(self._headings(), ["# projA", "## 12:30"])

    def test_same_project_accumulates_under_one_heading(self):
        self._write("projA", "12:30", "a1")
        self._write("projA", "15:06", "a2")
        self.assertEqual(self._headings(), ["# projA", "## 12:30", "## 15:06"])

    def test_entries_are_ordered_by_time_not_arrival(self):
        self._write("projA", "15:06", "a2")
        self._write("projA", "12:30", "a1")
        self.assertEqual(self._headings(), ["# projA", "## 12:30", "## 15:06"])

    def test_an_earlier_entry_does_not_split_a_later_one(self):
        """앵커가 시각 줄이면 새 블록이 기존 주석과 제목 사이로 끼어든다."""
        self._write("projA", "15:06", "a2")
        self._write("projA", "12:30", "a1")
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("<!-- engram-checkpoint:a2 -->\n## 15:06", text)
        self.assertIn("<!-- engram-checkpoint:a1 -->\n## 12:30", text)

    def test_a_new_project_gets_its_own_section(self):
        self._write("projA", "12:30", "a1")
        self._write("projB", "09:30", "b1")
        self._write("projA", "15:06", "a2")
        self.assertEqual(self._headings(),
                         ["# projA", "## 12:30", "## 15:06", "# projB", "## 09:30"])

    def test_the_same_checkpoint_is_not_written_twice(self):
        self.assertTrue(self._write("projA", "12:30", "a1"))
        self.assertFalse(self._write("projA", "12:30", "a1"))
        self.assertEqual(self._headings().count("## 12:30"), 1)

    def test_spacing_does_not_drift_with_repeated_writes(self):
        """끼워넣기를 반복해도 항목 간격이 호출 순서를 타지 않는다."""
        for i, time_text in enumerate(["15:06", "12:30", "16:40", "09:05", "13:00"]):
            self._write("projA", time_text, f"a{i}")
        self._write("projB", "10:00", "b0")
        self._write("projA", "11:11", "a9")
        self.assertNotIn("\n\n\n", self.path.read_text(encoding="utf-8"))

    def test_a_preamble_under_a_project_is_kept(self):
        """섹션을 다시 렌더링해도 사람이 써 둔 머리글은 지우지 않는다."""
        self._write("projA", "15:06", "a1")
        text = self.path.read_text(encoding="utf-8")
        self.path.write_text(text.replace("# projA\n", "# projA\n\n사람이 쓴 메모\n"), encoding="utf-8")
        self._write("projA", "12:30", "a2")
        self.assertIn("사람이 쓴 메모", self.path.read_text(encoding="utf-8"))

    def test_frontmatter_survives_an_insert_before_the_first_section(self):
        self._write("projA", "15:06", "a2")
        self._write("projA", "12:30", "a1")
        self.assertTrue(self.path.read_text(encoding="utf-8").startswith("---\nid: daily\n---\n"))


if __name__ == "__main__":
    unittest.main()
