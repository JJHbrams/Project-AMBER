import json
import errno
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from overlay.stm_server import STMServer, _ExclusiveHTTPServer


class STMStateApiTests(unittest.TestCase):
    def test_renderer_readiness_requires_auth_and_reports_current_instance(self):
        url = self.base + '/renderers'
        with self.assertRaises(HTTPError) as error:
            urlopen(url, timeout=2)
        self.assertEqual(error.exception.code, 401)
        state = {'catalog_connected': True, 'selected_ready': False, 'actual_mode': 'observer', 'renderers': []}
        with patch('overlay.event_api.renderer_startup_snapshot', return_value=state):
            request = Request(url, headers={'Authorization': 'Bearer ' + self.info['token']})
            with urlopen(request, timeout=2) as response:
                payload = json.loads(response.read())
        self.assertEqual(payload['instance_id'], self.info['instance_id'])
        import os
        self.assertEqual(payload['pid'], os.getpid())
        self.assertTrue(payload['catalog_connected'])
        self.assertFalse(payload['selected_ready'])
        self.assertNotIn('token', payload)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.discovery = Path(self.temp.name) / "overlay-state-api-v1.json"
        self.server = STMServer(port=0, state_discovery_path=self.discovery)
        self.server.start()
        self.info = json.loads(self.discovery.read_text(encoding="utf-8"))
        self.base = f"http://127.0.0.1:{self.server.port}/state"

    def tearDown(self):
        self.server.stop()
        self.temp.cleanup()

    def test_listener_is_exclusive_and_foreign_discovery_untouched(self):
        original = self.discovery.read_bytes()
        other_path = Path(self.temp.name) / 'conflicting.json'
        other = STMServer(port=self.server.port, state_discovery_path=other_path)
        try:
            with patch('overlay.stm_server._ExclusiveHTTPServer', wraps=_ExclusiveHTTPServer) as bind:
                other.start()
                self.assertEqual(bind.call_count, 1)
            self.assertFalse(other.listening)
            self.assertTrue(self.server.listening)
            self.assertFalse(other_path.exists())
            self.assertEqual(self.discovery.read_bytes(), original)
        finally:
            other.stop()
        self.assertEqual(self.discovery.read_bytes(), original)

    def test_transient_releasing_port_retries_then_binds_real_listener(self):
        path = Path(self.temp.name) / 'retry-discovery.json'
        server = STMServer(port=0, state_discovery_path=path)
        attempts = []

        def bind(*args, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError(errno.EADDRINUSE, 'handoff fixture')
            return _ExclusiveHTTPServer(*args, **kwargs)

        try:
            with patch('overlay.stm_server._ExclusiveHTTPServer', side_effect=bind), patch.object(
                server, '_probe_bind_owner', return_value=None
            ):
                server.start()
            self.assertEqual(len(attempts), 2)
            self.assertTrue(server.listening)
            info = json.loads(path.read_text())
            with urlopen(f'http://127.0.0.1:{info["port"]}/health', timeout=1) as response:
                self.assertEqual(response.status, 200)
        finally:
            server.stop()

    def test_persistent_nonresponding_bind_conflict_has_bounded_retries(self):
        path = Path(self.temp.name) / 'unavailable-discovery.json'
        server = STMServer(port=0, state_discovery_path=path)
        with patch('overlay.stm_server._ExclusiveHTTPServer', side_effect=OSError(errno.EADDRINUSE, 'fixture')) as bind, \
                patch.object(server, '_probe_bind_owner', return_value=None), \
                patch('overlay.stm_server.time.sleep') as sleep:
            server.start()
        self.assertEqual(bind.call_count, 10)
        self.assertEqual(sleep.call_count, 9)
        self.assertFalse(server.listening)
        self.assertFalse(path.exists())

    def test_bind_probe_uses_headers_only_and_disables_redirects_and_proxy(self):
        from urllib.request import HTTPRedirectHandler, ProxyHandler
        opener = MagicMock()
        response = opener.open.return_value.__enter__.return_value
        with patch('urllib.request.build_opener', return_value=opener) as build:
            self.assertEqual(self.server._probe_bind_owner(.05), {})
        response.read.assert_not_called()
        proxy, redirects = build.call_args.args
        self.assertIsInstance(proxy, ProxyHandler)
        self.assertEqual(proxy.proxies, {})
        self.assertIsInstance(redirects, HTTPRedirectHandler)
        self.assertIsNone(redirects.redirect_request(None, None, 302, 'redirect', {}, 'http://example.com'))
        opener.open.side_effect = HTTPError('http://127.0.0.1', 302, 'redirect', {}, None)
        with patch('urllib.request.build_opener', return_value=opener):
            self.assertEqual(self.server._probe_bind_owner(.05), {})

    def test_bind_probe_malformed_response_fails_closed_without_escaping(self):
        from http.client import BadStatusLine
        opener = MagicMock()
        opener.open.side_effect = BadStatusLine('malformed')
        with patch('urllib.request.build_opener', return_value=opener):
            self.assertIsNone(self.server._probe_bind_owner(.05))

    def test_authenticated_presence_preserves_state_and_rejects_content(self):
        self.request('POST', {'provider': 'mcp', 'session_id': 'presence-client', 'state': 'needs_input'}, self.info['token'])
        _, before = self.request('GET', token=self.info['token'])
        self.base += '/presence'
        with self.assertRaises(HTTPError) as missing:
            self.request('POST', {'provider': 'mcp', 'session_id': 'presence-client'})
        self.assertEqual(missing.exception.code, 401)
        with patch('overlay.stm_server.get_connection') as connection, patch('overlay.stm_server.save_message') as save:
            status, touched = self.request('POST', {'provider': 'mcp', 'session_id': 'presence-client'}, self.info['token'])
            self.assertEqual(status, 200)
            self.assertEqual(touched['session']['state'], 'needs_input')
            self.assertEqual(touched['session']['state_since'], before['sessions'][0]['state_since'])
            connection.assert_not_called()
            save.assert_not_called()
        with self.assertRaises(HTTPError) as bad:
            self.request('POST', {'provider': 'mcp', 'session_id': 'presence-client', 'content': 'private'}, self.info['token'])
        self.assertEqual(bad.exception.code, 400)

    def request(self, method, body=None, token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = Request(self.base, data=json.dumps(body).encode() if body is not None else None,
                          headers=headers, method=method)
        with urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read().decode())

    def test_authentication_snapshot_and_strict_payload(self):
        with self.assertRaises(HTTPError) as missing:
            self.request("GET")
        self.assertEqual(missing.exception.code, 401)
        self.assertEqual(missing.exception.headers["WWW-Authenticate"], "Bearer")
        missing_body = missing.exception.read().decode()
        self.assertEqual(json.loads(missing_body), {"error": "unauthorized"})
        self.assertNotIn(self.info["token"], missing_body)
        with self.assertRaises(HTTPError) as wrong:
            self.request("POST", {"provider": "claude", "session_id": "s1", "state": "working"}, "wrong")
        self.assertEqual(wrong.exception.code, 401)
        wrong_body = wrong.exception.read().decode()
        self.assertEqual(json.loads(wrong_body), {"error": "unauthorized"})
        self.assertNotIn(self.info["token"], wrong_body)
        status, item = self.request("POST", {"provider": "claude", "session_id": "s1", "state": "working", "label": "shell"}, self.info["token"])
        self.assertEqual(status, 200)
        self.assertEqual(item["session"]["key"], "claude:s1")
        _, snapshot = self.request("GET", token=self.info["token"])
        self.assertEqual([entry["key"] for entry in snapshot["sessions"]], ["claude:s1"])
        with self.assertRaises(HTTPError) as invalid:
            self.request("POST", {"provider": "claude", "session_id": "s2", "state": "working", "content": "forbidden"}, self.info["token"])
        self.assertEqual(invalid.exception.code, 400)

    def test_namespaced_upsert_and_state_route_avoids_stm_persistence(self):
        with patch("overlay.stm_server.get_connection") as connection, patch(
            "overlay.stm_server.save_message"
        ) as save_message, patch("overlay.stm_server.checkpoint_open_session") as checkpoint:
            self.request("POST", {"provider": "claude", "session_id": "same", "state": "working"}, self.info["token"])
            self.request("POST", {"provider": "codex", "session_id": "same", "state": "ready"}, self.info["token"])
            self.request("POST", {"provider": "claude", "session_id": "same", "state": "blocked"}, self.info["token"])
            _, snapshot = self.request("GET", token=self.info["token"])
        self.assertEqual([row["key"] for row in snapshot["sessions"]], ["claude:same", "codex:same"])
        self.assertEqual(snapshot["sessions"][0]["state"], "blocked")
        connection.assert_not_called()
        save_message.assert_not_called()
        checkpoint.assert_not_called()

    def test_title_route_is_authenticated_metadata_only_and_honors_user_override(self):
        self.request('POST', {'provider': 'claude', 'session_id': 'owned',
                              'state': 'working', 'label': 'Initial producer'}, self.info['token'])
        self.server._state_registry.set_label('claude:owned', '사용자 제목')
        self.base += '/title'
        status, result = self.request('POST', {'provider': 'claude', 'session_id': 'owned',
                                               'title': 'Agent generated title'}, self.info['token'])
        self.assertEqual((status, result), (200, {'accepted': True}))
        self.assertEqual(self.server._state_registry.snapshot()[0]['state'], 'working')
        self.assertEqual(self.server._state_registry.snapshot()[0]['label'], '사용자 제목')
        self.server._state_registry.set_label('claude:owned', None)
        self.assertEqual(self.server._state_registry.snapshot()[0]['label'], 'Agent generated title')
        with self.assertRaises(HTTPError) as invalid:
            self.request('POST', {'provider': 'claude', 'session_id': 'owned',
                                  'title': 'one'}, self.info['token'])
        self.assertEqual(invalid.exception.code, 400)
        with self.assertRaises(HTTPError) as null_title:
            self.request('POST', {'provider': 'claude', 'session_id': 'owned',
                                  'title': None}, self.info['token'])
        self.assertEqual(null_title.exception.code, 400)
        with self.assertRaises(HTTPError) as unknown:
            self.request('POST', {'provider': 'claude', 'session_id': 'missing',
                                  'title': 'Unknown session title'}, self.info['token'])
        self.assertEqual(unknown.exception.code, 404)

    def test_health_is_legacy_unauthenticated_and_discovery_is_complete(self):
        with urlopen(f"http://127.0.0.1:{self.server.port}/health", timeout=2) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(self.info["schema_version"], 1)
        self.assertEqual(self.info["host"], "127.0.0.1")
        self.assertEqual(self.info["port"], self.server.port)
        self.assertTrue(self.info["instance_id"])
        self.assertTrue(self.info["token"])
        if __import__("os").name != "nt":
            self.assertEqual(self.discovery.stat().st_mode & 0o777, 0o600)

    def test_successful_stop_removes_own_discovery(self):
        self.server.stop()
        self.assertFalse(self.discovery.exists())

    def test_legacy_bubble_and_shutdown_callbacks_are_unauthenticated(self):
        self.server.stop()
        bubble_called, shutdown_called = threading.Event(), threading.Event()
        self.server = STMServer(port=0, state_discovery_path=self.discovery,
                                new_session_callback=bubble_called.set,
                                shutdown_callback=shutdown_called.set)
        self.server.start()
        base = f"http://127.0.0.1:{self.server.port}"
        for endpoint, expected_status, called in (("/bubble/new", "ok", bubble_called),
                                                  ("/shutdown", "shutting_down", shutdown_called)):
            request = Request(endpoint.join((base, "")), data=b"{}",
                              headers={"Content-Type": "application/json"}, method="POST")
            with urlopen(request, timeout=2) as response:
                self.assertEqual(json.loads(response.read().decode())["status"], expected_status)
            self.assertTrue(called.wait(timeout=2), endpoint)

    def test_discovery_is_removed_only_for_its_instance(self):
        replacement = dict(self.info)
        replacement["instance_id"] = "replacement"
        self.discovery.write_text(json.dumps(replacement), encoding="utf-8")
        self.server.stop()
        self.assertTrue(self.discovery.exists())


if __name__ == "__main__":
    unittest.main()
