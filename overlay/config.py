"""설정 로딩 — 기본값 + 사용자 오버라이드 + 런타임 상태를 순서대로 병합."""

import copy
import os
import sys
import tempfile
import threading
from pathlib import Path

import yaml

from core.install.user_config import preserve_legacy_character_source_mode

_DEFAULT_REL = "config/overlay.yaml"


def _editable_project_root(start: Path) -> Path | None:
    """Find a real checkout without treating arbitrary copied installs as one."""
    try:
        candidates = (start, *start.parents)
    except OSError:
        return None
    for candidate in candidates:
        if (candidate / "INSTALL.ps1").is_file() and (candidate / "overlay" / "config.py").is_file():
            return candidate
    return None


def editable_project_root() -> Path | None:
    """Source checkout next to a frozen install, if it can be validated safely."""
    if not getattr(sys, "frozen", False):
        return Path(__file__).parent.parent
    return _editable_project_root(_get_base_dir())


def _get_base_dir() -> Path:
    """exe 실행 시 exe 위치, 개발 시 프로젝트 루트."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent.parent


def _get_bundle_dir() -> Path:
    """pyinstaller 번들 내부 리소스 경로."""
    if getattr(sys, "_MEIPASS", None):
        return Path(sys._MEIPASS)
    return Path(__file__).parent.parent


def resolve_path(rel: str) -> Path:
    """외부 파일 우선, 없으면 번들 내부 사용."""
    external = _get_base_dir() / rel
    if external.exists():
        return external
    return _get_bundle_dir() / rel


def resolve_editable_overlay_path(rel: str) -> Path:
    """Resolve editable overlay/character resources without redirecting other bundles.

    An explicit file beside the exe still wins.  A frozen install nested in a
    verified Project_Engram checkout can then use that checkout's live files;
    temporary PyInstaller extraction and copied installs stay bundle-only.
    """
    external = _get_base_dir() / rel
    if external.exists():
        return external
    project = editable_project_root()
    if project is not None:
        candidate = project / rel
        if candidate.exists():
            return candidate
    return _get_bundle_dir() / rel


def resolve_external_path(rel: str) -> Path:
    """실행 위치 기준 외부 경로를 반환한다(쓰기 가능한 대상 경로 계산용)."""
    return _get_base_dir() / rel


def _deep_merge(base: dict, override: dict) -> dict:
    """override 값을 base에 재귀적으로 병합. override가 우선."""
    result = copy.deepcopy(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


_USER_CONFIG_PATH = Path.home() / ".engram" / "overlay.user.yaml"
_STATE_PATH = Path.home() / ".engram" / "overlay.state.yaml"
_STATE_LOCK = threading.RLock()
_ENGRAM_USER_CONFIG_PATH = Path.home() / ".engram" / "user.config.yaml"
_SUPPORTED_CLI_PROVIDERS = {"copilot", "gemini", "codex", "claude-code", "claude-code-ollama", "ollama"}
_SUPPORTED_CHAT_MODES = {"tui", "bubble"}
_SUPPORTED_PERMISSION_LEVELS = {"auto", "confirm_risky", "confirm_always"}
_CLI_PROVIDER_ALIASES = {
    "claude": "claude-code",
    "claude_code": "claude-code",
    "claudecode": "claude-code",
    "claude-code(ollama)": "claude-code-ollama",
    "claude_code_ollama": "claude-code-ollama",
    "claudecodeollama": "claude-code-ollama",
}

_USER_TEMPLATE = """\
# 사용자 오버라이드 설정
# overlay.yaml의 기본값 위에 덮어씌워짐 (deep merge)
# 변경하고 싶은 값만 작성하면 됨 — 저장 후 다음 클릭에 바로 적용
# 캐릭터 리소스 탐색 순서:
# 1) ~/.engram/character/{name}/ 디렉토리 → sequence 설정에 따라 애니메이션
# 2) resource/character/sequences/{name}/ 디렉토리 → sequence 설정에 따라 애니메이션
# 3) ~/.engram/character/{name}.png → 정적 이미지
# 4) resource/character/static/{name}.png 또는 sets/{name}/character.png → 정적 이미지
# 5) ~/.engram/overlay.png → 정적 fallback
# 6) resource/overlay.png → 최종 fallback

# overlay:
#   char_height_ratio: 0.125
#   chat_mode: "bubble"  # bubble(기본) | tui
#   character:
#     source_mode: "sprite_grid"  # static | sequence | sprite_grid
#     set: "engram"  # ~/.engram/character/sets/<id>/ 우선, 없으면 bundled set
#     name: "smoke_chroma"
#     sequence:
#       enabled: true
#       trigger_chance: 0.12
#       start_index: 1
#       end_index: 2
#       repeat_count: 3
#       interval_min_sec: 0.2
#       interval_max_sec: 3.0
#       idle_check_interval_sec: 1.0
#     effects:
#       enabled: true
#       legacy_body_motion: false  # static에서 true일 때만 레거시 늘림/상하 이동
#       idle_asset: "resource/character/sets/engram/effects/idle.png"
#       click_asset: "resource/character/sets/engram/effects/click.png"
#       chroma_key: "#010101"
#       idle_interval_ms: 2400
#       click_frame_ms: 420
#       idle_thickness_px: 2
#       click_thickness_px: 3
#     reactions:
#       enabled: true  # engram 전용 state sheet. 공개 bubble event만 반응에 사용.
#       pack: "engram"  # ~/.engram/character/reactions/engram/manifest.yaml 우선
#       apply_to_custom: false
#       sprite_sheet: "resource/character/reactions/engram/states.png"
#       chroma_key: "#00FF00"
#       crop_y_offset_px: 0
#       columns: 6
#       rows: 4
#       scale_ratio: 0.38
#       dwell_ms: 2400
#       debounce_ms: 450
#       allow_text_keywords: true

# bubble:
#   permission_level: "auto"  # auto | confirm_risky | confirm_always

# terminal:
#   base_font_size: 8
#   width_ratio: 0.20
#   height_ratio: 0.60

# cli:
#   provider: "copilot"   # copilot | gemini | codex | claude-code | claude-code-ollama | ollama
#   # gemini/codex/claude-code는 ~/.engram 전용 shim을 우선 사용
#   gemini_command: "gemini"
#   codex_command: "codex"
#   # claude-code-ollama: 선택된 ollama_model을 Claude Code 백엔드 모델로 사용
#   # claude-code + ollama_model: claude --model <ollama_model>
#   # - model이 Claude alias/id가 아니면 ANTHROPIC_BASE_URL을
#   #   ollama_base_url(default: http://localhost:11434)로 주입
#   # - 모델이 tools capability가 없을 때 fallback 동작은
#   #   claude_ollama_no_tools_fallback으로 제어(ollama | none)
#   # ollama provider: ollama_command run <ollama_model>
#   ollama_command: "ollama"
#   ollama_model: "gemma3:4b"
#   ollama_base_url: "http://localhost:11434"
#   claude_ollama_no_tools_fallback: "ollama"

# discord:
#   # 단일값 + 배열값은 합집합으로 적용
#   guild_id: ""
#   guild_ids: []
#   channel_id: ""
#   channel_ids: []
#   allowed_user_ids: []
#   channel_cli_overrides:
#     "123456789012345678": "gemini"
#   guild_cli_overrides:
#     "987654321098765432": "ollama"
#   # scope_key 우선순위:
#   # 1) channel_scope_overrides[channel_id]
#   # 2) guild_scope_overrides[guild_id]
#   # 3) scope_key_template
#   # 4) 기본값 "{prefix}{channel_id}"
#   # 템플릿 토큰: {prefix}, {guild_id}, {channel_id}, {route_id}
#   scope_key_template: ""
#   channel_scope_overrides:
#     "123456789012345678": "{prefix}team-alpha"
#   guild_scope_overrides:
#     "987654321098765432": "{prefix}guild:{guild_id}"
#   deny_guild_ids: []
#   deny_channel_ids: []
#   deny_user_ids: []
#   queue:
#     max_per_channel: 8
#     ttl_seconds: 180
#     drop_policy: "drop_oldest"   # drop_oldest | drop_newest
#     max_parallel_channels: 3
#     notify_waiting: true
#     wait_notice_min_position: 2
#     wait_notice_cooldown_seconds: 20
#     notify_ttl_expired: true
"""


def _safe_load_yaml(path: Path, *, strict: bool = False) -> dict:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        if strict:
            raise ValueError(f"invalid YAML: {path}")
        return {}
    if isinstance(data, dict):
        return data
    if strict:
        raise ValueError(f"YAML mapping required: {path}")
    return {}


def get_overlay_state() -> dict:
    """Return a defensive copy of the optional runtime state mapping."""
    with _STATE_LOCK:
        return copy.deepcopy(_safe_load_yaml(_STATE_PATH))


def update_overlay_state(mutator) -> dict:
    """Atomically merge a small runtime-state update without clobbering peers."""
    with _STATE_LOCK:
        state = _safe_load_yaml(_STATE_PATH)
        result = mutator(state)
        if isinstance(result, dict):
            state = result
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="overlay.state.", suffix=".tmp", dir=str(_STATE_PATH.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                yaml.safe_dump(state, handle, sort_keys=False, allow_unicode=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, _STATE_PATH)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return copy.deepcopy(state)


def _filter_runtime_state_overrides(state: dict, user: dict) -> dict:
    """Keep explicit user CLI values authoritative over runtime state.

    Runtime state is useful for ephemeral selections, but if a key is explicitly
    present in `overlay.user.yaml`, we should not let `overlay.state.yaml`
    override it.
    """
    if not isinstance(state, dict) or not state:
        return {}

    filtered = copy.deepcopy(state)
    user_cli = user.get("cli") if isinstance(user, dict) else None
    state_cli = filtered.get("cli") if isinstance(filtered, dict) else None
    if isinstance(user_cli, dict) and isinstance(state_cli, dict):
        for key in tuple(state_cli.keys()):
            if key in user_cli:
                state_cli.pop(key, None)
        if not state_cli:
            filtered.pop("cli", None)
    return filtered


def normalize_cli_provider(provider: str | None) -> str:
    value = str(provider or "").strip().lower()
    value = _CLI_PROVIDER_ALIASES.get(value, value)
    if value in _SUPPORTED_CLI_PROVIDERS:
        return value
    return "copilot"


def get_cli_provider(cfg: dict | None = None) -> str:
    if cfg is None:
        cfg = load_cfg()
    cli_cfg = cfg.get("cli", {}) if isinstance(cfg, dict) else {}
    if not isinstance(cli_cfg, dict):
        cli_cfg = {}
    return normalize_cli_provider(cli_cfg.get("provider"))


def _set_user_cli_value(key: str, value: str) -> None:
    """overlay.user.yaml 의 cli 값을 업데이트한다."""
    user = _safe_load_yaml(_USER_CONFIG_PATH)
    cli_cfg = user.get("cli") if isinstance(user, dict) else None
    if not isinstance(cli_cfg, dict):
        cli_cfg = {}

    if value:
        cli_cfg[key] = value
    else:
        cli_cfg.pop(key, None)

    if cli_cfg:
        user["cli"] = cli_cfg
    else:
        user.pop("cli", None)

    _USER_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _USER_CONFIG_PATH.write_text(
        yaml.safe_dump(user, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )


def set_cli_provider(provider: str, sync_user: bool = False) -> str:
    """현재 CLI provider를 state에 저장하고, 필요 시 user 설정에도 동기화한다."""
    normalized = normalize_cli_provider(provider)
    def update(state: dict) -> None:
        cli_cfg = state.get("cli") if isinstance(state.get("cli"), dict) else {}
        cli_cfg["provider"] = normalized
        state["cli"] = cli_cfg
    update_overlay_state(update)
    if sync_user:
        _set_user_cli_value("provider", normalized)
    return normalized


def get_bubble_session_id() -> str | None:
    """말풍선 모드의 resume 대상 claude 세션 id. state.yaml에 영속화된다."""
    state = _safe_load_yaml(_STATE_PATH)
    bubble_cfg = state.get("bubble") if isinstance(state, dict) else None
    if not isinstance(bubble_cfg, dict):
        return None
    sid = bubble_cfg.get("claude_session_id")
    return str(sid) if sid else None


def set_bubble_session_id(session_id: str | None) -> None:
    """resume용 claude 세션 id를 state.yaml에 저장한다(None이면 제거)."""
    def update(state: dict) -> None:
        bubble_cfg = state.get("bubble") if isinstance(state.get("bubble"), dict) else {}
        if session_id:
            bubble_cfg["claude_session_id"] = session_id
        else:
            bubble_cfg.pop("claude_session_id", None)
        if bubble_cfg:
            state["bubble"] = bubble_cfg
        else:
            state.pop("bubble", None)
    update_overlay_state(update)


def get_ollama_model(cfg: dict | None = None) -> str:
    if cfg is None:
        cfg = load_cfg()
    cli_cfg = cfg.get("cli", {}) if isinstance(cfg, dict) else {}
    if not isinstance(cli_cfg, dict):
        cli_cfg = {}
    return str(cli_cfg.get("ollama_model") or "").strip()


def set_ollama_model(model: str, sync_user: bool = False) -> str:
    """ollama_model을 state에 저장하고, 필요 시 user 설정에도 동기화한다."""
    model = str(model or "").strip()
    def update(state: dict) -> None:
        cli_cfg = state.get("cli") if isinstance(state.get("cli"), dict) else {}
        cli_cfg["ollama_model"] = model
        state["cli"] = cli_cfg
    update_overlay_state(update)
    if sync_user:
        _set_user_cli_value("ollama_model", model)
    return model


def get_cli_model(provider: str, cfg: dict | None = None) -> str:
    if cfg is None: cfg = load_cfg()
    cli = cfg.get("cli", {}) if isinstance(cfg, dict) else {}
    keys = {"copilot": "copilot_model", "gemini": "gemini_model", "codex": "codex_model", "claude-code": "claude_model", "claude-code-ollama": "ollama_model", "ollama": "ollama_model"}
    return str(cli.get(keys.get(normalize_cli_provider(provider), "")) or "").strip() if isinstance(cli, dict) else ""


def set_cli_model(provider: str, model: str, sync_user: bool = False) -> str:
    provider, model = normalize_cli_provider(provider), str(model or "").strip()
    keys = {"copilot": "copilot_model", "gemini": "gemini_model", "codex": "codex_model", "claude-code": "claude_model", "claude-code-ollama": "ollama_model", "ollama": "ollama_model"}
    key = keys[provider]
    def update(state: dict) -> None:
        cli = state.get("cli") if isinstance(state.get("cli"), dict) else {}; cli[key] = model; state["cli"] = cli
    update_overlay_state(update)
    if sync_user: _set_user_cli_value(key, model)
    return model


def get_flip_horizontal(cfg: dict | None = None) -> bool:
    if cfg is None:
        cfg = load_cfg()
    overlay_cfg = cfg.get("overlay", {}) if isinstance(cfg, dict) else {}
    if not isinstance(overlay_cfg, dict):
        overlay_cfg = {}
    return bool(overlay_cfg.get("flip_horizontal", False))


def set_flip_horizontal(value: bool) -> bool:
    """캐릭터 좌우 반전 여부를 user.yaml에 영속화한다(설정창 체크박스·우클릭 메뉴 공용)."""
    value = bool(value)
    user = _safe_load_yaml(_USER_CONFIG_PATH)
    overlay_cfg = user.get("overlay") if isinstance(user, dict) else None
    if not isinstance(overlay_cfg, dict):
        overlay_cfg = {}

    if value:
        overlay_cfg["flip_horizontal"] = True
    else:
        overlay_cfg.pop("flip_horizontal", None)

    if overlay_cfg:
        user["overlay"] = overlay_cfg
    else:
        user.pop("overlay", None)

    _USER_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _USER_CONFIG_PATH.write_text(
        yaml.safe_dump(user, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )
    return value


def normalize_chat_mode(mode: str | None) -> str:
    value = str(mode or "").strip().lower()
    if value in _SUPPORTED_CHAT_MODES:
        return value
    return "bubble"


def get_chat_mode(cfg: dict | None = None) -> str:
    if cfg is None:
        cfg = load_cfg()
    overlay_cfg = cfg.get("overlay", {}) if isinstance(cfg, dict) else {}
    if not isinstance(overlay_cfg, dict):
        overlay_cfg = {}
    return normalize_chat_mode(overlay_cfg.get("chat_mode"))


def normalize_permission_level(level: str | None) -> str:
    value = str(level or "").strip().lower()
    if value in _SUPPORTED_PERMISSION_LEVELS:
        return value
    return "auto"


def get_permission_level(cfg: dict | None = None) -> str:
    if cfg is None:
        cfg = load_cfg()
    bubble_cfg = cfg.get("bubble", {}) if isinstance(cfg, dict) else {}
    if not isinstance(bubble_cfg, dict):
        bubble_cfg = {}
    return normalize_permission_level(bubble_cfg.get("permission_level"))


def get_bubble_cfg(cfg: dict | None = None) -> dict:
    """bubble: 섹션 전체(앵커/크기/dwell/fade 등 렌더링 설정)를 반환한다."""
    if cfg is None:
        cfg = load_cfg()
    bubble_cfg = cfg.get("bubble", {}) if isinstance(cfg, dict) else {}
    return bubble_cfg if isinstance(bubble_cfg, dict) else {}


def get_workdir(cfg: dict | None = None) -> Path:
    """overlay 터미널의 작업 디렉토리를 반환한다.

    우선순위:
    1) overlay config의 cli.workdir
    2) 설치 스크립트가 기록한 ~/.engram/user.config.yaml 의 workdir
    3) 사용자 홈 디렉토리
    """

    candidates: list[Path] = []

    if cfg is None:
        cfg = load_cfg()

    cli_cfg = cfg.get("cli", {}) if isinstance(cfg, dict) else {}
    if isinstance(cli_cfg, dict):
        raw_cli_workdir = str(cli_cfg.get("workdir") or "").strip()
        if raw_cli_workdir:
            p = Path(raw_cli_workdir).expanduser()
            if not p.is_absolute():
                p = _get_base_dir() / p
            candidates.append(p)

    engram_user_cfg = _safe_load_yaml(_ENGRAM_USER_CONFIG_PATH)
    raw_installed_workdir = str(engram_user_cfg.get("workdir") or "").strip()
    if raw_installed_workdir:
        candidates.append(Path(raw_installed_workdir).expanduser())

    for path in candidates:
        try:
            if path.exists() and path.is_dir():
                return path
        except OSError:
            continue

    return Path.home()


def load_cfg(*, strict: bool = False) -> dict:
    """기본 config 로드 후 user/state 오버라이드를 순서대로 병합."""
    with open(resolve_editable_overlay_path(_DEFAULT_REL), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, dict):
        raise ValueError("default overlay config must be a YAML mapping")

    if not _USER_CONFIG_PATH.exists():
        _USER_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _USER_CONFIG_PATH.write_text(_USER_TEMPLATE, encoding="utf-8")

    user = _safe_load_yaml(_USER_CONFIG_PATH, strict=strict)
    if user:
        preserve_legacy_character_source_mode(user)
        cfg = _deep_merge(cfg, user)

    state = _safe_load_yaml(_STATE_PATH)
    state = _filter_runtime_state_overrides(state, user)
    if state:
        cfg = _deep_merge(cfg, state)
    return cfg
