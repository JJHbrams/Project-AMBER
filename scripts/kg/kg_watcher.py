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


def _try_semantic_sync_via_mcp() -> bool:
    """MCP 서버가 살아있으면 /kg_sync 엔드포인트로 sync 위임.
    성공하면 True, 서버 없거나 실패하면 False 반환.
    KuzuDB single-writer 제약 회피용.
    """
    try:
        import json
        req = urllib.request.Request(
            f"{_MCP_BASE_URL}/kg_sync",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            sem = result.get("semantic", {})
            logger.info(
                "MCP kg_sync 완료 — KGNode=%s reembedded=%s",
                sem.get("nodes", "?"),
                sem.get("reembedded", "?"),
            )
            return True
    except Exception as exc:
        logger.debug("MCP 서버 경유 실패 (%s) — 직접 처리로 폴백", exc)
        return False


# ── Vault 싱크 ────────────────────────────────────────────


def _do_sync(vault_path: Path) -> None:
    """Vault 전체 싱크 — SQLite + KuzuDB (MCP 서버 경유 우선)"""
    try:
        from core.knowledge_graph import get_kg

        docs_dir = vault_path / "docs"
        kg = get_kg()
        synced = skipped = 0
        for f in docs_dir.rglob("*.md"):
            if "_templates" in f.parts:
                continue
            nid = kg.sync_file(f, docs_dir)
            if nid:
                synced += 1
            else:
                skipped += 1
        kg.resolve_links(docs_dir)
        logger.info("SQLite KG 싱크 완료 — %d개 동기화, %d개 건너뜀", synced, skipped)

        # 시맨틱 싱크: MCP 서버가 살아있으면 위임, 아니면 직접 처리
        if not _try_semantic_sync_via_mcp():
            from core.semantic_graph import get_semantic_graph
            sg = get_semantic_graph()
            sem = sg.sync_from_kg()
            logger.info(
                "직접 시맨틱 싱크 완료 — 노드 %d개 (재임베딩 %d개), 엣지 %d개",
                sem.get("nodes", 0),
                sem.get("reembedded", 0),
                sem.get("edges", 0),
            )
    except Exception as exc:
        logger.error("싱크 실패: %s", exc, exc_info=True)


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
    """프로젝트 파일 한 개를 wiki docs/projects/<name>/ 로 복사 + KG 싱크."""
    dest_dir = vault_path / "docs" / "projects" / project_name
    dest_dir.mkdir(parents=True, exist_ok=True)

    dest_name = src_path.name if src_path.suffix == ".md" else src_path.name + ".md"
    dest_path = dest_dir / dest_name

    try:
        text = src_path.read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:
        logger.warning("파일 읽기 실패 %s: %s", src_path, exc)
        return False

    dest_path.write_text(
        _inject_or_update_frontmatter(text, src_path, project_name),
        encoding="utf-8",
    )

    try:
        from core.knowledge_graph import get_kg
        from core.semantic_graph import get_semantic_graph

        docs_dir = vault_path / "docs"
        kg = get_kg()
        nid = kg.sync_file(dest_path, docs_dir)
        kg.resolve_links(docs_dir)
        if nid:
            # 시맨틱 싱크: MCP 서버가 살아있으면 위임, 아니면 직접 처리
            if not _try_semantic_sync_via_mcp():
                get_semantic_graph().sync_from_kg()
            logger.info("✅ [%s] %s → wiki 동기화", project_name, src_path.name)
        return bool(nid)
    except Exception as exc:
        logger.error("KG 싱크 실패 (%s): %s", src_path, exc, exc_info=True)
        return False


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
    def __init__(self, vault_path: Path, delay: float = 3.0):
        self.vault_path = vault_path
        self.delay = delay
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def schedule(self) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(self.delay, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        logger.info("변경 감지 → 싱크 시작...")
        _do_sync(self.vault_path)


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


def main() -> None:
    parser = argparse.ArgumentParser(description="LLM Wiki 워처 — vault 싱크 + workspace git repo 개념 파일 자동 동기화")
    parser.add_argument("--vault", default=None)
    parser.add_argument("--debounce", type=float, default=3.0)
    args = parser.parse_args()

    if not _acquire_singleton_lock(_WATCHER_LOCK_PATH):
        return

    if args.vault:
        vault_path = Path(args.vault)
    else:
        from core.runtime_config import get_db_root_dir

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

    from core.runtime_config import get_watch_workspaces, get_watch_conceptual_files

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
