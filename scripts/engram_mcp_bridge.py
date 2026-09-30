"""Console entry point for provider MCP stdio registration."""
from __future__ import annotations
import argparse, os, sys, asyncio
from pathlib import Path
# Source registration uses an absolute launcher from an arbitrary provider cwd.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path: sys.path.insert(0, str(_ROOT))
from core.integrations.mcp_recovery_bridge import BridgeConfig, run_sdk_stdio_bridge

def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--install":
        from core.install.mcp_bridge_config import main as install_main
        return install_main(sys.argv[2:])
    p = argparse.ArgumentParser()
    p.add_argument("--upstream-url", required=True); p.add_argument("--state-url", required=True)
    p.add_argument("--token-env", default="ENGRAM_STM_TOKEN"); p.add_argument("--root-id"); p.add_argument("--discovery-file")
    ns = p.parse_args(); token = os.environ.get(ns.token_env, "")
    if not token and not ns.discovery_file: return 2
    return asyncio.run(run_sdk_stdio_bridge(BridgeConfig(ns.upstream_url, ns.state_url, token, ns.root_id), discovery_file=ns.discovery_file))
if __name__ == "__main__": raise SystemExit(main())
