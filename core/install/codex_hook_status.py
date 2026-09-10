"""Read-only native hook review status. Never starts a conversation or approves trust."""
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time


def _path(value):
    return os.path.normcase(os.path.abspath(str(value)))


def native_list(root, cwd):
    executable = shutil.which('codex')
    if not executable:
        raise RuntimeError('codex-unavailable')
    env = {k: v for k, v in os.environ.items() if not k.startswith('ORCA_')}
    env['CODEX_HOME'] = str(root)
    child = subprocess.Popen([executable, 'app-server'], cwd=str(cwd), env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    messages = queue.Queue(maxsize=64)
    deadline = time.monotonic() + 12

    def read():
        while True:
            raw = child.stdout.readline(1_000_001)
            if not raw:
                return
            if len(raw) > 1_000_000:
                return
            try:
                messages.put_nowait(json.loads(raw))
            except (ValueError, queue.Full):
                return

    def request(identifier, method, params):
        child.stdin.write((json.dumps({'id': identifier, 'method': method, 'params': params}) + '\n').encode())
        child.stdin.flush()
        while time.monotonic() < deadline:
            try:
                result = messages.get(timeout=min(.2, max(.01, deadline - time.monotonic())))
            except queue.Empty:
                if child.poll() is not None:
                    break
                continue
            if isinstance(result, dict) and result.get('id') == identifier:
                if 'error' in result or 'result' not in result:
                    raise RuntimeError('native-request-failed')
                return result['result']
        raise TimeoutError('native-request-timeout')

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        request(1, 'initialize', {'clientInfo': {'name': 'engram-hook-status', 'version': '1'},
                                'capabilities': {'experimentalApi': True}})
        child.stdin.write(b'{"method":"initialized","params":{}}\n')
        child.stdin.flush()
        return request(2, 'hooks/list', {'cwds': [str(cwd)]})
    finally:
        child.stdin.close()
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            child.terminate()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2)
        reader.join(timeout=1)
        child.stdout.close()


def summarize(result, root, cwd, *, server='engram'):
    from .codex_monitor_hooks import EVENTS, SUBAGENT_EVENTS, TOOL, supported_title_commands
    base = {'status': 'unknown', 'trust_required': False, 'observed_hook_count': 0,
            'reason': 'native-status-invalid'}
    if not isinstance(result, dict) or not isinstance(result.get('data'), list):
        return base
    entries = [entry for entry in result['data'] if isinstance(entry, dict)
               and isinstance(entry.get('cwd'), str) and _path(entry['cwd']) == _path(cwd)]
    if len(entries) != 1:
        return base
    entry = entries[0]
    if (not isinstance(entry.get('errors'), list) or entry['errors']
            or not isinstance(entry.get('warnings'), list) or entry['warnings']
            or not isinstance(entry.get('hooks'), list)):
        return base
    expected = {name[0].lower() + name[1:] for name in (*EVENTS, 'SessionStart')}
    sources = {_path(Path(root) / name) for name in ('hooks.json', 'config.toml')}
    found = []
    for hook in entry['hooks']:
        if not isinstance(hook, dict) or not isinstance(hook.get('sourcePath'), str):
            return base
        if _path(hook['sourcePath']) not in sources:
            continue  # Another provider root or a project layer cannot satisfy this root.
        native = (hook.get('handlerType') == 'mcpTool' and hook.get('server') == server
                  and hook.get('tool') == TOOL)
        reminder = (hook.get('handlerType') == 'command' and hook.get('eventName') == 'sessionStart'
                    and isinstance(hook.get('command'), str)
                    and hook.get('command') in supported_title_commands())
        if native or reminder:
            found.append(hook)
    base['observed_hook_count'] = len(found)
    child_events = {event[0].lower() + event[1:] for event in SUBAGENT_EVENTS}
    if any(hook.get('eventName') in tuple(child_events) for hook in found):
        expected |= child_events
    if (len(found) != len(expected) or any(not isinstance(hook.get('eventName'), str) for hook in found)
            or {hook.get('eventName') for hook in found} != expected):
        return {**base, 'reason': 'monitor-hooks-missing-or-duplicated'}
    if any(type(hook.get('enabled')) is not bool or hook.get('trustStatus') not in
           ('trusted', 'managed', 'untrusted', 'modified') for hook in found):
        return base
    if any(not hook['enabled'] for hook in found):
        return {**base, 'status': 'disabled', 'reason': 'native-hook-disabled'}
    if any(hook['trustStatus'] in ('untrusted', 'modified') for hook in found):
        return {**base, 'status': 'review-needed', 'reason': 'native-hook-review-required', 'trust_required': True}
    return {**base, 'status': 'ready', 'reason': 'native-nine-hooks-ready' if len(expected) == 9 else 'native-seven-hooks-ready'}


def inspect_root(root, *, cwd=None, server='engram'):
    root, cwd = Path(root).absolute(), Path(cwd or Path.cwd()).absolute()
    try:
        status = summarize(native_list(root, cwd), root, cwd, server=server)
    except (OSError, ValueError, RuntimeError, TimeoutError, subprocess.SubprocessError):
        status = {'status': 'unknown', 'trust_required': False, 'observed_hook_count': 0,
                  'reason': 'native-status-unavailable'}
    return {**status, 'root': str(root), 'cwd': str(cwd),
            'review_instruction': 'Open Codex with CODEX_HOME=' + str(root)
                + ' and working directory ' + str(cwd) + '; enter /hooks to review this root.'}
