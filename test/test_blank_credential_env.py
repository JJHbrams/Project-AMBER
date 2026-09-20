"""빈 CLAUDE_CONFIG_DIR 이 claude CLI 인증을 깨뜨리는 회귀.

2026-09-20 실측 — overlay 를 띄운 셸에 ``CLAUDE_CONFIG_DIR=''`` 이 실려 있었다.
CLI 는 빈 문자열을 "설정 디렉터리 없음"으로 받아 ``~/.claude`` 의 OAuth 자격증명을
찾지 못하고 ``Not logged in · Please run /login`` 으로 exit 1 했다. 자동 체크포인트의
요약 호출이 전부 죽어 이틀 동안(4,807회) daily note 가 한 줄도 안 생겼다.

실패는 폴링마다 같은 WARNING 한 줄로 흘렀다. 원인 제거와 가시화를 함께 건다.
"""

import logging
import os
import unittest
from unittest import mock

from core.integrations.claude_cli_transport import drop_blank_credential_env
import core.graph.semantic.stm_promoter as stm_promoter


class BlankConfigDirTests(unittest.TestCase):
    def setUp(self):
        import core.integrations.claude_cli_transport as transport

        transport._blank_env_reported.clear()
        self.addCleanup(transport._blank_env_reported.clear)

    def test_a_blank_value_is_removed(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": ""}):
            self.assertEqual(drop_blank_credential_env(), ["CLAUDE_CONFIG_DIR"])
            self.assertNotIn("CLAUDE_CONFIG_DIR", os.environ)

    def test_whitespace_counts_as_blank(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "   "}):
            drop_blank_credential_env()
            self.assertNotIn("CLAUDE_CONFIG_DIR", os.environ)

    def test_a_real_path_is_left_alone(self):
        """빈 값만 해롭다. 진짜 격리 경로를 지우면 그쪽 설정이 무시된다."""
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": r"D:\profiles\claude"}):
            self.assertEqual(drop_blank_credential_env(), [])
            self.assertEqual(os.environ["CLAUDE_CONFIG_DIR"], r"D:\profiles\claude")

    def test_an_unset_variable_is_not_invented(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            self.assertEqual(drop_blank_credential_env(), [])
            self.assertNotIn("CLAUDE_CONFIG_DIR", os.environ)

    def test_the_removal_is_announced_once(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": ""}):
            with self.assertLogs("core.integrations.claude_cli_transport", logging.WARNING):
                drop_blank_credential_env()
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": ""}):
            with mock.patch.object(
                logging.getLogger("core.integrations.claude_cli_transport"), "warning"
            ) as warn:
                drop_blank_credential_env()
                warn.assert_not_called()

    def test_the_transport_sanitizes_before_spawning(self):
        """CLI 를 띄우는 모든 경로가 make_transport 를 지난다."""
        import core.integrations.claude_cli_transport as transport

        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": ""}), \
             mock.patch.object(transport, "PassthroughCLITransport") as spawn, \
             mock.patch.object(transport, "find_claude_cmd_parts", return_value=["claude"]):
            spawn.side_effect = lambda *a, **k: self.assertNotIn("CLAUDE_CONFIG_DIR", os.environ)
            transport.make_transport(None, mock.MagicMock())
            spawn.assert_called_once()


class SummaryFailureVisibilityTests(unittest.TestCase):
    """조용한 무한 재시도가 이틀을 잡아먹었다. 연속 실패는 크게 알린다."""

    def setUp(self):
        stm_promoter._summary_failure_streak = 0
        self.addCleanup(setattr, stm_promoter, "_summary_failure_streak", 0)

    def _fail(self, times):
        for _ in range(times):
            stm_promoter._note_summary_outcome(False, 678)

    def test_a_single_failure_stays_quiet(self):
        """한 번은 흔들림일 수 있다 — 매 폴링마다 경보를 울리면 무시하게 된다."""
        with mock.patch.object(stm_promoter.logger, "error") as err:
            self._fail(2)
            err.assert_not_called()

    def test_a_streak_raises_an_actionable_alert(self):
        with self.assertLogs(stm_promoter.logger, logging.ERROR) as caught:
            self._fail(3)
        message = caught.output[0]
        self.assertIn("CLAUDE_CONFIG_DIR", message)
        self.assertIn("daily note", message)

    def test_the_alert_does_not_repeat_every_poll(self):
        with mock.patch.object(stm_promoter.logger, "error") as err:
            self._fail(40)
            self.assertEqual(err.call_count, 1)

    def test_a_long_outage_is_re_announced(self):
        with mock.patch.object(stm_promoter.logger, "error") as err:
            self._fail(3 + stm_promoter._SUMMARY_FAILURE_ALERT_EVERY)
            self.assertEqual(err.call_count, 2)

    def test_recovery_is_reported_and_resets_the_streak(self):
        self._fail(5)
        with self.assertLogs(stm_promoter.logger, logging.WARNING) as caught:
            stm_promoter._note_summary_outcome(True, 678)
        self.assertIn("복구", caught.output[0])
        self.assertEqual(stm_promoter._summary_failure_streak, 0)

    def test_success_without_a_streak_is_silent(self):
        with mock.patch.object(stm_promoter.logger, "warning") as warn:
            stm_promoter._note_summary_outcome(True, 678)
            warn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
