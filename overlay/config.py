"""설정 로딩 — 기본값 + 사용자 오버라이드 + 런타임 상태를 순서대로 병합."""

import copy
import sys
from pathlib import Path

import yaml

_DEFAULT_REL = "config/overlay.yaml"


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
_ENGRAM_USER_CONFIG_PATH = Path.home() / ".engram" / "user.config.yaml"
_SUPPORTED_CLI_PROVIDERS = {"copilot", "gemini", "claude-code", "claude-code-ollama", "ollama"}
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
# 2) resource/character/{name}/ 디렉토리 → sequence 설정에 따라 애니메이션
# 3) ~/.engram/character/{name}.png → 정적 이미지
# 4) resource/character/{name}.png → 정적 이미지
# 5) ~/.engram/overlay.png → 정적 fallback
# 6) resource/overlay.png → 최종 fallback

# overlay:
#   char_height_ratio: 0.125
#   character:
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

# terminal:
#   base_font_size: 8
#   width_ratio: 0.20
#   height_ratio: 0.60

# cli:
#   provider: "copilot"   # copilot | gemini | claude-code | claude-code-ollama | ollama
#   # gemini/claude-code는 ~/.engram 전용 shim을 우선 사용
#   gemini_command: "gemini"
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


def _safe_load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(data, dict):
        return data
    return {}


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


def set_cli_provider(provider: str) -> str:
    """오버레이 런타임 상태 파일에 현재 CLI provider를 저장한다."""
    normalized = normalize_cli_provider(provider)
    state = _safe_load_yaml(_STATE_PATH)
    cli_cfg = state.get("cli") if isinstance(state, dict) else None
    if not isinstance(cli_cfg, dict):
        cli_cfg = {}
    cli_cfg["provider"] = normalized
    state["cli"] = cli_cfg

    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STATE_PATH.write_text(
        yaml.safe_dump(state, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )
    return normalized


def get_ollama_model(cfg: dict | None = None) -> str:
    if cfg is None:
        cfg = load_cfg()
    cli_cfg = cfg.get("cli", {}) if isinstance(cfg, dict) else {}
    if not isinstance(cli_cfg, dict):
        cli_cfg = {}
    return str(cli_cfg.get("ollama_model") or "").strip()


def set_ollama_model(model: str) -> str:
    """ollama_model을 런타임 상태 파일에 저장한다."""
    model = str(model or "").strip()
    state = _safe_load_yaml(_STATE_PATH)
    cli_cfg = state.get("cli") if isinstance(state, dict) else None
    if not isinstance(cli_cfg, dict):
        cli_cfg = {}
    cli_cfg["ollama_model"] = model
    state["cli"] = cli_cfg

    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STATE_PATH.write_text(
        yaml.safe_dump(state, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )
    return model


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


def load_cfg() -> dict:
    """기본 config 로드 후 user/state 오버라이드를 순서대로 병합."""
    with open(resolve_path(_DEFAULT_REL), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    if not _USER_CONFIG_PATH.exists():
        _USER_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _USER_CONFIG_PATH.write_text(_USER_TEMPLATE, encoding="utf-8")

    user = _safe_load_yaml(_USER_CONFIG_PATH)
    if user:
        cfg = _deep_merge(cfg, user)

    state = _safe_load_yaml(_STATE_PATH)
    if state:
        cfg = _deep_merge(cfg, state)
    return cfg
