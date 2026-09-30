import assert from "node:assert/strict";
import fs from "node:fs";
import http from "node:http";
import { EventEmitter } from "node:events";
import { createHash } from "node:crypto";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import {
  configuredEngramUrl,
  decodeResponse,
  reconnect,
  run,
} from "../scripts/engram-connect.mjs";

function rootWith(config) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "engram-connect-"));
  fs.writeFileSync(path.join(root, "config.toml"), config);
  return root;
}
test("only initializes, lists, and uses allowlisted read calls", async () => {
  const calls = [];
  const sessions = [];
  const server = http.createServer((req, res) => {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      const request = JSON.parse(body);
      calls.push(request.method);
      sessions.push(req.headers["mcp-session-id"] || "");
      if (request.method === "initialize")
        res.setHeader("mcp-session-id", "fixture-session");
      if (!("id" in request)) {
        res.statusCode = 202;
        return res.end();
      }
      const result =
        request.method === "tools/list"
          ? { tools: [{ name: "kg_search" }] }
          : { content: [] };
      res.end(JSON.stringify({ jsonrpc: "2.0", id: request.id, result }));
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  try {
    const root = rootWith(
      `[mcp_servers.engram]\nurl = "http://127.0.0.1:${server.address().port}/mcp"\n`,
    );
    await run(root, "search", "amber");
    assert.deepEqual(calls, [
      "initialize",
      "notifications/initialized",
      "tools/list",
      "tools/call",
    ]);
    assert.deepEqual(sessions, [
      "",
      "fixture-session",
      "fixture-session",
      "fixture-session",
    ]);
  } finally {
    server.close();
  }
});
test("rejects remote and authenticated config", () => {
  assert.throws(() =>
    configuredEngramUrl(
      rootWith('[mcp_servers.engram]\nurl = "https://example.com/mcp"\n'),
    ),
  );
  assert.throws(() =>
    configuredEngramUrl(
      rootWith(
        '[mcp_servers.engram]\nurl = "http://127.0.0.1:1/mcp"\nheaders = { Authorization = "secret" }\n',
      ),
    ),
  );
});
test("accepts SSE event message envelopes", () => {
  assert.deepEqual(
    decodeResponse(
      'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"tools":[]}}\n\n',
    ),
    { jsonrpc: "2.0", id: 1, result: { tools: [] } },
  );
});

function serverFrame(
  text,
  { fin = true, opcode = 1, rsv = 0, masked = false } = {},
) {
  const body = Buffer.from(text);
  const head = Buffer.from([
    (fin ? 0x80 : 0) | rsv | opcode,
    (masked ? 0x80 : 0) | body.length,
  ]);
  return Buffer.concat([
    head,
    masked ? Buffer.from([1, 2, 3, 4]) : Buffer.alloc(0),
    body,
  ]);
}
function fakeProxy(
  root,
  {
    wrongRoot = false,
    missingThread = false,
    empty = false,
    invalid = false,
    badFrame = false,
    laterThread = false,
    laterEngram = false,
    repeatedCursor = false,
  } = {},
) {
  const calls = [];
  let input = Buffer.alloc(0),
    upgraded = false;
  const child = new EventEmitter();
  child.stdout = new EventEmitter();
  child.stdin = new EventEmitter();
  child.stdin.end = () => {};
  child.kill = () => {};
  child.stdin.write = (chunk, done) => {
    input = Buffer.concat([input, chunk]);
    if (!upgraded) {
      const at = input.indexOf("\r\n\r\n");
      if (at >= 0) {
        upgraded = true;
        const key = /Sec-WebSocket-Key: ([^\r]+)/.exec(
          input.subarray(0, at).toString(),
        )?.[1];
        const accept = createHash("sha1")
          .update(`${key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11`)
          .digest("base64");
        child.stdout.emit(
          "data",
          Buffer.from(
            invalid
              ? "HTTP/1.1 200 Nope\r\n\r\n"
              : `HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`,
          ),
        );
        input = input.subarray(at + 4);
      }
    }
    while (upgraded && input.length >= 2) {
      let len = input[1] & 127,
        at = 2;
      if (len === 126) {
        if (input.length < 4) break;
        len = input.readUInt16BE(2);
        at = 4;
      }
      const total = at + 4 + len;
      if (input.length < total) break;
      const mask = input.subarray(at, at + 4),
        raw = input.subarray(at + 4, total),
        body = Buffer.from(raw.map((v, i) => v ^ mask[i % 4]));
      input = input.subarray(total);
      const request = JSON.parse(body);
      calls.push(request.method);
      if (!("id" in request)) continue;
      let result = {};
      if (request.method === "initialize")
        result = { codexHome: wrongRoot ? path.join(root, "other") : root };
      if (request.method === "thread/loaded/list")
        result =
          repeatedCursor && request.params.cursor
            ? { data: ["other"], nextCursor: "next" }
            : request.params.cursor
              ? { data: ["wanted"] }
              : {
                  data:
                    missingThread || laterThread || repeatedCursor
                      ? ["other"]
                      : ["wanted"],
                  ...(laterThread || repeatedCursor
                    ? { nextCursor: "next" }
                    : {}),
                };
      if (request.method === "mcpServerStatus/list")
        result = {
          data: [
            {
              name: laterEngram && !request.params.cursor ? "other" : "engram",
              tools: empty ? {} : { engram_get_context_once: {} },
            },
          ],
          ...(laterEngram && !request.params.cursor
            ? { nextCursor: "status-next" }
            : {}),
        };
      const text = JSON.stringify({ jsonrpc: "2.0", id: request.id, result });
      if (badFrame && request.method === "mcpServerStatus/list")
        child.stdout.emit("data", serverFrame(text, { masked: true }));
      else if (request.method === "mcpServerStatus/list")
        child.stdout.emit(
          "data",
          Buffer.concat([
            serverFrame(text.slice(0, 8), { fin: false }),
            serverFrame(text.slice(8), { opcode: 0 }),
          ]),
        );
      else child.stdout.emit("data", serverFrame(text));
    }
    done?.();
    return true;
  };
  return { calls, spawn: () => child };
}
test("reconnect uses native shapes and reloads once after initialized", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "engram-reconnect-")),
    fixture = fakeProxy(root);
  const result = await reconnect({
    root,
    threadId: "wanted",
    executable: "fake",
    spawnImpl: fixture.spawn,
  });
  assert.deepEqual(fixture.calls, [
    "initialize",
    "initialized",
    "thread/loaded/list",
    "config/mcpServer/reload",
    "mcpServerStatus/list",
  ]);
  assert.equal(result.requestAccepted, true);
  assert.equal(result.engramToolsRestored, true);
});
test("reconnect rejects wrong root or unloaded thread before reload", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "engram-reconnect-"));
  for (const options of [{ wrongRoot: true }, { missingThread: true }]) {
    const fixture = fakeProxy(root, options);
    await assert.rejects(
      reconnect({
        root,
        threadId: "wanted",
        executable: "fake",
        spawnImpl: fixture.spawn,
      }),
    );
    assert.equal(fixture.calls.includes("config/mcpServer/reload"), false);
  }
});
test("reconnect reports accepted reload separately from no restored tools", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "engram-reconnect-")),
    fixture = fakeProxy(root, { empty: true });
  const result = await reconnect({
    root,
    threadId: "wanted",
    executable: "fake",
    spawnImpl: fixture.spawn,
  });
  assert.equal(result.requestAccepted, true);
  assert.equal(result.engramToolsRestored, false);
});
test("finds target and Engram on later pages", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "engram-reconnect-"));
  const fixture = fakeProxy(root, { laterThread: true, laterEngram: true });
  const result = await reconnect({
    root,
    threadId: "wanted",
    executable: "fake",
    spawnImpl: fixture.spawn,
  });
  assert.equal(result.engramToolsRestored, true);
  assert.equal(
    fixture.calls.filter((item) => item === "config/mcpServer/reload").length,
    1,
  );
});
test("rejects a repeated loaded-thread cursor before reload", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "engram-reconnect-"));
  const fixture = fakeProxy(root, { repeatedCursor: true });
  await assert.rejects(
    reconnect({
      root,
      threadId: "wanted",
      executable: "fake",
      spawnImpl: fixture.spawn,
    }),
    /PAGINATION_CURSOR_INVALID/,
  );
  assert.equal(fixture.calls.includes("config/mcpServer/reload"), false);
});
test("rejects an upgrade failure and records post-reload frame failure", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "engram-reconnect-"));
  await assert.rejects(
    reconnect({
      root,
      threadId: "wanted",
      executable: "fake",
      spawnImpl: fakeProxy(root, { invalid: true }).spawn,
    }),
  );
  const result = await reconnect({
    root,
    threadId: "wanted",
    executable: "fake",
    spawnImpl: fakeProxy(root, { badFrame: true }).spawn,
  });
  assert.equal(result.requestAccepted, true);
  assert.equal(result.engramToolsRestored, false);
  assert.equal(typeof result.verificationError, "string");
});
