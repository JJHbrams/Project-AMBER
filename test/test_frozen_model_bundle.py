"""Frozen builds bundle the complete embedding model, load it in place, never download."""

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from core import entrypoint
from core.graph.semantic import semantic_graph as sg_module
from core.graph.semantic.semantic_graph import SemanticGraph, semantic_status_snapshot
from core.install import model_manifest
from core.install.model_manifest import (
    BUNDLE_CHECK_NAME,
    check_bundled_model,
    create_manifest,
    write_bundle_check,
)


ROOT = Path(__file__).resolve().parents[1]
MODEL_ID = "intfloat/multilingual-e5-small"


def _make_model(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "1_Pooling").mkdir(exist_ok=True)
    (directory / "1_Pooling" / "config.json").write_text("{}", encoding="utf-8")
    (directory / "config.json").write_text('{"a": 1}', encoding="utf-8")
    (directory / "model.safetensors").write_bytes(b"weights" * 100)
    (directory / "manifest.json").write_text(
        json.dumps(create_manifest(directory, MODEL_ID, resolved_revision="rev")),
        encoding="utf-8",
    )
    return directory


def _bundle(model_dir: Path, bundled: Path) -> Path:
    shutil.copytree(model_dir, bundled, dirs_exist_ok=True)
    write_bundle_check(model_dir, bundled / BUNDLE_CHECK_NAME)
    return bundled


class _Logging:
    @staticmethod
    def get_verbosity():
        return 20

    @staticmethod
    def set_verbosity(_value):
        return None

    @staticmethod
    def set_verbosity_error():
        return None


class BundleCheckTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.model = _make_model(self.tmp / "model")
        self.bundled = _bundle(self.model, self.tmp / "bundled")

    def test_complete_bundle_passes_without_hashing_payload(self):
        with patch.object(model_manifest, "_hash_file", wraps=model_manifest._hash_file) as hasher:
            valid, reason = check_bundled_model(self.bundled, MODEL_ID)
        self.assertTrue(valid, reason)
        hashed = {Path(call.args[0]).name for call in hasher.call_args_list}
        self.assertEqual(hashed, {"manifest.json"})

    def test_missing_file_is_reported_by_name(self):
        (self.bundled / "model.safetensors").unlink()
        valid, reason = check_bundled_model(self.bundled, MODEL_ID)
        self.assertFalse(valid)
        self.assertIn("file missing: model.safetensors", reason)

    def test_truncated_file_is_a_size_mismatch(self):
        (self.bundled / "model.safetensors").write_bytes(b"short")
        valid, reason = check_bundled_model(self.bundled, MODEL_ID)
        self.assertFalse(valid)
        self.assertIn("size mismatch: model.safetensors", reason)

    def test_manifest_only_bundle_fails(self):
        manifest_only = self.tmp / "manifest-only"
        manifest_only.mkdir()
        shutil.copyfile(self.model / "manifest.json", manifest_only / "manifest.json")
        valid, reason = check_bundled_model(manifest_only, MODEL_ID)
        self.assertFalse(valid)
        self.assertIn("size record unreadable", reason)

    def test_size_record_for_a_different_manifest_is_rejected(self):
        manifest = self.bundled / "manifest.json"
        manifest.write_bytes(manifest.read_bytes() + b" ")
        valid, reason = check_bundled_model(self.bundled, MODEL_ID)
        self.assertFalse(valid)
        self.assertIn("does not match its manifest", reason)

    def test_wrong_model_id_is_rejected(self):
        valid, reason = check_bundled_model(self.bundled, "other/model")
        self.assertFalse(valid)
        self.assertIn("model id mismatch", reason)

    def test_size_record_is_not_an_unexpected_extra_payload_file(self):
        self.assertTrue(model_manifest.validate_manifest(self.bundled, MODEL_ID)[0])

    def test_build_gate_rejects_corrupt_and_extra_files(self):
        out = self.tmp / "out" / BUNDLE_CHECK_NAME
        (self.model / "config.json").write_text('{"a": 2}', encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "hash mismatch: config.json"):
            write_bundle_check(self.model, out)
        (self.model / "config.json").unlink()
        with self.assertRaisesRegex(RuntimeError, "file list changed"):
            write_bundle_check(self.model, out)
        self.assertFalse(out.exists())

    def test_cli_check_bundle_reports_validity(self):
        argv = ["--model-dir", str(self.bundled), "--model-id", MODEL_ID, "--check-bundle"]
        self.assertEqual(model_manifest._main(argv), 0)
        (self.bundled / "config.json").unlink()
        self.assertEqual(model_manifest._main(argv), 1)


class SpecBundleCollectionTests(unittest.TestCase):
    """Execute the real spec helper functions against a temporary repo layout."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "core" / "install").mkdir(parents=True)
        shutil.copyfile(
            ROOT / "core" / "install" / "model_manifest.py",
            self.root / "core" / "install" / "model_manifest.py",
        )
        self.model = _make_model(self.root / "resource" / "embedding-model")
        tree = ast.parse((ROOT / "engram-overlay.spec").read_text(encoding="utf-8"))
        wanted = {"_load_model_manifest_module", "_collect_embedding_model_payload"}
        functions = [
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted
        ]
        self.assertEqual({f.name for f in functions}, wanted)
        namespace = {"Path": Path, "sys": sys}
        exec(
            compile(ast.Module(body=functions, type_ignores=[]), "engram-overlay.spec", "exec"),
            namespace,
        )
        self.collect = namespace["_collect_embedding_model_payload"]

    def _collect(self):
        previous = os.getcwd()
        os.chdir(self.root)
        try:
            return self.collect()
        finally:
            os.chdir(previous)
            sys.modules.pop("_engram_model_manifest", None)

    def test_complete_model_is_collected_with_size_record(self):
        items = self._collect()
        names = {Path(src).name for src, _ in items}
        self.assertTrue({"manifest.json", "model.safetensors", "config.json", BUNDLE_CHECK_NAME} <= names)
        destinations = {Path(src).name: Path(dest).as_posix() for src, dest in items}
        self.assertEqual(destinations["model.safetensors"], "resource/embedding-model")
        self.assertEqual(destinations[BUNDLE_CHECK_NAME], "resource/embedding-model")
        pooling = [
            Path(dest).as_posix() for src, dest in items if Path(src).parent.name == "1_Pooling"
        ]
        self.assertEqual(pooling, ["resource/embedding-model/1_Pooling"])
        record = json.loads((self.root / "build" / BUNDLE_CHECK_NAME).read_text(encoding="utf-8"))
        self.assertEqual(record["files"]["model.safetensors"], 700)

    def test_manifest_only_tree_fails_the_build(self):
        (self.model / "model.safetensors").unlink()
        with self.assertRaisesRegex(RuntimeError, "cannot be bundled"):
            self._collect()

    def test_content_mismatch_fails_the_build(self):
        (self.model / "model.safetensors").write_bytes(b"weights" * 99)
        with self.assertRaisesRegex(RuntimeError, "hash mismatch: model.safetensors"):
            self._collect()

    def test_spec_wires_payload_into_datas(self):
        spec = (ROOT / "engram-overlay.spec").read_text(encoding="utf-8")
        self.assertIn("*_embedding_model_datas,", spec)
        self.assertNotIn("Bundle only the canonical manifest", spec)


class FrozenLoadDecisionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.model = _make_model(self.tmp / "src-model")
        self.meipass = self.tmp / "_internal"
        self.bundled = self.meipass / "resource" / "embedding-model"

    def _graph(self):
        graph = object.__new__(SemanticGraph)
        graph._embedding_model_name = MODEL_ID
        graph._encoder = None
        return graph

    def _load(self, graph):
        calls = []

        class _SentenceTransformer:
            def __init__(self, *args, **kwargs):
                calls.append((args, kwargs))

        modules = {
            "sentence_transformers": types.SimpleNamespace(SentenceTransformer=_SentenceTransformer),
            "transformers": types.SimpleNamespace(logging=_Logging),
        }
        with patch.dict("sys.modules", modules), patch.object(
            sys, "frozen", True, create=True
        ), patch.object(sys, "_MEIPASS", str(self.meipass), create=True), patch.object(
            sg_module, "ensure_cached_model"
        ) as resolver, patch.object(
            model_manifest, "_export_to_staging"
        ) as downloader, patch.object(
            model_manifest.shutil, "copyfile"
        ) as copier:
            graph._load_encoder_blocking()
        return calls, resolver, downloader, copier

    def test_complete_bundle_loads_in_place_without_cache_copy_or_download(self):
        _bundle(self.model, self.bundled)
        graph = self._graph()
        calls, resolver, downloader, copier = self._load(graph)
        self.assertEqual(calls, [((str(self.bundled),), {"local_files_only": True, "device": "cpu"})])
        resolver.assert_not_called()
        downloader.assert_not_called()
        copier.assert_not_called()
        self.assertIsNot(graph._encoder, False)
        self.assertEqual(graph._encoder_error, "")

    def test_incomplete_bundle_fails_visibly_without_download_attempt(self):
        _bundle(self.model, self.bundled)
        (self.bundled / "model.safetensors").unlink()
        graph = self._graph()
        with self.assertLogs(sg_module.logger, level="ERROR") as logs:
            calls, resolver, downloader, copier = self._load(graph)
        self.assertIs(graph._encoder, False)
        self.assertEqual(calls, [])
        resolver.assert_not_called()
        downloader.assert_not_called()
        copier.assert_not_called()
        self.assertIn("file missing: model.safetensors", graph._encoder_error)
        self.assertTrue(any("model.safetensors" in line for line in logs.output))

    def test_manifest_only_bundle_fails_visibly(self):
        self.bundled.mkdir(parents=True)
        shutil.copyfile(self.model / "manifest.json", self.bundled / "manifest.json")
        graph = self._graph()
        with self.assertLogs(sg_module.logger, level="ERROR"):
            calls, resolver, downloader, _ = self._load(graph)
        self.assertIs(graph._encoder, False)
        self.assertEqual(calls, [])
        resolver.assert_not_called()
        downloader.assert_not_called()
        self.assertIn("size record unreadable", graph._encoder_error)

    def test_source_mode_still_resolves_through_the_verified_cache(self):
        graph = self._graph()
        calls = []

        class _SentenceTransformer:
            def __init__(self, *args, **kwargs):
                calls.append(args)

        modules = {
            "sentence_transformers": types.SimpleNamespace(SentenceTransformer=_SentenceTransformer),
            "transformers": types.SimpleNamespace(logging=_Logging),
        }
        with patch.dict("sys.modules", modules), patch.object(
            sg_module, "ensure_cached_model", return_value=Path("C:/cache/model")
        ) as resolver:
            graph._load_encoder_blocking()
        self.assertTrue(resolver.call_args.kwargs["allow_download"])
        self.assertEqual(calls, [(str(Path("C:/cache/model")),)])


class SemanticStatusTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.model = _make_model(self.tmp / "src-model")
        self.meipass = self.tmp / "_internal"
        self.bundled = self.meipass / "resource" / "embedding-model"

    def _graph(self, encoder=None, error=""):
        graph = object.__new__(SemanticGraph)
        graph._embedding_model_name = MODEL_ID
        graph._encoder = encoder
        graph._encoder_error = error
        return graph

    def _frozen(self):
        return (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "_MEIPASS", str(self.meipass), create=True),
        )

    def test_failed_encoder_reports_reason(self):
        status = self._graph(False, "RuntimeError: boom").semantic_status()
        self.assertFalse(status["enabled"])
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["reason"], "RuntimeError: boom")

    def test_ready_and_unloaded_states(self):
        self.assertEqual(self._graph(object()).semantic_status()["state"], "ready")
        with patch.dict(os.environ, {"ENGRAM_RUNTIME_ROLE": ""}):
            status = self._graph(None).semantic_status()
        self.assertTrue(status["enabled"])
        self.assertEqual(status["state"], "unloaded")

    def test_frozen_unloaded_graph_prechecks_the_bundle(self):
        a, b = self._frozen()
        with a, b, patch.dict(os.environ, {"ENGRAM_RUNTIME_ROLE": ""}):
            self.bundled.mkdir(parents=True)
            shutil.copyfile(self.model / "manifest.json", self.bundled / "manifest.json")
            status = self._graph(None).semantic_status()
            self.assertFalse(status["enabled"])
            self.assertIn("size record unreadable", status["reason"])
            _bundle(self.model, self.bundled)
            self.assertTrue(self._graph(None).semantic_status()["enabled"])

    def test_snapshot_does_not_construct_the_graph(self):
        a, b = self._frozen()
        with patch.object(sg_module, "_sg_instance", None):
            self.assertEqual(semantic_status_snapshot()["state"], "not_initialized")
            with a, b:
                self.bundled.mkdir(parents=True)
                shutil.copyfile(self.model / "manifest.json", self.bundled / "manifest.json")
                status = semantic_status_snapshot()
            self.assertFalse(status["enabled"])
            self.assertIsNone(sg_module._sg_instance)

    def test_health_exposes_semantic_status(self):
        with tempfile.TemporaryDirectory(prefix="engram-health-") as directory:
            profile = Path(directory)
            environment = dict(
                os.environ,
                HOME=str(profile),
                USERPROFILE=str(profile),
                APPDATA=str(profile / "AppData"),
                ENGRAM_SMOKE_DB_DIR=str(profile / "db"),
                PYTHONUTF8="1",
                PYTHONPATH=os.pathsep.join([str(ROOT), os.environ.get("PYTHONPATH", "")]),
            )
            code = """
import asyncio, json
import mcp_server as server
from core.graph.semantic import semantic_graph as m
g = object.__new__(m.SemanticGraph)
g._embedding_model_name = "x"
g._encoder = False
g._encoder_error = "RuntimeError: bundled embedding model file missing: model.safetensors"
m._sg_instance = g
response = asyncio.run(server._http_health(None))
print(json.dumps(json.loads(response.body)["semantic"]))
"""
            completed = subprocess.run(
                [sys.executable, "-c", code],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=180,
            )
        self.assertEqual(completed.returncode, 0, (completed.stderr or "")[-2000:])
        semantic = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertFalse(semantic["enabled"])
        self.assertEqual(semantic["state"], "failed")
        self.assertIn("model.safetensors", semantic["reason"])

    def test_status_tool_includes_semantic_block(self):
        source = (ROOT / "mcp_server.py").read_text(encoding="utf-8")
        self.assertIn('"semantic": semantic_status_snapshot()', source)


class FrozenOfflineEnvTests(unittest.TestCase):
    KEYS = tuple(model_manifest.OFFLINE_ENV)

    def _clean(self):
        patcher = patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in (*self.KEYS, model_manifest.FORCED_ENV_MARKER):
            os.environ.pop(key, None)

    def _configure(self, role, rest=(), frozen=True):
        with patch.object(sys, "frozen", frozen, create=True):
            applied = entrypoint._configure_model_network(role, list(rest))
        return applied, {key: os.environ.get(key) for key in self.KEYS}

    def test_model_roles_are_forced_offline(self):
        for role in ("mcp-server", "kg-watcher", "embedding-check", "smoke-check"):
            self._clean()
            applied, env = self._configure(role)
            self.assertTrue(applied, role)
            self.assertEqual(set(env.values()), {"1"}, role)

    def test_user_facing_roles_are_not_forced(self):
        for role in ("claude-root-launcher", "agent-policy-hook", "runtime-contract", "service-config"):
            self._clean()
            applied, env = self._configure(role)
            self.assertFalse(applied, role)
            self.assertEqual(set(env.values()), {None}, role)
            self.assertNotIn(model_manifest.FORCED_ENV_MARKER, os.environ)

    def test_source_mode_is_untouched(self):
        self._clean()
        applied, env = self._configure("mcp-server", frozen=False)
        self.assertFalse(applied)
        self.assertEqual(set(env.values()), {None})

    def test_model_cache_download_clears_inherited_offline_switches(self):
        self._clean()
        os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1")
        applied, env = self._configure("model-cache", ["--ensure-cache", "--allow-download"])
        self.assertFalse(applied)
        self.assertIsNone(env["HF_HUB_OFFLINE"])
        self.assertIsNone(env["TRANSFORMERS_OFFLINE"])
        self._clean()
        applied, _ = self._configure("model-cache", ["--ensure-cache"])
        self.assertFalse(applied)

    def test_dispatch_applies_the_role_policy_before_importing_the_role(self):
        source = (ROOT / "core" / "entrypoint.py").read_text(encoding="utf-8").replace("\r\n", "\n")
        self.assertLess(
            source.index("_configure_model_network(role, rest)"),
            source.index("import mcp_server\n            mcp_server.main(rest)"),
        )

    def test_load_time_forcing_precedes_the_sentence_transformers_import(self):
        source = (ROOT / "core" / "graph" / "semantic" / "semantic_graph.py").read_text(
            encoding="utf-8"
        ).replace("\r\n", "\n")
        body = source.split("def _load_encoder_blocking", 1)[1]
        self.assertLess(
            body.index("force_model_offline_env()"),
            body.index("from sentence_transformers import SentenceTransformer"),
        )

    def test_frozen_load_forces_offline_in_the_loading_process(self):
        self._clean()
        graph = object.__new__(SemanticGraph)
        graph._embedding_model_name = MODEL_ID
        graph._encoder = None
        modules = {
            "sentence_transformers": types.SimpleNamespace(SentenceTransformer=object),
            "transformers": types.SimpleNamespace(logging=_Logging),
        }
        with patch.object(sys, "frozen", True, create=True), patch.object(
            sys, "_MEIPASS", str(Path(tempfile.gettempdir()) / "missing-meipass"), create=True
        ), patch.dict("sys.modules", modules):
            with self.assertLogs(sg_module.logger, level="ERROR"):
                graph._load_encoder_blocking()
        self.assertEqual(os.environ["HF_HUB_OFFLINE"], "1")

    def test_user_process_env_restores_only_what_we_forced(self):
        self._clean()
        os.environ["HF_HUB_OFFLINE"] = "0"
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        changed = model_manifest.force_model_offline_env()
        self.assertEqual(set(changed), {"HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"})
        child = model_manifest.user_process_env()
        self.assertEqual(child["HF_HUB_OFFLINE"], "0")
        self.assertNotIn("TRANSFORMERS_OFFLINE", child)
        self.assertEqual(child["HF_HUB_DISABLE_TELEMETRY"], "1")
        self.assertNotIn(model_manifest.FORCED_ENV_MARKER, child)

    def test_claude_root_launcher_child_env_has_no_forced_values(self):
        self._clean()
        model_manifest.force_model_offline_env()
        from core.integrations import claude_root_launcher as launcher

        captured = {}

        def _popen(argv, cwd=None, env=None, **kwargs):
            captured["env"] = env
            raise OSError("stop after capturing the environment")

        class _Conn:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, *a, **k):
                return None

            def close(self):
                return None

        with patch.object(launcher, "get_connection", return_value=_Conn()), patch.object(
            launcher.subprocess, "Popen", _popen
        ):
            try:
                launcher.launch_root_claude(["claude"], cwd=str(ROOT), bootstrap="x")
            except Exception:
                pass
        env = captured["env"]
        for key in self.KEYS:
            self.assertNotIn(key, env)
        self.assertNotIn(model_manifest.FORCED_ENV_MARKER, env)
        self.assertIn("ENGRAM_ROOT_CLIENT_TOKEN", env)


if __name__ == "__main__":
    unittest.main()
