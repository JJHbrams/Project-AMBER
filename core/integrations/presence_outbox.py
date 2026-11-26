"""Bounded delivery outbox for MCP presence.

The MCP event loop must never wait for overlay IO or for any lock that IO can
hold, so reporters only record intent here and one sender thread delivers it.
One slot exists per (provider, owner); it coalesces presence, lifecycle/explicit
state packets (semantic events are appended, never dropped by a merge), the
latest title, the latest project and an "ended" mark.

This module is memory-only and NOT thread-safe: the owner (PresenceReporter)
serializes every call with its _state_lock.  It performs no IO.
"""

from collections import OrderedDict
import threading

MAX_PACKET_EVENTS = 2      # overlay.validate_semantic accepts at most 2 events per request
MAX_QUEUED_EVENTS = 16     # per slot; beyond this the oldest events are shed (state/seq are kept)
MAX_PACKETS = 16
MAX_ENDED_ATTEMPTS = 50
ENDED_RETRY_SECONDS = 2.0


class Ticket:
    """Delivery receipt a reporting thread may wait on (never the event loop)."""
    __slots__ = ('_event', 'status')

    def __init__(self):
        self._event = threading.Event()
        self.status = None  # 'delivered' | 'failed' | 'superseded' | 'dropped'

    def resolve(self, status):
        if self.status is None:
            self.status = status
            self._event.set()

    @property
    def done(self):
        return self._event.is_set()

    def wait(self, timeout):
        if timeout and timeout > 0:
            return self._event.wait(timeout)
        return self._event.is_set()


class Op:
    __slots__ = ('kind', 'payload', 'endpoint', 'lifecycle', 'seq', 'presence',
                 'want_status', 'tickets', 'retry_at', 'attempts', 'meta')

    def __init__(self, kind, payload, *, endpoint='/state/presence', lifecycle=False, seq=0,
                 presence=None, want_status=False, ticket=None, meta=None):
        self.kind, self.payload, self.endpoint, self.lifecycle = kind, payload, endpoint, lifecycle
        self.seq, self.presence, self.want_status = seq, presence, want_status
        self.tickets = [ticket] if ticket is not None else []
        self.retry_at, self.attempts, self.meta = 0.0, 0, dict(meta or {})

    def resolve(self, status):
        for ticket in self.tickets:
            ticket.resolve(status)


class Slot:
    __slots__ = ('key', 'presence', 'packets', 'title', 'project', 'ended', 'inflight', 'dead')

    def __init__(self, key):
        self.key = key
        self.presence = self.title = self.project = self.ended = None
        self.packets = []
        self.inflight = 0
        self.dead = False  # an "ended" mark was recorded: nothing else may be queued

    def empty(self):
        return not (self.presence or self.packets or self.title or self.project
                    or self.ended or self.inflight)

    def only_presence(self):
        return bool(self.presence) and not (self.packets or self.title or self.project
                                            or self.ended or self.inflight or self.dead)


def _header(lifecycle):
    return 'codex' if lifecycle == 'codex' else 'claude'


class Outbox:
    def __init__(self, capacity=256):
        self.capacity = capacity
        self.slots = OrderedDict()

    # -- slot management ---------------------------------------------------
    def _make_room(self):
        while len(self.slots) >= self.capacity:
            victim = next((s for s in self.slots.values() if s.only_presence()), None)
            if victim is None:
                victim = next((s for s in self.slots.values()
                               if not s.ended and not s.dead and not s.inflight), None)
            if victim is None:
                return False
            self._discard_slot(victim)
        return True

    def _discard_slot(self, slot):
        for op in self._ops(slot):
            op.resolve('dropped')
        self.slots.pop(slot.key, None)

    @staticmethod
    def _ops(slot):
        ops = [slot.presence, *slot.packets, slot.title, slot.project, slot.ended]
        return [op for op in ops if op is not None]

    def _slot(self, key, *, force=False):
        slot = self.slots.get(key)
        if slot is None:
            if not self._make_room() and not force:
                return None
            slot = self.slots[key] = Slot(key)
        return slot

    def _accept(self, key):
        slot = self._slot(key)
        return slot if slot is not None and not slot.dead else None

    @staticmethod
    def _swap(slot, name, new):
        old = getattr(slot, name)
        if old is not None:
            new.tickets = old.tickets + new.tickets
            new.meta = {**old.meta, **new.meta}
        setattr(slot, name, new)

    # -- recording intent --------------------------------------------------
    def put_presence(self, key, payload, *, want_status=True, ticket=None):
        slot = self._accept(key)
        if slot is None:
            if ticket:
                ticket.resolve('dropped')
            return False
        self._swap(slot, 'presence', Op('presence', payload, want_status=want_status, ticket=ticket))
        return True

    def put_title(self, key, payload, seq, *, presence=None, ticket=None, meta=None):
        slot = self._accept(key)
        if slot is None:
            if ticket:
                ticket.resolve('dropped')
            return False
        self._swap(slot, 'title', Op('title', payload, endpoint='/state/title', seq=seq,
                                     presence=presence, ticket=ticket, meta=meta))
        return True

    def put_project(self, key, payload, seq, *, presence=None, ticket=None, meta=None):
        slot = self._accept(key)
        if slot is None:
            if ticket:
                ticket.resolve('dropped')
            return False
        self._swap(slot, 'project', Op('project', payload, endpoint='/state/project', seq=seq,
                                       presence=presence, ticket=ticket, meta=meta))
        return True

    def put_explicit_state(self, key, payload, *, ticket=None):
        slot = self._accept(key)
        if slot is None:
            if ticket:
                ticket.resolve('dropped')
            return False
        op = Op('explicit', payload, endpoint='/state', ticket=ticket)
        last = slot.packets[-1] if slot.packets else None
        if last is not None and last.kind == 'explicit':
            op.tickets = last.tickets + op.tickets
            slot.packets[-1] = op
        else:
            slot.packets.append(op)
        self._bound_packets(slot)
        return True

    def put_lifecycle_state(self, key, payload, lifecycle, *, ticket=None):
        """Append (or merge into the previous pending packet) a native lifecycle packet."""
        slot = self._accept(key)
        if slot is None:
            if ticket:
                ticket.resolve('dropped')
            return False
        op = Op('lifecycle', payload, endpoint='/state', lifecycle=lifecycle,
                want_status=True, ticket=ticket)
        last = slot.packets[-1] if slot.packets else None
        merged = self._merge(last, op) if last is not None and last.kind == 'lifecycle' else None
        if merged is not None:
            slot.packets[-1] = merged
        else:
            slot.packets.append(op)
        self._bound_packets(slot)
        return True

    @staticmethod
    def _merge(prev, new):
        """Fold two pending packets into one the overlay accepts (see session_registry.upsert_lifecycle).

        A child_update never owns parent state; a normal packet always does.  Events
        concatenate (never silently dropped) and the newest seq/category wins."""
        if _header(prev.lifecycle) != _header(new.lifecycle):
            return None
        a, b = prev.payload, new.payload
        events = list(a['semantic']['events']) + list(b['semantic']['events'])
        if len(events) > MAX_PACKET_EVENTS:
            return None
        a_child, b_child = bool(a.get('child_update')), bool(b.get('child_update'))
        merged = dict(b)
        if b_child and not a_child:
            merged.pop('child_update', None)
            merged['state'] = a['state']
        semantic = dict(b['semantic'], events=events)
        if merged['state'] != 'working':
            semantic['active_category'] = None
        merged['semantic'] = semantic
        op = Op('lifecycle', merged, endpoint='/state', lifecycle=new.lifecycle, want_status=True)
        op.tickets = prev.tickets + new.tickets
        return op

    @staticmethod
    def _bound_packets(slot):
        packets = slot.packets
        while len(packets) > MAX_PACKETS:
            # Fold the oldest packet into its successor: tickets move, ordering is kept.
            oldest = packets.pop(0)
            packets[0].tickets = oldest.tickets + packets[0].tickets
        total = sum(len(p.payload['semantic']['events']) for p in packets if p.kind == 'lifecycle')
        for packet in packets:
            if total <= MAX_QUEUED_EVENTS:
                break
            if packet.kind == 'lifecycle' and packet.payload['semantic']['events']:
                total -= len(packet.payload['semantic']['events'])
                packet.payload = dict(packet.payload, semantic=dict(packet.payload['semantic'], events=[]))

    def put_ended(self, key, payload, *, lifecycle=False, retry=False, ticket=None, meta=None):
        """Record the end of a connection. Always kept; everything else queued is dropped."""
        slot = self._slot(key, force=True)
        previous = slot.ended
        for op in self._ops(slot):
            if op is not previous:
                op.resolve('dropped')
        slot.presence = slot.title = slot.project = None
        slot.packets = []
        slot.dead = True
        op = Op('ended', payload, lifecycle=lifecycle, ticket=ticket, meta=meta)
        op.meta['retry'] = bool(retry or (previous and previous.meta.get('retry')))
        if previous is not None:
            op.tickets = previous.tickets + op.tickets
            op.meta = {**previous.meta, **op.meta}
        slot.ended = op
        return True

    def discard(self, key):
        """Drop queued presence/state/title/project for a key (an ended mark is kept)."""
        slot = self.slots.get(key)
        if slot is None:
            return
        for name in ('presence', 'title', 'project'):
            op = getattr(slot, name)
            if op is not None:
                op.resolve('dropped')
                setattr(slot, name, None)
        for op in slot.packets:
            op.resolve('dropped')
        slot.packets = []
        if slot.empty():
            self.slots.pop(key, None)

    def discard_presence(self, key):
        slot = self.slots.get(key)
        if slot is not None and slot.presence is not None:
            slot.presence.resolve('dropped')
            slot.presence = None
            if slot.empty():
                self.slots.pop(key, None)

    def migrate_metadata(self, source, target, target_session_id, names=('title', 'project')):
        """Move a pending title/project from an alias slot to its canonical owner slot.

        Returns the names moved so the caller can keep its per-owner bookkeeping."""
        slot = self.slots.get(source)
        if slot is None or slot.dead:
            return ()
        moved = []
        for name in names:
            op = getattr(slot, name)
            if op is None:
                continue
            setattr(slot, name, None)
            target_slot = self._accept(target)
            if target_slot is None:
                op.resolve('dropped')
                continue
            current = getattr(target_slot, name)
            if current is not None and current.seq > op.seq:
                op.resolve('dropped')
                continue
            op.payload = dict(op.payload, session_id=target_session_id)
            if op.presence:
                op.presence = dict(op.presence, session_id=target_session_id)
            self._swap(target_slot, name, op)
            moved.append(name)
        return tuple(moved)

    # -- delivery ----------------------------------------------------------
    def take(self, now):
        """Detach the next sendable op (round robin across slots)."""
        for key, slot in list(self.slots.items()):
            op = None
            if slot.ended is not None:
                if slot.ended.retry_at <= now:
                    op, slot.ended = slot.ended, None
            elif slot.presence is not None:
                op, slot.presence = slot.presence, None
            elif slot.packets:
                op = slot.packets.pop(0)
            elif slot.title is not None:
                op, slot.title = slot.title, None
            elif slot.project is not None:
                op, slot.project = slot.project, None
            if op is not None:
                slot.inflight += 1
                self.slots.move_to_end(key)
                return key, op
        return None

    def finish(self, key, op, status, *, now=0.0):
        """Record the delivery result; returns True if the op was re-queued for retry."""
        slot = self.slots.get(key)
        requeued = False
        if slot is not None:
            slot.inflight = max(0, slot.inflight - 1)
        if (op.kind == 'ended' and status == 'failed' and op.meta.get('retry')
                and op.attempts + 1 < MAX_ENDED_ATTEMPTS):
            op.attempts += 1
            op.retry_at = now + ENDED_RETRY_SECONDS
            if slot is not None and slot.ended is None:
                slot.ended = op
                requeued = True
        if not requeued:
            op.resolve(status)
        if slot is not None and slot.empty():
            self.slots.pop(key, None)
        return requeued

    def pending(self, now):
        """Ops that could be sent right now (retry-delayed ended marks excluded)."""
        count = 0
        for slot in self.slots.values():
            if slot.ended is not None:
                count += slot.ended.retry_at <= now
            else:
                count += sum(op is not None for op in (slot.presence, slot.title, slot.project)) + len(slot.packets)
        return count

    def next_retry(self, now):
        delays = [slot.ended.retry_at - now for slot in self.slots.values()
                  if slot.ended is not None and slot.ended.retry_at > now]
        return min(delays) if delays else None

    def __len__(self):
        return len(self.slots)
