"""Portable, overlay-free entrypoint for the Engram HTTP service."""
from __future__ import annotations

import argparse
import ipaddress
import os
from pathlib import Path


def _loopback(value: str) -> bool:
    host = str(value or "").strip().lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _port(parser: argparse.ArgumentParser, value: int, option: str, allow_zero: bool = False) -> None:
    if allow_zero and value == 0:
        return
    if not 1 <= value <= 65535:
        parser.error(f"{option} must be {'0 or ' if allow_zero else ''}between 1 and 65535")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Engram headless HTTP service")
    parser.add_argument("--data-dir", required=True, help="isolated SQLite/data directory")
    parser.add_argument("--home-dir", help="isolated home (default: <data-dir>/home)")
    parser.add_argument("--host", default="127.0.0.1", help="unauthenticated local listener host")
    parser.add_argument("--port", type=int, default=17385, help="unauthenticated local listener port")
    parser.add_argument("--remote-host", default="127.0.0.1", help="authenticated direct-ingress listener host")
    parser.add_argument("--remote-port", type=int, default=0, help="authenticated listener port; 0 disables it")
    parser.add_argument("--token-file", default="", help="read-only YAML secret with bearer principals")
    parser.add_argument("--remote-allowed-host", action="append", default=[], help="allowed direct client/proxy Host header")
    args = parser.parse_args(argv)

    if not _loopback(args.host):
        parser.error("--host must be loopback; use --remote-host with --remote-port for direct ingress")
    _port(parser, args.port, "--port")
    _port(parser, args.remote_port, "--remote-port", allow_zero=True)
    if args.remote_port and args.remote_port == args.port:
        parser.error("--remote-port must differ from --port")
    if args.token_file and not Path(args.token_file).expanduser().is_file():
        parser.error("--token-file must name an existing file")

    data_dir = Path(args.data_dir).expanduser().resolve()
    home_dir = Path(args.home_dir).expanduser().resolve() if args.home_dir else data_dir / "home"
    # mcp_server initializes SQLite at import.  Select the service-owned paths
    # first so an installed user's config or overlay STM can never be adopted.
    os.environ["ENGRAM_SERVICE_DATA_DIR"] = str(data_dir)
    os.environ["ENGRAM_RUNTIME_MODE"] = "headless-service"
    os.environ["HOME"] = str(home_dir)
    os.environ["USERPROFILE"] = str(home_dir)
    if args.token_file:
        os.environ["ENGRAM_REMOTE_TOKENS_FILE"] = str(Path(args.token_file).expanduser().resolve())

    import mcp_server

    server_args = [
        "--transport", "streamable-http", "--host", args.host, "--port", str(args.port),
        "--remote-host", args.remote_host, "--remote-port", str(args.remote_port),
    ]
    for allowed_host in args.remote_allowed_host:
        server_args.extend(["--remote-allowed-host", allowed_host])
    if args.token_file:
        server_args.extend(["--remote-token-file", os.environ["ENGRAM_REMOTE_TOKENS_FILE"]])
    mcp_server.main(server_args)


if __name__ == "__main__":
    main()
