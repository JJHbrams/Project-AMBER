"""overlay 가 quit 정리 뒤 남은 작업 스레드를 기다리지 않고 끝나는지 (2026-10-26 재시작 실패).

quit 의 STM 승격 스레드가 인터프리터 종료를 30초가량 붙잡아, 재시작하는 새 host 가
옛 host 를 기다리다 시작을 거부했다.
"""

import unittest
from unittest.mock import MagicMock, patch

import overlay.main as overlay_main


class ExitWithoutStragglersTests(unittest.TestCase):
    def _app(self, quitting):
        app = overlay_main.OverlayApp.__new__(overlay_main.OverlayApp)
        app.root = MagicMock()
        app._quitting = quitting
        return app

    def test_quit_ends_process_after_mainloop(self):
        app = self._app(True)
        with patch.object(overlay_main.os, "_exit") as hard_exit, \
             patch("overlay.config.flush_overlay_state") as flush, \
             patch.object(overlay_main.logging, "shutdown") as log_shutdown:
            app.run()
        flush.assert_called_once()  # atexit 을 건너뛰므로 상태 저장은 직접 한다
        log_shutdown.assert_called_once()
        hard_exit.assert_called_once_with(0)

    def test_mainloop_exit_without_quit_does_not_force_exit(self):
        app = self._app(False)
        with patch.object(overlay_main.os, "_exit") as hard_exit:
            app.run()
        hard_exit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
