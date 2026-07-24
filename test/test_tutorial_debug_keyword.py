import unittest

from core.tutorial.progress import contains_tutorial_debug_keyword


class TutorialDebugKeywordTests(unittest.TestCase):
    def test_matches_korean_keyword(self):
        self.assertTrue(contains_tutorial_debug_keyword("튜토리얼 디버그 통과"))

    def test_matches_english_keyword(self):
        self.assertTrue(contains_tutorial_debug_keyword("tutorial debug pass"))

    def test_embedded_keyword_does_not_match(self):
        self.assertFalse(contains_tutorial_debug_keyword("이번 단계는 tutorial debug pass 로 처리"))

    def test_non_keyword_text_does_not_match(self):
        self.assertFalse(contains_tutorial_debug_keyword("일반 진행 문장"))


if __name__ == "__main__":
    unittest.main()
