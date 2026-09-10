"""Validation and private discovery-file support for the loopback state API."""

from __future__ import annotations

import getpass
import json
import os
import secrets
import stat
import subprocess
import tempfile
import uuid
import unicodedata
from pathlib import Path


ALLOWED_PROVIDERS = frozenset({"claude", "codex", "antigravity", "copilot", "mcp"})
ALLOWED_STATES = frozenset({"working", "needs_input", "ready", "blocked", "unknown"})
ALLOWED_FIELDS = frozenset({"provider", "session_id", "state", "label", "subagent_count", "is_bubble", "project_name", "agent_name"})
ALLOWED_AGENT_NAMES = frozenset({"Claude", "Codex", "Copilot", "Antigravity", "MCP"})
TITLE_FIELDS = frozenset({"provider", "session_id", "title"})


def state_discovery_file(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".engram" / "overlay-state-api-v1.json"


def new_credentials() -> tuple[str, str]:
    return uuid.uuid4().hex, secrets.token_urlsafe(32)


def authorized(header: str | None, token: str) -> bool:
    return bool(header and header.startswith("Bearer ") and
                secrets.compare_digest(header[7:], token))


def validate_payload(payload: object) -> tuple[dict | None, str | None]:
    if not isinstance(payload, dict) or set(payload) - ALLOWED_FIELDS:
        return None, "invalid state payload"
    required = {"provider", "session_id", "state"}
    if not required.issubset(payload):
        return None, "invalid state payload"
    provider, session_id, state = payload["provider"], payload["session_id"], payload["state"]
    if type(provider) is not str or type(state) is not str or provider not in ALLOWED_PROVIDERS or state not in ALLOWED_STATES:
        return None, "invalid state payload"
    if not _safe_text(session_id, 128) or any(char in session_id for char in "/\\"):
        return None, "invalid state payload"
    label = payload.get("label")
    if label is not None and (not _safe_text(label, 128) or any(char in label for char in "/\\:")):
        return None, "invalid state payload"
    count = payload.get("subagent_count")
    if count is not None and (type(count) is not int or not 0 <= count <= 9999):
        return None, "invalid state payload"
    bubble = payload.get("is_bubble")
    if bubble is not None and type(bubble) is not bool:
        return None, "invalid state payload"
    if payload.get("project_name") is not None and not valid_project_name(payload["project_name"]):
        return None, "invalid state payload"
    agent_name = payload.get("agent_name")
    if agent_name is not None and (type(agent_name) is not str or agent_name not in ALLOWED_AGENT_NAMES):
        return None, "invalid state payload"
    return dict(payload), None


def validate_lifecycle_payload(payload, agent):
    """Private native-only envelope; semantics never become public row fields."""
    from core.integrations.tool_semantics import validate_semantic
    if not isinstance(payload, dict):
        return None, None, 'invalid state payload'
    body = dict(payload)
    child = body.pop('child_activity', None)
    child_update = body.pop('child_update', None)
    semantic = body.pop('semantic', None)
    checked, error = validate_payload(body)
    if error or checked.get('provider') != 'mcp' or checked.get('agent_name') != agent:
        return None, None, 'invalid lifecycle payload'
    if 'semantic' in payload:
        semantic = validate_semantic(semantic)
        if semantic is None or (checked['state'] != 'working' and semantic['active_category'] is not None):
            return None, None, 'invalid lifecycle semantics'
    if 'child_activity' in payload:
        from core.integrations.child_activity import validate_summary
        child = validate_summary(child)
        if child is None or child['total'] != checked.get('subagent_count'):
            return None, None, 'invalid child activity'
        checked['child_activity'] = child
    if 'child_update' in payload:
        if child_update is not True or child is None or semantic is None:
            return None, None, 'invalid child update'
        checked['_child_update'] = True
    return checked, semantic, None


def valid_project_name(value: object) -> bool:
    return (_safe_text(value,64) and value.strip() not in (".","..")
            and not any(char in value for char in "/\\:")
            and not any(unicodedata.category(char) in {'Cc','Cf','Cs'} for char in value))


def project_display_name(project_name=None, cwd=None):
    """Caller input only: textual Windows/POSIX basename, never resolve/stat."""
    if project_name is not None:
        return project_name.strip() if valid_project_name(project_name) else None
    if not _safe_text(cwd,4096) or any(unicodedata.category(char) in {'Cc','Cf','Cs'} for char in cwd):
        return None
    normalized = cwd.strip().replace("\\", "/").rstrip("/")
    # A UNC server/share is a filesystem root, not a caller project folder.
    if normalized.startswith("//") and len([p for p in normalized.split("/") if p]) <= 2:
        return None
    name = normalized.rsplit("/", 1)[-1]
    return name.strip() if valid_project_name(name) else None


def validate_project_payload(payload):
    if not isinstance(payload,dict) or set(payload) != {"provider","session_id","project_name"}:
        return None,"invalid project payload"
    if not valid_project_name(payload.get("project_name")):
        return None,"invalid project payload"
    checked,error = validate_payload({**payload,"state":"unknown"})
    if error:
        return None,"invalid project payload"
    return {"provider":checked["provider"],"session_id":checked["session_id"],
            "project_name":checked["project_name"].strip()},None


def _safe_text(value: object, limit: int) -> bool:
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= limit and
            not any(ord(char) < 32 or ord(char) == 127 for char in value))


def validate_presence(payload: object) -> tuple[dict | None, str | None]:
    if not isinstance(payload, dict) or set(payload) - {'provider', 'session_id', 'bubble_owner', 'ended'}:
        return None, 'invalid presence payload'
    checked, error = validate_payload({'provider': payload.get('provider'),
                                      'session_id': payload.get('session_id'), 'state': 'unknown'})
    if error or any(type(payload.get(name, False)) is not bool for name in ('bubble_owner', 'ended')):
        return None, 'invalid presence payload'
    return dict(payload), None


def validate_title_payload(payload: object) -> tuple[dict | None, str | None]:
    """Validate explicit, short session-title metadata only."""
    if not isinstance(payload, dict) or set(payload) != TITLE_FIELDS:
        return None, "invalid title payload"
    title = payload.get("title")
    if not isinstance(title, str):
        return None, "invalid title payload"
    checked, error = validate_payload({
        "provider": payload.get("provider"),
        "session_id": payload.get("session_id"),
        "state": "unknown",
        "label": title,
    })
    if error:
        return None, "invalid title payload"
    words = checked["label"].split()
    if not 2 <= len(words) <= 8:
        return None, "invalid title payload"
    return {"provider": checked["provider"], "session_id": checked["session_id"],
            "title": checked["label"]}, None


def validate_bubble_title_metadata(metadata):
    if not isinstance(metadata, dict) or set(metadata) != {'producer', 'manual'}:
        return None
    producer, manual = metadata['producer'], metadata['manual']
    if producer is not None:
        _, error = validate_title_payload({'provider':'claude', 'session_id':'title', 'title':producer})
        if error:
            return None
    _, error = validate_payload({'provider':'claude', 'session_id':'title', 'state':'unknown', 'label':manual})
    if error:
        return None
    return {'producer':producer, 'manual':manual}


def publish_discovery(path: Path, *, port: int, instance_id: str, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".overlay-state-api-", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        os.chmod(temp_path, 0o600)
        _protect_current_user(temp_path)
        payload = {"schema_version": 1, "host": "127.0.0.1", "port": port,
                   "instance_id": instance_id, "token": token}
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = -1
            json.dump(payload, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        if fd >= 0:
            os.close(fd)
        if temp_path.exists():
            temp_path.unlink()


def remove_discovery_if_owner(path: Path, instance_id: str) -> None:
    try:
        if json.loads(path.read_text(encoding="utf-8")).get("instance_id") == instance_id:
            path.unlink()
    except (OSError, ValueError, json.JSONDecodeError):
        pass


def _protect_current_user(path: Path) -> None:
    if os.name == "nt":
        domain = os.environ.get("USERDOMAIN", "").strip()
        account = f"{domain}\\{getpass.getuser()}" if domain else getpass.getuser()
        result = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{account}:F"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode != 0:
            raise OSError("unable to protect discovery file")
    elif stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise OSError("discovery file permissions are not private")
