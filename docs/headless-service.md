# Headless Engram service

`engram_service.py` runs the MCP HTTP server without the Windows overlay.  It
selects its own data directory and home before `mcp_server` is imported, so it
does not read the installed desktop user's database override or attach to an
overlay STM broker.

Run a minimal service runtime with Python 3.11+:

```sh
python -m pip install -r requirements-service.txt
python engram_service.py \
  --data-dir /srv/engram/data \
  --host 127.0.0.1 --port 17385 \
  --remote-host 0.0.0.0 --remote-port 17386 \
  --token-file /run/secrets/engram-tokens.yaml \
  --remote-allowed-host engram.example:443
```

The minimal dependency file is enough for SQLite-backed core MCP tools.  Use
`requirements-service-semantic.txt` for semantic graph/search tools; the
provided `Dockerfile.service` installs that full service set.  Persist `/data`
and, when semantic retrieval is enabled, mount a model cache appropriate to
the deployment.  The image does not include `pywin32`, overlay, tray, or
keyboard dependencies.

The local listener remains loopback-only and unauthenticated.  Direct clients
connect to the authenticated listener through a TLS reverse proxy:

```text
https://engram.example/mcp  ->  http://service:17386/mcp
Authorization: Bearer <principal token>
```

Route only port 17386 to the proxy.  Do not publish or proxy the local port.
`--remote-allowed-host` is required for each external `Host` header because
FastMCP's DNS-rebinding protection remains enabled; include the proxy's
host-and-port form (for example `engram.example:443`).  The token YAML is a
mounted secret and is never printed.  A missing explicit token file stops
startup; the service does not generate a replacement credential.

The token file uses the existing principal format:

```yaml
tokens:
  - name: direct-client
    token: "replace-with-secret"
    scope: "overlay"
```

Every remote request requires bearer authentication.  The principal's default
deny list is enforced before a tool call, and its `scope` replaces caller-supplied
scope values for scope-aware tools.  Supplying `deny` replaces that default list,
so copy every required restriction when customizing it.  `/health` is intentionally unauthenticated
for liveness checks; other non-MCP HTTP routes are unavailable on this port.

The previous SSH reverse-tunnel setup remains compatible: tunnel its remote
loopback endpoint to the authenticated listener.  Direct ingress is the normal
service deployment path.  This repository does not open a firewall, acquire a
public address, issue TLS certificates, or claim a successful deployment on a
different machine.

For the desktop overlay, configure `mcp.remote_enabled`, `mcp.remote_port`,
and optionally `mcp.remote_host` plus `mcp.remote_token_file` in
`~/.engram/overlay.user.yaml`; the default host stays `127.0.0.1`.
