"""Provider-specific CLI choices, independent from the UI toolkit."""
from __future__ import annotations
import json
from pathlib import Path

_COPILOT_MODELS = ["auto", "claude-sonnet-4.6", "gpt-5.4", "claude-haiku-4.5", "gpt-5.3-codex", "gemini-3.1-pro-preview", "gemini-3.5-flash", "gemini-3.6-flash", "mai-code-1-flash"]
_GEMINI_MODELS = ["auto", "pro", "flash", "flash-lite"]
_CLAUDE_MODELS = ["default", "best", "sonnet", "opus", "haiku", "opusplan", "sonnet[1m]", "opus[1m]"]
_COPILOT_EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]
_CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]

def model_key(provider: str) -> str | None:
    return {"copilot":"copilot_model", "gemini":"gemini_model", "codex":"codex_model", "claude-code":"claude_model", "claude-code-ollama":"ollama_model", "ollama":"ollama_model"}.get(provider)
def effort_key(provider: str) -> str | None:
    return {"copilot":"copilot_effort", "codex":"codex_reasoning_effort", "claude-code":"claude_effort", "claude-code-ollama":"claude_effort"}.get(provider)
def _display(values: list[str], current: object) -> list[str]:
    current = str(current or "").strip(); return values + ([current] if current and current not in values else [])
def codex_catalog(cache_path: Path | None = None) -> dict[str, list[str]]:
    try: data = json.loads((cache_path or Path.home()/".codex"/"models_cache.json").read_text(encoding="utf-8"))
    except Exception: return {}
    result: dict[str, list[str]] = {}
    for row in data.get("models", []) if isinstance(data, dict) else []:
        if not isinstance(row, dict) or not row.get("slug"): continue
        levels = row.get("supported_reasoning_levels", [])
        values = [str(x.get("effort")) for x in levels if isinstance(x, dict) and x.get("effort")]
        values += [str(x) for x in levels if isinstance(x, str)]
        default = row.get("default_reasoning_level")
        if default and str(default) not in values: values.append(str(default))
        result[str(row["slug"])] = values
    return result
def supported_models(provider: str, ollama_models: list[str] | None = None) -> list[str]:
    if provider == "copilot": return list(_COPILOT_MODELS)
    if provider == "gemini": return list(_GEMINI_MODELS)
    if provider == "codex": return list(codex_catalog())
    if provider == "claude-code": return list(_CLAUDE_MODELS)
    if provider in {"ollama", "claude-code-ollama"}: return list(ollama_models or [])
    return []
def supported_efforts(provider: str, model: str | None = None) -> list[str]:
    if provider == "copilot": return list(_COPILOT_EFFORTS)
    if provider == "codex": return codex_catalog().get(str(model or ""), [])
    if provider in {"claude-code", "claude-code-ollama"}: return list(_CLAUDE_EFFORTS)
    return []
def models(provider: str, cli: dict, ollama_models: list[str] | None = None) -> list[str]:
    key = model_key(provider); return _display(supported_models(provider, ollama_models), cli.get(key) if key else "")
def efforts(provider: str, cli: dict, model: str | None = None) -> list[str]:
    key = effort_key(provider); return _display(supported_efforts(provider, model or cli.get("codex_model")), cli.get(key) if key else "")
def control_state(provider: str, model: str | None = None) -> tuple[str, str]:
    return ("readonly" if supported_models(provider) else "disabled", "readonly" if supported_efforts(provider, model) else "disabled")
def validate(provider: str, cli: dict, ollama_models: list[str] | None = None) -> str | None:
    key = model_key(provider); model = str(cli.get(key) or "") if key else ""; known_models = supported_models(provider, ollama_models)
    if provider == "codex" and model and known_models and model not in known_models: return f"{provider} model is not supported: {model}"
    effort = str(cli.get(effort_key(provider) or "") or ""); known_efforts = supported_efforts(provider, model)
    if effort and known_efforts and effort not in known_efforts: return f"{provider} effort is not supported: {effort}"
    return None
