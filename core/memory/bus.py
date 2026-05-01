from dataclasses import dataclass
from typing import List, Optional, Union

from core.context.context_builder import build_system_prompt
from core.context.project_scope import resolve_project_key, resolve_scope_key
from core.identity import update_themes_from_text
from .store import (
    DEFAULT_SCOPE_KEY,
    append_working_memory_hint,
    create_session,
    get_recent_messages,
    save_memory,
    save_message,
)


@dataclass(frozen=True)
class MemorySession:
    session_id: int
    scope_key: str = DEFAULT_SCOPE_KEY


SessionLike = Union[int, MemorySession]


class MemoryBus:
    def start_session(
        self,
        scope_key: Optional[str] = None,
        *,
        project_key: Optional[str] = None,
        project_keys: Optional[List[str]] = None,
        cwd: Optional[str] = None,
    ) -> MemorySession:
        resolved_scope = resolve_scope_key(scope_key, project_key=project_key, cwd=cwd)
        # project_key(단일)와 project_keys(복수)를 합친다
        all_keys: List[str] = []
        if project_key:
            all_keys.append(project_key)
        if project_keys:
            all_keys.extend(project_keys)
        session_id = create_session(scope_key=resolved_scope, project_keys=all_keys or None)
        return MemorySession(session_id=session_id, scope_key=resolved_scope)

    def record_user_message(self, session: SessionLike, content: str, update_themes: bool = False):
        session_id = self._get_session_id(session)
        save_message(session_id, "user", content)
        if update_themes:
            update_themes_from_text(content)

    def record_assistant_message(
        self,
        session: SessionLike,
        content: str,
        *,
        user_content: str = "",
        scope_key: Optional[str] = None,
        project_key: Optional[str] = None,
        cwd: Optional[str] = None,
        update_themes: bool = False,
        update_working_memory: bool = False,
    ):
        session_id = self._get_session_id(session)
        resolved_scope = self._resolve_scope_key(session, scope_key, project_key=project_key, cwd=cwd)

        save_message(session_id, "assistant", content)
        if update_themes:
            update_themes_from_text(content)
        if update_working_memory:
            append_working_memory_hint(resolved_scope, user_content, content)

    def get_recent_conversation(self, session: SessionLike, limit: int = 20) -> list[dict]:
        session_id = self._get_session_id(session)
        return get_recent_messages(session_id, limit=limit)

    def compose_prompt_context(
        self,
        user_query: str = "",
        *,
        caller: str = "all",
        scope_key: Optional[str] = None,
        project_key: Optional[str] = None,
        cwd: Optional[str] = None,
        session: Optional[MemorySession] = None,
        is_session_init: bool = False,
    ) -> str:
        resolved_scope = self._resolve_scope_key(session, scope_key, project_key=project_key, cwd=cwd)
        resolved_project_key = project_key or resolve_project_key(cwd=cwd)
        return build_system_prompt(user_query, caller=caller, scope_key=resolved_scope, project_key=resolved_project_key or "", is_session_init=is_session_init)

    def maybe_save_episodic_memory(
        self,
        session: SessionLike,
        user_content: str,
        assistant_content: str,
        *,
        user_turn_count: int,
        cadence: int = 4,
    ) -> bool:
        if cadence <= 0 or user_turn_count <= 0 or user_turn_count % cadence != 0:
            return False

        session_id = self._get_session_id(session)
        save_memory(session_id, f"Q: {user_content[:100]} / A: {assistant_content[:200]}")
        return True

    @staticmethod
    def _get_session_id(session: SessionLike) -> int:
        if isinstance(session, MemorySession):
            return session.session_id
        return int(session)

    @staticmethod
    def _resolve_scope_key(
        session: Optional[SessionLike],
        scope_key: Optional[str],
        *,
        project_key: Optional[str] = None,
        cwd: Optional[str] = None,
    ) -> str:
        if isinstance(session, MemorySession):
            return session.scope_key

        return resolve_scope_key(scope_key, project_key=project_key, cwd=cwd)


memory_bus = MemoryBus()


