"""[AC6] async 함수 본문의 블로킹 호출 래칫.

async def 본문에서 이벤트 루프를 막을 수 있는 호출을 직접 하는 곳을 정적으로 센다.
기준선(test/data/async_blocking_baseline.json)은 "지금 알려진 빚"이며 늘어나면 실패한다.
빚을 갚으면(기준선에 있는데 사라지거나 줄면) 기준선을 갱신해야 하며, 그래야 다시 늘지 않는다.

기준선 재생성(최초 생성과 동일한 방법; 기존 항목의 classification 은 보존):
    python test/test_async_blocking_ratchet.py --regen
    또는 ENGRAM_REGEN_ASYNC_BASELINE=1 python -m pytest test/test_async_blocking_ratchet.py
재생성 후 아래 MAX_BASELINE_ENTRIES 를 새 항목 수 이하로 낮출 것(늘리는 것은 리뷰 대상).

규칙 요약
- AsyncFunctionDef 본문의 직접 Call 만 본다. 중첩 def/lambda 내부는 제외하되, 그 중첩 def 를
  같은 async 본문에서 직접 호출하면 포함한다. 중첩 async def 는 별도 async 함수로 따로 검사한다.
- asyncio.to_thread(f, ...) / run_helper(..., f) / run_in_executor(..., f) 처럼 함수 "참조"를
  넘기는 것은 Call 이 아니므로 위반이 아니다.
- 같은 모듈의 동기 헬퍼가 블로킹 호출을 하면 그 헬퍼도 블로킹으로 전파(고정점 반복).
- 키 = (파일 상대경로, async 함수 qualname, 호출 대상 정규화 이름), 값 = 개수. 줄 번호는 쓰지 않는다.
"""
from __future__ import annotations

import ast
import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = ROOT / "test" / "data" / "async_blocking_baseline.json"
TARGET_FILES = [
    "mcp_server.py",
    "core/integrations/mcp_presence.py",
    "core/integrations/project_metadata.py",
    "core/integrations/mcp_tool_runtime.py",
]
# 기준선 항목 수의 상한(래칫). 빚을 갚을 때마다 낮춘다. 올리지 않는다.
MAX_BASELINE_ENTRIES = 30

_LAST_NAME_BLOCKING = {"get_semantic_graph", "get_kg", "_get_kg", "get_connection", "initialize_db"}
_EXACT_BLOCKING = {
    "sqlite3.connect", "time.sleep", "os.system",
    "subprocess.run", "subprocess.call", "subprocess.check_output",
    "subprocess.check_call", "subprocess.Popen",
}
_PREFIX_BLOCKING = ("urllib.request.", "requests.", "http.client.")
_PATH_IO_ATTRS = {"read_text", "write_text", "read_bytes", "write_bytes"}
_PRESENCE_BLOCKING = {
    "report_state", "report_title", "report_project", "report_claude_event",
    "report_codex_event", "flush", "_send",
}
_FUNC_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


# -- 스캐너 -------------------------------------------------------------------
def _dotted(node) -> str | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _collect_aliases(tree) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                aliases[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
        elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
            for a in n.names:
                aliases[a.asname or a.name] = f"{n.module}.{a.name}"
    return aliases


def _resolve(dotted: str, aliases: dict[str, str]) -> str:
    head, _, rest = dotted.partition(".")
    if head in aliases:
        head = aliases[head]
    return f"{head}.{rest}" if rest else head


def _lock_like(name: str | None) -> bool:
    return bool(name) and (name.endswith("lock") or name.endswith("LOCK"))


def _classify_call(call: ast.Call, aliases: dict[str, str]) -> str | None:
    """Call 이 블로킹 목록이면 정규화 이름, 아니면 None."""
    func = call.func
    raw = _dotted(func)
    if isinstance(func, ast.Attribute):
        if func.attr in _PATH_IO_ATTRS:
            return "." + func.attr
        if func.attr == "acquire":
            recv = _dotted(func.value)
            if _lock_like(recv):
                return f"{recv}.acquire"
    if raw is None:
        return None
    resolved = _resolve(raw, aliases)
    last = resolved.rsplit(".", 1)[-1]
    if last in _LAST_NAME_BLOCKING:
        return last
    if resolved in _EXACT_BLOCKING:
        return resolved
    if last == "urlopen" or resolved.startswith(_PREFIX_BLOCKING):
        return resolved
    if raw == "open" and "open" not in aliases:
        return "open"
    if raw.startswith("_mcp_presence.") and raw.count(".") == 1 and last in _PRESENCE_BLOCKING:
        return raw
    return None


def _body_nodes(fn):
    """fn 본문의 노드를 순회하되 중첩 def/lambda 는 들어가지 않는다. (노드, 중첩 def 맵) 반환."""
    nested: dict[str, ast.AST] = {}
    out = []
    stack = list(reversed(fn.body))
    while stack:
        n = stack.pop()
        if isinstance(n, _SCOPES):
            if isinstance(n, ast.FunctionDef):
                nested[n.name] = n
            continue
        out.append(n)
        stack.extend(reversed(list(ast.iter_child_nodes(n))))
    return out, nested


def _direct_events(fn, aliases, helper_lookup, _seen=None):
    """fn 의 직접 블로킹 이벤트 이름 리스트(중복 포함). helper_lookup(call)->이름|None."""
    _seen = _seen or set()
    nodes, nested = _body_nodes(fn)
    events: list[str] = []
    for n in nodes:
        if isinstance(n, ast.With):  # async with 는 AsyncWith 라 제외
            for item in n.items:
                name = _dotted(item.context_expr)
                if _lock_like(name):
                    events.append(f"with:{name}")
        if not isinstance(n, ast.Call):
            continue
        name = _classify_call(n, aliases)
        if name:
            events.append(name)
            continue
        if isinstance(n.func, ast.Name) and n.func.id in nested and n.func.id not in _seen:
            events.extend(_direct_events(nested[n.func.id], aliases, helper_lookup, _seen | {n.func.id}))
            continue
        h = helper_lookup(n)
        if h:
            events.append(h)
    return events


def _collect_functions(tree):
    """(qualname, node, 소속 클래스명|None) 목록. 함수 안의 중첩 def 는 qualname 에 점으로 잇는다."""
    out = []

    def visit(body, prefix, cls):
        for n in body:
            if isinstance(n, ast.ClassDef):
                visit(n.body, f"{prefix}{n.name}.", n.name)
            elif isinstance(n, _FUNC_DEFS):
                out.append((f"{prefix}{n.name}", n, cls))
                visit(n.body, f"{prefix}{n.name}.", None)
            else:
                for attr in ("body", "orelse", "finalbody"):
                    sub = getattr(n, attr, None)
                    if isinstance(sub, list):
                        visit(sub, prefix, cls)
                for h in getattr(n, "handlers", None) or []:
                    visit(h.body, prefix, cls)

    visit(tree.body, "", None)
    return out


def scan_source(source: str, relpath: str = "<mem>") -> Counter:
    tree = ast.parse(source)
    aliases = _collect_aliases(tree)
    funcs = _collect_functions(tree)
    sync = {q: (n, c) for q, n, c in funcs if isinstance(n, ast.FunctionDef)}
    sync_module = {q for q in sync if "." not in q}
    blocking: set[str] = set()

    def make_lookup(cls):
        def lookup(call):
            f = call.func
            q = None
            if isinstance(f, ast.Name) and f.id in sync_module:
                q = f.id
            elif (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                  and f.value.id in ("self", "cls") and cls):
                q = f"{cls}.{f.attr}"
            if q and q in blocking:
                return f"helper:{q}"
            return None
        return lookup

    changed = True
    while changed:  # 고정점 반복
        changed = False
        for q, (node, cls) in sync.items():
            if q in blocking:
                continue
            if _direct_events(node, aliases, make_lookup(cls)):
                blocking.add(q)
                changed = True

    result: Counter = Counter()
    for q, node, cls in funcs:
        if isinstance(node, ast.AsyncFunctionDef):
            for ev in _direct_events(node, aliases, make_lookup(cls)):
                result[(relpath, q, ev)] += 1
    return result


def scan_repo() -> Counter:
    total: Counter = Counter()
    for rel in TARGET_FILES:
        path = ROOT / rel
        if path.exists():
            total.update(scan_source(path.read_text(encoding="utf-8"), rel))
    return total


# -- 기준선 입출력 -------------------------------------------------------------
def _default_classification(target: str) -> str:
    if target in ("get_semantic_graph", "get_kg", "_get_kg") or target.startswith("helper:") and "semantic" in target:
        return "2단계(의미 처리 프로세스 분리)에서 제거 예정"
    if target.startswith("_mcp_presence."):
        return "presence 레인/아웃박스로 이전 예정"
    return "미분류: 블로킹 호출을 to_thread/전용 레인으로 이전 예정"


def load_baseline() -> tuple[Counter, dict]:
    data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    counts: Counter = Counter()
    notes = {}
    for e in data["entries"]:
        key = (e["file"], e["function"], e["target"])
        counts[key] = e["count"]
        notes[key] = e.get("classification", "")
    return counts, notes


def write_baseline(current: Counter) -> None:
    notes = {}
    if BASELINE_PATH.exists():
        try:
            notes = load_baseline()[1]
        except Exception:
            notes = {}
    entries = [
        {"file": f, "function": fn, "target": t, "count": c,
         "classification": notes.get((f, fn, t)) or _default_classification(t)}
        for (f, fn, t), c in sorted(current.items())
    ]
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_PATH.write_text(
        json.dumps({"_comment": "test_async_blocking_ratchet.py --regen 으로 생성. 손으로 늘리지 말 것.",
                    "entries": entries}, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )


# -- 테스트: 래칫 --------------------------------------------------------------
def _maybe_regen():
    if os.environ.get("ENGRAM_REGEN_ASYNC_BASELINE") == "1":
        write_baseline(scan_repo())


def test_async_blocking_ratchet():
    _maybe_regen()
    current = scan_repo()
    baseline, _ = load_baseline()
    regen = "python test/test_async_blocking_ratchet.py --regen"

    problems = []
    for (f, fn, t), c in current.items():
        b = baseline.get((f, fn, t))
        if b is None:
            problems.append(
                f"[새 블로킹 호출] {f} 의 async 함수 {fn} 이(가) {t} 을(를) 직접 호출함({c}곳). "
                f"async 본문에서 직접 부르지 말고 asyncio.to_thread/run_helper 로 함수 참조를 넘기거나 전용 레인으로 옮기세요."
            )
        elif c > b:
            problems.append(
                f"[호출 증가] {f} 의 {fn} 의 {t}: 기준선 {b} -> 현재 {c}. 늘어난 호출을 오프로드하세요. "
                f"기준선을 올려서 통과시키지 마세요."
            )
    assert not problems, "\n".join(problems)

    stale = [(k, b, current.get(k, 0)) for k, b in baseline.items() if current.get(k, 0) < b]
    assert not stale, (
        "빚이 해소되었는데 기준선이 그대로입니다(래칫 고정 필요). "
        f"`{regen}` 로 기준선을 재생성하고 MAX_BASELINE_ENTRIES 를 낮추세요:\n"
        + "\n".join(f"  {f} {fn} {t}: 기준선 {b} -> 현재 {c}" for (f, fn, t), b, c in stale)
    )


def test_baseline_size_never_grows():
    baseline, _ = load_baseline()
    assert len(baseline) <= MAX_BASELINE_ENTRIES, (
        f"기준선 항목 수 {len(baseline)} 가 상한 {MAX_BASELINE_ENTRIES} 을(를) 넘었습니다. "
        "새 블로킹 호출을 기준선에 추가하지 말고 코드에서 없애세요."
    )


def test_get_semantic_graph_direct_calls_are_detected():
    current = scan_repo()
    n = sum(c for (f, fn, t), c in current.items() if t == "get_semantic_graph")
    assert n > 0, "get_semantic_graph 직접 호출이 하나도 탐지되지 않았습니다(스캐너 점검 필요)."


# -- 테스트: 스캐너 자체 --------------------------------------------------------
def _scan(src):
    return scan_source(src, "m.py")


def test_scanner_catches_direct_calls():
    src = (
        "import time, sqlite3\n"
        "async def f():\n"
        "    time.sleep(1)\n"
        "    sqlite3.connect('x')\n"
        "    get_semantic_graph()\n"
        "    open('p')\n"
        "    p.read_text()\n"
        "    _mcp_presence.report_state(1)\n"
    )
    got = _scan(src)
    for t in ("time.sleep", "sqlite3.connect", "get_semantic_graph", "open", ".read_text", "_mcp_presence.report_state"):
        assert got[("m.py", "f", t)] == 1, t


def test_scanner_allows_function_references_and_allowed_presence_calls():
    src = (
        "import asyncio, time\n"
        "async def f(loop):\n"
        "    await asyncio.to_thread(time.sleep, 1)\n"
        "    await run_helper('x', get_semantic_graph, 1)\n"
        "    await loop.run_in_executor(None, get_kg)\n"
        "    _mcp_presence.submit(1)\n"
        "    _mcp_presence.report_context(1)\n"
        "    _mcp_presence.context_identity(1)\n"
        "    async with a_lock:\n"
        "        pass\n"
    )
    assert not _scan(src)


def test_scanner_flags_call_result_passed_to_to_thread():
    src = "import asyncio\nasync def f():\n    await asyncio.to_thread(get_kg())\n"
    assert _scan(src)[("m.py", "f", "get_kg")] == 1


def test_scanner_excludes_nested_def_unless_called_directly():
    src = (
        "import time\n"
        "async def f():\n"
        "    def unused():\n"
        "        time.sleep(1)\n"
        "    g = lambda: time.sleep(1)\n"
        "    def used():\n"
        "        time.sleep(1)\n"
        "    used()\n"
    )
    assert _scan(src) == Counter({("m.py", "f", "time.sleep"): 1})


def test_scanner_resolves_import_aliases():
    src = (
        "import sqlite3 as s\n"
        "from time import sleep as nap\n"
        "from urllib.request import urlopen as uo\n"
        "import subprocess as sp\n"
        "async def f():\n"
        "    s.connect('x')\n"
        "    nap(1)\n"
        "    uo('u')\n"
        "    sp.run(['a'])\n"
    )
    got = _scan(src)
    assert got[("m.py", "f", "sqlite3.connect")] == 1
    assert got[("m.py", "f", "time.sleep")] == 1
    assert got[("m.py", "f", "urllib.request.urlopen")] == 1
    assert got[("m.py", "f", "subprocess.run")] == 1


def test_scanner_propagates_blocking_through_sync_helpers():
    src = (
        "import time\n"
        "def inner():\n"
        "    time.sleep(1)\n"
        "def outer():\n"
        "    inner()\n"
        "def pure():\n"
        "    return 1\n"
        "class C:\n"
        "    def m(self):\n"
        "        outer()\n"
        "    async def a(self):\n"
        "        self.m()\n"
        "        pure()\n"
        "async def f():\n"
        "    outer()\n"
        "    pure()\n"
    )
    got = _scan(src)
    assert got[("m.py", "f", "helper:outer")] == 1
    assert got[("m.py", "C.a", "helper:C.m")] == 1
    assert not any(t == "helper:pure" for (_, _, t) in got)


def test_scanner_locks():
    src = (
        "async def f(self):\n"
        "    with self._lock:\n"
        "        pass\n"
        "    self.state_lock.acquire()\n"
        "    with open_ctx():\n"
        "        pass\n"
    )
    got = _scan(src)
    assert got[("m.py", "f", "with:self._lock")] == 1
    assert got[("m.py", "f", "self.state_lock.acquire")] == 1


if __name__ == "__main__":
    if "--regen" in sys.argv:
        write_baseline(scan_repo())
        base, _ = load_baseline()
        print(f"기준선 재생성: {len(base)} 항목, 총 {sum(base.values())} 호출 -> {BASELINE_PATH}")
    else:
        print("사용법: python test/test_async_blocking_ratchet.py --regen")
