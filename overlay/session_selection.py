"""Local selection and acknowledgement; never writes STM or provider state."""

import time


class SessionSelection:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.rows = []
        self.auto_enabled = True
        self.pinned_key = None
        self.candidate_key = None
        self.deadline = None
        self._ack = {}
        self._selected_key = None
        self._arrivals = {}
        self._arrival_clock = 0
        self._handoff_after = 0
        self._previous_states = {}
        self._working_activations = {}
        self._activation_clock = 0

    def update(self, rows):
        self.rows = sorted((dict(r) for r in rows), key=lambda r: r["first_seen"])
        keys = {r["key"] for r in self.rows}
        for row in self.rows:
            if row["key"] not in self._arrivals:
                self._arrival_clock += 1
                self._arrivals[row["key"]] = self._arrival_clock
            if (row['state'] == 'working'
                    and self._previous_states.get(row['key']) != 'working'):
                self._activation_clock += 1
                self._working_activations[row['key']] = self._activation_clock
        self._previous_states = {row['key']: row['state'] for row in self.rows}
        self._working_activations = {key: stamp for key, stamp in self._working_activations.items()
                                     if self._previous_states.get(key) == 'working'}
        self._arrivals = {
            key: stamp for key, stamp in self._arrivals.items() if key in keys
        }
        self._ack = {k: v for k, v in self._ack.items() if k in keys}
        if self.pinned_key not in keys:
            self.pinned_key = None
        if self.candidate_key is not None and self.candidate_key not in keys:
            self.pinned_key = None
            self.candidate_key = self.deadline = None
        for row in self.rows:
            row["acknowledged"] = self._ack.get(row["key"]) == (
                row["state"],
                row.get("state_since"),
            )
        self._follow_completion()
        self.settle()

    def _follow_completion(self):
        """New work or an arrival, never a heartbeat, can replace a ready target."""
        if not self.rows:
            self._selected_key = None
            return
        row = self.selected_row()
        if row is not None and self.candidate_key is not None:
            return  # A manual preview never silently changes the committed target.
        if row is None:
            eligible = [candidate for candidate in self.rows
                        if candidate['state'] in ('working', 'needs_input', 'blocked')] or self.rows
        elif self.auto_enabled and row['state'] == 'unknown':
            eligible = [candidate for candidate in self.rows
                        if candidate['state'] in ('working', 'needs_input', 'blocked')]
        elif self.auto_enabled and row["state"] == "ready":
            active = [candidate for candidate in self.rows
                      if candidate['key'] != row['key'] and candidate['state'] == 'working'
                      and candidate['key'] in self._working_activations]
            if active:
                target = max(active, key=lambda candidate: self._working_activations[candidate['key']])
                self._selected_key = target['key']
                # Consume this activation only. Older queued candidates may
                # still be working when the latest selected one completes.
                self._working_activations.pop(target['key'], None)
                # Work wins over unrelated new unknown/completed rows. Consume
                # those already observed arrivals too, avoiding a later bounce.
                self._handoff_after = self._arrival_clock
                self.pinned_key = None
                return
            eligible = [
                candidate
                for candidate in self.rows
                if candidate["key"] != row["key"]
                and self._arrivals[candidate["key"]] > self._handoff_after
            ]
        else:
            return
        if eligible:
            target = max(
                eligible, key=lambda candidate: self._arrivals[candidate["key"]]
            )
            self._selected_key = target["key"]
            if row is None or row['state'] == 'unknown':
                self._working_activations.clear()
            else:
                self._working_activations.pop(target['key'], None)
            self._handoff_after = (self._arrival_clock if row is None or row['state'] == 'unknown'
                                   else self._arrivals[target["key"]])
            self.pinned_key = None

    @property
    def selected_key(self):
        return self._selected_key

    def selected_row(self):
        return next((r for r in self.rows if r["key"] == self.selected_key), None)

    def browse(self, delta):
        keys = [r["key"] for r in self.rows]
        if not keys:
            return
        current = self.candidate_key or self.selected_key
        self.candidate_key = keys[(keys.index(current) + delta) % len(keys)]
        self.deadline = self.clock() + 0.4

    def settle(self):
        if self.deadline is not None and self.clock() >= self.deadline:
            self.pin(self.candidate_key)

    def pin(self, key):
        row = next((r for r in self.rows if r["key"] == key), None)
        self.candidate_key = self.deadline = None
        if row:
            self.auto_enabled = False
            self._selected_key = key
            # Deliberate selection consumes all arrivals already observed at
            # commit, so choosing an older completed row does not bounce away.
            self._handoff_after = self._arrival_clock
            self._working_activations.clear()
            self.pinned_key = key
            self._ack[key] = (row["state"], row.get("state_since"))
            row["acknowledged"] = True

    def resume_auto(self):
        self.auto_enabled = True
        self.pinned_key = self.candidate_key = self.deadline = None
        self._follow_completion()

    def set_auto_enabled(self, enabled):
        """Changing monitoring policy does not acknowledge or discard arrivals."""
        if enabled:
            self.resume_auto()
        else:
            self.auto_enabled = False
            self.candidate_key = self.deadline = None
            self.pinned_key = self.selected_key

    def jump_attention(self, state=None):
        rows = [
            r
            for r in self.rows
            if r["state"] in ("needs_input", "blocked")
            and (state is None or r["state"] == state)
        ]
        if rows:
            keys = [r["key"] for r in rows]
            self.pin(
                keys[(keys.index(self.selected_key) + 1) % len(keys)]
                if self.selected_key in keys
                else keys[0]
            )
