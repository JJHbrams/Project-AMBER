"""Fault-injection coverage for the STM listener's bounded handler launcher."""

import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

from overlay.stm_server import STMServer, _ExclusiveHTTPServer


class STMServerResilienceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "discovery.json"
        self.server = STMServer(port=0, state_discovery_path=self.path)
        self.server.start()

    def tearDown(self):
        if self.server._server is not None:
            self.server.stop()
        self.temp.cleanup()

    def test_stalled_handler_start_does_not_block_accept_stop_or_rebind(self):
        """A real loopback request cannot freeze accept, shutdown, or its port."""
        port = self.server.port
        server_impl = self.server._server
        server_impl._STALL_TIMEOUT = .05
        entered = threading.Event()
        release = threading.Event()
        original_start = threading.Thread.start

        def stall_handler_start(thread, *args, **kwargs):
            # ThreadingMixIn creates unnamed handler threads.  The launcher and
            # accept threads retain explicit names and must remain operational.
            # Limit the fault to this old server, so the replacement's real
            # HTTP health request proves the released port works.
            target = getattr(thread, "_target", None)
            if getattr(target, "__self__", None) is server_impl:
                entered.set()
                release.wait(3)
                return None
            return original_start(thread, *args, **kwargs)

        first = None
        try:
            with patch.object(threading.Thread, "start", new=stall_handler_start):
                first = socket.create_connection(("127.0.0.1", port), timeout=1)
                first.sendall(b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n")
                self.assertTrue(entered.wait(1))
                time.sleep(.08)
                self.assertFalse(self.server.listening)
                self.assertEqual(self.server.health_reason, "handler_launch_stalled")

                # The accept loop remains live, but a stalled launcher rejects
                # new work promptly instead of accumulating blocked sockets.
                second = socket.create_connection(("127.0.0.1", port), timeout=1)
                try:
                    second.sendall(b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n")
                    second.settimeout(.5)
                    try:
                        received = second.recv(1)
                    except ConnectionAbortedError:
                        received = b""
                    self.assertEqual(received, b"")
                finally:
                    second.close()

                started = time.monotonic()
                self.server.stop()
                self.assertLess(time.monotonic() - started, 1.5)

                # Closing the old listener releases its real Windows loopback
                # port even while the sacrificial launcher remains blocked.
                replacement_path = Path(self.temp.name) / "replacement.json"
                replacement = STMServer(port=port, state_discovery_path=replacement_path)
                try:
                    replacement.start()
                    self.assertTrue(replacement.listening)
                    with urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:
                        self.assertEqual(response.status, 200)
                finally:
                    replacement.stop()
        finally:
            release.set()
            if first is not None:
                first.close()

    def test_launcher_startup_wait_is_bounded(self):
        server = _ExclusiveHTTPServer(("127.0.0.1", 0), object)
        try:
            with patch.object(server, "_run_launcher", return_value=None):
                started = time.monotonic()
                with self.assertRaisesRegex(RuntimeError, "did not become ready"):
                    server.start_launcher(.03)
                self.assertLess(time.monotonic() - started, .5)
        finally:
            server.request_shutdown()

    def test_normal_local_burst_is_not_rejected(self):
        """The bounded gate still accepts ordinary concurrent local requests."""
        outcomes = []
        lock = threading.Lock()

        def request_health():
            try:
                with urlopen(f"http://127.0.0.1:{self.server.port}/health", timeout=2) as response:
                    value = response.status
            except Exception as exc:  # assertion keeps individual failures visible
                value = exc
            with lock:
                outcomes.append(value)

        workers = [threading.Thread(target=request_health) for _ in range(32)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(3)
        self.assertEqual(outcomes, [200] * 32)
        self.assertTrue(self.server.listening)

    def test_partial_body_handler_is_closed_by_bounded_stop_and_port_rebinds(self):
        """An active handler blocked in body read cannot retain the old listener."""
        port = self.server.port
        partial = socket.create_connection(("127.0.0.1", port), timeout=1)
        replacement = None
        try:
            partial.sendall(
                b"POST /stm/session/start HTTP/1.1\r\nHost: localhost\r\n"
                b"Content-Length: 100\r\nContent-Type: application/json\r\n\r\n{"
            )
            deadline = time.monotonic() + 1
            while not self.server._server._owned_requests and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(self.server._server._owned_requests)
            started = time.monotonic()
            self.server.stop()
            self.assertLess(time.monotonic() - started, 1.5)

            replacement = STMServer(
                port=port, state_discovery_path=Path(self.temp.name) / "partial-replacement.json"
            )
            replacement.start()
            self.assertTrue(replacement.listening)
            with urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:
                self.assertEqual(response.status, 200)
        finally:
            partial.close()
            if replacement is not None:
                replacement.stop()


if __name__ == "__main__":
    unittest.main()
