#!/usr/bin/env node
import { createHash, randomUUID } from 'node:crypto';
import { lstat, mkdir, open, readFile } from 'node:fs/promises';
import { dirname, isAbsolute, relative, resolve, sep } from 'node:path';
import { spawn } from 'node:child_process';
import { realpathSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const TOOL = 'engram_report_codex_event';
const SERVER = 'engram';
const EVENTS = ['UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'PermissionRequest', 'Stop', 'Interrupt', 'SubagentStart', 'SubagentStop'];
const EVENT_KEY = Object.fromEntries(EVENTS.map((event) => [event, event.replace(/[A-Z]/g, (c, i) => (i ? '_' : '') + c.toLowerCase())]));
const MAX_BACKUP_BYTES = 128 * 1024;

function die(code, detail) { const error = new Error(code); error.code = code; error.detail = detail; throw error; }
function parseArgs(argv) {
  if (argv.length === 1 && ['--help', '-h'].includes(argv[0])) return { help: true };
  const [command, ...rest] = argv;
  const args = { command, confirm: false };
  for (let i = 0; i < rest.length; i += 1) {
    const token = rest[i];
    if (token === '--confirm') args.confirm = true;
    else if (['--root', '--backup', '--codex-executable'].includes(token)) args[token.slice(2)] = rest[++i];
    else die('INVALID_ARGUMENT');
  }
  if (!['check', 'backup', 'restore'].includes(command)) die('USAGE');
  if (!args.root) die('ROOT_REQUIRED');
  if ((command === 'backup' || command === 'restore') && !args.backup) die('BACKUP_REQUIRED');
  if (command === 'restore' && !args.confirm) die('CONFIRM_REQUIRED');
  return args;
}
function help() {
  console.log('Usage: engram-hook-trust.mjs <check|backup|restore> --root <CODEX_HOME> [--backup <CODEX_HOME/.engram-hook-trust/file.json>] [--confirm] [--codex-executable <codex.exe>]');
}
function canonical(path) { const value = resolve(path).replaceAll('\\', '/'); return process.platform === 'win32' ? value.toLowerCase() : value; }
function inside(root, value) { const rel = relative(root, value); return rel === '' || (!rel.startsWith(`..${sep}`) && rel !== '..' && !isAbsolute(rel)); }
async function assertUnlinked(path, allowMissing = false) {
  for (let current = resolve(path);;) {
    try { if ((await lstat(current)).isSymbolicLink()) die('LINKED_PATH_REJECTED'); }
    catch (error) { if (!(allowMissing && error.code === 'ENOENT')) throw error; }
    const parent = dirname(current); if (parent === current) break; current = parent;
  }
}
async function rootInfo(rootInput) {
  const root = resolve(rootInput);
  const configured = process.env.CODEX_HOME ? resolve(process.env.CODEX_HOME) : null;
  if (!configured || canonical(root) !== canonical(configured)) die('ROOT_NOT_CODEX_HOME');
  const configPath = resolve(root, 'config.toml'), hooksPath = resolve(root, 'hooks.json');
  for (const path of [root, configPath, hooksPath]) await assertUnlinked(path);
  return { root, configPath, hooksPath, backupDir: resolve(root, '.engram-hook-trust'), cwd: process.cwd() };
}
async function checkedBackupPath(value, info) {
  const path = resolve(value);
  if (!inside(info.backupDir, path) || path === info.backupDir || !path.toLowerCase().endsWith('.json')) die('BACKUP_PATH_INVALID');
  await assertUnlinked(path, true);
  return path;
}
function expectedHandler(event) {
  const input = { event, turn_id: '${turn_id}', native_session_id: '${session_id}' };
  if (event === 'SubagentStart' || event === 'SubagentStop') input.agent_id = '${agent_id}';
  if (['PreToolUse', 'PostToolUse', 'PermissionRequest'].includes(event)) input.tool_name = '${tool_name}';
  if (['PreToolUse', 'PostToolUse'].includes(event)) input.tool_use_id = '${tool_use_id}';
  return { type: 'mcp_tool', server: SERVER, tool: TOOL, input, timeout: 2 };
}
function stable(value) {
  if (Array.isArray(value)) return value.map(stable);
  if (value && typeof value === 'object') return Object.fromEntries(Object.keys(value).sort().map((key) => [key, stable(value[key])]));
  return value;
}
function definitionHash(handler) { return `sha256:${createHash('sha256').update(JSON.stringify(stable(handler))).digest('hex')}`; }
function equal(a, b) { return JSON.stringify(stable(a)) === JSON.stringify(stable(b)); }
function parseKey(key) {
  const match = /^(.*):([a-z_]+):(\d+):(\d+)$/.exec(key);
  return match ? { sourcePath: match[1], eventKey: match[2], groupIndex: Number(match[3]), handlerIndex: Number(match[4]) } : null;
}
function selectUserLayer(response, info) {
  const layers = Array.isArray(response?.layers) ? response.layers : [];
  const exact = layers.filter((layer) => canonical(layer?.path ?? layer?.name?.file ?? layer?.source?.path ?? '') === canonical(info.configPath));
  if (exact.length !== 1 || exact[0]?.version === undefined || !exact[0]?.config || (exact[0]?.name?.type ?? exact[0]?.source?.type) !== 'user' || exact[0]?.name?.profile) die('USER_LAYER_NOT_EXACT');
  return exact[0];
}
function statePresent(layer, key) {
  const state = layer?.config?.hooks?.state;
  return !!state && typeof state === 'object' && Object.hasOwn(state, key);
}
function hookRecords(listResponse, layer, rawHooks, info) {
  const data = listResponse?.data;
  if (!Array.isArray(data) || data.length !== 1 || typeof data[0]?.cwd !== 'string' || canonical(data[0].cwd) !== canonical(info.cwd)
      || !Array.isArray(data[0].errors) || data[0].errors.length || !Array.isArray(data[0].warnings) || data[0].warnings.length
      || !Array.isArray(data[0].hooks)) die('NATIVE_SNAPSHOT_INVALID');
  const listed = data[0].hooks;
  const hooks = rawHooks?.hooks;
  if (!hooks || typeof hooks !== 'object') die('HOOKS_MISSING');
  const records = [];
  for (const event of EVENTS) {
    const groups = hooks[event];
    if (!Array.isArray(groups)) continue;
    groups.forEach((group, groupIndex) => (Array.isArray(group?.hooks) ? group.hooks : []).forEach((handler, handlerIndex) => {
      if (!equal(group, { hooks: [expectedHandler(event)] }) || handlerIndex !== 0 || !equal(handler, expectedHandler(event))) return;
      const match = listed.filter((item) => {
        const key = parseKey(item?.key ?? '');
        return key && canonical(key.sourcePath) === canonical(info.hooksPath) && key.eventKey === EVENT_KEY[event] && key.groupIndex === groupIndex && key.handlerIndex === handlerIndex;
      });
      if (match.length !== 1 || typeof match[0].currentHash !== 'string') die('NATIVE_KEY_NOT_EXACT');
      const native = match[0];
      if (!native.currentHash || native.currentHash.length > 256 || typeof native.enabled !== 'boolean'
          || !['trusted', 'untrusted', 'modified', 'managed'].includes(native.trustStatus)
          || typeof native.sourcePath !== 'string' || canonical(native.sourcePath) !== canonical(info.hooksPath)
          || native.eventName !== event[0].toLowerCase() + event.slice(1)
          || native.handlerType !== 'mcpTool' || native.server !== SERVER || native.tool !== TOOL) die('NATIVE_METADATA_INVALID');
      if (native.trustStatus === 'trusted' && layer.config?.hooks?.state?.[native.key]?.trusted_hash !== native.currentHash) die('NATIVE_STATE_INCONSISTENT');
      records.push({ event, key: match[0].key, currentHash: match[0].currentHash, trustStatus: match[0].trustStatus, enabled: match[0].enabled, statePresent: statePresent(layer, match[0].key), generatedHash: definitionHash(expectedHandler(event)) });
    }));
  }
  if (records.length !== EVENTS.length || new Set(records.map((record) => record.event)).size !== EVENTS.length) die('ALLOWLIST_INCOMPLETE');
  return records;
}
class Rpc {
  constructor(command = 'codex') { this.command = command; this.id = 0; this.pending = new Map(); this.closed = false; }
  async start() {
    this.child = spawn(this.command, ['app-server'], { stdio: ['pipe', 'pipe', 'ignore'], windowsHide: true });
    this.child.stdout.setEncoding('utf8'); let buffer = '';
    const rejectAll = (code) => { for (const waiter of this.pending.values()) waiter.reject(Object.assign(new Error(code), { code })); this.pending.clear(); };
    this.child.stdin.on('error', () => rejectAll('RPC_WRITE_FAILED'));
    this.child.on('error', () => rejectAll('RPC_SPAWN_FAILED')); this.child.on('exit', () => rejectAll('RPC_EXITED'));
    this.child.stdout.on('data', (chunk) => { buffer += chunk; if (Buffer.byteLength(buffer) > 1024 * 1024) { this.child.kill(); rejectAll('RPC_OUTPUT_TOO_LARGE'); return; } for (;;) { const pos = buffer.indexOf('\n'); if (pos < 0) break; const line = buffer.slice(0, pos); buffer = buffer.slice(pos + 1); try { const message = JSON.parse(line); const waiter = this.pending.get(message.id); if (waiter) { this.pending.delete(message.id); message.error ? waiter.reject(Object.assign(new Error('RPC_ERROR'), { code: 'RPC_ERROR' })) : waiter.resolve(message.result); } } catch {} } });
    await this.call('initialize', { clientInfo: { name: 'engram-hook-trust', version: '1' }, capabilities: { experimentalApi: true } });
    this.child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', method: 'initialized', params: {} })}\n`);
  }
  call(method, params) { if (this.closed) die('RPC_CLOSED'); const id = ++this.id; const payload = `${JSON.stringify({ jsonrpc: '2.0', id, method, params })}\n`; return new Promise((resolveCall, reject) => { const timer = setTimeout(() => { this.pending.delete(id); reject(Object.assign(new Error('RPC_TIMEOUT'), { code: 'RPC_TIMEOUT' })); }, 10000); this.pending.set(id, { resolve: (value) => { clearTimeout(timer); resolveCall(value); }, reject: (error) => { clearTimeout(timer); reject(error); } }); this.child.stdin.write(payload, (error) => { if (error) { clearTimeout(timer); this.pending.delete(id); reject(Object.assign(error, { code: 'RPC_WRITE_FAILED' })); } }); }); }
  async close() { if (this.closed) return; this.closed = true; this.child?.stdin.end(); this.child?.kill(); }
}
async function inspect(args, write = false) {
  const info = await rootInfo(args.root); const rpc = new Rpc(args['codex-executable'] ?? 'codex');
  try {
    await rpc.start();
    const layer = selectUserLayer(await rpc.call('config/read', { cwd: info.cwd, includeLayers: true }), info);
    const rawHooksText = await readFile(info.hooksPath, 'utf8'); const rawHooks = JSON.parse(rawHooksText);
    const records = hookRecords(await rpc.call('hooks/list', { cwds: [info.cwd] }), layer, rawHooks, info);
    return { info, rpc, layer, records, rawHooksText, write };
  } catch (error) { await rpc.close(); throw error; }
}
function publicRecords(records) { return records.map(({ event, trustStatus, enabled, statePresent }) => ({ event, trustStatus: typeof trustStatus === 'string' ? trustStatus : 'unknown', enabled: enabled === true, statePresent })); }
async function writeExclusive(path, object) {
  await mkdir(dirname(path), { recursive: true, mode: 0o700 });
  await assertUnlinked(path, true);
  const text = `${JSON.stringify(object)}\n`; if (Buffer.byteLength(text) > MAX_BACKUP_BYTES) die('BACKUP_TOO_LARGE');
  let handle; try { handle = await open(path, 'wx', 0o600); await handle.writeFile(text, 'utf8'); } finally { await handle?.close(); }
}
async function loadBackup(path, info) {
  await assertUnlinked(path);
  if ((await lstat(path)).size > MAX_BACKUP_BYTES) die('BACKUP_TOO_LARGE');
  const text = await readFile(path, 'utf8'); if (Buffer.byteLength(text) > MAX_BACKUP_BYTES) die('BACKUP_TOO_LARGE');
  const backup = JSON.parse(text);
  if (backup?.version !== 1 || backup?.root !== canonical(info.root) || backup?.configPath !== canonical(info.configPath) || !Array.isArray(backup?.records)) die('BACKUP_ROOT_MISMATCH');
  if (!backup.records.length || backup.records.length > EVENTS.length || backup.records.some((record) => !EVENTS.includes(record?.event)) || new Set(backup.records.map((record) => record.event)).size !== backup.records.length) die('BACKUP_SCHEMA_INVALID');
  for (const record of backup.records) {
    if (typeof record.key !== 'string' || typeof record.currentHash !== 'string' || typeof record.generatedHash !== 'string' || record.generatedHash !== definitionHash(expectedHandler(record.event))) die('BACKUP_SCHEMA_INVALID');
    const key = parseKey(record.key); if (!key || key.eventKey !== EVENT_KEY[record.event] || canonical(key.sourcePath) !== canonical(info.hooksPath)) die('BACKUP_SCHEMA_INVALID');
  }
  return backup;
}
async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help) { help(); return; }
  const state = await inspect(args, args.command === 'restore');
  try {
    if (args.command === 'check') {
      let recoverable = 0, snapshotRecords = 0;
      if (args.backup) { const backup = await loadBackup(await checkedBackupPath(args.backup, state.info), state.info); snapshotRecords = backup.records.length; const saved = new Map(backup.records.map((record) => [record.event, record])); recoverable = state.records.filter((record) => { const item = saved.get(record.event); return item && item.key === record.key && item.currentHash === record.currentHash && item.generatedHash === record.generatedHash && !record.statePresent && record.trustStatus === 'untrusted' && record.enabled === true; }).length; }
      console.log(JSON.stringify({ command: 'check', records: publicRecords(state.records), snapshotRecords, recoverable })); return;
    }
    if (args.command === 'backup') {
      const eligible = state.records.filter((record) => record.trustStatus === 'trusted' && record.enabled === true);
      if (!eligible.length) die('BACKUP_NO_TRUSTED_ENABLED');
      const fresh = selectUserLayer(await state.rpc.call('config/read', { cwd: state.info.cwd, includeLayers: true }), state.info);
      if (fresh.version !== state.layer.version || await readFile(state.info.hooksPath, 'utf8') !== state.rawHooksText) die('BACKUP_SOURCE_CHANGED');
      await writeExclusive(await checkedBackupPath(args.backup, state.info), { version: 1, root: canonical(state.info.root), configPath: canonical(state.info.configPath), createdAt: new Date().toISOString(), nonce: randomUUID(), records: eligible.map(({ event, key, currentHash, generatedHash }) => ({ event, key, currentHash, generatedHash })) });
      console.log(JSON.stringify({ command: 'backup', backedUp: eligible.length })); return;
    }
    const backup = await loadBackup(await checkedBackupPath(args.backup, state.info), state.info); const backupByEvent = new Map(backup.records.map((record) => [record.event, record])); const edits = [];
    for (const record of state.records) {
      const saved = backupByEvent.get(record.event);
      if (!saved) continue;
      if (saved.key !== record.key || saved.currentHash !== record.currentHash || saved.generatedHash !== record.generatedHash) die('BACKUP_RECORD_MISMATCH');
      if (record.trustStatus === 'trusted' && record.enabled === true) continue;
      if (record.statePresent || record.trustStatus !== 'untrusted' || record.enabled !== true) die('RESTORE_STATE_NOT_MISSING');
      edits.push({ keyPath: `hooks.state.${JSON.stringify(record.key)}`, value: { trusted_hash: record.currentHash }, mergeStrategy: 'replace' });
    }
    if (edits.length) {
      if (await readFile(state.info.hooksPath, 'utf8') !== state.rawHooksText) die('HOOK_DEFINITION_CHANGED');
      const latestLayer = selectUserLayer(await state.rpc.call('config/read', { cwd: state.info.cwd, includeLayers: true }), state.info);
      if (latestLayer.version !== state.layer.version) die('CONFIG_VERSION_CHANGED');
      await state.rpc.call('config/batchWrite', { filePath: state.info.configPath, expectedVersion: state.layer.version, edits, reloadUserConfig: true });
      const postLayer = selectUserLayer(await state.rpc.call('config/read', { cwd: state.info.cwd, includeLayers: true }), state.info);
      const postText = await readFile(state.info.hooksPath, 'utf8');
      const post = hookRecords(await state.rpc.call('hooks/list', { cwds: [state.info.cwd] }), postLayer, JSON.parse(postText), state.info);
      if (postText !== state.rawHooksText || edits.some((edit) => { const key = JSON.parse(edit.keyPath.slice('hooks.state.'.length)); const found = post.find((record) => record.key === key); return !found || found.trustStatus !== 'trusted' || found.enabled !== true; })) die('POSTWRITE_VERIFY_FAILED');
      const expectedConfig = structuredClone(state.layer.config);
      expectedConfig.hooks ??= {}; expectedConfig.hooks.state ??= {};
      for (const edit of edits) expectedConfig.hooks.state[JSON.parse(edit.keyPath.slice('hooks.state.'.length))] = edit.value;
      if (!equal(expectedConfig, postLayer.config)) die('POSTWRITE_CONFIG_CHANGED');
    }
    console.log(JSON.stringify({ command: 'restore', restored: edits.length, skipped: EVENTS.length - edits.length }));
  } finally { await state.rpc.close(); }
}
const isMain = process.argv[1] && realpathSync(process.argv[1]) === realpathSync(fileURLToPath(import.meta.url));
if (isMain) main().catch((error) => { console.error(JSON.stringify({ error: error.code ?? 'UNEXPECTED' })); process.exitCode = 1; });

export { EVENTS, expectedHandler, equal, hookRecords, parseKey, selectUserLayer, checkedBackupPath };
