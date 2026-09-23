"""Deterministic behavior and lifecycle coverage for situational humor."""

import unittest

from core.identity.personality import (
    PERSONALITY_MODULES,
    clear_situational_humor_state,
    evaluate_situational_humor,
)


class SituationalHumorTests(unittest.TestCase):
    def tearDown(self):
        clear_situational_humor_state(101)
        clear_situational_humor_state(202)

    def test_only_the_approved_module_is_registered(self):
        self.assertEqual(PERSONALITY_MODULES, ("situational_humor",))

    def test_bootstrap_and_empty_query_are_adaptive(self):
        self.assertEqual(evaluate_situational_humor(.9, is_bootstrap=True).mode, "adaptive")
        self.assertEqual(evaluate_situational_humor(.9, user_query="").mode, "adaptive")

    def test_sensitive_context_is_off(self):
        self.assertEqual(evaluate_situational_humor(.9, user_query="응급 상황입니다").mode, "off")

    def test_low_risk_context_is_light_above_threshold(self):
        self.assertEqual(evaluate_situational_humor(.35, user_query="테스트 결과를 정리해줘").mode, "light")
        self.assertEqual(evaluate_situational_humor(.34, user_query="테스트 결과를 정리해줘").mode, "adaptive")

    def test_cadence_is_session_isolated_and_stateless_unknowns_do_not_suppress(self):
        first = evaluate_situational_humor(.8, user_query="문서 정리", session_key=101, now=100)
        repeat = evaluate_situational_humor(.8, user_query="문서 정리", session_key=101, now=101)
        other = evaluate_situational_humor(.8, user_query="문서 정리", session_key=202, now=101)
        unknown = evaluate_situational_humor(.8, user_query="문서 정리", now=101)
        self.assertEqual((first.mode, repeat.mode, other.mode, unknown.mode), ("light", "adaptive", "light", "light"))

    def test_cleanup_and_ttl_clear_cadence(self):
        evaluate_situational_humor(.8, user_query="문서 정리", session_key=101, now=100)
        clear_situational_humor_state(101)
        self.assertEqual(evaluate_situational_humor(.8, user_query="문서 정리", session_key=101, now=101).mode, "light")
        self.assertEqual(evaluate_situational_humor(.8, user_query="문서 정리", session_key=101, now=2001).mode, "light")


if __name__ == "__main__":
    unittest.main()
