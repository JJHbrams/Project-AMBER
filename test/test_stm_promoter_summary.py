import unittest
from unittest.mock import patch

from core.graph.semantic import stm_promoter


class StmPromoterSummaryTest(unittest.TestCase):
    @patch("core.graph.semantic.stm_promoter._call_claude_once")
    def test_working_memory_summary_uses_claude_code(self, call_claude):
        call_claude.return_value = "요약: 설치 문제를 수정함\n다음 작업: 배포 빌드"

        result = stm_promoter._summarize_working_memory_with_claude(
            [{"role": "user", "content": "installer를 고쳐줘"}]
        )

        self.assertEqual(result, {"summary": "설치 문제를 수정함", "open_intents": "배포 빌드"})
        call_claude.assert_called_once()

    @patch("core.graph.semantic.stm_promoter._call_claude_once")
    def test_promotion_summary_uses_claude_code(self, call_claude):
        call_claude.return_value = "템플릿과 directives 설치 누락을 수정했다."

        result = stm_promoter._summarize_with_claude(
            [{"role": "assistant", "content": "수정 완료"}]
        )

        self.assertEqual(result, "템플릿과 directives 설치 누락을 수정했다.")
        call_claude.assert_called_once()


if __name__ == "__main__":
    unittest.main()
