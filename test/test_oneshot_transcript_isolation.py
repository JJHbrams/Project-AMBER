"""engram 내부 단발 LLM 호출이 사용자 대화로 다시 적재되지 않게 하는 회귀.

2026-11-20 실측: 열린 engram 세션 5,046개 중 약 3,660개가 engram 자신의
요약·일지·말풍선 프레이징 호출이었다. 그 호출이 홈 디렉터리를 cwd 로 써서
transcript 가 사용자 대화와 같은 폴더에 남았고, 캡처 루프가 그걸 대화로
읽어 세션을 하나씩 열었다(닫히지 않아 쌓임).
"""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from core.memory import transcript_capture as tc


class OneshotTranscriptIsolationTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self._home = mock.patch.object(Path, "home", return_value=self.home)
        self._home.start()
        self.projects = self.home / ".claude" / "projects"

    def tearDown(self):
        self._home.stop()

    def _transcript(self, dir_name, name, age_seconds=0):
        d = self.projects / dir_name
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{name}.jsonl"
        f.write_text("{}\n", encoding="utf-8")
        t = time.time() - age_seconds
        os.utime(f, (t, t))
        return f

    def test_oneshot_dir_name_matches_claude_encoding(self):
        # Claude 는 영숫자 외 문자를 모두 '-' 로 바꾼다 ('.engram' → '-engram').
        expected = tc._claude_dir_name(self.home) + "--engram-llm-oneshot"
        self.assertEqual(tc._claude_dir_name(tc.engram_oneshot_cwd()), expected)

    def test_capture_skips_engram_internal_calls_even_when_newest(self):
        user = self._transcript(tc._claude_dir_name(self.home / "proj"), "user-chat", age_seconds=60)
        self._transcript(tc._claude_dir_name(tc.engram_oneshot_cwd()), "internal-summary")
        self.assertEqual(tc.find_active_transcript_any(), user)

    def test_capture_still_sees_real_home_directory_chats(self):
        home_chat = self._transcript(tc._claude_dir_name(self.home), "home-chat")
        self.assertEqual(tc.find_active_transcript_any(), home_chat)

    def test_oneshot_calls_run_in_the_isolated_cwd(self):
        from core.identity import reflection_client

        seen = {}

        async def fake_query(prompt, options, transport):
            seen["cwd"] = options.cwd
            if False:
                yield None

        with mock.patch.object(reflection_client, "_sdk_query", fake_query), \
                mock.patch.object(reflection_client, "_make_transport", return_value=None):
            reflection_client.call_claude_resumable("x", timeout=10)

        self.assertEqual(Path(seen["cwd"]), tc.engram_oneshot_cwd())
        self.assertTrue(tc.engram_oneshot_cwd().is_dir())


class ReflectionResumeTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self._home = mock.patch.object(Path, "home", return_value=self.home)
        self._home.start()
        from core.graph.semantic import stm_promoter

        self.sp = stm_promoter
        self.state = self.home / ".engram" / "_reflection_session.json"
        self.state.parent.mkdir(parents=True)
        self._file = mock.patch.object(stm_promoter, "_REFLECTION_SESSION_FILE", self.state)
        self._file.start()
        self.projects = self.home / ".claude" / "projects"

    def tearDown(self):
        self._file.stop()
        self._home.stop()

    def _dir(self, cwd):
        d = self.projects / tc._claude_dir_name(cwd)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def test_legacy_home_transcript_is_moved_and_resumed(self):
        (self._dir(self.home) / "sid-1.jsonl").write_text("{}\n", encoding="utf-8")
        self.state.write_text(json.dumps({"persona_session_id": "sid-1"}))

        self.assertEqual(self.sp._get_reflection_session_id(), "sid-1")
        self.assertTrue((self._dir(tc.engram_oneshot_cwd()) / "sid-1.jsonl").is_file())
        self.assertEqual(json.loads(self.state.read_text())["cwd"], "oneshot")

    def test_missing_transcript_starts_a_fresh_session(self):
        """Claude 가 지운 transcript 의 id 로 resume 하면 매번 exit 1 로 실패한다."""
        self.state.write_text(json.dumps({"persona_session_id": "gone", "cwd": "oneshot"}))
        self.assertIsNone(self.sp._get_reflection_session_id())

    def test_isolated_transcript_resumes(self):
        (self._dir(tc.engram_oneshot_cwd()) / "sid-2.jsonl").write_text("{}\n", encoding="utf-8")
        self.state.write_text(json.dumps({"persona_session_id": "sid-2", "cwd": "oneshot"}))
        self.assertEqual(self.sp._get_reflection_session_id(), "sid-2")


if __name__ == "__main__":
    unittest.main()
