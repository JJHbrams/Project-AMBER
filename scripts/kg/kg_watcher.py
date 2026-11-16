"""
kg_watcher.py — LLM Wiki 파일 워처 데몬

감시 대상:
  1. Vault (D:\\intel_engram\\docs\\) — .md 파일 변경 시 kg_sync 실행
  2. Workspace 하위 git 프로젝트 — user.config.yaml의 watch_workspaces에 등록된
     디렉토리 아래 git repo들을 자동 탐색하여 개념 파일(README, architecture 등)
     변경 시 wiki로 자동 복사. 프로젝트명은 git repo 디렉토리명에서 자동 유도.

변경이 연속으로 발생할 때 디바운싱(기본 3초)으로 과도한 싱크를 방지한다.

사용법:
    conda run -n intel_engram python scripts/kg_watcher.py
    conda run -n intel_engram python scripts/kg_watcher.py --vault D:\\intel_engram --debounce 5
"""

import argparse
import atexit
import fnmatch
import logging
import re
import sys
import os
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [watcher] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("kg_watcher")

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))
os.chdir(_ROOT)

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_UPDATED_RE = re.compile(r"^(updated\s*:\s*).*$", re.MULTILINE)
_WATCHER_LOCK_PATH = Path.home() / ".engram" / "kg_watcher.lock"


def _is_process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    # Windows: os.kill(pid, 0) 은 존재확인이 아니라 TerminateProcess 를 시도하며(위험),
    # 죽은 PID 엔 SystemError(OSError 아님)를 던져 크래시한다. 종료된 process object는
    # OpenProcess가 잠시 성공할 수 있으므로 GetExitCodeProcess까지 확인해야 한다.
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        SYNCHRONIZE = 0x00100000
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(
            SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            int(pid),
        )
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return int(exit_code.value) == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 권한 부족은 프로세스가 존재한다는 의미로 간주
        return True
    except OSError:
        return False
    return True


def _acquire_singleton_lock(lock_path: Path) -> bool:
    """중복 실행 방지용 lock 파일을 획득한다."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    my_pid = os.getpid()

    def _cleanup() -> None:
        try:
            if not lock_path.exists():
                return
            text = lock_path.read_text(encoding="utf-8").strip()
            if text == str(my_pid):
                lock_path.unlink(missing_ok=True)
        except Exception:
            pass

    for _ in range(2):
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(str(my_pid))
            atexit.register(_cleanup)
            return True
        except FileExistsError:
            try:
                text = lock_path.read_text(encoding="utf-8").strip()
                running_pid = int(text) if text else 0
            except Exception:
                running_pid = 0

            if running_pid and _is_process_alive(running_pid):
                logger.info("이미 실행 중인 kg_watcher 감지 (PID=%d) — 중복 실행 종료", running_pid)
                return False

            try:
                lock_path.unlink(missing_ok=True)
            except Exception:
                logger.warning("stale lock 제거 실패: %s", lock_path)
                return False

    logger.warning("kg_watcher lock 획득 실패 — 중복 실행 방지를 위해 종료")
    return False


# ── MCP 서버 경유 시맨틱 싱크 ────────────────────────────────

_MCP_BASE_URL = "http://127.0.0.1:17385"


_MCP_SYNC_TIMEOUT = 300


def _sync_via_mcp() -> bool | None:
    """MCP 서버의 /kg_sync 로 SQLite+시맨틱 싱크 전체를 위임한다.

    True: 완료. False: 서버에 연결할 수 없음(직접 처리해야 함).
    None: 서버는 받았지만 응답이 늦거나 실패 — 서버 쪽 sync 가 계속 돌고 있을 수 있으니
    여기서 직접 처리하면 같은 DB 에 쓰기가 겹친다. 다음 변경 때 다시 맡긴다.
    """
    import json
    import socket
    import urllib.error

    req = urllib.request.Request(
        f"{_MCP_BASE_URL}/kg_sync",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_MCP_SYNC_TIMEOUT) as resp:
            result = json.loads(resp.read())
    except urllib.error.URLError as exc:
        # 요청이 아예 못 나간 경우만 "서버 없음"이다. 리셋·끊김은 서버가 요청을 받은 뒤일 수
        # 있다 — 과부하 서버도 WinError 10054 로 끊는다.
        if isinstance(exc.reason, (ConnectionRefusedError, socket.gaierror)):
            logger.debug("MCP 서버 연결 불가 (%s) — 직접 처리로 폴백", exc)
            return False
        logger.warning("MCP kg_sync 실패 (%s) — 직접 처리하지 않음", exc)
        return None
    except ConnectionRefusedError as exc:
        logger.debug("MCP 서버 연결 불가 (%s) — 직접 처리로 폴백", exc)
        return False
    except Exception as exc:
        logger.warning("MCP kg_sync 응답 없음 (%s) — 직접 처리하지 않음", exc)
        return None

    if result.get("busy"):
        # 앞선 sync 가 락을 쥐고 있어 이번 변경은 반영되지 않았다 — 나중에 다시 맡긴다.
        logger.info("MCP kg_sync busy — 잠시 뒤 재시도")
        return None

    sem = result.get("semantic", {})
    logger.info(
        "MCP kg_sync 완료 — 동기화 %s, 변경없음 %s, KGNode=%s reembedded=%s",
        result.get("synced", "?"),
        result.get("unchanged", "?"),
        sem.get("nodes", "?"),
        sem.get("reembedded", "?"),
    )
    return True


# ── Vault 싱크 ────────────────────────────────────────────


def _do_sync(vault_path: Path) -> bool:
    """Vault 증분 싱크 — MCP 서버에 위임하고, 서버가 없을 때만 직접 처리한다.

    직접 쓰기와 서버 쓰기가 같은 SQLite 를 동시에 잡지 않도록 한쪽만 쓴다.
    서버가 받았지만 반영을 확인 못 했으면 True(재시도 필요)를 돌려준다."""
    try:
        delegated = _sync_via_mcp()
        if delegated is None:
            return True
        if delegated:
            return False

        from core.graph.knowledge import get_kg

        res = get_kg().sync_vault(vault_path / "docs")
        logger.info(
            "SQLite KG 싱크 완료 — %d개 동기화, %d개 변경없음, %d개 건너뜀, %d개 고아 정리",
            res["synced"], res["unchanged"], res["skipped"], len(res["pruned"]),
        )

        from core.graph.semantic import get_semantic_graph, run_sg_coro
        sg = get_semantic_graph()
        sem = run_sg_coro(sg.sync_from_kg())
        logger.info(
            "직접 시맨틱 싱크 완료 — 노드 %d개 (재임베딩 %d개), 엣지 %d개",
            sem.get("nodes", 0),
            sem.get("reembedded", 0),
            sem.get("edges", 0),
        )
    except Exception as exc:
        logger.error("싱크 실패: %s", exc, exc_info=True)
    return False


# ── 프로젝트 파일 처리 ────────────────────────────────────


def _inject_or_update_frontmatter(text: str, src_path: Path, project_name: str) -> str:
    """frontmatter 없으면 주입, 있으면 updated 날짜만 갱신."""
    today = datetime.now().strftime("%Y-%m-%d")

    if _FRONTMATTER_RE.match(text):
        updated = _UPDATED_RE.sub(lambda m: f"{m.group(1)}{today}", text)
        if updated == text:
            text = text.replace("---\n", f"---\nupdated: {today}\n", 1)
            return text
        return updated

    title_match = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else src_path.stem.replace("-", " ").replace("_", " ").title()

    slug = re.sub(r"[^\w\s가-힣-]", "", title.lower())
    slug = re.sub(r"[\s_]+", "-", slug.strip())[:80]

    fm_lines = [
        "---",
        f"id: {slug}",
        f"title: {title}",
        "note_type: project",
        "tags:",
        f"  - {project_name.lower()}",
        f"  - {src_path.stem.lower()}",
        f"created: {today}",
        f"updated: {today}",
        "---",
        "",
    ]
    return "\n".join(fm_lines) + text


def _sync_project_file(src_path: Path, project_name: str, vault_path: Path) -> bool:
    """프로젝트 파일 한 개를 wiki docs/projects/<name>/ 로 복사한다.

    KG 반영은 직접 하지 않는다 — 복사본이 docs/ 아래에 생기면 vault 감시가 잡아
    _DebounceSync 한 경로로만 싱크한다(쓰기 경로가 둘이면 락을 서로 다툰다)."""
    dest_dir = vault_path / "docs" / "projects" / project_name
    dest_dir.mkdir(parents=True, exist_ok=True)

    dest_name = src_path.name if src_path.suffix == ".md" else src_path.name + ".md"
    dest_path = dest_dir / dest_name

    try:
        text = src_path.read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:
        logger.warning("파일 읽기 실패 %s: %s", src_path, exc)
        return False

    try:
        dest_path.write_text(
            _inject_or_update_frontmatter(text, src_path, project_name),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.error("wiki 복사 실패 (%s): %s", src_path, exc)
        return False
    logger.info("✅ [%s] %s → wiki 복사", project_name, src_path.name)
    return True


# ── Git repo 자동 탐색 ────────────────────────────────────


def _find_git_repos(workspace: Path, max_depth: int = 2) -> list[Path]:
    """workspace 하위에서 git repo 루트 목록을 반환 (최대 depth까지 탐색)."""
    repos = []

    def _scan(directory: Path, depth: int) -> None:
        if depth < 0:
            return
        try:
            if (directory / ".git").exists():
                repos.append(directory)
                return  # git repo 안은 더 탐색하지 않음
            for child in directory.iterdir():
                if child.is_dir() and not child.name.startswith("."):
                    _scan(child, depth - 1)
        except PermissionError:
            pass

    _scan(workspace, max_depth)
    return repos


# ── 디바운서 ──────────────────────────────────────────────


class _DebounceSync:
    RETRY_DELAY = 30.0

    def __init__(self, vault_path: Path, delay: float = 3.0):
        self.vault_path = vault_path
        self.delay = delay
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._running = False
        self._rerun = False

    def schedule(self, delay: float | None = None) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(self.delay if delay is None else delay, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        # 디바운스는 시작 전만 막는다. 싱크 중에 온 변경은 새 싱크를 겹쳐 띄우지 않고
        # 끝난 뒤 한 번 더 돌린다.
        with self._lock:
            if self._running:
                self._rerun = True
                return
            self._running = True
        try:
            while True:
                logger.info("변경 감지 → 싱크 시작...")
                retry = _do_sync(self.vault_path)
                with self._lock:
                    if not self._rerun:
                        self._running = False
                        break
                    self._rerun = False
            if retry:
                # 서버가 바빠 반영을 확인 못 했다 — 새 변경이 없어도 한 번 더 맡긴다.
                self.schedule(self.RETRY_DELAY)
        except BaseException:
            with self._lock:
                self._running = False
            raise


class _ProjectDebounce:
    def __init__(self, vault_path: Path, delay: float = 3.0):
        self.vault_path = vault_path
        self.delay = delay
        self._pending: dict[Path, str] = {}  # src_path → project_name
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def schedule(self, src_path: Path, project_name: str) -> None:
        with self._lock:
            self._pending[src_path] = project_name
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(self.delay, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        with self._lock:
            items = list(self._pending.items())
            self._pending.clear()
        for src_path, project_name in items:
            _sync_project_file(src_path, project_name, self.vault_path)


# ── 핸들러 팩토리 ─────────────────────────────────────────


def _make_vault_handler(debouncer: _DebounceSync):
    from watchdog.events import FileSystemEventHandler

    class _Handler(FileSystemEventHandler):
        def on_any_event(self, event):
            if event.is_directory:
                return
            src = str(getattr(event, "src_path", "") or "")
            if src.endswith(".md"):
                debouncer.schedule()

    return _Handler()


def _make_project_handler(
    debouncer: _ProjectDebounce,
    repo_root: Path,
    project_name: str,
    patterns: list[str],
):
    from watchdog.events import FileSystemEventHandler

    class _Handler(FileSystemEventHandler):
        def on_any_event(self, event):
            if event.is_directory:
                return
            src_path = Path(getattr(event, "src_path", "") or "")
            if not src_path.exists():
                return
            try:
                rel = src_path.relative_to(repo_root).as_posix()
            except ValueError:
                return
            if any(fnmatch.fnmatch(rel, p) for p in patterns):
                logger.debug("[%s] 변경 감지: %s", project_name, src_path.name)
                debouncer.schedule(src_path, project_name)

    return _Handler()


# ── 메인 ─────────────────────────────────────────────────


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="LLM Wiki 워처 — vault 싱크 + workspace git repo 개념 파일 자동 동기화")
    parser.add_argument("--vault", default=None)
    parser.add_argument("--debounce", type=float, default=3.0)
    args = parser.parse_args(argv)

    if not _acquire_singleton_lock(_WATCHER_LOCK_PATH):
        return

    if args.vault:
        vault_path = Path(args.vault)
    else:
        from core.config.runtime_config import get_db_root_dir

        vault_path = Path(get_db_root_dir())

    docs_dir = vault_path / "docs"
    if not docs_dir.exists():
        logger.error("docs 디렉토리 없음: %s", docs_dir)
        sys.exit(1)

    try:
        from watchdog.observers import Observer
    except ImportError:
        logger.error("watchdog 미설치. `pip install watchdog`")
        sys.exit(1)

    from core.config.runtime_config import get_watch_workspaces, get_watch_conceptual_files

    observer = Observer()
    proj_debouncer = _ProjectDebounce(vault_path, delay=args.debounce)

    # 1. Vault 감시
    vault_debouncer = _DebounceSync(vault_path, delay=args.debounce)
    observer.schedule(_make_vault_handler(vault_debouncer), str(docs_dir), recursive=True)
    logger.info("📡 Vault: %s", docs_dir)

    # 2. Workspace → git repo 자동 탐색
    patterns = get_watch_conceptual_files()
    watch_workspaces = get_watch_workspaces()
    registered = 0

    for ws_str in watch_workspaces:
        ws = Path(ws_str)
        if not ws.exists():
            logger.warning("workspace 경로 없음: %s", ws)
            continue
        repos = _find_git_repos(ws)
        for repo in repos:
            project_name = repo.name
            handler = _make_project_handler(proj_debouncer, repo, project_name, patterns)
            observer.schedule(handler, str(repo), recursive=True)
            logger.info("📡 [%s] %s", project_name, repo)
            registered += 1

    if not watch_workspaces:
        logger.info("ℹ️  watch_workspaces 미설정 — Vault만 감시")
    else:
        logger.info("개념 파일 패턴: %s | 프로젝트 %d개 등록", ", ".join(patterns), registered)

    observer.start()
    logger.info("Ctrl+C 로 종료")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
    logger.info("워처 종료")


if __name__ == "__main__":
    main()


