import os
import tempfile
import unittest
import uuid
from unittest.mock import patch


class DirectiveRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"ENGRAM_DB_DIR": self.tempdir.name})
        self.env.start()
        from core.storage.db import initialize_db
        initialize_db()
        from core.context import directive_registration as registration
        self.r = registration
        self.key = "approved-rule-" + uuid.uuid4().hex
        self.actor = "test-agent"
        self.session_id = "session-" + uuid.uuid4().hex

    def tearDown(self):
        self.env.stop()
        self.tempdir.cleanup()

    def payload(self, **overrides):
        value = {"key": self.key, "content": "Use the approved workflow.", "source": "user", "scope": "all", "priority": 4, "active": True, "trigger_type": "code", "enforcement_level": "workflow", "workflow_skill_id": "engram-task-workflow"}
        value.update(overrides)
        return value

    def test_no_directive_before_explicit_approval_and_one_time_commit(self):
        from core.storage.db import get_connection
        draft = self.r.begin_registration(self.actor, self.session_id)
        completed = self.r.complete_registration(draft["draft_id"], self.actor, self.session_id, self.payload())
        conn = get_connection()
        self.assertIsNone(conn.execute("SELECT 1 FROM directives WHERE key = ?", (self.key,)).fetchone())
        conn.close()
        preview = self.r.preview_registration(draft["draft_id"], self.actor, self.session_id)
        approval = self.r.approve_registration(draft["draft_id"], self.actor, self.session_id, preview["digest"], True)
        committed = self.r.commit_registration(draft["draft_id"], self.actor, self.session_id, completed["digest"], approval["approval_token"])
        self.assertEqual(committed["status"], "directive_committed")
        with self.assertRaises(self.r.RegistrationError):
            self.r.commit_registration(draft["draft_id"], self.actor, self.session_id, preview["digest"], approval["approval_token"])

    def test_invalid_and_conditional_values_are_rejected(self):
        draft = self.r.begin_registration(self.actor, self.session_id)
        with self.assertRaises(self.r.RegistrationError):
            self.r.complete_registration(draft["draft_id"], self.actor, self.session_id, self.payload(scope="codex"))
        with self.assertRaises(self.r.RegistrationError):
            self.r.complete_registration(draft["draft_id"], self.actor, self.session_id, self.payload(enforcement_level="blocking", workflow_skill_id="", guard_id="bogus"))
        with self.assertRaises(self.r.RegistrationError):
            self.r.complete_registration(draft["draft_id"], self.actor, self.session_id, self.payload(trigger_type="conditional", trigger_data={}))
        with self.assertRaises(self.r.RegistrationError):
            self.r.complete_registration(draft["draft_id"], self.actor, self.session_id, self.payload(workflow_skill_id="invented-workflow"))

    def test_preview_digest_binds_approval_and_detects_target_change(self):
        draft = self.r.begin_registration(self.actor, self.session_id)
        self.r.complete_registration(draft["draft_id"], self.actor, self.session_id, self.payload())
        preview = self.r.preview_registration(draft["draft_id"], self.actor, self.session_id)
        with self.assertRaises(self.r.RegistrationError):
            self.r.approve_registration(draft["draft_id"], self.actor, self.session_id, "wrong", True)
        approval = self.r.approve_registration(draft["draft_id"], self.actor, self.session_id, preview["digest"], True)
        from core.context.directives import add_directive
        add_directive(self.key, "concurrent update")
        with self.assertRaises(self.r.RegistrationError):
            self.r.commit_registration(draft["draft_id"], self.actor, self.session_id, preview["digest"], approval["approval_token"])

    def test_approval_requires_preview_of_current_digest(self):
        draft = self.r.begin_registration(self.actor, self.session_id)
        first = self.r.complete_registration(draft["draft_id"], self.actor, self.session_id, self.payload())
        with self.assertRaisesRegex(self.r.RegistrationError, "preview"):
            self.r.approve_registration(draft["draft_id"], self.actor, self.session_id, first["digest"], True)
        preview = self.r.preview_registration(draft["draft_id"], self.actor, self.session_id)
        changed = self.r.complete_registration(draft["draft_id"], self.actor, self.session_id, self.payload(content="Changed after preview."))
        self.assertNotEqual(preview["digest"], changed["digest"])
        with self.assertRaisesRegex(self.r.RegistrationError, "digest"):
            self.r.approve_registration(draft["draft_id"], self.actor, self.session_id, preview["digest"], True)
        with self.assertRaisesRegex(self.r.RegistrationError, "preview"):
            self.r.approve_registration(draft["draft_id"], self.actor, self.session_id, changed["digest"], True)
        refreshed = self.r.preview_registration(draft["draft_id"], self.actor, self.session_id)
        approval = self.r.approve_registration(draft["draft_id"], self.actor, self.session_id, refreshed["digest"], True)
        self.assertEqual(approval["status"], "approved")

    def test_actor_session_token_and_expiry_are_bound(self):
        draft = self.r.begin_registration(self.actor, self.session_id)
        with self.assertRaises(self.r.RegistrationError):
            self.r.complete_registration(draft["draft_id"], "other", self.session_id, self.payload())
        self.r.complete_registration(draft["draft_id"], self.actor, self.session_id, self.payload())
        with self.assertRaises(self.r.RegistrationError):
            self.r.preview_registration(draft["draft_id"], "other", self.session_id)
        with self.assertRaises(self.r.RegistrationError):
            self.r.preview_registration(draft["draft_id"], self.actor, "other-session")
        preview = self.r.preview_registration(draft["draft_id"], self.actor, self.session_id)
        with self.assertRaises(self.r.RegistrationError):
            self.r.approve_registration(draft["draft_id"], self.actor, "other-session", preview["digest"], True)
        approval = self.r.approve_registration(draft["draft_id"], self.actor, self.session_id, preview["digest"], True)
        with self.assertRaises(self.r.RegistrationError):
            self.r.commit_registration(draft["draft_id"], "other", self.session_id, preview["digest"], approval["approval_token"])
        with self.assertRaises(self.r.RegistrationError):
            self.r.commit_registration(draft["draft_id"], self.actor, self.session_id, preview["digest"], "wrong-token")
        from core.storage.db import get_connection
        conn = get_connection()
        with conn:
            conn.execute("UPDATE directive_registration_drafts SET approval_expires_at = 0 WHERE draft_id = ?", (draft["draft_id"],))
        conn.close()
        with self.assertRaises(self.r.RegistrationError):
            self.r.commit_registration(draft["draft_id"], self.actor, self.session_id, preview["digest"], approval["approval_token"])

    def test_schema_exposes_known_workflow_choices(self):
        ids = {choice["id"] for choice in self.r.registration_schema()["workflow_skill_id"]["choices"]}
        self.assertTrue({"engram-task-workflow", "engram-wiki-workflow", "engram-close-session", "engram-new-session"} <= ids)
