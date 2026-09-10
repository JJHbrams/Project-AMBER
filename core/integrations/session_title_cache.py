"""Private, bounded public-title cache. Native identifiers never reach disk."""
import hashlib
import hmac
import os
from contextlib import closing
from pathlib import Path
import re
import secrets
import sqlite3
import time


def _protect(value, *, decode=False):
    if os.name != 'nt':
        return value
    import ctypes
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
    source, target = Blob(len(value), buffer), Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    function = crypt.CryptUnprotectData if decode else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise OSError('private cache secret unavailable')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


def valid_identity(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', value) is not None


def valid_title(title):
    from overlay.state_api import validate_title_payload
    checked, error = validate_title_payload({'provider': 'mcp', 'session_id': 'cache', 'title': title})
    return checked['title'] if not error else None


class SessionTitleCache:
    def __init__(self, directory, *, capacity=512, ttl=30 * 86400, clock=time.time):
        self.path = Path(directory) / 'session-title-cache.sqlite3'
        self.capacity, self.ttl, self.clock = capacity, ttl, clock

    def access(self, provider, principal, native_id, title=None):
        if provider not in ('claude', 'codex') or not valid_identity(native_id):
            return None
        if title is not None:
            title = valid_title(title)
            if title is None:
                return None
        try:
            if any(p.is_symlink() or getattr(p, 'is_junction', lambda: False)()
                   for p in (self.path, *self.path.parents)):
                return None
            if self.path.exists() and self.path.stat().st_size > 4_000_000:
                return None
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path, timeout=.25)) as db, db:
                db.execute('BEGIN IMMEDIATE')
                version = db.execute('PRAGMA user_version').fetchone()[0]
                if version not in (0, 1):
                    return None
                if version == 0 and db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                    return None
                db.execute('CREATE TABLE IF NOT EXISTS installation (id INTEGER PRIMARY KEY CHECK(id=1), secret BLOB NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS titles (key TEXT PRIMARY KEY, title TEXT NOT NULL, updated REAL NOT NULL)')
                db.execute('PRAGMA user_version=1')
                if not db.execute('SELECT 1 FROM installation WHERE id=1').fetchone():
                    db.execute('INSERT INTO installation VALUES (1, ?)', (_protect(secrets.token_bytes(32)),))
                secret = _protect(db.execute('SELECT secret FROM installation WHERE id=1').fetchone()[0], decode=True)
                if not isinstance(secret, bytes) or len(secret) != 32:
                    return None
                key = hmac.new(secret, '\0'.join((provider, principal, native_id)).encode(), hashlib.sha256).hexdigest()
                now = self.clock()
                db.execute('DELETE FROM titles WHERE updated < ?', (now - self.ttl,))
                if title is not None:
                    db.execute('INSERT OR REPLACE INTO titles VALUES (?, ?, ?)', (key, title, now))
                db.execute('DELETE FROM titles WHERE key NOT IN (SELECT key FROM titles ORDER BY updated DESC, key LIMIT ?)', (self.capacity,))
                row = db.execute('SELECT title FROM titles WHERE key=?', (key,)).fetchone()
                return valid_title(row[0]) if row else None
        except (OSError, sqlite3.Error, ValueError, TypeError):
            return None  # Never remove/recreate corrupt or inaccessible data.
