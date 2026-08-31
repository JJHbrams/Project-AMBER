"""원격(SSH 리버스 터널) 세션에 skill 과 SessionStart hook 을 배치한다.

hook 과 skill 은 MCP 로 넘어가지 않는다 — 둘 다 순수 클라이언트 사이드 기능이라
원격 머신의 파일시스템에 물건이 있어야 한다. 터널이 옮겨주는 것은 MCP 엔드포인트
하나뿐이다. 그래서 원격에 "무엇을" 놓을지는 여기서 렌더링하고, 실제 배치는 이 모듈이
만들어낸 파이썬 스크립트가 원격에서 실행되며 수행한다.

렌더링을 파이썬에 두는 이유는 단일 출처다. SessionStart 지시문은
``build_bootstrap_directive`` 한 곳에서만 나오고, 원격/로컬이 같은 문구를 쓴다.
PowerShell(`scripts/setup-remote.ps1`) 은 바이트를 옮기는 역할만 한다.

원격에 배치하는 것:

- ``~/.claude/skills/<name>/SKILL.md`` — MCP 도구만 쓰는 skill 만 고른다.
  로컬 바이너리나 터널에 실려 있지 않은 포트를 때리는 skill 은 원격에서 실패하며,
  실패하는 절차를 쥐어주면 모델이 그걸 시도하다 막힌다.
- ``~/.engram/engram-sessionstart-hook.sh`` + ``~/.claude/settings.json`` 등록 —
  stdout 이 그대로 세션 컨텍스트에 붙는 동작을 쓴다. 백엔드 호출이 없으므로
  원격에 런타임 의존성이 생기지 않는다(sh 만 있으면 된다).

PreToolUse 정책 hook은 원격에도 배치한다. 다만 서버의
``classify_agent_pretool_payload``를 호출하지 않는다. 렌더된 표준 라이브러리 hook이
원격 파일시스템에서 git root와 현재 branch를 직접 판별한다.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

from core.integrations.engram_bootstrap import (
    SESSIONSTART_HOOK_MARKER,
    build_bootstrap_directive,
    render_session_start_powershell_script,
)
from core.integrations.remote_agent_policy import policy_payload

# MCP 도구만 사용하는 skill — 원격에서 그대로 동작한다.
#
# 제외한 것과 이유:
#   engram-new-session : overlay bubble HTTP(127.0.0.1:17384) 를 때린다. 그 포트는
#                        리버스 터널에 실려 있지 않아 원격에서 ConnectionRefused 다.
#   engram             : Copilot 전용 skill 이다.
REMOTE_SAFE_SKILLS = (
    "orchestrate",
    "engram-task-workflow",
    "engram-wiki-workflow",
    "engram-close-session",
)

# 원격 hook 스크립트 경로. 로컬 PowerShell 판과 같은 marker 를 쓴다 —
# settings.json 병합이 marker 로 기존 항목을 걷어내므로 여기서 갈리면 중복이 쌓인다.
REMOTE_SKILLS_RELDIR = ".claude/skills"

logger = logging.getLogger(__name__)

# 어느 호스트에 무엇을 배치했는지의 로컬 기록. 자동 갱신이 ssh 를 띄우기 전에
# 이 파일만 읽고 "바뀐 게 없으면 아무것도 하지 않는다"를 판정한다.
_STATE_PATH = Path.home() / ".engram" / "remote-provisioned.json"

# 원격 OS 별 hook 파일과 실행 명령. setup-remote.ps1 이 이미 POSIX/Windows 원격을
# 모두 지원하므로 배치도 양쪽을 낸다. 명령 형식은 로컬 ``_hook_command`` 와 같다.
_HOOK_TARGETS = {
    "posix": (".engram/engram-sessionstart-hook.sh", '/bin/sh "{path}"'),
    "windows": (
        ".engram/engram-sessionstart-hook.ps1",
        'powershell -NoProfile -ExecutionPolicy Bypass -File "{path}"',
    ),
}


def render_session_start_script(
    *,
    remote_os: str = "posix",
    caller: str = "claude-code",
    scope_key: str = "overlay",
) -> str:
    """원격 SessionStart hook 이 실행하는 스크립트.

    로컬 PowerShell 판(``_render_hook_script``)과 같은 지시문을 뱉는다. cwd 는 hook
    실행 시점의 현재 디렉토리로 채운다 — 원격 경로라 서버에서 프로젝트를 판별하지는
    못하지만, 스코프는 토큰에 묶여 있으므로(``RemotePrincipal.scope``) 문제가 없다.

    지시문에는 큰따옴표가 없어 sh 이중 인용으로 안전하게 감쌀 수 있다($dir 만 확장).
    """
    if remote_os == "windows":
        # Windows 원격은 로컬과 완전히 같은 스크립트를 쓴다.
        return render_session_start_powershell_script()
    directive = build_bootstrap_directive(caller=caller, scope_key=scope_key, cwd="$dir")
    return (
        "#!/bin/sh\n"
        f"# {SESSIONSTART_HOOK_MARKER} — engram 원격 배치 스크립트가 생성/관리한다. 직접 편집 금지.\n"
        "# stdout 이 그대로 세션 컨텍스트에 추가된다. 백엔드 호출 없음.\n"
        'dir=$(pwd)\n'
        f'echo "{directive}"\n'
    )


def resolve_skills_root(explicit: Path | str | None = None) -> Path | None:
    """``.github/skills`` 를 담은 디렉토리를 찾는다.

    소스 실행이면 리포 루트, 설치본이면 ``{app}`` 이다(exe 는 ``{app}/dist/engram-overlay``
    에 있고 skill 은 ``{app}/.github/skills`` 에 깔린다 — ``engram-overlay.iss`` 참조).
    양쪽 다 "위로 올라가며 찾기"로 커버된다.
    """
    if explicit:
        candidate = Path(explicit)
        return candidate if (candidate / ".github" / "skills").is_dir() else None

    starts: list[Path] = []
    if getattr(sys, "frozen", False):
        starts.append(Path(sys.executable).resolve().parent)
    starts.append(Path(__file__).resolve().parent)

    for start in starts:
        for base in (start, *start.parents):
            if (base / ".github" / "skills").is_dir():
                return base
    return None


def collect_remote_skills(repo_root: Path | str) -> dict[str, str]:
    """배치할 skill 의 ``{name: SKILL.md 본문}``.

    원본은 리포의 ``.github/skills`` — 로컬 설치(``installer/configure.ps1``)가 쓰는
    것과 같은 출처다. 원격용으로 내용을 고쳐 보내지 않는다. 포크를 만들면 로컬과
    원격의 절차가 갈리고, 갈린 쪽은 조용히 낡는다.
    """
    source_dir = Path(repo_root) / ".github" / "skills"
    collected: dict[str, str] = {}
    for name in REMOTE_SAFE_SKILLS:
        skill_file = source_dir / name / "SKILL.md"
        if not skill_file.exists():
            continue
        collected[name] = skill_file.read_text(encoding="utf-8")
    return collected


def build_remote_payload(
    repo_root: Path | str,
    *,
    remote_os: str = "posix",
    caller: str = "claude-code",
    scope_key: str = "overlay",
) -> dict[str, object]:
    if remote_os not in _HOOK_TARGETS:
        raise ValueError(f"unsupported remote_os '{remote_os}'")
    hook_relpath, hook_command_template = _HOOK_TARGETS[remote_os]
    return {
        "marker": SESSIONSTART_HOOK_MARKER,
        "hook_relpath": hook_relpath,
        "hook_command_template": hook_command_template,
        "skills_reldir": REMOTE_SKILLS_RELDIR,
        "hook_script": render_session_start_script(
            remote_os=remote_os, caller=caller, scope_key=scope_key
        ),
        "skills": collect_remote_skills(repo_root),
        # Remote policy is self-contained: it resolves the remote git root and
        # never calls the local/server classifier with unusable remote paths.
        "policy_hooks": policy_payload(remote_os),
    }


def payload_fingerprint(payload: dict[str, object]) -> str:
    """배치 내용의 지문. 자동 갱신은 이게 바뀌었을 때만 ssh 를 띄운다."""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()[:16]


def load_records() -> dict[str, dict]:
    try:
        data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def record_provisioned(
    host: str,
    *,
    fingerprint: str,
    remote_python: str,
    remote_os: str,
) -> None:
    """어느 호스트에 무엇을 배치했는지 기록한다.

    첫 배치는 ``scripts/setup-remote.ps1`` 이 한다 — 토큰 전송과 터널 실측이 거기 있고,
    그 두 개를 백그라운드에서 조용히 할 물건이 아니다. 이 기록이 있는 호스트만
    이후 터널 연결 시 자동 갱신 대상이 된다.
    """
    records = load_records()
    records[host] = {
        "fingerprint": fingerprint,
        "remote_python": remote_python,
        "remote_os": remote_os,
        "at": int(time.time()),
    }
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_STATE_PATH)


def _ssh_command(host: str, remote_python: str, installer_b64: str, remote_os: str) -> list[str]:
    # 경로 인용은 원격 셸에 맞춘다. base64 는 영숫자와 +/= 뿐이라 메타문자가 없다.
    # cmd.exe 는 작은따옴표를 인용으로 보지 않으므로 Windows 원격은 큰따옴표를 쓴다.
    code = "import base64;exec(base64.b64decode('" + installer_b64 + "'))"
    quote = '"' if remote_os == "windows" else "'"
    inner = quote + remote_python + quote + ' -c "' + code + '"'
    return [
        "ssh",
        "-o", "BatchMode=yes",          # 백그라운드다. 비밀번호를 물을 수 없다.
        "-o", "ConnectTimeout=10",
        host,
        inner,
    ]


def provision_host(
    host: str,
    *,
    skills_root: Path | str | None = None,
    force: bool = False,
    timeout: float = 60.0,
) -> dict[str, object]:
    """기록된 호스트의 배치를 갱신한다. 바뀐 게 없으면 ssh 를 띄우지 않는다.

    실패는 조용히 넘기지 않되 치명적으로 다루지도 않는다 — 터널 상태와 무관하다.
    """
    record = load_records().get(host)
    if not isinstance(record, dict) or not record.get("remote_python"):
        return {"ok": False, "skipped": True, "reason": "not provisioned yet"}

    root = resolve_skills_root(skills_root)
    if root is None:
        return {"ok": False, "skipped": True, "reason": "skills source not found"}

    remote_os = str(record.get("remote_os") or "posix")
    try:
        payload = build_remote_payload(root, remote_os=remote_os)
    except ValueError as exc:
        return {"ok": False, "skipped": True, "reason": str(exc)}
    if not payload["skills"]:
        return {"ok": False, "skipped": True, "reason": "no skills collected"}

    fingerprint = payload_fingerprint(payload)
    if not force and fingerprint == str(record.get("fingerprint") or ""):
        return {"ok": True, "skipped": True, "reason": "unchanged", "fingerprint": fingerprint}

    cmd = _ssh_command(host, str(record["remote_python"]), encode_payload_script(), remote_os)
    try:
        completed = subprocess.run(
            cmd,
            input=encode_payload(payload),
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        return {"ok": False, "skipped": False, "reason": f"ssh failed: {exc}"}

    stdout = completed.stdout or ""
    if "PROVISIONED" not in stdout:
        detail = (completed.stderr or stdout or "").strip().splitlines()
        return {
            "ok": False,
            "skipped": False,
            "reason": detail[-1] if detail else f"exit {completed.returncode}",
        }

    record_provisioned(
        host,
        fingerprint=fingerprint,
        remote_python=str(record["remote_python"]),
        remote_os=remote_os,
    )
    return {"ok": True, "skipped": False, "fingerprint": fingerprint}


def refresh_host_on_tunnel_up(host: str) -> None:
    """터널이 UP 으로 전이할 때 호출된다. 예외를 밖으로 내지 않는다."""
    try:
        result = provision_host(host)
    except Exception:
        logger.exception("[remote-provision] %s 자동 갱신 실패", host)
        return
    if result.get("skipped"):
        logger.debug("[remote-provision] %s 건너뜀 — %s", host, result.get("reason"))
    elif result.get("ok"):
        logger.info("[remote-provision] %s 갱신됨 (%s)", host, result.get("fingerprint"))
    else:
        logger.warning("[remote-provision] %s 갱신 실패 — %s", host, result.get("reason"))


# ── 원격에서 실행되는 배치 스크립트 ──────────────────────────────────────────
#
# 표준 라이브러리만 쓴다(json/pathlib/base64/os). 원격 파이썬이 무엇이든 돌아야 하고,
# setup-remote.ps1 이 이미 그 전제로 인터프리터를 고른다.
#
# payload 는 base64 로 **stdin** 으로 넘긴다. argv 가 아닌 이유는 길이다 — skill 본문
# 네 개면 base64 가 수만 바이트가 되고, Windows CreateProcess 커맨드라인 한도(32767)를
# 넘긴다. stdin 은 한도가 없다. base64 를 쓰는 것은 한글 본문이 PowerShell → ssh 파이프
# 인코딩에서 깨지는 것을 막기 위한 것이고(이 리포가 이미 BOM·latin-1 로 데인 자리다),
# 비밀이 아니다 — 스킬 문서와 지시문뿐이다. 토큰은 여기 들어가지 않는다.
_REMOTE_INSTALLER = r'''
import base64, json, os, sys
from pathlib import Path

# stdin 은 bytes 로 읽는다. 텍스트로 읽으면 원격 파이썬이 자기 로케일 인코딩(cp949 등)으로
# 디코드하므로 UTF-8 BOM 3바이트가 깨진 2글자가 되어 문자 단위 BOM 제거를 그냥 통과한다.
# base64 는 ASCII 라 bytes 가 옳은 레벨이다.
raw = sys.stdin.buffer.read().strip()
if raw[:3] == bytes((0xEF, 0xBB, 0xBF)):
    raw = raw[3:].strip()
if not raw:
    print('PAYLOAD=EMPTY')
    raise SystemExit(1)
payload = json.loads(base64.b64decode(raw).decode("utf-8"))
home = Path.home()
marker = payload["marker"]
results = []

# 1) skills
skills_dir = home / payload["skills_reldir"]
for name, body in sorted((payload.get("skills") or {}).items()):
    target = skills_dir / name
    target.mkdir(parents=True, exist_ok=True)
    (target / "SKILL.md").write_text(body, encoding="utf-8", newline="\n")
    results.append("SKILL=%s" % name)

# 2) SessionStart hook 스크립트
hook_path = home / payload["hook_relpath"]
hook_path.parent.mkdir(parents=True, exist_ok=True)
hook_path.write_text(payload["hook_script"], encoding="utf-8", newline="\n")
try:
    hook_path.chmod(0o700)
except Exception:
    pass
results.append("HOOK=%s" % hook_path)

# 3) ~/.claude/settings.json 의 SessionStart 등록 (marker 기준 멱등)
#
# 이 파일은 engram 전용이 아니다 — 다른 도구의 hook 과 사용자 설정(model, theme 등)이
# 같이 들어 있다. marker 로 engram 항목만 걷어내고 나머지는 보존하지만, 병합 전에
# 백업 한 부를 남긴다. 남의 물건을 고치는 자리라 값싼 보험을 든다.
settings_path = home / ".claude" / "settings.json"
if settings_path.exists():
    backup = settings_path.with_name(settings_path.name + ".engram-bak")
    try:
        backup.write_bytes(settings_path.read_bytes())
        results.append("BACKUP=%s" % backup)
    except Exception as exc:
        # 백업을 못 뜨면 병합하지 않는다 — 되돌릴 수 없는 변경을 하지 않는다.
        print("BACKUP=FAIL %s" % exc)
        raise SystemExit(1)
try:
    settings = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
except Exception:
    # 파싱 실패한 사용자 파일은 건드리지 않는다. 덮어쓰면 사용자 설정이 날아간다.
    print("SETTINGS=PARSE_FAIL")
    print("\n".join(results))
    raise SystemExit(1)
if not isinstance(settings, dict):
    print("SETTINGS=NOT_OBJECT")
    raise SystemExit(1)

hooks = settings.get("hooks")
if not isinstance(hooks, dict):
    hooks = {}
entries = hooks.get("SessionStart")
if not isinstance(entries, list):
    entries = []

# 기존 engram 항목을 먼저 걷어낸다 — 중복·구버전 정리.
kept = []
for entry in entries:
    if not isinstance(entry, dict):
        kept.append(entry)
        continue
    inner = entry.get("hooks")
    if not isinstance(inner, list):
        kept.append(entry)
        continue
    filtered = [h for h in inner
                if not (isinstance(h, dict) and marker in str(h.get("command", "")))]
    if not filtered:
        continue
    if len(filtered) == len(inner):
        kept.append(entry)
        continue
    updated = dict(entry)
    updated["hooks"] = filtered
    kept.append(updated)

kept.append({"hooks": [{
    "type": "command",
    "command": payload["hook_command_template"].format(path=hook_path),
    "timeout": 15,
}]})
hooks["SessionStart"] = kept
settings["hooks"] = hooks

settings_path.parent.mkdir(parents=True, exist_ok=True)
tmp = settings_path.with_suffix(".json.engram-tmp")
tmp.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n",
               encoding="utf-8", newline="\n")
os.replace(str(tmp), str(settings_path))

# 되읽어 검증한다 — ssh 를 또 부르면 비밀번호를 또 묻는다.
back = json.loads(settings_path.read_text(encoding="utf-8"))
registered = sum(
    1
    for entry in back.get("hooks", {}).get("SessionStart", [])
    if isinstance(entry, dict)
    for h in (entry.get("hooks") or [])
    if isinstance(h, dict) and marker in str(h.get("command", ""))
)
results.append("SETTINGS=%s registered=%d" % (settings_path, registered))

# 4) Remote-local PreToolUse policy hooks.  These scripts deliberately make
# their git decision on this machine; do not route them through Engram's local
# classifier.  Each independently-owned user config is backup-first, marker
# deduped, atomically replaced, and read back before success is reported.
def atomic_json(path, value):
    tmp = path.with_name(path.name + ".engram-tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    os.replace(str(tmp), str(path))
    back = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(back, dict): raise ValueError("readback is not object")
    return back
def load_backup(path):
    if path.exists():
        backup=path.with_name(path.name + ".engram-bak")
        if not backup.exists():
            try: backup.write_bytes(path.read_bytes())
            except Exception as exc: raise RuntimeError("BACKUP=FAIL %s" % exc)
        try: value=json.loads(path.read_text(encoding="utf-8"))
        except Exception: raise RuntimeError("SETTINGS=PARSE_FAIL")
        if not isinstance(value, dict): raise RuntimeError("SETTINGS=NOT_OBJECT")
        return value
    return {}
def managed_command(spec): return spec["command"]
def merge_pretool(path, provider, spec):
    value=load_backup(path)
    hooks=value.get("hooks")
    if not isinstance(hooks, dict): hooks={}
    entries=hooks.get("PreToolUse")
    if not isinstance(entries, list): entries=[]
    kept=[]
    for entry in entries:
        if not isinstance(entry, dict): kept.append(entry); continue
        inner=entry.get("hooks")
        if not isinstance(inner, list): kept.append(entry); continue
        clean=[h for h in inner if not (isinstance(h, dict) and spec["marker"] in str(h.get("command", "")))]
        if clean:
            copy=dict(entry); copy["hooks"]=clean; kept.append(copy)
    kept.append({"matcher":"*", "hooks":[{"type":"command","command":managed_command(spec),"timeout":15}]})
    hooks["PreToolUse"]=kept; value["hooks"]=hooks
    path.parent.mkdir(parents=True, exist_ok=True); atomic_json(path, value)
def merge_antigravity(path, spec):
    value=load_backup(path)
    value.pop(spec["marker"], None)
    value[spec["marker"]]={"PreToolUse":[{"matcher":"run_command|write_to_file|replace_file_content|multi_replace_file_content","hooks":[{"type":"command","command":managed_command(spec),"timeout":15}]}]}
    path.parent.mkdir(parents=True, exist_ok=True); atomic_json(path, value)
for provider, spec in sorted((payload.get("policy_hooks") or {}).items()):
    hook=home / spec["relpath"]
    hook.parent.mkdir(parents=True, exist_ok=True)
    temp=hook.with_name(hook.name + ".engram-tmp")
    temp.write_text(spec["body"], encoding="utf-8", newline="\n"); os.replace(str(temp), str(hook))
    # The enrolled interpreter can be /opt/conda/bin/python and Windows often
    # has no PATH python.  Persist exactly the interpreter and absolute script
    # selected on the remote host, never a relative .engram command.
    spec=dict(spec)
    spec["command"]='"%s" "%s" --provider %s' % (sys.executable, hook, provider)
    try: hook.chmod(0o700)
    except Exception: pass
    try:
        if provider == "claude-code": merge_pretool(home / ".claude" / "settings.json", provider, spec)
        elif provider == "codex": merge_pretool(home / ".codex" / "hooks.json", provider, spec)
        elif provider == "antigravity": merge_antigravity(home / ".gemini" / "config" / "hooks.json", spec)
        else: raise RuntimeError("unknown policy provider")
    except Exception as exc:
        print("POLICY=%s FAIL %s" % (provider, exc)); raise SystemExit(1)
    results.append("POLICY=%s" % provider)
print("\n".join(results))
print("PROVISIONED")
'''


def render_remote_installer() -> str:
    """원격 파이썬으로 실행할 배치 스크립트 본문."""
    return _REMOTE_INSTALLER


def encode_payload_script() -> str:
    """배치 스크립트 본문의 base64. ssh 명령줄에 실린다."""
    return base64.b64encode(render_remote_installer().encode("utf-8")).decode("ascii")


def encode_payload(payload: dict[str, object]) -> str:
    """배치 스크립트 argv 에 실을 base64 payload."""
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def main(argv: list[str] | None = None) -> int:
    """setup-remote.ps1 이 쓰는 CLI.

    ``--emit installer`` 는 원격에서 실행할 스크립트를 base64 로, ``--emit payload`` 는
    그 스크립트가 stdin 으로 받을 payload 를 base64 로 낸다. 둘 다 ASCII 라 PowerShell
    파이프 인코딩에 영향을 받지 않는다.
    """
    import argparse

    parser = argparse.ArgumentParser(description="Emit the remote session provisioning payload")
    parser.add_argument(
        "--emit", required=True, choices=("installer", "payload", "skills", "record")
    )
    parser.add_argument("--host", default="", help="--emit record 용 ssh 호스트")
    parser.add_argument("--remote-python", default="", help="--emit record 용 원격 파이썬 경로")
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--scope-key", default="overlay")
    parser.add_argument("--remote-os", default="posix", choices=sorted(_HOOK_TARGETS))
    args = parser.parse_args(argv)

    if args.emit == "installer":
        print(base64.b64encode(render_remote_installer().encode("utf-8")).decode("ascii"))
        return 0
    if args.emit == "record":
        # 첫 배치 성공 뒤 setup-remote.ps1 이 호출한다. 이 기록이 있는 호스트만
        # 이후 터널 UP 때 자동 갱신 대상이 된다.
        if not args.host or not args.remote_python:
            print("MISSING_HOST_OR_PYTHON", file=sys.stderr)
            return 1
        payload = build_remote_payload(args.repo_root, remote_os=args.remote_os)
        record_provisioned(
            args.host,
            fingerprint=payload_fingerprint(payload),
            remote_python=args.remote_python,
            remote_os=args.remote_os,
        )
        print("RECORDED")
        return 0
    if args.emit == "skills":
        # 사람이 확인용으로 보는 목록. 실제로 수집된 것만 낸다.
        for name in sorted(collect_remote_skills(args.repo_root)):
            print(name)
        return 0
    payload = build_remote_payload(
        args.repo_root, remote_os=args.remote_os, scope_key=args.scope_key
    )
    if not payload["skills"]:
        print("NO_SKILLS_FOUND", file=sys.stderr)
        return 1
    print(encode_payload(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
