"""Explicit Windows integration probe; never collected by unittest.

All descendants are assigned to a kill-on-close Job before the worker gate opens.
No startup registration, user installation, or live profile mutation is performed.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def request(port, path, token=None):
    headers = {'Authorization': 'Bearer ' + token} if token else {}
    with urllib.request.urlopen(urllib.request.Request(f'http://127.0.0.1:{port}{path}', headers=headers), timeout=3) as response:
        return json.load(response)


def capture_hover(base, port, host_pid, iteration):
    import win32api
    import win32gui
    import win32process
    from PIL import ImageGrab
    discovery = json.loads((base/'.engram/overlay-state-api-v1.json').read_text())
    title = '외장 오버레이 연결 이후 긴 세션 제목 전체 미리보기와 자동 재연결 상태를 확인하는 통합 테스트'
    payload = {'provider': 'claude', 'session_id': 'isolated-title-preview', 'state': 'working', 'label': title}
    with urllib.request.urlopen(urllib.request.Request(f'http://127.0.0.1:{port}/state',
            data=json.dumps(payload).encode(), headers={'Authorization': 'Bearer '+discovery['token'],
                                                       'Content-Type': 'application/json'}, method='POST'), timeout=3):
        pass
    def windows():
        result = []
        def inspect(hwnd, unused):
            if win32gui.IsWindowVisible(hwnd) and win32process.GetWindowThreadProcessId(hwnd)[1] == host_pid:
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                result.append((hwnd, (left, top, right, bottom)))
        win32gui.EnumWindows(inspect, None)
        return result
    deadline = time.monotonic()+15
    cards = []
    while time.monotonic() < deadline:
        cards = [(hwnd, rect) for hwnd, rect in windows()
                 if 180 <= rect[2]-rect[0] <= 650 and 72 <= rect[3]-rect[1] <= 160
                 and 3.5 <= (rect[2]-rect[0])/(rect[3]-rect[1]) <= 5.5]
        if cards:
            break
        time.sleep(.3)
    if not cards:
        launchers = [(hwnd, rect) for hwnd, rect in windows()
                     if 45 <= rect[2]-rect[0] <= 70 and rect[2]-rect[0] == rect[3]-rect[1]]
        if len(launchers) == 1:
            launcher_hwnd, rect = launchers[0]
            ping_started = time.monotonic()
            win32gui.SendMessageTimeout(launcher_hwnd, 0, 0, 0, 2, 2000)
            print(f'UI_MESSAGE_RESPONSIVE {time.monotonic()-ping_started:.3f}', flush=True)
            position = ((rect[0]+rect[2])//2, (rect[1]+rect[3])//2)
            cursor = win32api.GetCursorPos()
            try:
                win32api.SetCursorPos(position)
                target = win32gui.WindowFromPoint(position)
                assert win32process.GetWindowThreadProcessId(target)[1] == host_pid, 'Launcher is obscured by another application'
                win32api.mouse_event(2, 0, 0)
                time.sleep(.1)
                win32api.mouse_event(4, 0, 0)
                time.sleep(2)
            finally:
                win32api.SetCursorPos(cursor)
            cards = [(hwnd, rect) for hwnd, rect in windows()
                     if 180 <= rect[2]-rect[0] <= 650 and 72 <= rect[3]-rect[1] <= 160
                     and 3.5 <= (rect[2]-rect[0])/(rect[3]-rect[1]) <= 5.5]
            print('OWNED_LAUNCHER_CLICK_ATTEMPT', flush=True)
    assert cards, 'Actual external-anchored session card not visible: ' + repr([rect for unused, rect in windows()])
    hwnd, rect = max(cards, key=lambda item: item[1][2]-item[1][0])
    before = {handle for handle, unused in windows()}
    saved_cursor = win32api.GetCursorPos()
    try:
        scale = (rect[3]-rect[1])/76
        win32api.SetCursorPos((rect[0]+round(45*scale), rect[1]+round(38*scale)))
        time.sleep(1)
        after = windows()
        tooltips = [(handle, bounds) for handle, bounds in after if handle not in before]
        assert tooltips, 'Hover did not create a visible tooltip window'
        all_rects = [rect]+[bounds for unused, bounds in tooltips]
        capture = (min(r[0] for r in all_rects)-4, min(r[1] for r in all_rects)-4,
                   max(r[2] for r in all_rects)+4, max(r[3] for r in all_rects)+4)
        path = base/f'external-title-hover-{iteration}.png'
        ImageGrab.grab(bbox=capture, all_screens=True).save(path)
        print(f'EXTERNAL_HOVER_CAPTURE {path}', flush=True)
    finally:
        win32api.SetCursorPos(saved_cursor)


def worker(args, base):
    if sys.stdin.readline().strip() != 'GO':
        raise RuntimeError('Job assignment gate was not opened')
    # Native window bounds and cursor/image coordinates must share physical
    # pixels when source and manifest-aware frozen windows use different DPI.
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
    cfg = json.loads((base / '.engram/user.config.yaml').read_text())
    ports = [cfg['overlay']['stm_server_port'], cfg['mcp']['http_port'], cfg['dashboard']['port']]
    for port in ports:
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', port))
    from core.install.service_config import effective_service_config
    from overlay.stm_server import _get_port
    effective = effective_service_config()
    assert _get_port() == effective['overlay']['stm_server_port'] == ports[0]
    assert effective['mcp']['http_port'] == ports[1]
    contract = subprocess.run([sys.executable, str(ROOT / 'engram_overlay_entry.py'), '--role', 'runtime-contract'],
                              capture_output=True, text=True, encoding='utf-8', timeout=90)
    assert contract.returncode == 0, contract.stderr[-1000:]
    evidence = json.loads(next(line for line in reversed(contract.stdout.splitlines()) if line.startswith('{')))
    assert (evidence['stm_port'], evidence['mcp_port']) == tuple(ports[:2])
    assert all(str(base) in path for path in evidence['service_config'].values())
    if args.frozen_exe:
        frozen_probe = subprocess.run([args.frozen_exe, '--role', 'service-config'], capture_output=True,
                                      text=True, encoding='utf-8', timeout=90)
        assert frozen_probe.returncode == 0
        frozen_info = json.loads(next(line for line in reversed(frozen_probe.stdout.splitlines()) if line.startswith('{')))
        assert (frozen_info['stm_port'], frozen_info['mcp_port']) == tuple(ports[:2])
    print('PREFLIGHT_PASS ' + json.dumps({'ports': ports, 'runtime': evidence['runtime'], 'profile': str(base)}), flush=True)
    # Renderer-owned installed code is read-only; discovery and settings use the
    # isolated inherited profile. Launching it never registers login startup.
    ps = 'C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe'
    provider = None
    if args.dev:
        # Exercise the actual bundled runtime installer, confined to LOCALAPPDATA
        # in this profile. Autostart is deliberately not requested here.
        install_env = dict(os.environ, PROBE_WHEEL=str(Path(args.wheel).resolve()), PROBE_PYTHON=args.provider_python)
        command = ". ./installer/external-overlay.ps1; $r=Install-ExternalOverlayRuntime -WheelPath $env:PROBE_WHEEL -AmberVersion '1.5.14.703' -PythonCommand $env:PROBE_PYTHON; if (-not $r.Installed) { throw $r.Reason }; Write-Output 'RUNTIME_INSTALL_PASS'"
        installed = subprocess.run([ps, '-NoProfile', '-Command', command], cwd=ROOT, env=install_env,
                                   capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=180)
        (base/'runtime-install.log').write_text(installed.stdout+installed.stderr, encoding='utf-8')
        assert installed.returncode == 0, installed.stderr[-1000:]
        common = [ps, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(ROOT/'dev-rebuild.ps1'),
                  '-PythonPath', sys.executable, '-AutoStart', 'preserve', '-ExternalOverlay', 'start']
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (base/'.engram').glob('*.yaml')}
        no_start = subprocess.run(common+['-NoStart'], cwd=ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=90)
        (base/'nostart.log').write_text(no_start.stdout+no_start.stderr, encoding='utf-8')
        assert no_start.returncode == 0
        assert before == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (base/'.engram').glob('*.yaml')}
        assert not (base/'.engram/overlay-event-api-v2.json').exists()
        print('RUNTIME_INSTALL_AND_NOSTART_PASS', flush=True)
    else:
        provider_log = (base / 'provider.log').open('w', encoding='utf-8')
        provider = subprocess.Popen([args.provider_python, '-m', 'engram_overlay', '--provider'],
                                    stdout=provider_log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
    previous_pid = None
    for iteration in range(3 if args.frozen_exe else 2):
        if args.hover:
            (base/'.engram/overlay.state.yaml').write_text(json.dumps({'restore_presentation':'full'}), encoding='utf-8')
        log = (base / f'host-{iteration}.log').open('w', encoding='utf-8')
        host = None
        if args.frozen_exe and iteration == 1:
            launch_env = dict(os.environ, PROBE_HOST=str(Path(args.frozen_exe).resolve()))
            command = ". ./installer/joint-startup.ps1; Invoke-EngramInstalledLaunch -Executable $env:PROBE_HOST -ExternalOverlay skip"
            run = subprocess.run([ps, '-NoProfile', '-Command', command], cwd=ROOT, env=launch_env,
                                 stdout=log, stderr=subprocess.STDOUT, timeout=210)
            log.flush()
            assert run.returncode == 0, f'Frozen launch failed; inspect {base}'
        elif args.dev:
            run = subprocess.run(common+['-ReadyTimeoutSeconds','120'], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, timeout=200)
            log.flush()
            assert run.returncode == 0, f'dev-rebuild failed; inspect {base}'
        else:
            host = subprocess.Popen([sys.executable, str(ROOT / 'engram_overlay_entry.py')], cwd=ROOT,
                                    stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.monotonic() + 120
        ready = None
        while time.monotonic() < deadline:
            assert host is None or host.poll() is None, f'Host exited; inspect {base}'
            assert provider is None or provider.poll() is None, f'Provider exited; inspect {base}'
            try:
                stm = request(ports[0], '/health')
                mcp = request(ports[1], '/health')
                discovery = json.loads((base / '.engram/overlay-state-api-v1.json').read_text())
                catalog = request(ports[0], '/state/renderers', discovery['token'])
                host_pid = host.pid if host else stm['pid']
                if (stm['pid'] == host_pid and mcp['parent_pid'] == host_pid and
                    catalog.get('pid') == host_pid and catalog.get('instance_id') == discovery['instance_id'] and
                    catalog.get('catalog_connected') and catalog.get('selected_ready') and catalog.get('actual_mode') == 'replace'):
                    ready = {'host_pid': host_pid, 'mcp_pid': mcp['pid'], 'catalog': catalog}
                    break
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(0.4)
        assert ready, f'Readiness timed out; inspect {base}'
        if previous_pid:
            from core.install.process_identity import get_process_identity
            assert get_process_identity(previous_pid, strict=True) is None
        previous_pid = ready['host_pid']
        # No auth/discovery payload is logged, only public renderer metadata.
        print('ROUND_PASS ' + json.dumps(ready), flush=True)
        if args.hover:
            capture_hover(base, ports[0], ready['host_pid'], iteration)
    print('SOURCE_FROZEN_SOURCE_PASS' if args.frozen_exe else ('DEV_REBUILD_PASS' if args.dev else 'DIRECT_ENTRY_PASS'), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--provider-python', required=True)
    parser.add_argument('--worker', type=Path)
    parser.add_argument('--dev', action='store_true')
    parser.add_argument('--wheel')
    parser.add_argument('--frozen-exe')
    parser.add_argument('--hover', action='store_true')
    args = parser.parse_args()
    if args.dev and not args.wheel:
        parser.error('--dev requires --wheel')
    if args.dev and args.frozen_exe:
        parser.error('--dev and --frozen-exe are separate probes')
    if args.worker:
        worker(args, args.worker)
        return
    import win32job
    base = Path(tempfile.mkdtemp(prefix='engram-joint-runtime-'))
    for relative in ('.engram', 'AppData/Roaming', 'AppData/Local', 'db/docs', 'codex', 'claude'):
        (base / relative).mkdir(parents=True, exist_ok=True)
    sockets = [socket.socket() for _ in range(3)]
    for item in sockets:
        item.bind(('127.0.0.1', 0))
    ports = [item.getsockname()[1] for item in sockets]
    for item in sockets:
        item.close()
    config = {'db': {'root_dir': str(base / 'db')},
              'overlay': {'stm_server_port': ports[0], 'external_renderer': {'selected_renderer_id': 'engram.bolttagu-2d', 'mode': 'replace'}},
              'mcp': {'http_port': ports[1]}, 'dashboard': {'enabled': False, 'port': ports[2]},
              'bubble': {'initiative': {'enabled': False}}, 'discord': {'enabled': False}}
    for filename in ('user.config.yaml', 'overlay.user.yaml'):
        (base / '.engram' / filename).write_text(json.dumps(config), encoding='utf-8')
    env = dict(os.environ, HOME=str(base), USERPROFILE=str(base), APPDATA=str(base/'AppData/Roaming'),
               LOCALAPPDATA=str(base/'AppData/Local'), ENGRAM_SMOKE_DB_DIR=str(base/'db'),
               CODEX_HOME=str(base/'codex'), CLAUDE_CONFIG_DIR=str(base/'claude'), PYTHONUTF8='1', PYTHONPATH=str(ROOT))
    for name in ('ENGRAM_RUNTIME_ROLE', 'ENGRAM_STM_PORT', 'ENGRAM_BUILD_SMOKE'):
        env.pop(name, None)
    job = win32job.CreateJobObject(None, '')
    limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
    limits['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
    worker_args = [sys.executable, str(Path(__file__).resolve()), '--worker', str(base), '--provider-python', args.provider_python]
    if args.dev:
        worker_args += ['--dev', '--wheel', args.wheel]
    if args.frozen_exe:
        worker_args += ['--frozen-exe', args.frozen_exe]
    if args.hover:
        worker_args += ['--hover']
    child = subprocess.Popen(worker_args, env=env, cwd=ROOT,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        win32job.AssignProcessToJobObject(job, int(child._handle))
        child.stdin.write('GO\n')
        child.stdin.flush()
        output, _ = child.communicate(timeout=720 if args.dev or args.frozen_exe else 300)
        (base/'report.log').write_text(output, encoding='utf-8')
        print(output)
    finally:
        job.Close()
        child.wait(timeout=10)
    time.sleep(1)
    for port in ports:
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', port))
    print(f'JOB_CLEANUP_PASS profile={base}')
    sys.exit(child.returncode)


if __name__ == '__main__':
    main()
