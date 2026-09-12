import assert from 'node:assert/strict';
import { EVENTS, expectedHandler, hookRecords, checkedBackupPath } from '../scripts/engram-hook-trust.mjs';
import { mkdtemp, mkdir, symlink } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';

const hooksPath = 'C:/fixture/home/hooks.json';
const snake = (name) => name.replace(/[A-Z]/g, (c, i) => (i ? '_' : '') + c.toLowerCase());
const hooks = Object.fromEntries(EVENTS.map((event) => [event, [{ hooks: [expectedHandler(event)] }]]));
const listed = EVENTS.map((event) => ({ key: `${hooksPath}:${snake(event)}:0:0`, currentHash: `hash-${event}`, trustStatus: 'trusted', enabled: true, sourcePath: hooksPath, eventName: event[0].toLowerCase() + event.slice(1), handlerType: 'mcpTool', server: 'engram', tool: 'engram_report_codex_event' }));
const layer = { config: { hooks: { state: Object.fromEntries(listed.map((entry) => [entry.key, { trusted_hash: entry.currentHash }])) } } };
const info = { hooksPath, cwd: 'C:/fixture' };
const response = () => ({ data: [{ cwd: info.cwd, errors: [], warnings: [], hooks: structuredClone(listed) }] });
assert.equal(hookRecords(response(), layer, { hooks }, info).length, 8);
for (const mutate of [r => { r.data[0].cwd = 'C:/wrong'; }, r => { r.data[0].errors.push({}); }, r => { r.data[0].warnings.push({}); }, r => { r.data.push(r.data[0]); }]) {
  const r = response(); mutate(r); assert.throws(() => hookRecords(r, layer, { hooks }, info), /NATIVE_SNAPSHOT_INVALID/);
}
for (const field of ['enabled', 'trustStatus', 'sourcePath', 'handlerType', 'server', 'eventName']) {
  const r = response(); delete r.data[0].hooks[0][field]; assert.throws(() => hookRecords(r, layer, { hooks }, info), /NATIVE_METADATA_INVALID/);
}
const inconsistent = structuredClone(layer); delete inconsistent.config.hooks.state[listed[0].key];
assert.throws(() => hookRecords(response(), inconsistent, { hooks }, info), /NATIVE_STATE_INCONSISTENT/);
hooks.PreToolUse[0].hooks[0].timeout = 9;
assert.throws(() => hookRecords(response(), layer, { hooks }, info), /ALLOWLIST_INCOMPLETE/);
const temp = await mkdtemp(join(tmpdir(), 'engram-trust-link-test-'));
const backupDir = join(temp, '.engram-hook-trust'), outside = join(temp, 'outside');
await mkdir(outside); await symlink(outside, backupDir, process.platform === 'win32' ? 'junction' : 'dir');
await assert.rejects(checkedBackupPath(join(backupDir, 'backup.json'), { backupDir }), /LINKED_PATH_REJECTED/);
await assert.rejects(checkedBackupPath(join(outside, 'backup.json'), { backupDir }), /BACKUP_PATH_INVALID/);
console.log('engram-hook-trust pure validation tests passed');
