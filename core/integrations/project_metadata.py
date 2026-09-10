"""Safe, volatile project labels derived from an MCP client's advertised root."""

import asyncio
import time
from collections import OrderedDict
from urllib.parse import unquote, urlsplit

from overlay.state_api import project_display_name, valid_project_name


class ClientRootProjectResolver:
    """Ask an opted-in MCP client for one root without retaining its URI."""

    def __init__(self, *, ttl=20.0, capacity=256, timeout=0.4):
        self.ttl, self.capacity, self.timeout = ttl, capacity, timeout
        self._labels = OrderedDict()  # opaque reporter identity -> (expiry, label)

    @staticmethod
    def _label(root):
        """Return only a validated label; never resolve, stat, or retain the URI."""
        uri = str(getattr(root, 'uri', ''))
        parsed = urlsplit(uri)
        if parsed.scheme != 'file' or parsed.query or parsed.fragment or parsed.netloc not in ('', 'localhost'):
            return None
        name = getattr(root, 'name', None)
        if valid_project_name(name):
            return name.strip()
        path = unquote(parsed.path)
        # file roots only; reject controls and root-like paths through the shared
        # textual display validator. This deliberately does no filesystem access.
        return project_display_name(cwd=path)

    def _remember(self, opaque_identity, now, label):
        self._labels[opaque_identity] = (now + self.ttl, label)
        self._labels.move_to_end(opaque_identity)
        while len(self._labels) > self.capacity:
            self._labels.popitem(last=False)
        return label

    async def resolve(self, context, opaque_identity):
        now = time.monotonic()
        cached = self._labels.get(opaque_identity)
        if cached and cached[0] > now:
            self._labels.move_to_end(opaque_identity)
            return cached[1]
        try:
            session = context.session
            capabilities = getattr(getattr(session, 'client_params', None), 'capabilities', None)
            if capabilities is None or getattr(capabilities, 'roots', None) is None:
                return self._remember(opaque_identity, now, None)
            roots_result = await asyncio.wait_for(session.list_roots(), timeout=self.timeout)
            roots = getattr(roots_result, 'roots', None)
            if not isinstance(roots, list) or len(roots) != 1:
                return self._remember(opaque_identity, now, None)
            label = self._label(roots[0])
            if label is None:
                return self._remember(opaque_identity, now, None)
        except Exception:
            return self._remember(opaque_identity, now, None)
        return self._remember(opaque_identity, now, label)
