"""Bounded frozen-EXE README capture; never sends an AI/provider prompt."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import traceback
from collections import deque
from urllib.request import Request, urlopen

EXE_HASH = "590f44f1ac28b016eb1b2920a2cfdd69e0195e33fbe86dd18bc7d62162280818"


def main(exe: Path, mapping: Path, output: Path, visual_config: Path) -> int:
    if os.name != "nt" or os.environ.get("ENGRAM_BUILD_SMOKE") != "1":
        raise RuntimeError("Run inside Enter-EngramBuildSmokeProfile only")
    profile = Path(os.environ["USERPROFILE"]).resolve()
    db = Path(os.environ["ENGRAM_SMOKE_DB_DIR"]).resolve()
    if not profile.name.startswith("engram-build-smoke-") or db.parent != profile:
        raise RuntimeError("unexpected smoke profile boundary")
    if hashlib.sha256(exe.read_bytes()).hexdigest() != EXE_HASH:
        raise RuntimeError("not the approved v1.5.15.736 executable")
    if not mapping.is_file():
        raise FileNotFoundError(mapping)
    import psutil
    import win32api
    import win32con
    import win32gui
    import win32process
    import win32job
    import win32ui
    from PIL import Image
    import yaml

    owned_job=win32job.CreateJobObject(None,'')
    limits=win32job.QueryInformationJobObject(owned_job,win32job.JobObjectExtendedLimitInformation)
    limits['BasicLimitInformation']['LimitFlags']|=win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    win32job.SetInformationJobObject(owned_job,win32job.JobObjectExtendedLimitInformation,limits)
    win32job.AssignProcessToJobObject(owned_job,win32api.GetCurrentProcess())
    def capture_hwnd(hwnd):
        check('capture_owned',win32process.GetWindowThreadProcessId(hwnd)[1]==discovery['pid'])
        left,top,right,bottom=win32gui.GetWindowRect(hwnd);width,height=right-left,bottom-top
        dc=win32gui.GetWindowDC(hwnd);source=win32ui.CreateDCFromHandle(dc);target=source.CreateCompatibleDC()
        bitmap=win32ui.CreateBitmap();bitmap.CreateCompatibleBitmap(source,width,height);target.SelectObject(bitmap)
        try:
            if not ctypes.windll.user32.PrintWindow(hwnd,target.GetSafeHdc(),2):raise RuntimeError('PrintWindow failed')
            return Image.frombuffer('RGB',(width,height),bitmap.GetBitmapBits(True),'raw','BGRX',0,1)
        finally:
            win32gui.DeleteObject(bitmap.GetHandle());target.DeleteDC();source.DeleteDC();win32gui.ReleaseDC(hwnd,dc)
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    output.mkdir(parents=True, exist_ok=True)
    owned_mapping = profile / ".engram" / "native-bolttagu" / "mappings" / "trickcal-readme.json"
    owned_mapping.parent.mkdir(parents=True, exist_ok=True)
    demo_mapping=json.loads(mapping.read_text(encoding='utf-8'))
    overrides={'categories':{'write':'trickcal-writing','read':'trickcal-searching','communication':'trickcal-speaking'},'oneshots':{'success':'trickcal-success'}}
    for section,entries in overrides.items():demo_mapping.setdefault(section,{}).update(entries)
    for key,value in list(demo_mapping.get('lifecycle',{}).items()):
        if not value.startswith('trickcal-'):
            demo_mapping['lifecycle'][key]='trickcal-'+value
            overrides.setdefault('lifecycle',{})[key]='trickcal-'+value
    owned_mapping.write_text(json.dumps(demo_mapping,ensure_ascii=False,indent=2),encoding='utf-8')
    cfg_path = profile / ".engram" / "overlay.user.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    visual_source=yaml.safe_load(visual_config.read_text(encoding='utf-8')) or {}
    visual_settings={}
    for section,keys in {'overlay':['char_height_ratio'],'terminal':['base_font_size'],'bubble':['font_size','font_family','theme','thought_detail']}.items():
        selected={key:visual_source.get(section,{}).get(key) for key in keys if key in visual_source.get(section,{})}
        cfg.setdefault(section,{}).update(selected);visual_settings[section]=selected
    cfg.setdefault("overlay", {}).setdefault("character", {}).update({
        "source_mode": "native_bolttagu", "bolttagu": {"mapping_path": str(owned_mapping), "face_pointer": True, "show_floor": False},
    })
    cfg.setdefault("bubble", {}).setdefault("initiative", {})["enabled"] = False
    cfg["overlay"]["chat_mode"] = "bubble"
    demo_workdir=profile/'demo-project'; demo_workdir.mkdir(exist_ok=True)
    cfg.setdefault("cli", {})["workdir"] = str(demo_workdir)
    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")

    report = {"runtime": "frozen-normal-launch", "exe_sha256": EXE_HASH,"visual_settings":visual_settings,
              'demo_mapping_sha256':hashlib.sha256(owned_mapping.read_bytes()).hexdigest(),'demo_mapping_overrides':overrides,
              "mapping_sha256": hashlib.sha256(mapping.read_bytes()).hexdigest(), "checks": [],
              "captures": [], "limitations": ["No provider query submitted.", "State labels are safe synthetic metadata.", "Owned HWND PrintWindow composition on neutral matte, not a desktop screenshot."]}
    process = discovery = None
    def check(name, value):
        report["checks"].append({"name": name, "pass": bool(value)})
        if not value: raise AssertionError(name)
    def wait(predicate, timeout=45):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            value = predicate()
            if value: return value
            if process and process.poll() is not None: raise RuntimeError("frozen host exited")
            time.sleep(.15)
        raise TimeoutError("timed out waiting for frozen UI")
    def request(path, payload=None, lifecycle=None):
        headers = {"Authorization": "Bearer " + discovery["token"], "Content-Type": "application/json"}
        if lifecycle: headers["X-Engram-Lifecycle"] = lifecycle
        req = Request(f"http://127.0.0.1:{discovery['port']}{path}", data=json.dumps(payload).encode() if payload is not None else None, headers=headers)
        with urlopen(req, timeout=5) as response: return json.load(response)
    def owned_windows():
        found=[]
        def collect(hwnd, _):
            if win32gui.IsWindowVisible(hwnd) and win32process.GetWindowThreadProcessId(hwnd)[1] == discovery["pid"]:
                found.append(hwnd)
        win32gui.EnumWindows(collect, None)
        return found
    def edge_alpha(image):
        image=image.convert("RGBA"); pix=image.load(); w,h=image.size; transparent=set(); q=deque()
        for x in range(w): q.extend(((x,0),(x,h-1)))
        for y in range(h): q.extend(((0,y),(w-1,y)))
        while q:
            x,y=q.popleft()
            if (x,y) in transparent or not (0<=x<w and 0<=y<h): continue
            r,g,b,a=pix[x,y]
            if max(r,g,b)>4: continue
            transparent.add((x,y)); q.extend(((x+1,y),(x-1,y),(x,y+1),(x,y-1)))
        for x,y in transparent: pix[x,y]=(0,0,0,0)
        return image
    def compose(name, hwnds):
        records=[]
        for hwnd in hwnds:
            rect=win32gui.GetWindowRect(hwnd); records.append((hwnd,rect,capture_hwnd(hwnd)))
        left=min(r[1][0] for r in records); top=min(r[1][1] for r in records); right=max(r[1][2] for r in records); bottom=max(r[1][3] for r in records)
        canvas=Image.new("RGBA", (right-left+24,bottom-top+24), "#272727")
        for hwnd,rect,image in reversed(records):
            frame=edge_alpha(image); canvas.alpha_composite(frame,(rect[0]-left+12,rect[1]-top+12))
        canvas.convert("RGB").save(output/name)
        report["captures"].append({"name":name,"windows":[{"hwnd":h,"rect":list(r),"class":win32gui.GetClassName(h)} for h,r,_ in records]})
    try:
        with (output/"host.log").open("wb") as log:
            child_env=os.environ.copy(); child_env.pop("DISCORD_BOT_TOKEN", None)
            process=subprocess.Popen([str(exe)],cwd=str(exe.parent),stdout=log,stderr=log,env=child_env,creationflags=subprocess.CREATE_NO_WINDOW)
        path=profile/".engram"/"overlay-state-api-v1.json"; wait(path.is_file); discovery=json.loads(path.read_text(encoding="utf-8"))
        renderer=request('/state/renderers'); discovery['pid']=int(renderer['pid'])
        check('native_renderer_selected', not renderer.get('selected_renderer_id'));report['host_pid']=discovery['pid']
        descendants=[psutil.Process(process.pid)]+psutil.Process(process.pid).children(recursive=True)
        check("host_owned", discovery["pid"] in {p.pid for p in descendants}); check("no_external_provider", not any("custom-overlay" in p.name().lower() for p in descendants))
        launcher=wait(lambda:[h for h in owned_windows() if (lambda r:r[2]-r[0]==52 and r[3]-r[1]==52)(win32gui.GetWindowRect(h))])[0]
        cursor=win32api.GetCursorPos(); rect=win32gui.GetWindowRect(launcher)
        try:
            point=(rect[0]+26,rect[1]+26); win32api.SetCursorPos(point); time.sleep(.2)
            check('launcher_click_owned',win32process.GetWindowThreadProcessId(win32gui.WindowFromPoint(point))[1]==discovery['pid'])
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN,0,0); time.sleep(.1); win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP,0,0)
        finally: win32api.SetCursorPos(cursor)
        character=wait(lambda: launcher if win32gui.GetWindowRect(launcher)[3]-win32gui.GetWindowRect(launcher)[1]>100 else None)
        time.sleep(2)
        idle_frames=[]
        for _ in range(12): idle_frames.append(capture_hwnd(character).convert('RGBA'));time.sleep(.125)
        for agent,label in [('Codex','API refactor'),('Copilot','Release checklist')]:
            request('/state',{'provider':'mcp','session_id':'readme-'+agent.lower(),'state':'unknown','agent_name':agent,'label':label})
        def active(seq, state='working', category=None):
            request('/state',{'provider':'mcp','session_id':'readme-claude','state':state,'agent_name':'Claude','label':'Project notes','semantic':{'seq':seq,'active_category':category,'events':[{'type':'tool.started','category':category}] if category else []}},'claude')
        active(1,category='search'); time.sleep(1)
        check('monitor_windows_visible', len(owned_windows())>=4)
        compose("session-monitor.png", owned_windows())
        # Capture actual character layers continuously while exercising only safe state metadata.
        frames=idle_frames
        for seq,state,category in [(2,'working','search'),(3,'working','write'),(4,'ready',None),(5,'unknown',None)]:
            active(seq,state,category)
            for _ in range(16): frames.append(capture_hwnd(character).convert("RGBA")); time.sleep(.125)
        frames[0].save(output/"native-trickcal.gif",save_all=True,append_images=frames[1:],duration=125,loop=0,disposal=2)
        frames[0].save(output/'native-trickcal.png')
        check("trickcal_gif_frames", len(frames) >= 48)
        # Open the real InputBar, type a safe fixture, but never submit a provider prompt.
        rect=win32gui.GetWindowRect(character); cursor=win32api.GetCursorPos()
        try:
            point=((rect[0]+rect[2])//2,(rect[1]+rect[3])//2);win32api.SetCursorPos(point);time.sleep(.2)
            check('character_click_owned',win32process.GetWindowThreadProcessId(win32gui.WindowFromPoint(point))[1]==discovery['pid'])
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN,0,0);time.sleep(.1);win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP,0,0)
        finally: win32api.SetCursorPos(cursor)
        time.sleep(2)
        class GUIINFO(ctypes.Structure):
            _fields_=[('cbSize',ctypes.c_uint),('flags',ctypes.c_uint)]+[(name,ctypes.c_void_p) for name in ('hwndActive','hwndFocus','hwndCapture','hwndMenuOwner','hwndMoveSize','hwndCaret')]+[('rcCaret',ctypes.c_long*4)]
        gui=GUIINFO();gui.cbSize=ctypes.sizeof(gui)
        ctypes.windll.user32.GetGUIThreadInfo(win32process.GetWindowThreadProcessId(character)[0],ctypes.byref(gui))
        focus=gui.hwndFocus
        check('bubble_input_focus_owned', bool(focus) and win32process.GetWindowThreadProcessId(focus)[1]==discovery['pid'])
        input_hwnd=win32gui.GetAncestor(focus,2)
        check('separate_input_window',input_hwnd!=character)
        for letter in 'What did we decide for this project?':win32gui.SendMessage(focus,win32con.WM_CHAR,ord(letter),0)
        rect=win32gui.GetWindowRect(character)
        win32gui.SetWindowPos(input_hwnd,win32con.HWND_TOPMOST,rect[2]+20,rect[1]+70,0,0,win32con.SWP_NOSIZE|win32con.SWP_NOACTIVATE)
        time.sleep(.3)
        compose("bubble-input.png", [input_hwnd,character])
        report['input_fixture']='What did we decide for this project? (not submitted)'
        report['bubble_windows']=[{'class':win32gui.GetClassName(h),'rect':list(win32gui.GetWindowRect(h))} for h in owned_windows()]
        check('mapping_source_unchanged',hashlib.sha256(mapping.read_bytes()).hexdigest()==report['mapping_sha256'])
        report["result"]="PASS: bounded frozen Trickcal README capture"
    except Exception:
        report["result"]="FAIL"; report["error"]=traceback.format_exc()
    finally:
        if process and process.poll() is None:
            try: request("/shutdown",{}); process.wait(timeout=30); report["graceful_exit"]=process.returncode
            except subprocess.TimeoutExpired:
                report['cleanup_mode']='owned Job Object closes probe descendants at probe exit'
                report['shutdown_timeout_seconds']=30
            except Exception: report["cleanup_error"]=traceback.format_exc()
        (output/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"result":report["result"],"output":str(output)})); return 0 if report["result"].startswith("PASS") and "cleanup_error" not in report else 1

if __name__ == "__main__":
    p=argparse.ArgumentParser(); p.add_argument("--exe",type=Path,required=True); p.add_argument("--mapping",type=Path,required=True); p.add_argument("--output",type=Path,required=True); p.add_argument("--visual-config",type=Path,required=True); a=p.parse_args(); raise SystemExit(main(a.exe.resolve(),a.mapping.resolve(),a.output.resolve(),a.visual_config.resolve()))
