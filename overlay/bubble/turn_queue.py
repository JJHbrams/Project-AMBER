"""Host-owned, provider-agnostic turn scheduling.

This module deliberately knows nothing about UI, provider messages, or image
bytes.  It owns the small state machine that those layers must correlate with.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from threading import RLock
from typing import Iterable


MIB = 1024 * 1024
MAX_IMAGES_PER_TURN = 4
MAX_IMAGE_BYTES = 5 * MIB
MAX_TURN_IMAGE_BYTES = 20 * MIB
MAX_QUEUED_IMAGE_BYTES = 40 * MIB
MAX_WAITING = 5


@dataclass(frozen=True, slots=True)
class TurnKey:
    session_id: str
    request_id: str
    attempt_generation: int


class TurnState(str, Enum):
    WAITING = "waiting"
    EDITING = "editing"
    ACTIVE = "active"
    INTERRUPT_REQUESTED = "interrupt_requested"
    SENT = "sent"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    UNKNOWN = "unknown"
    REMOVED = "removed"


@dataclass(frozen=True, slots=True)
class TurnSnapshot:
    """Sanitized internal diagnostic metadata; never an Event API payload."""

    key: TurnKey
    state: TurnState
    image_bytes: int


@dataclass(frozen=True, slots=True)
class QueueSnapshot:
    active: TurnSnapshot | None
    waiting: tuple[TurnSnapshot, ...]
    held: bool
    queued_image_bytes: int


@dataclass(slots=True, repr=False)
class _Turn:
    key: TurnKey
    text_private: str = field(repr=False)
    image_sizes: tuple[int, ...] = field(repr=False)
    state: TurnState = TurnState.WAITING
    edit_text_private: str | None = field(default=None, repr=False)
    edit_image_sizes: tuple[int, ...] | None = field(default=None, repr=False)

    @property
    def image_bytes(self) -> int:
        return sum(self.image_sizes)

    @property
    def edit_image_bytes(self) -> int:
        return sum(self.edit_image_sizes or ())


class TurnQueue:
    """A single-session, single-attempt FIFO with explicit terminal correlation.

    This is intentionally per provider attempt rather than a reconnect queue.
    Host integration must never silently carry waiting entries into a new
    generation; it may migrate only explicitly selected, held entries after
    their old attempt is known safe to abandon.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._turns: dict[TurnKey, _Turn] = {}
        self._waiting: list[TurnKey] = []
        self._request_ids: set[str] = set()
        self._active: TurnKey | None = None
        self._held = False
        self._priority: TurnKey | None = None
        self._session_id: str | None = None
        self._attempt_generation: int | None = None

    def __repr__(self) -> str:
        with self._lock:
            return f"TurnQueue(active={self._active!r}, waiting={len(self._waiting)}, held={self._held})"

    def enqueue(
        self,
        session_id: str,
        request_id: str,
        attempt_generation: int,
        *,
        text_private: str = "",
        image_sizes: Iterable[int] = (),
    ) -> TurnKey:
        """Add a waiting turn.  Dispatch is always a separate, explicit action."""
        key = self._key(session_id, request_id, attempt_generation)
        sizes = self._validated_sizes(image_sizes)
        with self._lock:
            self._bind_or_reject_locked(key)
            if request_id in self._request_ids:
                raise ValueError("request_id has already been used")
            if len(self._waiting) >= MAX_WAITING:
                raise OverflowError("waiting queue is full")
            if self._queued_image_bytes_locked() + sum(sizes) > MAX_QUEUED_IMAGE_BYTES:
                raise OverflowError("queued image limit exceeded")
            self._request_ids.add(request_id)
            self._turns[key] = _Turn(key, text_private, sizes)
            self._waiting.append(key)
        return key

    def dispatch_next(self) -> TurnKey | None:
        """Make exactly the FIFO head active, unless it is being edited or held."""
        with self._lock:
            if self._held or self._active is not None or not self._waiting:
                return None
            key = self._waiting[0]
            turn = self._turns[key]
            if turn.state is TurnState.EDITING:
                return None
            if turn.state is not TurnState.WAITING:
                raise RuntimeError("waiting queue contains a non-waiting turn")
            self._waiting.pop(0)
            turn.state = TurnState.ACTIVE
            self._active = key
            return key

    def dispatch_priority_after_terminal(self, key: TurnKey) -> TurnKey | None:
        """Select one waiting item only while idle; all remaining work is held."""
        with self._lock:
            if self._active is not None or key not in self._waiting or (self._priority is not None and key != self._priority): return None
            turn=self._turns[key]
            if turn.state is not TurnState.WAITING: return None
            self._waiting.remove(key); turn.state=TurnState.ACTIVE; self._active=key; self._held=True; self._priority=None
            return key

    def reserve_priority(self, key: TurnKey) -> bool:
        with self._lock:
            turn=self._waiting_turn_locked(key)
            if self._priority is not None or turn is None or turn.state is not TurnState.WAITING:
                return False
            self._priority=key
            self._held=True
            return True

    def release_priority(self) -> None:
        with self._lock:
            self._priority=None

    def update_edit(self, key: TurnKey, text_private: str, image_sizes: Iterable[int]) -> bool:
        sizes=self._validated_sizes(image_sizes)
        with self._lock:
            turn=self._turns.get(key)
            if turn is None or turn.state is not TurnState.EDITING: return False
            if self._queued_image_bytes_locked()-turn.edit_image_bytes+sum(sizes)>MAX_QUEUED_IMAGE_BYTES:
                raise OverflowError('queued image limit exceeded')
            turn.edit_text_private=text_private
            turn.edit_image_sizes=sizes
            return True

    def begin_edit(self, key: TurnKey, *, text_private: str | None = None,
                   image_sizes: Iterable[int] | None = None) -> bool:
        """Reserve an edit copy; an editing FIFO head cannot be overtaken."""
        with self._lock:
            turn = self._waiting_turn_locked(key)
            if turn is None or turn.state is TurnState.EDITING or key == self._priority:
                return False
            draft_sizes = turn.image_sizes if image_sizes is None else self._validated_sizes(image_sizes)
            if self._queued_image_bytes_locked() + sum(draft_sizes) > MAX_QUEUED_IMAGE_BYTES:
                raise OverflowError("queued image limit exceeded")
            turn.edit_text_private = turn.text_private if text_private is None else text_private
            turn.edit_image_sizes = draft_sizes
            turn.state = TurnState.EDITING
            return True

    def save_edit(self, key: TurnKey) -> bool:
        with self._lock:
            turn = self._turns.get(key)
            if turn is None or turn.state is not TurnState.EDITING:
                return False
            assert turn.edit_text_private is not None and turn.edit_image_sizes is not None
            turn.text_private = turn.edit_text_private
            turn.image_sizes = turn.edit_image_sizes
            turn.edit_text_private = None
            turn.edit_image_sizes = None
            turn.state = TurnState.WAITING
            return True

    def cancel_edit(self, key: TurnKey) -> bool:
        with self._lock:
            turn = self._turns.get(key)
            if turn is None or turn.state is not TurnState.EDITING:
                return False
            turn.edit_text_private = None
            turn.edit_image_sizes = None
            turn.state = TurnState.WAITING
            return True

    def delete(self, key: TurnKey) -> bool:
        """Remove only a not-yet-dispatched item; active turns are never hidden."""
        with self._lock:
            turn = self._waiting_turn_locked(key)
            if turn is None or key == self._priority:
                return False
            self._waiting.remove(key)
            turn.state = TurnState.REMOVED
            self._release_private_payload_locked(turn)
            return True

    def request_interrupt(self, key: TurnKey) -> bool:
        """Record a request only; provider confirmation decides terminal state."""
        with self._lock:
            if key != self._active or self._turns[key].state is not TurnState.ACTIVE:
                return False
            self._turns[key].state = TurnState.INTERRUPT_REQUESTED
            return True

    def terminal_success(self, key: TurnKey) -> bool:
        return self._terminal(key, TurnState.SENT, hold=False)

    def terminal_failure(self, key: TurnKey) -> bool:
        return self._terminal(key, TurnState.FAILED, hold=True)

    def confirmed_interrupt(self, key: TurnKey) -> bool:
        with self._lock:
            if key != self._active or self._turns[key].state not in (TurnState.INTERRUPT_REQUESTED, TurnState.UNKNOWN, TurnState.ACTIVE):
                return False
            self._turns[key].state = TurnState.INTERRUPTED
            self._release_private_payload_locked(self._turns[key])
            self._active = None
            self._held = True
            return True

    def mark_unknown(self, key: TurnKey) -> bool:
        with self._lock:
            if key != self._active or self._turns[key].state not in (TurnState.ACTIVE, TurnState.INTERRUPT_REQUESTED):
                return False
            self._turns[key].state = TurnState.UNKNOWN
            self._held = True
            return True

    def resume(self) -> bool:
        """Release a held queue only after any unknown active turn is reconciled."""
        with self._lock:
            if not self._held or self._active is not None:
                return False
            self._held = False
            return True

    def hold(self) -> None:
        """Hold future dispatch (for example during collapse) without touching active work."""
        with self._lock:
            self._held = True

    def snapshot(self) -> QueueSnapshot:
        with self._lock:
            active = self._snapshot_locked(self._active) if self._active else None
            waiting = tuple(self._snapshot_locked(key) for key in self._waiting)
            return QueueSnapshot(active, waiting, self._held, self._queued_image_bytes_locked())

    def _terminal(self, key: TurnKey, state: TurnState, *, hold: bool) -> bool:
        with self._lock:
            if key != self._active:
                return False
            turn = self._turns[key]
            if turn.state not in (TurnState.ACTIVE, TurnState.INTERRUPT_REQUESTED, TurnState.UNKNOWN):
                return False
            stop_was_requested = turn.state is TurnState.INTERRUPT_REQUESTED
            turn.state = state
            self._release_private_payload_locked(turn)
            self._active = None
            # A user stop request conservatively holds later work even if a
            # success result won the provider race.
            self._held = self._held or hold or stop_was_requested
            return True

    def _waiting_turn_locked(self, key: TurnKey) -> _Turn | None:
        turn = self._turns.get(key)
        return turn if turn is not None and key in self._waiting else None

    def _bind_or_reject_locked(self, key: TurnKey) -> None:
        """A queue is one session/attempt generation, never a cross-session bus.

        A caller that begins a new provider attempt must use a new queue (or
        explicitly drain this one first); accepting a different generation
        here would make stale provider events ambiguous.
        """
        if self._session_id is None:
            self._session_id = key.session_id
            self._attempt_generation = key.attempt_generation
            return
        if key.session_id != self._session_id:
            raise ValueError("queue is bound to a different session")
        if key.attempt_generation != self._attempt_generation:
            raise ValueError("queue is bound to a different attempt generation")

    @staticmethod
    def _release_private_payload_locked(turn: _Turn) -> None:
        """Keep only the tombstone key/state after removal or terminal completion."""
        turn.text_private = ""
        turn.image_sizes = ()
        turn.edit_text_private = None
        turn.edit_image_sizes = None

    def _queued_image_bytes_locked(self) -> int:
        return sum(self._turns[key].image_bytes + self._turns[key].edit_image_bytes for key in self._waiting)

    def _snapshot_locked(self, key: TurnKey) -> TurnSnapshot:
        turn = self._turns[key]
        return TurnSnapshot(key, turn.state, turn.image_bytes)

    @staticmethod
    def _key(session_id: str, request_id: str, attempt_generation: int) -> TurnKey:
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id must be a non-empty string")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("request_id must be a non-empty string")
        if not TurnQueue._safe_size(attempt_generation):
            raise ValueError("attempt_generation must be a non-negative integer")
        return TurnKey(session_id, request_id, attempt_generation)

    @staticmethod
    def _validated_sizes(image_sizes: Iterable[int]) -> tuple[int, ...]:
        sizes = tuple(image_sizes)
        if len(sizes) > MAX_IMAGES_PER_TURN:
            raise ValueError("too many images")
        if any(not TurnQueue._safe_size(size) or size > MAX_IMAGE_BYTES for size in sizes):
            raise ValueError("image size must be a safe non-negative integer within the per-image limit")
        if sum(sizes) > MAX_TURN_IMAGE_BYTES:
            raise ValueError("turn image limit exceeded")
        return sizes

    @staticmethod
    def _safe_size(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0
