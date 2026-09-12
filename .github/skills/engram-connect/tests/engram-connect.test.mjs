import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { configuredEngramUrl, run } from '../scripts/engram-connect.mjs';

function rootWith(config) { const root = fs.mkdtempSync(path.join(os.tmpdir(), 'engram-connect-')); fs.writeFileSync(path.join(root, 'config.toml'), config); return root; }
test('only initializes, lists, and uses allowlisted read calls', async () => {
  const calls = []; const sessions = []; const server = http.createServer((req, res) => { let body = ''; req.on('data', c => body += c); req.on('end', () => { const request = JSON.parse(body); calls.push(request.method); sessions.push(req.headers['mcp-session-id'] || ''); if (request.method === 'initialize') res.setHeader('mcp-session-id', 'fixture-session'); if (!('id' in request)) { res.statusCode = 202; return res.end(); } const result = request.method === 'tools/list' ? { tools: [{ name: 'kg_search' }] } : { content: [] }; res.end(JSON.stringify({ jsonrpc: '2.0', id: request.id, result })); }); });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try { const root = rootWith(`[mcp_servers.engram]\nurl = "http://127.0.0.1:${server.address().port}/mcp"\n`); await run(root, 'search', 'amber'); assert.deepEqual(calls, ['initialize', 'notifications/initialized', 'tools/list', 'tools/call']); assert.deepEqual(sessions, ['', 'fixture-session', 'fixture-session', 'fixture-session']); } finally { server.close(); }
});
test('rejects remote and authenticated config', () => {
  assert.throws(() => configuredEngramUrl(rootWith('[mcp_servers.engram]\nurl = "https://example.com/mcp"\n')));
  assert.throws(() => configuredEngramUrl(rootWith('[mcp_servers.engram]\nurl = "http://127.0.0.1:1/mcp"\nheaders = { Authorization = "secret" }\n')));
});
