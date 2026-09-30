"""App-only payload handling for the code-only (fast) overlay build path.

The frozen overlay is a PyInstaller onedir built with ``noarchive=True``: every
application module already lives as a loose ``.pyc`` under ``_internal``.  This
module knows which of those files are application-owned (the TOC written by the
spec), how they depend on each other, whether a code change can be applied
without PyInstaller, and how to apply it without ever writing through a
hardlink shared with the source artifact.

Only the standard library is imported at module level so the frozen runtime
contract can use :func:`payload_digest_from_dir` cheaply.
"""

from __future__ import annotations

import ast
import fnmatch
import hashlib
import importlib.machinery
import json
import os
import py_compile
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Any, Iterable, Mapping

TOC_NAME = "engram-app-toc.json"
TOC_SCHEMA = 1
VERSION_SNAPSHOT_REL = "build/engram-version.json"

# Directories PyInstaller was told to search (spec pathex) for app modules.
APP_SEARCH_DIRS = ("", "scripts/kg")
BRIDGE_ENTRY = "scripts/engram_mcp_bridge.py"
_NON_APP_PARTS = {"site-packages", "__pycache__", ".venv", ".git", "node_modules"}

# Closure seeds: a change inside the forward import closure of a seed requires
# the matching frozen smoke role.  Patterns are repo-relative globs.
SMOKE_SEEDS: dict[str, tuple[str, ...]] = {
    "dashboard": ("core/dashboard/**",),
    "embedding": (
        "core/graph/semantic/**",
        "core/install/model_manifest.py",
        "resource/embedding-model/manifest.json",
    ),
    "smoke": ("mcp_server.py", "scripts/kg/kg_watcher.py", "overlay/main.py"),
}
# Changes here exercise everything, regardless of import closure.
SELECT_ALL_PATTERNS = ("config/*.yaml", "core/entrypoint.py", "engram_overlay_entry.py")


def _posix(value: str | os.PathLike[str]) -> str:
    return str(value).replace("\\", "/")


def internal_dir(artifact: Path) -> Path:
    """PyInstaller contents directory of an onedir artifact."""
    candidate = Path(artifact) / "_internal"
    return candidate if candidate.is_dir() else Path(artifact)


# ---------------------------------------------------------------- TOC ------


def app_relpath(root: Path, source: str | os.PathLike[str] | None) -> str | None:
    """Repo-relative posix path for an app-owned source, else ``None``."""
    if not source or str(source) == "-":
        return None
    # abspath/normcase only: resolve() per entry is far too slow for the ~30k
    # entries of a PyInstaller Analysis.
    base = os.path.normcase(os.path.abspath(root))
    candidate = os.path.normcase(os.path.abspath(source))
    if candidate != base and not candidate.startswith(base.rstrip("\\/") + os.sep):
        return None
    relative = os.path.relpath(os.path.abspath(source), os.path.abspath(root))
    parts = Path(relative).parts
    if not parts or parts[0] == ".." or {part.lower() for part in parts} & _NON_APP_PARTS:
        return None
    return Path(relative).as_posix()


def pyc_dest(module: str, source_rel: str) -> str:
    base = module.replace(".", "/")
    if Path(source_rel).name == "__init__.py":
        return f"{base}/__init__.pyc"
    return f"{base}.pyc"


def _dfile(entry: Mapping[str, Any]) -> str:
    """``co_filename`` PyInstaller records: relative, backslash separated."""
    return entry["dest"][:-1].replace("/", "\\")


def _pure_entries(root: Path, items: Iterable[Any]) -> list[dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for item in items:
        name, source, kind = item[0], item[1], item[2]
        if kind != "PYMODULE":
            continue
        rel = app_relpath(root, source)
        if rel is None or not rel.endswith(".py"):
            continue
        entries[name] = {
            "module": name,
            "source": rel,
            "dest": pyc_dest(name, rel),
            "package": Path(rel).name == "__init__.py",
        }
    return [entries[key] for key in sorted(entries)]


def _pure_entries_from_pyc_datas(
    root: Path, items: Iterable[Any], search_dirs: Iterable[str] = ()
) -> list[dict[str, Any]]:
    """Recover app modules when ``noarchive=True`` moved them out of ``a.pure``.

    PyInstaller 6 then empties ``a.pure`` and ships each module as a DATA entry
    whose source is a compiled ``localpycs`` copy, so ownership comes from the
    destination: ``core\\x.pyc`` is app-owned only if ``core/x.py`` is in the repo.
    """
    entries: dict[str, dict[str, Any]] = {}
    root = Path(root)
    # Analysis pathex entries (e.g. scripts\kg for kg_watcher) are import roots too.
    bases = [root] + [root / d if not Path(d).is_absolute() else Path(d) for d in search_dirs]
    for item in items:
        dest, kind = item[0], item[2]
        dest_rel = _posix(dest)
        if kind != "DATA" or not dest_rel.endswith(".pyc"):
            continue
        stem = dest_rel[: -len(".pyc")]
        package = stem.endswith("/__init__")
        source_rel = None
        for base in bases:
            candidate = base / f"{stem}.py"
            rel = app_relpath(root, candidate)
            if rel is not None and candidate.is_file():
                source_rel = rel
                break
        if source_rel is None:
            continue
        module = (stem[: -len("/__init__")] if package else stem).replace("/", ".")
        entries[module] = {
            "module": module,
            "source": source_rel,
            "dest": pyc_dest(module, source_rel),
            "package": package,
        }
    return [entries[key] for key in sorted(entries)]


def _data_entries(root: Path, items: Iterable[Any]) -> list[dict[str, str]]:
    entries: dict[str, dict[str, str]] = {}
    for item in items:
        dest, source, kind = item[0], item[1], item[2]
        if kind != "DATA":
            continue
        rel = app_relpath(root, source)
        if rel is None:
            continue
        if rel.startswith("build/") and rel != VERSION_SNAPSHOT_REL:
            continue
        dest_rel = _posix(dest)
        if dest_rel.endswith((".pyc", ".pyo")):
            continue
        entries[dest_rel] = {"dest": dest_rel, "source": rel}
    return [entries[key] for key in sorted(entries)]


def build_toc(
    root: Path,
    pure: Iterable[Any],
    datas: Iterable[Any],
    bridge_pure: Iterable[Any] = (),
    bridge_scripts: Iterable[Any] = (),
    search_dirs: Iterable[str] = (),
) -> dict[str, Any]:
    scripts = []
    for item in bridge_scripts:
        rel = app_relpath(root, item[1])
        if rel:
            scripts.append(rel)
    datas = list(datas)
    pure_entries = _pure_entries(root, pure) or _pure_entries_from_pyc_datas(root, datas, search_dirs)
    return {
        "schema": TOC_SCHEMA,
        "pure": pure_entries,
        "datas": _data_entries(root, datas),
        "bridge_pure": _pure_entries(root, bridge_pure),
        "bridge_scripts": sorted(set(scripts)),
    }


def dump_toc(root: Path, analysis: Any, bridge_analysis: Any, out_path: Path) -> Path:
    """Called from engram-overlay.spec once both Analysis objects exist."""
    toc = build_toc(
        Path(root),
        analysis.pure,
        analysis.datas,
        bridge_analysis.pure,
        bridge_analysis.scripts,
        getattr(analysis, "pathex", None) or (),
    )
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(toc, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return out_path


def empty_toc() -> dict[str, Any]:
    return {"schema": TOC_SCHEMA, "pure": [], "datas": [], "bridge_pure": [], "bridge_scripts": []}


def load_toc(source: Path | str | Mapping[str, Any] | None) -> dict[str, Any]:
    if source is None:
        return empty_toc()
    if isinstance(source, Mapping):
        data = dict(source)
    else:
        data = json.loads(Path(source).read_text(encoding="utf-8"))
    if data.get("schema") != TOC_SCHEMA:
        raise ValueError(f"unsupported app TOC schema: {data.get('schema')!r}")
    toc = empty_toc()
    for key in ("pure", "datas", "bridge_pure", "bridge_scripts"):
        toc[key] = [dict(item) if isinstance(item, Mapping) else item for item in (data.get(key) or [])]
    return toc


def payload_files(toc: Mapping[str, Any]) -> list[str]:
    """Sorted ``_internal``-relative destinations owned by the app payload."""
    dests = {entry["dest"] for entry in toc.get("pure", [])}
    dests.update(entry["dest"] for entry in toc.get("datas", []))
    return sorted(dests)


# -------------------------------------------------------- import graph -----


def collect_imports(source: str, module: str, is_package: bool) -> list[tuple[str, bool]]:
    """All absolute dotted imports in ``source`` as ``(name, optional)``.

    ``optional`` marks ``from pkg import name`` candidates, which may be an
    attribute instead of a submodule.  Function-level imports, relative imports
    and ``importlib.import_module("literal")`` / ``__import__("literal")`` are
    included.
    """
    tree = ast.parse(source)
    package = module if is_package else module.rpartition(".")[0]
    found: set[tuple[str, bool]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add((alias.name, False))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".") if package else []
                drop = node.level - 1
                if drop > len(parts):
                    continue
                base_parts = parts[: len(parts) - drop]
                base = ".".join(base_parts + ([node.module] if node.module else []))
            else:
                base = node.module or ""
            if not base:
                continue
            found.add((base, False))
            for alias in node.names:
                if alias.name != "*":
                    found.add((f"{base}.{alias.name}", True))
        elif isinstance(node, ast.Call) and node.args:
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            first = node.args[0]
            if (
                name in {"import_module", "__import__"}
                and isinstance(first, ast.Constant)
                and isinstance(first.value, str)
                and first.value
                and not first.value.startswith(".")
            ):
                found.add((first.value, False))
    return sorted(found)


class ImportGraph:
    """Lazy, cached import graph over the repository's app modules."""

    def __init__(self, root: Path, toc: Mapping[str, Any]):
        self.root = Path(root)
        self._by_source: dict[str, dict[str, Any]] = {}
        self._by_module: dict[str, str] = {}
        for entry in list(toc.get("pure", [])) + list(toc.get("bridge_pure", [])):
            self._by_source.setdefault(entry["source"], entry)
            self._by_module.setdefault(entry["module"], entry["source"])
        self._refs: dict[str, tuple[frozenset[str], frozenset[str]]] = {}

    def find_file(self, dotted: str) -> str | None:
        known = self._by_module.get(dotted)
        if known is not None and (self.root / known).is_file():
            return known
        path = dotted.replace(".", "/")
        for base in APP_SEARCH_DIRS:
            prefix = f"{base}/{path}" if base else path
            for candidate in (f"{prefix}.py", f"{prefix}/__init__.py"):
                if (self.root / candidate).is_file():
                    return candidate
        return None

    def _is_app_top(self, top: str) -> bool:
        return top == "scripts" or self.find_file(top) is not None

    def module_name(self, rel: str) -> tuple[str, bool]:
        entry = self._by_source.get(rel)
        if entry is not None:
            return entry["module"], bool(entry.get("package"))
        is_package = Path(rel).name == "__init__.py"
        base = rel[:-3]
        if is_package:
            base = base.rpartition("/")[0]
        return base.replace("/", "."), is_package

    def refs(self, rel: str) -> tuple[frozenset[str], frozenset[str]]:
        """``(app source files, external dotted names)`` imported by ``rel``."""
        cached = self._refs.get(rel)
        if cached is not None:
            return cached
        module, is_package = self.module_name(rel)
        text = (self.root / rel).read_text(encoding="utf-8-sig")
        app: set[str] = set()
        external: set[str] = set()
        for name, _optional in collect_imports(text, module, is_package):
            parts = name.split(".")
            target = self.find_file(name)
            if target is None:
                for length in range(len(parts) - 1, 0, -1):
                    target = self.find_file(".".join(parts[:length]))
                    if target is not None:
                        break
            if target is not None:
                app.add(target)
                for length in range(1, len(parts)):
                    parent = self.find_file(".".join(parts[:length]))
                    if parent is not None:
                        app.add(parent)
            elif not self._is_app_top(parts[0]):
                external.add(name)
        app.discard(rel)
        result = (frozenset(app), frozenset(external))
        self._refs[rel] = result
        return result

    def closure(self, seeds: Iterable[str]) -> set[str]:
        seen: set[str] = set()
        stack = [s for s in seeds if s.endswith(".py") and (self.root / s).is_file()]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(self.refs(current)[0] - seen)
        return seen


def expand_seed(root: Path, pattern: str) -> list[str]:
    """Repo-relative files matched by a seed glob (``dir/**`` or a file)."""
    if pattern.endswith("/**"):
        base = Path(root) / pattern[:-3]
        if not base.is_dir():
            return []
        return sorted(
            path.relative_to(root).as_posix()
            for path in base.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        )
    return [pattern] if (Path(root) / pattern).is_file() else []


def _matches(patterns: Iterable[str], rel: str) -> bool:
    for pattern in patterns:
        if pattern.endswith("/**"):
            if rel.startswith(pattern[:-2]):
                return True
        elif fnmatch.fnmatchcase(rel, pattern):
            return True
    return False


def select_smokes(root: Path, toc: Mapping[str, Any], changed: Iterable[str]) -> dict[str, bool]:
    """Which frozen smoke roles a change set requires (closure based)."""
    changed_set = sorted(set(changed))
    result = {"runtime-contract": True, "embedding": False, "smoke": False, "dashboard": False}
    if any(_matches(SELECT_ALL_PATTERNS, rel) for rel in changed_set):
        result.update({"embedding": True, "smoke": True, "dashboard": True})
    elif changed_set:
        graph = ImportGraph(root, toc)
        for role, patterns in SMOKE_SEEDS.items():
            seeds = [f for pattern in patterns for f in expand_seed(root, pattern)]
            reach = graph.closure(seeds) | set(seeds)
            if any(rel in reach or _matches(patterns, rel) for rel in changed_set):
                result[role] = True
    # smoke-check computes a real embedding, so it subsumes embedding-check.
    result["embedding_in_smoke"] = True
    return result


def resolve_toc(
    root: Path, toc: Mapping[str, Any], changed: Iterable[str] = ()
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Apply tree changes to a baseline TOC.

    Returns ``(new_toc, added_sources, removed_sources)``.  New modules come
    from (a) new ``.py`` files inside packages already in the TOC and (b) app
    modules newly imported by changed files.  New non-``.py`` files join only
    when they sit next to existing non-``.py`` datas of the same directory and
    are real build inputs.  Entries whose source disappeared are removed.
    """
    root = Path(root)
    new = load_toc(toc)
    added: list[str] = []
    removed: list[str] = []

    for key in ("pure", "datas"):
        kept = []
        for entry in new[key]:
            if (root / entry["source"]).is_file():
                kept.append(entry)
            else:
                removed.append(entry["source"])
        new[key] = kept

    known = {entry["source"] for entry in new["pure"]}
    modules = {entry["module"] for entry in new["pure"]}

    def add_module(rel: str) -> bool:
        if rel in known or not rel.endswith(".py"):
            return False
        module, is_package = ImportGraph(root, new).module_name(rel)
        if module in modules:
            return False
        new["pure"].append(
            {"module": module, "source": rel, "dest": pyc_dest(module, rel), "package": is_package}
        )
        known.add(rel)
        modules.add(module)
        added.append(rel)
        return True

    # (a) new .py files inside packages already present
    package_dirs = sorted(
        {
            str(Path(entry["source"]).parent).replace("\\", "/")
            for entry in new["pure"]
            if entry["package"]
        }
    )
    for rel in sorted(set(changed)):
        if (
            rel.endswith(".py")
            and (root / rel).is_file()
            and str(Path(rel).parent).replace("\\", "/") in package_dirs
        ):
            add_module(rel)

    # (b) app modules newly imported by changed/new files (transitively)
    pending = [rel for rel in dict.fromkeys(list(changed) + added) if rel.endswith(".py")]
    visited: set[str] = set()
    while pending:
        rel = pending.pop()
        if rel in visited or not (root / rel).is_file():
            continue
        visited.add(rel)
        for target in sorted(ImportGraph(root, new).refs(rel)[0]):
            if add_module(target):
                pending.append(target)

    # (c) sibling assets next to existing non-.py datas
    data_dirs: dict[str, str] = {}
    for entry in new["datas"]:
        if not entry["source"].endswith(".py"):
            data_dirs.setdefault(
                str(Path(entry["source"]).parent).replace("\\", "/"),
                str(Path(entry["dest"]).parent).replace("\\", "/"),
            )
    data_sources = {entry["source"] for entry in new["datas"]}
    inputs: set[Path] | None = None
    for rel in sorted(set(changed)):
        if rel in data_sources or rel.endswith(".py") or not (root / rel).is_file():
            continue
        parent = str(Path(rel).parent).replace("\\", "/")
        if parent not in data_dirs:
            continue
        if inputs is None:
            from core.install.overlay_manifest import input_files

            inputs = set(input_files(root))
        if (root / rel) in inputs:
            dest = f"{data_dirs[parent]}/{Path(rel).name}"
            new["datas"].append({"dest": dest, "source": rel})
            data_sources.add(rel)
            added.append(rel)

    new["pure"].sort(key=lambda entry: entry["module"])
    new["datas"].sort(key=lambda entry: entry["dest"])
    return new, sorted(set(added)), sorted(set(removed))


def bridge_closure(root: Path, toc: Mapping[str, Any]) -> dict[str, str]:
    """``{repo-relative source: sha256}`` of every app file the bridge exe embeds."""
    root = Path(root)
    graph = ImportGraph(root, toc)
    seeds = [s for s in toc.get("bridge_scripts", []) if s] or [BRIDGE_ENTRY]
    seeds.append("core/install/mcp_bridge_config.py")
    files = graph.closure(seeds)
    files.update(entry["source"] for entry in toc.get("bridge_pure", []))
    files.update(seeds)
    return {rel: _hash_file(root / rel) for rel in sorted(files) if (root / rel).is_file()}


def bridge_closure_hash(closure: Mapping[str, str]) -> str:
    return hashlib.sha256(json.dumps(dict(closure), sort_keys=True).encode("utf-8")).hexdigest()


# ------------------------------------------------ unresolved imports -------


def _env_resolvable(dotted: str) -> bool:
    """Is ``dotted`` a module in the build env?  Never imports anything."""
    parts = dotted.split(".")
    if parts[0] in sys.builtin_module_names:
        return len(parts) == 1
    spec = importlib.machinery.PathFinder.find_spec(parts[0])
    if spec is None:
        spec = importlib.machinery.FrozenImporter.find_spec(parts[0])
    for index in range(1, len(parts)):
        if spec is None or spec.submodule_search_locations is None:
            return False
        spec = importlib.machinery.PathFinder.find_spec(
            ".".join(parts[: index + 1]), list(spec.submodule_search_locations)
        )
    return spec is not None


class _ArtifactIndex:
    def __init__(self, artifact: Path):
        self.internal = internal_dir(Path(artifact))
        self._zip: set[str] | None = None

    def _base_library(self) -> set[str]:
        if self._zip is None:
            try:
                with zipfile.ZipFile(self.internal / "base_library.zip") as archive:
                    self._zip = set(archive.namelist())
            except (OSError, zipfile.BadZipFile):
                self._zip = set()
        return self._zip

    def has(self, dotted: str) -> bool:
        parts = dotted.split(".")
        if len(parts) == 1 and dotted in sys.builtin_module_names:
            return True
        rel = "/".join(parts)
        internal = self.internal
        for candidate in (f"{rel}.pyc", f"{rel}.py", f"{rel}/__init__.pyc", f"{rel}/__init__.py"):
            if (internal / candidate).is_file():
                return True
        if (internal / rel).is_dir():
            return True
        parent = internal.joinpath(*parts[:-1])
        if parent.is_dir() and any(parent.glob(f"{parts[-1]}.*pyd")):
            return True
        zipped = self._base_library()
        return f"{rel}.pyc" in zipped or f"{rel}/__init__.pyc" in zipped


def unresolved_imports(artifact: Path, imports: Iterable[str]) -> list[str]:
    """External dotted imports resolvable in the build env but absent from ``artifact``.

    A non-empty result means PyInstaller must re-collect (full build).
    """
    index = _ArtifactIndex(Path(artifact))
    return [
        name
        for name in sorted(set(imports))
        if _env_resolvable(name) and not index.has(name)
    ]


def external_imports(
    root: Path,
    toc: Mapping[str, Any],
    files: Iterable[str],
    baseline: Mapping[str, Iterable[str]] | None = None,
) -> dict[str, list[str]]:
    """External imports of ``files``, minus those already in ``baseline``."""
    graph = ImportGraph(root, toc)
    out: dict[str, list[str]] = {}
    for rel in sorted(set(files)):
        if not rel.endswith(".py") or not (Path(root) / rel).is_file():
            continue
        names = set(graph.refs(rel)[1])
        if baseline is not None:
            names -= set(baseline.get(rel, ()))
        if names:
            out[rel] = sorted(names)
    return out


def all_external_imports(root: Path, toc: Mapping[str, Any]) -> dict[str, list[str]]:
    return external_imports(root, toc, [e["source"] for e in toc.get("pure", [])])


# ------------------------------------------------------ compile / copy -----


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _replace_atomic(temp: Path, target: Path) -> None:
    try:
        os.replace(temp, target)
    except BaseException:
        try:
            temp.unlink()
        except OSError:
            pass
        raise


def _temp_beside(target: Path) -> Path:
    return target.with_name(f".{target.name}.{os.getpid()}.tmp")


def compile_payload(
    stage: Path,
    root: Path,
    toc: Mapping[str, Any],
    changed: Iterable[str],
    removed: Iterable[str] = (),
) -> dict[str, list[str]]:
    """Apply source changes to a (hardlink-cloned) stage without PyInstaller.

    ``changed``/``removed`` are repo-relative sources.  Every write goes to a
    temp file beside the target followed by ``os.replace``; an existing target
    is never opened for writing, so files hardlinked to the source artifact
    stay untouched.  Bytecode matches the spec: optimize=0, unchecked-hash
    header, relative backslash ``co_filename``.
    """
    root = Path(root)
    contents = internal_dir(Path(stage))
    pure = {entry["source"]: entry for entry in toc.get("pure", [])}
    datas: dict[str, list[dict[str, str]]] = {}
    for entry in toc.get("datas", []):
        datas.setdefault(entry["source"], []).append(entry)
    result: dict[str, list[str]] = {"compiled": [], "copied": [], "removed": []}

    for rel in sorted(set(changed)):
        source = root / rel
        entry = pure.get(rel)
        if entry is not None:
            target = contents / entry["dest"]
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = _temp_beside(target)
            try:
                py_compile.compile(
                    str(source),
                    cfile=str(temp),
                    dfile=_dfile(entry),
                    doraise=True,
                    optimize=0,
                    invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
                )
            except BaseException:
                temp.unlink(missing_ok=True)
                raise
            _replace_atomic(temp, target)
            result["compiled"].append(entry["dest"])
        for data in datas.get(rel, []):
            target = contents / data["dest"]
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = _temp_beside(target)
            shutil.copyfile(source, temp)
            _replace_atomic(temp, target)
            result["copied"].append(data["dest"])

    for rel in sorted(set(removed)):
        dests = []
        if rel in pure:
            dests.append(pure[rel]["dest"])
        dests.extend(data["dest"] for data in datas.get(rel, []))
        for dest in dests:
            try:
                (contents / dest).unlink()
                result["removed"].append(dest)
            except FileNotFoundError:
                pass
    return result


# ------------------------------------------------------------- digest ------


def digest_files(contents: Path, files: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for dest in sorted(set(files)):
        path = Path(contents) / dest
        value = _hash_file(path) if path.is_file() else "MISSING"
        digest.update(f"{dest}\0{value}\n".encode("utf-8"))
    return digest.hexdigest()


def payload_digest_from_dir(contents: Path, files: Iterable[str]) -> str:
    return digest_files(contents, files)


def payload_digest(artifact: Path, files: Iterable[str] | None = None) -> str:
    """sha256 over the sorted app payload files of an artifact."""
    artifact = Path(artifact)
    if files is None:
        manifest = json.loads((artifact / "build-manifest.json").read_text(encoding="utf-8"))
        files = manifest["app"]["payload_files"]
    return digest_files(internal_dir(artifact), files)
