"""STM HTTP 서버 — overlay.exe 내 상주 STM 브로커.

overlay.exe가 실행 중일 때 localhost:PORT에 바인딩하여
모든 MCP 클라이언트(wt copilot, VS Code copilot)가 동일한 STM을 공유하게 한다.

노출 엔드포인트 (STM 관련만):
  POST /stm/session/start          → { scope_key } → { session_id, scope_key }
  POST /stm/message                → { session_id, role, content, request_id? } → { status }
  GET  /stm/messages?scope_key=... → [{ role, content }]
  POST /stm/session/close          → { session_id?, scope_key?, summary? } → { status, closed_session_id }
  POST /bubble/new                 → {} → { status } (말풍선 상주 세션 리셋 = 새 대화 시작)
  GET  /health                     → { status: "ok", pid }
"""

import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

from core.storage.db import get_connection
from core.graph.semantic import checkpoint_open_session
from core.memory import close_session as _close_session, save_message
from core.memory.store import session_has_external_journal_eligibility
from core.config.runtime_config import get_cfg_value

logger = logging.getLogger(__name__)

DEFAULT_PORT = 17384
_SEEN_REQUEST_IDS: set[str] = set()
_SEEN_LOCK = threading.Lock()
_MAX_SEEN = 1000
_shutdown_callback: "Optional[callable]" = None
_new_session_callback: "Optional[callable]" = None


def _resolve_open_session_id(session_id: object, scope_key: Optional[str]) -> Optional[int]:
    """닫을 세션 id를 결정한다. session_id 우선, 없으면 scope_key 기준 최신 open 세션."""
    if session_id is not None:
        try:
            sid = int(session_id)
            if sid > 0:
                conn = get_connection()
                try:
                    row = conn.execute(
                        "SELECT id FROM sessions WHERE id=? AND ended_at IS NULL" + (" AND scope_key=?" if scope_key else ""),
                        (sid, scope_key) if scope_key else (sid,),
                    ).fetchone()
                    return sid if row else None
                finally:
                    conn.close()
        except (TypeError, ValueError):
            return None

    try:
        conn = get_connection()
        if scope_key:
            rows = conn.execute(
                "SELECT id FROM sessions WHERE ended_at IS NULL AND scope_key = ? "
                "ORDER BY started_at DESC, id DESC LIMIT 2",
                (scope_key,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id FROM sessions WHERE ended_at IS NULL "
                "ORDER BY started_at DESC, id DESC LIMIT 2"
            ).fetchall()
        conn.close()
    except Exception:
        return None

    if len(rows) != 1:
        return None
    try:
        return int(rows[0][0])
    except (TypeError, ValueError, IndexError, KeyError):
        return None


def _has_multiple_open_sessions(scope_key: Optional[str]) -> bool:
    conn = get_connection()
    try:
        if scope_key:
            rows = conn.execute("SELECT id FROM sessions WHERE ended_at IS NULL AND scope_key=? LIMIT 2", (scope_key,)).fetchall()
        else:
            rows = conn.execute("SELECT id FROM sessions WHERE ended_at IS NULL LIMIT 2").fetchall()
        return len(rows) > 1
    finally:
        conn.close()


def _explicit_session_status(session_id: object, scope_key: Optional[str]) -> str:
    try:
        sid = int(session_id)
    except (TypeError, ValueError):
        return "invalid_session_id"
    if sid <= 0:
        return "invalid_session_id"
    conn = get_connection()
    try:
        row = conn.execute("SELECT scope_key, ended_at FROM sessions WHERE id=?", (sid,)).fetchone()
    finally:
        conn.close()
    if not row:
        return "session_not_found"
    if scope_key and row["scope_key"] != scope_key:
        return "session_scope_mismatch"
    return "open" if row["ended_at"] is None else "ended_session"


def _get_port() -> int:
    try:
        from core.config.runtime_config import get_cfg_value

        return int(get_cfg_value("overlay.stm_server_port", DEFAULT_PORT))
    except Exception:
        return DEFAULT_PORT


def _dedup(request_id: Optional[str]) -> bool:
    """True이면 이미 처리된 요청 (중복). request_id 없으면 항상 False."""
    if not request_id:
        return False
    with _SEEN_LOCK:
        if request_id in _SEEN_REQUEST_IDS:
            return True
        _SEEN_REQUEST_IDS.add(request_id)
        if len(_SEEN_REQUEST_IDS) > _MAX_SEEN:
            # 가장 오래된 절반 제거 (순서 보장 없음, LRU 불필요)
            to_remove = list(_SEEN_REQUEST_IDS)[: _MAX_SEEN // 2]
            for r in to_remove:
                _SEEN_REQUEST_IDS.discard(r)
        return False


class _STMHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: N802
        logger.debug("STM HTTP: " + fmt, *args)

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/health":
            # ENGRAM_RUNTIME_ROLE="overlay" → overlay 내장 STM (기존 overlay 종료 감지 대상)
            # 그 외 (dev_backend 등 standalone) → "stm-broker" 반환, shutdown 대상 아님
            role = "overlay-stm" if os.environ.get("ENGRAM_RUNTIME_ROLE") == "overlay" else "stm-broker"
            self._send_json({"status": "ok", "pid": os.getpid(), "role": role})

        elif path == "/stm/messages":
            qs = parse_qs(parsed.query)
            scope_key = qs.get("scope_key", [""])[0] or None
            limit = int(qs.get("limit", [50])[0])
            within_minutes = int(qs.get("within_minutes", [120])[0])
            try:
                from core.memory import get_recent_messages_by_scope

                msgs = get_recent_messages_by_scope(scope_key, limit=limit, within_minutes=within_minutes)
                self._send_json({"messages": msgs})
            except Exception as e:
                logger.error("get_recent_messages_by_scope 실패: %s", e)
                self._send_json({"error": str(e)}, 500)
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        try:
            body = self._read_body()
        except Exception as e:
            self._send_json({"error": f"body parse error: {e}"}, 400)
            return

        if path == "/stm/session/start":
            scope_key = body.get("scope_key") or os.environ.get("ENGRAM_SCOPE_KEY") or None
            # projects (comma-separated) 우선, 없으면 project_key 단일값
            projects_raw = body.get("projects") or ""
            if projects_raw.strip():
                parsed_keys = [k.strip() for k in projects_raw.split(",") if k.strip()]
            else:
                pk = body.get("project_key") or None
                parsed_keys = [pk] if pk else []
            try:
                from core.memory.bus import memory_bus

                session = memory_bus.start_session(scope_key=scope_key, project_keys=parsed_keys or None)
                self._send_json({"session_id": session.session_id, "scope_key": session.scope_key, "projects": parsed_keys or ["general"]})
            except Exception as e:
                logger.error("start_session 실패: %s", e)
                self._send_json({"error": str(e)}, 500)

        elif path == "/stm/message":
            if _dedup(body.get("request_id")):
                self._send_json({"status": "duplicate_ignored"})
                return
            session_id = body.get("session_id")
            scope_key = body.get("scope_key")
            role = body.get("role", "user")
            content = body.get("content", "")
            # scope_key로 session_id 자동 resolve
            if session_id is None and scope_key:
                from core.memory import resolve_session_id_by_scope
                session_id = resolve_session_id_by_scope(scope_key)
            if session_id is None:
                self._send_json({"error": "session_id 또는 scope_key가 필요합니다."}, 400)
                return
            try:
                save_message(int(session_id), role, content)
                self._send_json({"status": "ok"})
            except ValueError as e:
                if "not open" in str(e):
                    self._send_json({"status": "ended_session"}, 409)
                else:
                    self._send_json({"error": str(e)}, 400)
            except Exception as e:
                logger.error("save_message 실패: %s", e)
                self._send_json({"error": str(e)}, 500)

        elif path == "/stm/session/close":
            session_id = body.get("session_id")
            scope_key = body.get("scope_key") or None
            summary = body.get("summary", "") or ""
            open_intents = body.get("open_intents", "") or ""
            progress = body.get("progress", "") or ""
            journal_origin = body.get("journal_origin", "automatic") or "automatic"
            project_key = body.get("project_key", "") or ""
            project_node_id = body.get("project_node_id", "") or None
            project_label = body.get("project_label", "") or ""
            try:
                if session_id is not None:
                    explicit_status = _explicit_session_status(session_id, scope_key)
                    if explicit_status != "open":
                        self._send_json({"status": explicit_status}, 404 if explicit_status == "session_not_found" else 409)
                        return
                closed_session_id = _resolve_open_session_id(session_id, scope_key)
                if closed_session_id is None and session_id is None and _has_multiple_open_sessions(scope_key):
                    self._send_json({"status": "ambiguous_open_session"}, 409)
                    return
                if closed_session_id is not None:
                    # HTTP hints are not authority: durable session provenance is.
                    external_dir = (str(get_cfg_value("memory.auto_checkpoint.external_daily_dir", "") or "")
                                    if session_has_external_journal_eligibility(closed_session_id) else "")
                    checkpoint = checkpoint_open_session(
                        closed_session_id, scope_key or "", str(summary), str(open_intents),
                        progress=str(progress), cwd=str(body.get("cwd", "") or ""), source="automatic", external_daily_dir=external_dir,
                    )
                    if checkpoint.get("status") not in {"checkpointed", "no_new_messages"}:
                        self._send_json(checkpoint, 409 if checkpoint.get("status") == "busy" else 500)
                        return
                    _close_session(closed_session_id, str(summary), str(open_intents), str(progress), str(journal_origin), str(project_key), str(project_node_id) if project_node_id else None, str(project_label))
                self._send_json({"status": "ok" if closed_session_id is not None else "no_open_session", "closed_session_id": closed_session_id})
            except Exception as e:
                logger.error("session/close 실패: %s", e)
                self._send_json({"error": str(e)}, 500)

        elif path == "/stm/session/summarize":
            session_id = body.get("session_id")
            scope_key = body.get("scope_key") or ""
            try:
                if session_id is not None:
                    explicit_status = _explicit_session_status(session_id, scope_key)
                    if explicit_status != "open":
                        self._send_json({"status": explicit_status}, 404 if explicit_status == "session_not_found" else 409)
                        return
                target = _resolve_open_session_id(session_id, scope_key)
                if target is None and session_id is None and _has_multiple_open_sessions(scope_key):
                    self._send_json({"status": "ambiguous_open_session"}, 409)
                    return
                if target is None:
                    self._send_json({"status": "no_open_session"})
                else:
                    external_dir = (str(get_cfg_value("memory.auto_checkpoint.external_daily_dir", "") or "")
                                    if session_has_external_journal_eligibility(target) else "")
                    self._send_json(checkpoint_open_session(target, scope_key, str(body.get("summary", "") or ""), str(body.get("open_intents", "") or ""), cwd=str(body.get("cwd", "") or ""), external_daily_dir=external_dir))
            except Exception as e:
                logger.error("session/summarize 실패: %s", e)
                self._send_json({"error": str(e)}, 500)

        elif path == "/shutdown":
            # graceful shutdown 요청 — 응답 후 앱 종료 트리거
            self._send_json({"status": "shutting_down", "pid": os.getpid()})
            if _shutdown_callback is not None:
                import threading

                threading.Thread(target=_shutdown_callback, daemon=True).start()

        elif path == "/bubble/new":
            # 말풍선 상주 세션 리셋(새 대화 시작) — 응답 후 콜백 트리거.
            # overlay 재시작 없이 claude_session_id 를 비우고 현재 세션을 종료한다.
            if _new_session_callback is not None:
                self._send_json({"status": "ok", "action": "bubble_new_session"})
                threading.Thread(target=_new_session_callback, daemon=True).start()
            else:
                self._send_json({"error": "bubble new session not available"}, 503)

        else:
            self._send_json({"error": "not found"}, 404)


class STMServer:
    """overlay.exe 내 상주 STM HTTP 서버."""

    def __init__(self, port: Optional[int] = None, shutdown_callback: "Optional[callable]" = None,
                 new_session_callback: "Optional[callable]" = None):
        global _shutdown_callback, _new_session_callback
        self._port = port or _get_port()
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        _shutdown_callback = shutdown_callback
        _new_session_callback = new_session_callback

    def start(self):
        try:
            self._server = ThreadingHTTPServer(("127.0.0.1", self._port), _STMHandler)
            self._thread = threading.Thread(
                target=self._server.serve_forever,
                daemon=True,
                name="stm-http-server",
            )
            self._thread.start()
            logger.info("STM HTTP 서버 시작: port=%d", self._port)
        except OSError as e:
            # 포트 충돌 — /health로 점유 주체 확인
            try:
                import urllib.request as _ureq, json as _json
                with _ureq.urlopen(f"http://127.0.0.1:{self._port}/health", timeout=2) as _r:
                    info = _json.loads(_r.read().decode())
                role = info.get("role", "unknown")
                if role in ("overlay-stm", "stm-broker"):
                    logger.info("STM 포트 %d 이미 engram STM 점유 (role=%s, pid=%s) — 재사용",
                                self._port, role, info.get("pid"))
                else:
                    logger.error("STM 포트 %d 비-engram 프로세스 점유 (role=%s) — STM 비활성화: %s",
                                 self._port, role, e)
            except Exception:
                logger.error("STM 포트 %d 점유 주체 불명 (health 응답 없음) — STM 비활성화: %s",
                             self._port, e)

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server = None
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("STM HTTP 서버 종료")

    @property
    def port(self) -> int:
        return self._port



