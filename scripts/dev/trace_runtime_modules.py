"""Trace which modules every frozen role actually loads (source mode).

Runs each role's import/exercise path in an isolated subprocess under a throw-away
HOME (like installer/smoke-profile.ps1) and unions sys.modules into a JSON file.
The union is the evidence behind Analysis(excludes=...) in engram-overlay.spec:
anything in the union must never be excluded.

Never starts servers or the overlay GUI and never touches the real DB.

Usage:
  C:/Users/jhjang/miniconda3/envs/intel_engram/python.exe scripts/dev/trace_runtime_modules.py [--out FILE]
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_EMBED = (
    "from core.graph.semantic import get_semantic_graph, run_sg_coro\n"
    "g = get_semantic_graph()\n"
    "v = run_sg_coro(g.compute_query_embedding('engram embedding check'))\n"
    "assert len(v) == 384, len(v)\n"
)

PROBES: dict[str, str] = {
    "runtime-contract": (
        "from core.install.runtime_contract import evaluate_runtime_contract\n"
        "evaluate_runtime_contract()\n"
    ),
    "smoke-check": (
        "from mcp.server.fastmcp import FastMCP\n"
        "import mcp_server, kg_watcher, overlay.main\n" + _EMBED
    ),
    "embedding-check": _EMBED,
    "dashboard": (
        "from streamlit.testing.v1 import AppTest\n"
        "r = AppTest.from_file(r'%s', default_timeout=60).run()\n"
        "print('dashboard exceptions:', [str(e.value) for e in r.exception])\n"
        % (ROOT / "core" / "dashboard" / "app.py")
    ),
    "mcp-server-import": "import mcp_server\n",
    "kg-watcher-import": "import kg_watcher\n",
    "overlay-main-import": "import overlay.main\n",
    "mcp-recovery-bridge": "import core.integrations.mcp_recovery_bridge\n",
    "mcp-bridge-script": "import runpy\nimport importlib.util as u\n"
                         "import core.install.mcp_bridge_config\n",
    "other-roles-import": (
        "import core.integrations.claude_root_launcher, core.install.bootstrap,"
        " core.install.model_manifest, core.integrations.policy_preflight,"
        " core.integrations.agent_policy_hook, core.integrations.git_policy_hook,"
        " core.install.user_config, core.install.claude_monitor_hooks,"
        " core.install.codex_monitor_hooks, core.install.service_config,"
        " core.entrypoint\n"
    ),
}

_WRAPPER = """
import json, sys, traceback
sys.path.insert(0, {root!r}); sys.path.insert(0, {kg!r})
err = None
try:
    exec(compile({code!r}, '<probe>', 'exec'), {{'__name__': '__probe__'}})
except BaseException:
    err = traceback.format_exc()
json.dump({{'modules': sorted(sys.modules), 'error': err}}, open({out!r}, 'w'))
"""


def _free_ports(n: int) -> list[int]:
    socks = [socket.socket() for _ in range(n)]
    for s in socks:
        s.bind(("127.0.0.1", 0))
    ports = [s.getsockname()[1] for s in socks]
    for s in socks:
        s.close()
    return ports


def _profile(tmp: Path) -> dict[str, str]:
    for rel in (".engram", "AppData/Roaming", "AppData/Local", "db", "codex", "claude"):
        (tmp / rel).mkdir(parents=True, exist_ok=True)
    p = _free_ports(3)
    settings = json.dumps({
        "db": {"root_dir": str(tmp / "db")},
        "overlay": {"stm_server_port": p[0]},
        "mcp": {"http_port": p[1]},
        "dashboard": {"enabled": False, "port": p[2]},
    })
    for name in ("user.config.yaml", "overlay.user.yaml"):
        (tmp / ".engram" / name).write_text(settings, encoding="utf-8")
    env = dict(os.environ)
    real_models = Path.home() / ".engram" / "models"  # read-only embedding payload
    env.update({
        "HOME": str(tmp), "USERPROFILE": str(tmp),
        "APPDATA": str(tmp / "AppData/Roaming"), "LOCALAPPDATA": str(tmp / "AppData/Local"),
        "CODEX_HOME": str(tmp / "codex"), "CLAUDE_CONFIG_DIR": str(tmp / "claude"),
        "ENGRAM_SMOKE_DB_DIR": str(tmp / "db"), "ENGRAM_BUILD_SMOKE": "1",
        "ENGRAM_MODEL_CACHE_DIR": str(real_models),
        "PYTHONIOENCODING": "utf-8",
    })
    env.pop("ENGRAM_RUNTIME_ROLE", None)
    env.pop("ENGRAM_STM_PORT", None)
    return env


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "build" / "runtime-modules.json"))
    args = ap.parse_args()
    union: set[str] = set()
    per_role: dict[str, dict] = {}
    with tempfile.TemporaryDirectory(prefix="engram-trace-") as td:
        tmp = Path(td)
        env = _profile(tmp)
        for role, code in PROBES.items():
            out = tmp / f"{role}.json"
            script = _WRAPPER.format(root=str(ROOT), kg=str(ROOT / "scripts" / "kg"), code=code, out=str(out))
            proc = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env,
                                  capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
            if not out.is_file():
                per_role[role] = {"count": 0, "error": f"child died rc={proc.returncode}: {proc.stderr[-500:]}"}
                continue
            data = json.loads(out.read_text(encoding="utf-8"))
            union.update(data["modules"])
            per_role[role] = {"count": len(data["modules"]), "error": data["error"]}
            print(f"{role}: {len(data['modules'])} modules" + (" [ERROR]" if data["error"] else ""))
    dest = Path(args.out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps({"roles": per_role, "modules": sorted(union)}, indent=1), encoding="utf-8")
    print(f"union: {len(union)} modules -> {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
