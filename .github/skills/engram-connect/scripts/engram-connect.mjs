#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';

const MAX_CONFIG = 262144;
const MAX_OUTPUT = 65536;
const TIMEOUT_MS = 3000;
const ALLOWED_HOSTS = new Set(['127.0.0.1', 'localhost']);
const ALLOWED = { search: 'kg_search', 'read-note': 'kg_read_note' };

function argument(name) {
  const index = process.argv.indexOf(name);
  return index < 0 ? '' : (process.argv[index + 1] || '');
}

export function configuredEngramUrl(root) {
  if (!root) throw new Error('--root is required');
  const config = path.resolve(root, 'config.toml');
  const stat = fs.lstatSync(config);
  if (!stat.isFile() || stat.isSymbolicLink() || stat.size > MAX_CONFIG) throw new Error('unsafe Codex config.toml');
  const text = fs.readFileSync(config, 'utf8');
  const section = /^\s*\[mcp_servers\.engram\]\s*$/m.exec(text);
  if (!section) throw new Error('Engram MCP entry is missing');
  const tail = text.slice(section.index + section[0].length).split(/^\s*\[/m, 1)[0];
  if (/^\s*(headers|bearer_token|authorization)\s*=/mi.test(tail)) throw new Error('authenticated MCP entries are not supported');
  const match = /^\s*url\s*=\s*["']([^"']+)["']\s*$/mi.exec(tail);
  if (!match) throw new Error('Engram MCP URL is missing');
  const url = new URL(match[1]);
  if (url.protocol !== 'http:' || !ALLOWED_HOSTS.has(url.hostname) || url.username || url.password) throw new Error('only unauthenticated loopback http URLs are allowed');
  return url;
}

let requestId = 0;

function decodeResponse(body) {
  if (!body.trim()) return null;
  if (!body.trimStart().startsWith('data:')) return JSON.parse(body);
  const messages = body.split(/\r?\n\r?\n/).map(block =>
    block.split(/\r?\n/).filter(line => line.startsWith('data:')).map(line => line.slice(5).trim()).join('\n')
  ).filter(Boolean).map(value => JSON.parse(value));
  return messages.at(-1) || null;
}

async function rpc(client, method, params = {}, { notification = false } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const headers = { 'content-type': 'application/json', accept: 'application/json, text/event-stream', 'mcp-protocol-version': '2025-03-26' };
    if (client.session) headers['mcp-session-id'] = client.session;
    const payload = { jsonrpc: '2.0', method, params };
    if (!notification) payload.id = ++requestId;
    const response = await fetch(client.url, { method: 'POST', redirect: 'error', signal: controller.signal,
      headers, body: JSON.stringify(payload) });
    const body = await response.text();
    if (!response.ok || body.length > MAX_OUTPUT) throw new Error('MCP response rejected');
    const issued = response.headers.get('mcp-session-id');
    if (issued) client.session = issued;
    if (notification) return null;
    const json = decodeResponse(body);
    if (json.error || !json.result) throw new Error('MCP method failed');
    return json.result;
  } finally { clearTimeout(timer); }
}

export async function run(root, command, value = '') {
  const url = configuredEngramUrl(root);
  const client = { url, session: '' };
  await rpc(client, 'initialize', { protocolVersion: '2025-03-26', capabilities: {}, clientInfo: { name: 'engram-connect', version: '1' } });
  await rpc(client, 'notifications/initialized', {}, { notification: true });
  const tools = await rpc(client, 'tools/list');
  if (command === 'probe') return { ok: true, tools: Array.isArray(tools.tools) ? tools.tools.map(item => item.name).slice(0, 32) : [] };
  const tool = ALLOWED[command];
  if (!tool || !value || value.length > 256) throw new Error('invalid read-only command');
  if (!Array.isArray(tools.tools) || !tools.tools.some(item => item.name === tool)) throw new Error('required Engram tool is unavailable');
  return rpc(client, 'tools/call', { name: tool, arguments: command === 'search' ? { query: value } : { identifier: value } });
}

if (import.meta.url === `file://${process.argv[1].replace(/\\/g, '/')}`) {
  const command = process.argv[2];
  run(argument('--root'), command, command === 'search' ? argument('--query') : argument('--id'))
    .then(result => console.log(JSON.stringify(result).slice(0, MAX_OUTPUT)))
    .catch(error => { console.error(`engram-connect: ${error.message}`); process.exitCode = 1; });
}
