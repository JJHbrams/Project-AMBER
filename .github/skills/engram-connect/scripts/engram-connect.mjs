#!/usr/bin/env node
import { createHash, randomBytes } from "node:crypto";
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

// The daemon's toolsAndAuthOnly status can include several hundred tool schemas.
// Keep transport bounded while allowing a real loaded session; CLI output is still names only.
const MAX_CONFIG = 262144,
  MAX_OUTPUT = 4 * 1024 * 1024,
  MAX_HEADER = 16384,
  TIMEOUT = 5000,
  MAX_PAGES = 16,
  PAGE_SIZE = 10;
const ALLOWED_HOSTS = new Set(["127.0.0.1", "localhost"]);
const ALLOWED = { search: "kg_search", "read-note": "kg_read_note" };
const arg = (argv, name) => {
  const i = argv.indexOf(name);
  return i < 0 ? "" : argv[i + 1] || "";
};
const canonical = (v) => {
  const r = path.resolve(v).replaceAll("\\", "/");
  return process.platform === "win32" ? r.toLowerCase() : r;
};
const samePath = (a, b) => canonical(a) === canonical(b);

export function configuredEngramUrl(root) {
  if (!root) throw new Error("--root is required");
  const file = path.resolve(root, "config.toml"),
    stat = fs.lstatSync(file);
  if (!stat.isFile() || stat.isSymbolicLink() || stat.size > MAX_CONFIG)
    throw new Error("unsafe Codex config.toml");
  const text = fs.readFileSync(file, "utf8"),
    section = /^\s*\[mcp_servers\.engram\]\s*$/m.exec(text);
  if (!section) throw new Error("Engram MCP entry is missing");
  const tail = text
    .slice(section.index + section[0].length)
    .split(/^\s*\[/m, 1)[0];
  if (/^\s*(headers|bearer_token|authorization)\s*=/im.test(tail))
    throw new Error("authenticated MCP entries are not supported");
  const found = /^\s*url\s*=\s*["']([^"']+)["']\s*$/im.exec(tail);
  if (!found) throw new Error("Engram MCP URL is missing");
  const url = new URL(found[1]);
  if (
    url.protocol !== "http:" ||
    !ALLOWED_HOSTS.has(url.hostname) ||
    url.username ||
    url.password
  )
    throw new Error("only unauthenticated loopback http URLs are allowed");
  return url;
}

export function decodeResponse(body, expectedId = undefined) {
  if (!body.trim()) return null;
  if (
    !body.trimStart().startsWith("data:") &&
    !body.trimStart().startsWith("event:")
  )
    return JSON.parse(body);
  const messages = body
    .split(/\r?\n\r?\n/)
    .map((block) =>
      block
        .split(/\r?\n/)
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trimStart())
        .join("\n"),
    )
    .filter(Boolean)
    .map(JSON.parse);
  return expectedId === undefined
    ? messages.at(-1) || null
    : messages.find((item) => item?.id === expectedId) || null;
}
let requestId = 0;
async function httpRpc(client, method, params = {}, notification = false) {
  const abort = new AbortController(),
    timer = setTimeout(() => abort.abort(), TIMEOUT);
  try {
    const headers = {
      "content-type": "application/json",
      accept: "application/json, text/event-stream",
      "mcp-protocol-version": "2025-03-26",
    };
    if (client.session) headers["mcp-session-id"] = client.session;
    const payload = { jsonrpc: "2.0", method, params };
    if (!notification) payload.id = ++requestId;
    const response = await fetch(client.url, {
      method: "POST",
      redirect: "error",
      signal: abort.signal,
      headers,
      body: JSON.stringify(payload),
    });
    if (!response.ok) throw new Error("MCP response rejected");
    const reader = response.body?.getReader();
    let size = 0,
      chunks = [];
    while (reader) {
      const next = await reader.read();
      if (next.done) break;
      size += next.value.byteLength;
      if (size > MAX_OUTPUT) {
        await reader.cancel();
        throw new Error("MCP response rejected");
      }
      chunks.push(next.value);
    }
    const body = Buffer.concat(
      chunks.map((chunk) => Buffer.from(chunk)),
    ).toString("utf8");
    const issued = response.headers.get("mcp-session-id");
    if (issued) client.session = issued;
    if (notification) return null;
    const json = decodeResponse(body, payload.id);
    if (!json || json.error || !json.result)
      throw new Error("MCP method failed");
    return json.result;
  } finally {
    clearTimeout(timer);
  }
}
export async function run(root, command, value = "") {
  const client = { url: configuredEngramUrl(root), session: "" };
  await httpRpc(client, "initialize", {
    protocolVersion: "2025-03-26",
    capabilities: {},
    clientInfo: { name: "engram-connect", version: "1" },
  });
  await httpRpc(client, "notifications/initialized", {}, true);
  const tools = await httpRpc(client, "tools/list");
  if (command === "probe")
    return {
      ok: true,
      tools: Array.isArray(tools.tools)
        ? tools.tools.map((x) => x.name).slice(0, 32)
        : [],
    };
  const tool = ALLOWED[command];
  if (!tool || !value || value.length > 256)
    throw new Error("invalid read-only command");
  if (!tools.tools?.some((x) => x.name === tool))
    throw new Error("required Engram tool is unavailable");
  return httpRpc(client, "tools/call", {
    name: tool,
    arguments: command === "search" ? { query: value } : { identifier: value },
  });
}

function frame(opcode, data) {
  const payload = Buffer.isBuffer(data) ? data : Buffer.from(data);
  if (payload.length > MAX_OUTPUT) throw new Error("RPC_OUTPUT_TOO_LARGE");
  let head;
  if (payload.length < 126)
    head = Buffer.from([0x80 | opcode, 0x80 | payload.length]);
  else if (payload.length <= 65535) {
    head = Buffer.alloc(4);
    head[0] = 0x80 | opcode;
    head[1] = 254;
    head.writeUInt16BE(payload.length, 2);
  } else {
    head = Buffer.alloc(10);
    head[0] = 0x80 | opcode;
    head[1] = 255;
    head.writeBigUInt64BE(BigInt(payload.length), 2);
  }
  const mask = randomBytes(4),
    body = Buffer.alloc(payload.length);
  for (let i = 0; i < payload.length; i += 1)
    body[i] = payload[i] ^ mask[i % 4];
  return Buffer.concat([head, mask, body]);
}
class Parser {
  constructor(message, control) {
    this.message = message;
    this.control = control;
    this.buffer = Buffer.alloc(0);
    this.parts = [];
    this.opcode = null;
    this.size = 0;
  }
  push(chunk) {
    this.buffer = Buffer.concat([this.buffer, chunk]);
    if (this.buffer.length > MAX_OUTPUT + 32)
      throw new Error("RPC_OUTPUT_TOO_LARGE");
    for (;;) {
      if (this.buffer.length < 2) return;
      const a = this.buffer[0],
        b = this.buffer[1],
        fin = !!(a & 128),
        opcode = a & 15,
        masked = !!(b & 128);
      if ((a & 0x70) !== 0 || masked)
        throw new Error("WEBSOCKET_FRAME_INVALID");
      let len = b & 127,
        at = 2;
      if (len === 126) {
        if (this.buffer.length < 4) return;
        len = this.buffer.readUInt16BE(2);
        at = 4;
      } else if (len === 127) {
        if (this.buffer.length < 10) return;
        const n = this.buffer.readBigUInt64BE(2);
        if (n > BigInt(MAX_OUTPUT)) throw new Error("RPC_OUTPUT_TOO_LARGE");
        len = Number(n);
        at = 10;
      }
      if (len > MAX_OUTPUT || (opcode >= 8 && (!fin || len > 125)))
        throw new Error("WEBSOCKET_FRAME_INVALID");
      if (this.buffer.length < at + (masked ? 4 : 0) + len) return;
      const mask = masked ? this.buffer.subarray(at, at + 4) : null,
        raw = this.buffer.subarray(
          at + (masked ? 4 : 0),
          at + (masked ? 4 : 0) + len,
        );
      this.buffer = this.buffer.subarray(at + (masked ? 4 : 0) + len);
      const data = masked
        ? Buffer.from(raw.map((v, i) => v ^ mask[i % 4]))
        : raw;
      if (opcode >= 8) {
        this.control(opcode, data);
        continue;
      }
      if (opcode === 0) {
        if (this.opcode === null) throw new Error("WEBSOCKET_FRAGMENT_INVALID");
      } else if (opcode === 1 || opcode === 2) {
        if (this.opcode !== null) throw new Error("WEBSOCKET_FRAGMENT_INVALID");
        this.opcode = opcode;
      } else throw new Error("WEBSOCKET_OPCODE_INVALID");
      this.parts.push(data);
      this.size += data.length;
      if (this.size > MAX_OUTPUT) throw new Error("RPC_OUTPUT_TOO_LARGE");
      if (fin) {
        const done = Buffer.concat(this.parts, this.size),
          type = this.opcode;
        this.parts = [];
        this.size = 0;
        this.opcode = null;
        if (type !== 1) throw new Error("WEBSOCKET_BINARY_UNSUPPORTED");
        this.message(done.toString("utf8"));
      }
    }
  }
}
class DaemonRpc {
  constructor(executable, socket, spawnImpl = spawn) {
    this.executable = executable;
    this.socket = socket;
    this.spawnImpl = spawnImpl;
    this.id = 0;
    this.pending = new Map();
    this.closed = false;
    this.handshake = Buffer.alloc(0);
  }
  reject(code) {
    for (const item of this.pending.values())
      item.reject(Object.assign(new Error(code), { code }));
    this.pending.clear();
  }
  async start() {
    const key = randomBytes(16).toString("base64");
    this.accept = createHash("sha1")
      .update(`${key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11`)
      .digest("base64");
    this.child = this.spawnImpl(
      this.executable,
      ["app-server", "proxy", "--sock", this.socket],
      { stdio: ["pipe", "pipe", "ignore"], windowsHide: true },
    );
    this.child.on("error", () => this.reject("PROXY_SPAWN_FAILED"));
    this.child.on("exit", () => this.reject("PROXY_EXITED"));
    this.child.stdin.on("error", () => this.reject("PROXY_WRITE_FAILED"));
    this.parser = new Parser(
      (x) => this.receive(x),
      (op, data) => {
        if (op === 9)
          this.write(frame(10, data)).catch(() =>
            this.reject("PROXY_WRITE_FAILED"),
          );
        else if (op === 8) this.reject("WEBSOCKET_CLOSED");
      },
    );
    this.child.stdout.on("data", (x) => this.bytes(x));
    const ready = new Promise((resolve, reject) => {
      const timer = setTimeout(
        () => reject(new Error("WEBSOCKET_HANDSHAKE_TIMEOUT")),
        TIMEOUT,
      );
      this.ready = () => {
        clearTimeout(timer);
        resolve();
      };
      this.failed = (e) => {
        clearTimeout(timer);
        reject(e);
      };
    });
    await this.write(
      Buffer.from(
        `GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: ${key}\r\nSec-WebSocket-Version: 13\r\n\r\n`,
      ),
    );
    await ready;
  }
  bytes(chunk) {
    try {
      if (!this.upgraded) {
        this.handshake = Buffer.concat([this.handshake, chunk]);
        if (this.handshake.length > MAX_HEADER)
          throw new Error("WEBSOCKET_HANDSHAKE_TOO_LARGE");
        const p = this.handshake.indexOf("\r\n\r\n");
        if (p < 0) return;
        const lines = this.handshake
            .subarray(0, p)
            .toString("ascii")
            .split("\r\n"),
          headers = new Map(
            lines.slice(1).map((x) => {
              const i = x.indexOf(":");
              return [x.slice(0, i).toLowerCase(), x.slice(i + 1).trim()];
            }),
          );
        if (
          !/^HTTP\/1\.1 101(?: |$)/.test(lines[0] || "") ||
          !/websocket/i.test(headers.get("upgrade") || "") ||
          headers.get("sec-websocket-accept") !== this.accept
        )
          throw new Error("WEBSOCKET_UPGRADE_REJECTED");
        this.upgraded = true;
        this.ready();
        const rest = this.handshake.subarray(p + 4);
        if (rest.length) this.parser.push(rest);
      } else this.parser.push(chunk);
    } catch (e) {
      this.failed?.(e);
      this.reject(e.message);
      this.close();
    }
  }
  async write(data) {
    if (this.closed) throw new Error("PROXY_CLOSED");
    await new Promise((resolve, reject) =>
      this.child.stdin.write(data, (e) => (e ? reject(e) : resolve())),
    );
  }
  receive(text) {
    try {
      const msg = JSON.parse(text),
        item = this.pending.get(msg.id);
      if (!item) return;
      this.pending.delete(msg.id);
      msg.error
        ? item.reject(
            Object.assign(new Error("RPC_ERROR"), { code: "RPC_ERROR" }),
          )
        : item.resolve(msg.result);
    } catch {
      this.reject("RPC_MESSAGE_INVALID");
    }
  }
  call(method, params) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error("RPC_TIMEOUT"));
      }, TIMEOUT);
      this.pending.set(id, {
        resolve: (x) => {
          clearTimeout(timer);
          resolve(x);
        },
        reject: (e) => {
          clearTimeout(timer);
          reject(e);
        },
      });
      this.write(
        frame(1, JSON.stringify({ jsonrpc: "2.0", id, method, params })),
      ).catch((e) => {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(e);
      });
    });
  }
  notify(method, params) {
    return this.write(
      frame(1, JSON.stringify({ jsonrpc: "2.0", method, params })),
    );
  }
  async close() {
    if (this.closed) return;
    this.closed = true;
    try {
      this.child?.stdin.end();
    } catch {}
    this.child?.kill();
  }
}
const threads = (x) =>
  Array.isArray(x?.data) ? x.data : Array.isArray(x?.threads) ? x.threads : [];
const statuses = (x) =>
  Array.isArray(x?.data) ? x.data : Array.isArray(x?.servers) ? x.servers : [];
function tools(entries) {
  return entries
    .filter(
      (x) =>
        x?.name === "engram" || x?.server === "engram" || x?.id === "engram",
    )
    .flatMap((x) =>
      Array.isArray(x?.tools)
        ? x.tools
        : x?.tools && typeof x.tools === "object"
          ? Object.keys(x.tools)
          : x?.toolNames || [],
    )
    .map((x) => (typeof x === "string" ? x : x?.name))
    .filter(Boolean);
}
async function findPaged(rpc, method, params, values, match) {
  const seen = new Set();
  let cursor = undefined;
  for (let page = 0; page < MAX_PAGES; page += 1) {
    const result = await rpc.call(method, {
      ...params,
      limit: PAGE_SIZE,
      ...(cursor ? { cursor } : {}),
    });
    const found = values(result).find(match);
    if (found !== undefined) return found;
    const next = result?.nextCursor;
    if (!next) return undefined;
    if (typeof next !== "string" || next.length > 4096 || seen.has(next))
      throw new Error("PAGINATION_CURSOR_INVALID");
    seen.add(next);
    cursor = next;
  }
  throw new Error("PAGINATION_PAGE_LIMIT");
}
export async function reconnect({ root, threadId, executable, spawnImpl }) {
  if (!root) throw new Error("ROOT_REQUIRED");
  if (!executable) throw new Error("CODEX_EXECUTABLE_REQUIRED");
  const resolved = path.resolve(root),
    stat = fs.lstatSync(resolved);
  if (!stat.isDirectory() || stat.isSymbolicLink())
    throw new Error("UNSAFE_CODEX_ROOT");
  const home = process.env.CODEX_HOME
    ? path.resolve(process.env.CODEX_HOME)
    : "";
  const thread =
    threadId ||
    (home && samePath(resolved, home) ? process.env.CODEX_THREAD_ID : "");
  if (!thread || thread.length > 256) throw new Error("THREAD_ID_REQUIRED");
  const rpc = new DaemonRpc(
    executable,
    path.join(resolved, "app-server-control", "app-server-control.sock"),
    spawnImpl,
  );
  try {
    await rpc.start();
    const init = await rpc.call("initialize", {
      clientInfo: { name: "engram-connect", version: "1" },
      capabilities: { experimentalApi: true },
    });
    await rpc.notify("initialized", {});
    if (!init?.codexHome || !samePath(init.codexHome, resolved))
      throw new Error("ROOT_MISMATCH");
    const loaded = await findPaged(
      rpc,
      "thread/loaded/list",
      {},
      threads,
      (x) =>
        (typeof x === "string"
          ? x
          : (x?.id ?? x?.threadId ?? x?.thread?.id)) === thread,
    );
    if (!loaded) throw new Error("THREAD_NOT_LOADED");
    await rpc.call("config/mcpServer/reload", {});
    try {
      const entry = await findPaged(
        rpc,
        "mcpServerStatus/list",
        { detail: "toolsAndAuthOnly" },
        statuses,
        (value) =>
          value?.name === "engram" ||
          value?.server === "engram" ||
          value?.id === "engram",
      );
      const restored = entry ? tools([entry]) : [];
      return {
        requestAccepted: true,
        engramToolsRestored: restored.length > 0,
        threadId: thread,
        engramTools: restored.slice(0, 64),
        note: "Reload is app-server-wide and can refresh other loaded threads; this command only verifies the requested thread.",
      };
    } catch (error) {
      return {
        requestAccepted: true,
        engramToolsRestored: false,
        threadId: thread,
        verificationError: error.code || error.message,
      };
    }
  } finally {
    await rpc.close();
  }
}
function parse(argv) {
  const command = argv[0];
  if (!["probe", "search", "read-note", "reconnect"].includes(command))
    throw new Error("USAGE");
  return command === "reconnect"
    ? {
        command,
        root: arg(argv, "--root"),
        threadId: arg(argv, "--thread-id"),
        executable: arg(argv, "--codex-executable"),
      }
    : {
        command,
        root: arg(argv, "--root"),
        value: command === "search" ? arg(argv, "--query") : arg(argv, "--id"),
      };
}
const main =
  process.argv[1] &&
  path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (main)
  Promise.resolve()
    .then(async () => {
      const a = parse(process.argv.slice(2)),
        r =
          a.command === "reconnect"
            ? await reconnect(a)
            : await run(a.root, a.command, a.value);
      console.log(JSON.stringify(r).slice(0, MAX_OUTPUT));
      if (a.command === "reconnect" && !r.engramToolsRestored)
        process.exitCode = 1;
    })
    .catch((e) => {
      console.error(`engram-connect: ${e.message}`);
      process.exitCode = 1;
    });
