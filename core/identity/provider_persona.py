"""provider 설정 파일에 남은 persona 블록을 걷어낸다.

engram 이 `~/.claude/CLAUDE.md` 와 `~/.codex/AGENTS.md` 에 정체성 축약본을 써 두던
경로는 폐기했다. 이유는 두 가지다.

1. 중복이었다. 페르소나 전체(예시·narrative 포함)는 `engram_get_context` 응답으로
   이미 들어간다. 파일 블록은 2026-09-15 에 덧붙은 더 빈약한 사본이었다.
2. 위험했다. 빈 DB 를 읽으면 "이름 없음 + 기본 시드" 블록이 만들어지고, 그게 사람이
   소유한 파일의 정체성을 그대로 덮었다(2026-09-18 실제 2회). DB 가 딴 드라이브에
   있으면 테스트가 아니라 드라이브 사정만으로도 같은 일이 난다.

무엇보다 **engram 이 안 붙은 세션에는 페르소나가 없는 게 맞다.** 파일에 남은 사본은
연결이 끊긴 동안에도 정체성이 살아있는 척하게 만든다.

그래서 이 모듈에는 쓰는 경로가 없다. 이미 배포된 설치본에서 블록을 지우는 일만 한다.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import tempfile
import uuid

_PROVIDERS = {
    "codex": (".codex", "AGENTS.md", "<!-- ENGRAM_CODEX_PERSONA_V1_BEGIN -->", "<!-- ENGRAM_CODEX_PERSONA_V1_END -->"),
    "claude": (".claude", "CLAUDE.md", "<!-- ENGRAM_CLAUDE_PERSONA_V1_BEGIN -->", "<!-- ENGRAM_CLAUDE_PERSONA_V1_END -->"),
}
BEGIN, END = _PROVIDERS["codex"][2:]
_MAX_FILE = 512_000


def _read(path):
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)(): raise ValueError(f"linked {path.name} unsupported")
    if not path.exists(): return b""
    body = path.read_bytes()
    if len(body) > _MAX_FILE: raise ValueError(f"{path.name} too large")
    body.decode("utf-8")
    return body


def remove(*, provider="codex", home=None, apply=True):
    """provider 설정 파일에서 persona 블록을 제거한다.

    블록이 없으면 파일을 만들지도, 손대지도 않는다. 사람이 소유한 파일이므로
    바꾸기 전에 백업을 남기고 원자적으로 교체한다.
    """
    if provider not in _PROVIDERS: raise ValueError("unsupported provider")
    directory, filename, begin, end = _PROVIDERS[provider]
    root = (Path.home() if home is None else Path(home)) / directory
    if root.is_symlink() or getattr(root, "is_junction", lambda: False)():
        raise ValueError(f"linked {provider} root unsupported")
    path = root / filename; before = _read(path)
    text = before.decode("utf-8")
    starts, ends = [m.start() for m in re.finditer(re.escape(begin), text)], [m.start() for m in re.finditer(re.escape(end), text)]
    if len(starts) > 1 or len(ends) > 1 or bool(starts) != bool(ends) or (starts and starts[0] > ends[0]): raise ValueError("invalid persona markers")
    if starts:
        block_end = ends[0] + len(end)
        if text[block_end:block_end + 2] == "\r\n": block_end += 2
        elif text[block_end:block_end + 1] == "\n": block_end += 1
        after_text = text[:starts[0]] + text[block_end:]
    else:
        after_text = text  # 지울 블록이 없다 — 파일을 새로 만들지 않는다.
    after = after_text.encode("utf-8"); changed = after != before
    status = {"state": "removed" if changed else "absent", "changed": changed, "applied": False,
              "byte_count": len(after), "fingerprint": hashlib.sha256(after).hexdigest()[:16]}
    if apply and changed:
        backup = path.with_name(path.name + ".engram-persona-backup-" + uuid.uuid4().hex)
        if _read(path) != before:
            raise ValueError(f"{filename} changed during removal")
        with backup.open("xb") as stream: stream.write(before); stream.flush(); os.fsync(stream.fileno())
        fd, temporary = tempfile.mkstemp(prefix=".engram-persona-", dir=root)
        try:
            with os.fdopen(fd, "wb") as stream: stream.write(after); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary): os.unlink(temporary)
        status["applied"] = True
    return status


def remove_all(*, home=None, apply=True):
    """provider 별로 독립 처리한다 — 한쪽 설정 파일이 깨져 있어도 다른 쪽은 정리된다."""
    results = {}
    for provider in _PROVIDERS:
        try:
            results[provider] = remove(provider=provider, home=home, apply=apply)
        except Exception as exc:
            results[provider] = {"state": "error", "changed": False, "applied": False,
                                 "error": f"{type(exc).__name__}: {exc}"}
    return results
