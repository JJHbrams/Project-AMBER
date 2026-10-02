import json
import marshal
import os
import shutil
import struct
import sys
from pathlib import Path

import pytest

from core.install import app_payload, overlay_manifest
from core.install.model_manifest import create_manifest
from core.install.versioning import resolve_version, write_snapshot


ROOT = Path(__file__).resolve().parents[1]
FAKE_ENV = {
    "python_version": "3.11.0",
    "packages": {"pyinstaller": "6.0"},
    "fastmcp_import": True,
    "fastmcp_error": "",
}

SOURCES = {
    "VERSION": "1.5.5\n",
    "requirements.txt": "mcp\n",
    "environment.yml": "name: x\n",
    "engram_overlay_entry.py": "from core.entrypoint import run\n",
    "engram-overlay.spec": "# spec\n",
    "engram-mcp-bridge.spec": "# bridge spec\n",
    "installer/pyi_rth_engram_tk.py": "# hook\n",
    "native-bubble-shell/src/main.rs": "fn main() {}\n",
    "resource/icon.ico": "ico",
    "resource/icon.png": "png",
    "resource/overlay.png": "png",
    "config/overlay.yaml": "a: 1\n",
    "config/config.yaml": "b: 1\n",
    "overlay/__init__.py": "",
    "overlay/main.py": "from core import misc\nfrom core.install import mcp_bridge_config\n",
    "core/__init__.py": "",
    "core/misc.py": "VALUE = 1\n",
    "core/common/__init__.py": "",
    "core/common/util.py": "X = 1\n",
    "core/dashboard/__init__.py": "",
    "core/dashboard/app.py": "from core.common import util\nDASH = 1\n",
    "core/graph/__init__.py": "",
    "core/graph/semantic/__init__.py": "from core.common import util\n",
    "core/install/__init__.py": "",
    "core/install/model_manifest.py": "M = 1\n",
    "core/install/mcp_bridge_config.py": "B = 1\n",
    "scripts/engram_mcp_bridge.py": "from core.install import mcp_bridge_config\n",
    "scripts/kg/kg_watcher.py": "W = 1\n",
    "mcp_server.py": "import core.graph.semantic\n",
}
PURE = {
    "overlay": "overlay/__init__.py",
    "overlay.main": "overlay/main.py",
    "core": "core/__init__.py",
    "core.misc": "core/misc.py",
    "core.common": "core/common/__init__.py",
    "core.common.util": "core/common/util.py",
    "core.dashboard": "core/dashboard/__init__.py",
    "core.dashboard.app": "core/dashboard/app.py",
    "core.graph": "core/graph/__init__.py",
    "core.graph.semantic": "core/graph/semantic/__init__.py",
    "core.install": "core/install/__init__.py",
    "core.install.model_manifest": "core/install/model_manifest.py",
    "core.install.mcp_bridge_config": "core/install/mcp_bridge_config.py",
    "kg_watcher": "scripts/kg/kg_watcher.py",
    "mcp_server": "mcp_server.py",
}
DATAS = {
    "config/overlay.yaml": "config/overlay.yaml",
    "config/config.yaml": "config/config.yaml",
    "core/dashboard/app.py": "core/dashboard/app.py",
    "engram-version.json": "build/engram-version.json",
    "resource/embedding-model/manifest.json": "resource/embedding-model/manifest.json",
}


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class Project:
    def __init__(self, tmp_path: Path):
        self.root = tmp_path / "repo"
        self.artifact = tmp_path / "artifact"
        for rel, text in SOURCES.items():
            _write(self.root, rel, text)
        model_dir = self.root / "resource" / "embedding-model"
        model_dir.mkdir(parents=True, exist_ok=True)
        (model_dir / "config.json").write_text("{}", encoding="utf-8")
        (model_dir / "model.safetensors").write_bytes(b"weights")
        self.model_manifest = model_dir / "manifest.json"
        self.model_manifest.write_text(
            json.dumps(create_manifest(model_dir, model_id="t/m", resolved_revision="local")),
            encoding="utf-8",
        )
        write_snapshot(self.root / "build" / "engram-version.json", resolve_version(self.root))
        toc = app_payload.build_toc(
            self.root,
            [(name, str(self.root / rel), "PYMODULE") for name, rel in PURE.items()],
            [(dest, str(self.root / rel), "DATA") for dest, rel in DATAS.items()],
            [("core.install.mcp_bridge_config", str(self.root / PURE["core.install.mcp_bridge_config"]), "PYMODULE")],
            [("engram_mcp_bridge", str(self.root / "scripts/engram_mcp_bridge.py"), "PYSOURCE")],
        )
        (self.root / "build" / app_payload.TOC_NAME).write_text(json.dumps(toc), encoding="utf-8")
        self.toc = toc
        (self.artifact / "_internal").mkdir(parents=True)
        (self.artifact / "engram-overlay.exe").write_bytes(b"exe")
        (self.artifact / "engram-dashboard.exe").write_bytes(b"exe")
        (self.artifact / "engram-mcp-bridge.exe").write_bytes(b"bridge")
        sources = [e["source"] for e in toc["pure"]] + [e["source"] for e in toc["datas"]]
        app_payload.compile_payload(self.artifact, self.root, toc, sources)
        overlay_manifest.write_manifest(self.root, self.artifact, self.model_manifest, "rebuild")

    def plan(self, artifact: Path | None = None) -> dict:
        return overlay_manifest.make_plan(self.root, artifact or self.artifact, self.model_manifest)

    def edit(self, rel: str, text: str) -> None:
        _write(self.root, rel, text)


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("SEMVER4_BUILD", "7")
    monkeypatch.setattr(overlay_manifest, "environment_metadata", lambda: json.loads(json.dumps(FAKE_ENV)))
    return Project(tmp_path)


def _clone(source: Path, target: Path) -> Path:
    """Hardlink clone, like the fast path stage."""
    for path in source.rglob("*"):
        destination = target / path.relative_to(source)
        if path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.link(path, destination)
    return target


def test_fresh_artifact_reuses_and_plan_is_deterministic(project):
    first = project.plan()
    second = project.plan()
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first["build"] == "reuse", first["reasons"]
    assert first["changed_files"] == []
    assert first["smoke"]["runtime-contract"] is True
    assert overlay_manifest.validate_build(project.root, project.artifact, project.model_manifest)[0]


def test_plan_cli_output_is_byte_identical(project, capsys):
    project.edit("core/misc.py", "VALUE = 2\n")
    argv = ["--plan", "--root", str(project.root), "--artifact", str(project.artifact),
            "--model-manifest", str(project.model_manifest)]
    assert overlay_manifest._main(argv) == 0
    first = capsys.readouterr().out
    assert overlay_manifest._main(argv) == 0
    assert capsys.readouterr().out == first
    assert json.loads(first)["build"] == "fast"


@pytest.mark.parametrize(
    "rel",
    [
        "requirements.txt",
        "environment.yml",
        "engram-overlay.spec",
        "engram-mcp-bridge.spec",
        "installer/pyi_rth_engram_tk.py",
        "native-bubble-shell/src/main.rs",
        "engram_overlay_entry.py",
        "resource/icon.ico",
    ],
)
def test_each_engine_trigger_requires_full_build(project, rel):
    project.edit(rel, "changed\n")
    plan = project.plan()
    assert plan["build"] == "full"
    assert f"engine input changed: {rel}" in plan["reasons"]
    assert all(plan["smoke"][role] for role in ("runtime-contract", "embedding", "smoke", "dashboard"))


def test_environment_change_requires_full_build(project, monkeypatch):
    changed = dict(FAKE_ENV, python_version="3.12.0")
    monkeypatch.setattr(overlay_manifest, "environment_metadata", lambda: changed)
    plan = project.plan()
    assert plan["build"] == "full"
    assert "Python or package environment changed" in plan["reasons"]


def test_version_only_change_is_fast_not_invalid(project, monkeypatch):
    monkeypatch.setenv("SEMVER4_BUILD", "8")
    plan = project.plan()
    assert plan["build"] == "fast"
    assert plan["restamp"] is True
    assert plan["changed_files"] == []
    assert plan["compile"] == []
    assert plan["copy"] == ["build/engram-version.json"]
    assert plan["smoke"] == {
        "runtime-contract": True, "embedding": False, "smoke": False,
        "dashboard": False, "embedding_in_smoke": True,
    }
    assert plan["bridge_rebuild"] is False
    valid, reason = overlay_manifest.validate_build(project.root, project.artifact, project.model_manifest)
    assert not valid and "version" in reason


def test_schema_one_manifest_requires_full_build(project):
    path = project.artifact / overlay_manifest.MANIFEST_NAME
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["schema_version"] = 1
    path.write_text(json.dumps(manifest), encoding="utf-8")
    plan = project.plan()
    assert plan["build"] == "full"
    assert any("schema" in reason for reason in plan["reasons"])
    assert overlay_manifest.validate_build(project.root, project.artifact, project.model_manifest)[0] is False


def test_missing_artifact_manifest_requires_full_build(project, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert project.plan(empty)["build"] == "full"


def test_dashboard_change_selects_only_dashboard(project):
    project.edit("core/dashboard/app.py", "from core.common import util\nDASH = 2\n")
    plan = project.plan()
    assert plan["build"] == "fast"
    assert plan["compile"] == ["core/dashboard/app.py"]
    assert plan["copy"] == ["core/dashboard/app.py"]
    assert plan["smoke"]["dashboard"] is True
    assert plan["smoke"]["embedding"] is False
    assert plan["smoke"]["smoke"] is False


def test_embedding_seed_change_selects_embedding_and_smoke(project):
    project.edit("core/graph/semantic/__init__.py", "from core.common import util\nS = 2\n")
    smoke = project.plan()["smoke"]
    assert smoke["embedding"] is True
    assert smoke["smoke"] is True  # mcp_server imports core.graph.semantic
    assert smoke["dashboard"] is False
    assert smoke["embedding_in_smoke"] is True


def test_shared_dependency_selects_every_closure(project):
    project.edit("core/common/util.py", "X = 2\n")
    smoke = project.plan()["smoke"]
    assert smoke["dashboard"] and smoke["embedding"] and smoke["smoke"]


def test_unrelated_module_selects_only_runtime_contract_and_smoke(project):
    project.edit("core/misc.py", "VALUE = 2\n")
    smoke = project.plan()["smoke"]
    assert smoke["smoke"] is True  # overlay.main imports core.misc
    assert smoke["embedding"] is False and smoke["dashboard"] is False


@pytest.mark.parametrize("rel", ["config/overlay.yaml", "config/config.yaml"])
def test_config_change_selects_all_smokes(project, rel):
    project.edit(rel, "changed: true\n")
    plan = project.plan()
    assert plan["build"] == "fast"
    assert plan["copy"] == [rel]
    assert plan["smoke"]["dashboard"] and plan["smoke"]["embedding"] and plan["smoke"]["smoke"]


def test_unresolved_import_requires_full_build(project):
    project.edit("core/misc.py", "import csv\nVALUE = 2\n")
    plan = project.plan()
    assert plan["build"] == "full"
    assert plan["unresolved_imports"] == ["csv"]
    assert "import not present in artifact: csv" in plan["reasons"]


def test_import_present_in_artifact_stays_fast(project):
    (project.artifact / "_internal" / "csv.pyc").write_bytes(b"x")
    project.edit("core/misc.py", "def f():\n    import csv\n    return csv\n")
    assert project.plan()["build"] == "fast"


def test_preexisting_optional_import_does_not_force_full(project):
    project.edit("core/misc.py", "try:\n    import csv\nexcept ImportError:\n    csv = None\n")
    assert project.plan()["build"] == "full"
    # Re-baseline as if that import had been present at the full build.
    overlay_manifest.write_manifest(project.root, project.artifact, project.model_manifest, "rebuild")
    project.edit("core/misc.py", "try:\n    import csv\nexcept ImportError:\n    csv = None\nVALUE = 3\n")
    assert project.plan()["build"] == "fast"


def test_new_module_in_known_package_and_new_package_are_added(project):
    project.edit("core/newmod.py", "N = 1\n")
    project.edit("core/misc.py", "from core import newmod\nfrom core.fresh import thing\n")
    project.edit("core/fresh/__init__.py", "")
    project.edit("core/fresh/thing.py", "T = 1\n")
    plan = project.plan()
    assert plan["build"] == "fast", plan["reasons"]
    assert set(plan["added_modules"]) == {"core/newmod.py", "core/fresh/__init__.py", "core/fresh/thing.py"}
    assert set(plan["compile"]) >= set(plan["added_modules"])


def test_deleted_source_is_removed_from_payload(project):
    (project.root / "core" / "misc.py").unlink()
    project.edit("overlay/main.py", "from core.install import mcp_bridge_config\n")
    plan = project.plan()
    assert plan["build"] == "fast"
    assert plan["remove"] == ["core/misc.py"]


def test_bridge_closure_change_flags_bridge_rebuild(project):
    project.edit("core/install/mcp_bridge_config.py", "B = 2\n")
    plan = project.plan()
    assert plan["build"] == "fast"
    assert plan["bridge_rebuild"] is True
    project.edit("core/install/mcp_bridge_config.py", "B = 1\n")
    project.edit("core/misc.py", "VALUE = 5\n")
    assert project.plan()["bridge_rebuild"] is False


def test_payload_drift_requires_full_build(project):
    (project.artifact / "_internal" / "core" / "misc.pyc").write_bytes(b"tampered")
    plan = project.plan()
    assert plan["build"] == "full"
    assert "artifact app payload drifted from build manifest" in plan["reasons"]


def test_collect_imports_sees_function_relative_and_importlib_forms():
    source = (
        "import a.b\n"
        "from . import sibling\n"
        "from ..up import thing\n"
        "def f():\n"
        "    import lazy.mod\n"
        "    importlib.import_module('dyn.mod')\n"
        "    __import__('other')\n"
        "    importlib.import_module(name)\n"
    )
    names = {name for name, _ in app_payload.collect_imports(source, "pkg.sub.mod", False)}
    assert {"a.b", "pkg.sub", "pkg.sub.sibling", "pkg.up", "pkg.up.thing", "lazy.mod", "dyn.mod", "other"} <= names
    package_names = {name for name, _ in app_payload.collect_imports("from . import x\n", "pkg", True)}
    assert "pkg.x" in package_names


def test_toc_excludes_site_packages_and_bridge_is_separate(tmp_path):
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "a.py").write_text("", encoding="utf-8")
    third = tmp_path / "env" / "Lib" / "site-packages" / "dep"
    third.mkdir(parents=True)
    (third / "__init__.py").write_text("", encoding="utf-8")
    toc = app_payload.build_toc(
        tmp_path,
        [("core.a", str(tmp_path / "core" / "a.py"), "PYMODULE"),
         ("dep", str(third / "__init__.py"), "PYMODULE"),
         ("scripts", "-", "PYMODULE")],
        [("x/f.txt", str(tmp_path / "core" / "a.py"), "DATA")],
        [("core.a", str(tmp_path / "core" / "a.py"), "PYMODULE")],
    )
    assert [e["module"] for e in toc["pure"]] == ["core.a"]
    assert toc["pure"][0]["dest"] == "core/a.pyc"
    assert [e["module"] for e in toc["bridge_pure"]] == ["core.a"]


def test_compile_payload_never_writes_through_a_hardlink(project, tmp_path):
    stage = _clone(project.artifact, tmp_path / "stage")
    original = project.artifact / "_internal" / "core" / "misc.pyc"
    linked = stage / "_internal" / "core" / "misc.pyc"
    assert os.path.samefile(original, linked)
    before = original.read_bytes()
    data_before = (project.artifact / "_internal" / "config" / "overlay.yaml").read_bytes()

    project.edit("core/misc.py", "VALUE = 99\n")
    project.edit("config/overlay.yaml", "a: 99\n")
    result = app_payload.compile_payload(
        stage, project.root, project.toc, ["core/misc.py", "config/overlay.yaml"]
    )

    assert result["compiled"] == ["core/misc.pyc"]
    assert original.read_bytes() == before
    assert (project.artifact / "_internal" / "config" / "overlay.yaml").read_bytes() == data_before
    assert linked.read_bytes() != before
    assert not os.path.samefile(original, linked)
    assert (stage / "_internal" / "config" / "overlay.yaml").read_text(encoding="utf-8") == "a: 99\n"
    assert not list(stage.rglob("*.tmp"))


def test_compile_payload_bytecode_matches_pyinstaller_conventions(project, tmp_path):
    stage = _clone(project.artifact, tmp_path / "stage")
    project.edit("core/dashboard/app.py", "DASH = 3\n")
    app_payload.compile_payload(stage, project.root, project.toc, ["core/dashboard/app.py"])
    data = (stage / "_internal" / "core" / "dashboard" / "app.pyc").read_bytes()
    assert struct.unpack("<I", data[4:8])[0] == 0b01  # hash based, unchecked
    code = marshal.loads(data[16:])
    assert code.co_filename == "core\\dashboard\\app.py"
    package = (stage / "_internal" / "core" / "__init__.pyc").read_bytes()
    assert marshal.loads(package[16:]).co_filename == "core\\__init__.py"


def test_compile_payload_syntax_error_leaves_target_and_no_temp(project, tmp_path):
    stage = _clone(project.artifact, tmp_path / "stage")
    target = stage / "_internal" / "core" / "misc.pyc"
    before = target.read_bytes()
    project.edit("core/misc.py", "def broken(:\n")
    with pytest.raises(Exception):
        app_payload.compile_payload(stage, project.root, project.toc, ["core/misc.py"])
    assert target.read_bytes() == before
    assert not list(stage.rglob("*.tmp"))


def test_remove_only_unlinks_stage_copy(project, tmp_path):
    stage = _clone(project.artifact, tmp_path / "stage")
    result = app_payload.compile_payload(stage, project.root, project.toc, [], ["core/misc.py"])
    assert result["removed"] == ["core/misc.pyc"]
    assert not (stage / "_internal" / "core" / "misc.pyc").exists()
    assert (project.artifact / "_internal" / "core" / "misc.pyc").exists()


def test_fast_patch_roundtrip_apply_write_record_reuse(project, tmp_path):
    original_manifest = (project.artifact / overlay_manifest.MANIFEST_NAME).read_bytes()
    original_pyc = (project.artifact / "_internal" / "core" / "misc.pyc").read_bytes()
    stage = _clone(project.artifact, tmp_path / "stage")
    project.edit("core/misc.py", "VALUE = 42\n")
    project.edit("core/newmod.py", "N = 1\n")

    outcome = overlay_manifest.apply_fast(project.root, stage, project.model_manifest)
    assert outcome["applied"]["compiled"] == ["core/misc.pyc", "core/newmod.pyc"]
    overlay_manifest.write_manifest(project.root, stage, project.model_manifest, "rebuild", "fast-patch")
    overlay_manifest.record_smoke(stage, {"runtime-contract": "pass", "embedding": "skipped:closure-unaffected"})

    manifest = json.loads((stage / overlay_manifest.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 2
    assert manifest["build_kind"] == "fast-patch"
    assert manifest["smoke"] == {"runtime-contract": "pass", "embedding": "skipped:closure-unaffected"}
    assert "core/newmod.pyc" in manifest["app"]["payload_files"]
    assert manifest["app"]["payload_digest"] == app_payload.payload_digest(stage)
    assert manifest["patched_from"]["build_kind"] == "full"
    assert overlay_manifest.validate_build(project.root, stage, project.model_manifest) == (True, "valid")
    assert project.plan(stage)["build"] == "reuse"
    # the source artifact is untouched, including its hardlinked manifest
    assert (project.artifact / overlay_manifest.MANIFEST_NAME).read_bytes() == original_manifest
    assert (project.artifact / "_internal" / "core" / "misc.pyc").read_bytes() == original_pyc
    assert project.plan()["build"] == "fast"


def test_record_smoke_rejects_unknown_status(project):
    with pytest.raises(ValueError):
        overlay_manifest.record_smoke(project.artifact, {"embedding": "maybe"})
    with pytest.raises(ValueError):
        overlay_manifest.record_smoke(project.artifact, {"embedding": "skipped:"})


def test_apply_refuses_when_full_build_required(project, tmp_path):
    stage = _clone(project.artifact, tmp_path / "stage")
    project.edit("requirements.txt", "changed\n")
    with pytest.raises(RuntimeError, match="full build required"):
        overlay_manifest.apply_fast(project.root, stage, project.model_manifest)


def test_spec_dumps_app_toc_after_both_analyses():
    spec = (ROOT / "engram-overlay.spec").read_text(encoding="utf-8")
    assert spec.index("bridge_a = Analysis(") < spec.index("_payload.dump_toc(")
    assert spec.index("_payload.dump_toc(") < spec.index("bridge_exe = EXE(")
    assert "TOC_NAME" in spec


def test_runtime_contract_helpers(tmp_path, monkeypatch):
    from core.install import runtime_contract

    assert runtime_contract._build_manifest_facts() == ("", "")
    origins = runtime_contract._module_origins({"core": sys.modules["core"]})
    assert origins["core"].endswith("__init__.py")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    with pytest.raises(RuntimeError, match="outside the bundle"):
        runtime_contract._module_origins({"core": sys.modules["core"]})


def _dist_exe() -> Path | None:
    candidates = [os.environ.get("ENGRAM_TEST_DIST_EXE", "")]
    candidates += [
        str(base / "dist" / "engram-overlay" / "engram-dashboard.exe")
        for base in (ROOT, ROOT.parents[1] / "ProjectIntelContunuum")
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


def test_pe_restamp_preserves_overlay_and_never_writes_through_hardlink(tmp_path):
    pytest.importorskip("pefile")
    pytest.importorskip("PyInstaller")
    pytest.importorskip("win32api")
    from core.install import pe_version

    source = _dist_exe()
    if source is None:
        pytest.skip("no built engram-dashboard.exe available")
    exe = tmp_path / "engram-dashboard.exe"
    shutil.copyfile(source, exe)
    linked = tmp_path / "linked.exe"
    os.link(exe, linked)
    before = exe.read_bytes()
    old_version = pe_version.read_version(exe)

    import pefile

    with pefile.PE(data=before, fast_load=True) as pe:
        cut = pe.get_overlay_data_start_offset()
    overlay = before[cut:]

    pe_version.restamp(exe, "9.8.7.6")

    assert pe_version.read_version(exe) == "9.8.7.6"
    assert linked.read_bytes() == before
    assert pe_version.read_version(linked) == old_version
    after = exe.read_bytes()
    assert after.endswith(overlay)
    assert not list(tmp_path.glob(".*restamp"))
    with pefile.PE(str(exe), fast_load=True) as pe:
        assert pe.verify_checksum()


def test_runtime_digest_matches_build_side_digest(tmp_path):
    from core.install import runtime_contract

    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.pyc").write_bytes(b"one")
    (tmp_path / "y.json").write_bytes(b"two")
    files = ["y.json", "a/x.pyc", "gone.pyc"]
    assert runtime_contract._payload_digest(tmp_path, files) == app_payload.digest_files(tmp_path, files)


def test_noarchive_toc_recovers_app_modules_from_pyc_datas(tmp_path):
    # PyInstaller 6 with noarchive=True empties a.pure at spec time and ships each
    # module as a localpycs DATA entry; the TOC must still list app modules.
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "core" / "entrypoint.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "scripts" / "kg").mkdir(parents=True)
    (tmp_path / "scripts" / "kg" / "kg_watcher.py").write_text("", encoding="utf-8")
    local = str(tmp_path / "build" / "localpycs" / "0")
    datas = [
        ("core\entrypoint.pyc", local + "\core\entrypoint.pyc", "DATA"),
        ("core\__init__.pyc", local + "\core\__init__.pyc", "DATA"),
        ("kg_watcher.pyc", local + "\kg_watcher.pyc", "DATA"),
        ("torch\__init__.pyc", "C:\env\site-packages\torch\__init__.pyc", "DATA"),
    ]
    toc = app_payload.build_toc(tmp_path, [], datas, search_dirs=["scripts\kg"])
    got = {entry["module"]: (entry["source"], entry["dest"], entry["package"]) for entry in toc["pure"]}
    assert got == {
        "core": ("core/__init__.py", "core/__init__.pyc", True),
        "core.entrypoint": ("core/entrypoint.py", "core/entrypoint.pyc", False),
        "kg_watcher": ("scripts/kg/kg_watcher.py", "kg_watcher.pyc", False),
    }


def test_source_hash_ignores_line_endings_but_not_content(tmp_path):
    lf, crlf, other = tmp_path / "a.py", tmp_path / "b.py", tmp_path / "c.py"
    lf.write_bytes(b"x = 1\ny = 2\n")
    crlf.write_bytes(b"x = 1\r\ny = 2\r\n")
    other.write_bytes(b"x = 1\ny = 3\n")
    assert app_payload.hash_source(lf) == app_payload.hash_source(crlf)
    assert app_payload.hash_source(lf) != app_payload.hash_source(other)
    icon = tmp_path / "icon.ico"
    icon.write_bytes(b"\x00\r\n\x01")
    assert app_payload.hash_source(icon) == app_payload._hash_file(icon)


def test_build_only_imports_never_force_a_full_build(tmp_path):
    (tmp_path / "_internal").mkdir()
    assert app_payload.unresolved_imports(tmp_path, ["PyInstaller.utils.win32.versioninfo", "pefile"]) == []
