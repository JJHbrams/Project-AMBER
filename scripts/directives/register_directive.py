"""Interactive, approval-gated directive registration CLI."""
from __future__ import annotations
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core.context.directive_registration import (RegistrationError, approve_registration, begin_registration, commit_registration, complete_registration, preview_registration, registration_schema)

def _ask(name, default=""):
    value = input(f"{name}" + (f" [{default}]" if default != "" else "") + ": ").strip()
    return value if value else default

def main():
    schema = registration_schema()
    print(json.dumps(schema, ensure_ascii=False, indent=2))
    draft = begin_registration()
    fields = {"key": _ask("key"), "content": _ask("content"), "source": _ask("source", "user"), "scope": _ask("scope", "all"), "priority": int(_ask("priority", "0")), "trigger_type": _ask("trigger_type", "always"), "enforcement_level": _ask("enforcement_level", "advisory"), "active": _ask("active true/false", "true").lower() == "true"}
    if fields["enforcement_level"] == "workflow": fields["workflow_skill_id"] = _ask("workflow_skill_id")
    if fields["enforcement_level"] == "blocking": fields["guard_id"] = _ask("guard_id")
    if fields["trigger_type"] == "conditional": fields["trigger_data"] = json.loads(_ask("trigger_data JSON"))
    complete_registration(draft["draft_id"], fields)
    preview = preview_registration(draft["draft_id"])
    print(json.dumps(preview, ensure_ascii=False, indent=2))
    if _ask("Approve this exact directive? yes/no", "no").lower() != "yes":
        print("not approved; nothing was stored")
        return
    approval = approve_registration(draft["draft_id"], preview["digest"], True)
    print(json.dumps(commit_registration(draft["draft_id"], preview["digest"], approval["approval_token"]), ensure_ascii=False, indent=2))

if __name__ == "__main__":
    try: main()
    except (RegistrationError, ValueError) as exc: raise SystemExit(f"registration failed: {exc}")
