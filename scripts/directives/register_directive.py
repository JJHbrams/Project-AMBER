"""Interactive, approval-gated directive registration CLI."""
from __future__ import annotations
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core.context.directive_registration import (RegistrationError, approve_registration, begin_registration, commit_registration, complete_registration, preview_registration, registration_schema)
from core.storage.db import initialize_db

def _ask(name, default=""):
    value = input(f"{name}" + (f" [{default}]" if default != "" else "") + ": ").strip()
    return value if value else default


def _choose(name, choices):
    """Numbered selection driven only by the server-owned registration schema."""
    while True:
        print(f"{name} choices:")
        for number, choice in enumerate(choices, start=1):
            print(f"  {number}. {choice['id']} - {choice['description']}")
        raw = input(f"Select {name} number: ").strip()
        try:
            selected = choices[int(raw) - 1]
        except (ValueError, IndexError):
            print("Invalid selection; choose one of the displayed numbers.")
            continue
        return selected["id"]


def _conditional_json():
    while True:
        try:
            value = json.loads(_ask("trigger_data JSON"))
        except json.JSONDecodeError:
            print("Invalid JSON object; try again.")
            continue
        if isinstance(value, dict) and value:
            return value
        print("trigger_data must be a non-empty JSON object; try again.")

def main():
    # The script is a public entrypoint and may target a freshly isolated DB.
    # Ensure both the draft table and additive migrations exist before begin.
    initialize_db()
    schema = registration_schema()
    actor = "local-script"
    session_id = "cli-" + uuid.uuid4().hex
    draft = begin_registration(actor, session_id)
    fields = {"key": _ask("key"), "content": _ask("content"), "source": _ask("source", "user"), "scope": _choose("scope", schema["scope"]["choices"]), "priority": int(_ask("priority", "0")), "trigger_type": _choose("trigger_type", schema["trigger_type"]["choices"]), "enforcement_level": _choose("enforcement_level", schema["enforcement_level"]["choices"]), "active": _choose("active", schema["active"]["choices"])}
    if fields["enforcement_level"] == "workflow": fields["workflow_skill_id"] = _choose("workflow_skill_id", schema["workflow_skill_id"]["choices"])
    if fields["enforcement_level"] == "blocking": fields["guard_id"] = _choose("guard_id", schema["guard_id"]["choices"])
    if fields["trigger_type"] == "conditional": fields["trigger_data"] = _conditional_json()
    complete_registration(draft["draft_id"], actor, session_id, fields)
    preview = preview_registration(draft["draft_id"], actor, session_id)
    print(json.dumps(preview, ensure_ascii=False, indent=2))
    if _ask("Approve this exact directive? yes/no", "no").lower() != "yes":
        print("not approved; nothing was stored")
        return
    approval = approve_registration(draft["draft_id"], actor, session_id, preview["digest"], True)
    print(json.dumps(commit_registration(draft["draft_id"], actor, session_id, preview["digest"], approval["approval_token"]), ensure_ascii=False, indent=2))

if __name__ == "__main__":
    try: main()
    except (RegistrationError, ValueError) as exc: raise SystemExit(f"registration failed: {exc}")
