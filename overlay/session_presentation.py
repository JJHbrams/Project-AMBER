"""Local completion presentation; never changes producer metadata or selection."""

import time

COMPLETION_DWELL_SECONDS = 2.0


class SessionPresentation:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self._seen = {}
        self._deadlines = {}
        self._selected = None

    def update(self, rows, selected_key):
        now = self.clock()
        keys = {row['key'] for row in rows}
        if selected_key != self._selected:
            self._deadlines.pop(self._selected, None)
        self._selected = selected_key
        for row in rows:
            key = row['key']
            token = (row['state'], row.get('state_since'))
            previous = self._seen.get(key)
            if (token[0] == 'ready' and previous is not None and token != previous
                    and key == selected_key):
                self._deadlines[key] = now + COMPLETION_DWELL_SECONDS
            elif token[0] != 'ready':
                self._deadlines.pop(key, None)
            self._seen[key] = token
        self._seen = {key: token for key, token in self._seen.items() if key in keys}
        self._deadlines = {key: deadline for key, deadline in self._deadlines.items()
                           if key in keys and deadline > now}

    def state(self, row):
        if row is None:
            return 'idle'
        if row['state'] != 'ready':
            return row['state']
        return ('ready' if row['key'] == self._selected
                and self._deadlines.get(row['key'], 0) > self.clock() else 'idle')
