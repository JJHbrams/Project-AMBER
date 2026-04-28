"""캐릭터 오버레이 창 — 투명 always-on-top, 드래그/클릭 가능."""

import random
import re
import tkinter as tk
from pathlib import Path
from typing import Callable

from PIL import Image, ImageTk

from overlay.config import resolve_path, load_cfg

USER_CONFIG_DIR = Path.home() / ".engram"
USER_OVERLAY_RESOURCE = USER_CONFIG_DIR / "overlay.png"
USER_CHARACTER_DIR = USER_CONFIG_DIR / "character"
RESOURCE_OVERLAY = resolve_path("resource/overlay.png")
CHARACTER_DIR = resolve_path("resource/character")
_CHROMA = "#010101"
_SMALL_MODEL_RE = re.compile(r"\b[0-4](?:\.\d+)?b\b", re.IGNORECASE)


def _clamp_float(value, minimum: float, maximum: float, default: float) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, num))


def _clamp_int(value, minimum: int, maximum: int, default: int) -> int:
    try:
        num = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, num))


def _discover_numbered_frames(base_name: str, search_dirs: tuple[Path, ...]) -> dict[int, Path]:
    indexed: dict[int, Path] = {}
    pattern = re.compile(rf"^{re.escape(base_name)}_(\d+)\.png$", re.IGNORECASE)
    for directory in search_dirs:
        if not directory.exists():
            continue
        for path in directory.glob("*.png"):
            m = pattern.match(path.name)
            if not m:
                continue
            try:
                idx = int(m.group(1))
            except ValueError:
                continue
            # 앞선 directory가 우선순위를 갖도록 같은 index는 최초 발견값 유지
            if idx in indexed:
                continue
            indexed[idx] = path
    return indexed


class _CharacterProfile:
    def __init__(self, cfg: dict):
        overlay_cfg = cfg.get("overlay", {})
        character_cfg = overlay_cfg.get("character", {})
        seq_cfg = character_cfg.get("sequence", {})

        self.name = str(character_cfg.get("name", "")).strip()

        self.sequence_enabled = bool(seq_cfg.get("enabled", True))
        self.trigger_chance = _clamp_float(seq_cfg.get("trigger_chance", 0.12), 0.0, 1.0, 0.12)
        self.start_index = _clamp_int(seq_cfg.get("start_index", 1), 0, 999, 1)
        self.end_index = _clamp_int(seq_cfg.get("end_index", 2), 0, 999, 2)
        self.repeat_count = _clamp_int(seq_cfg.get("repeat_count", 3), 1, 20, 3)
        self.interval_min_sec = _clamp_float(seq_cfg.get("interval_min_sec", 0.2), 0.05, 10.0, 0.2)
        self.interval_max_sec = _clamp_float(seq_cfg.get("interval_max_sec", 3.0), self.interval_min_sec, 30.0, 3.0)
        self.idle_check_interval_sec = _clamp_float(seq_cfg.get("idle_check_interval_sec", 1.0), 0.1, 30.0, 1.0)

        self.frames_by_index: dict[int, Path] = {}
        self.default_frame: Path = RESOURCE_OVERLAY
        self.has_numbered_frames = False

        self._discover_frames()

    def _discover_frames(self):
        # 이름 미설정 → 바로 fallback
        if not self.name:
            self.default_frame = USER_OVERLAY_RESOURCE if USER_OVERLAY_RESOURCE.exists() else RESOURCE_OVERLAY
            return

        # 절대경로 직접 참조 (설정 창에서 파일/폴더를 선택한 경우)
        name_path = Path(self.name)
        if name_path.is_absolute():
            if name_path.is_dir():
                numbered = _discover_numbered_frames(name_path.name, (name_path,))
                if numbered:
                    self.frames_by_index = numbered
                    self.has_numbered_frames = True
                    self.default_frame = numbered.get(0, numbered[min(numbered.keys())])
                    return
                # 디렉토리에 번호 프레임이 없으면 첫 번째 .png 를 정적 이미지로
                pngs = sorted(name_path.glob("*.png"))
                if pngs:
                    self.default_frame = pngs[0]
                    return
            elif name_path.is_file() and name_path.suffix.lower() == ".png":
                self.default_frame = name_path
                return
            # 절대경로가 존재하지 않으면 fallback
            self.default_frame = USER_OVERLAY_RESOURCE if USER_OVERLAY_RESOURCE.exists() else RESOURCE_OVERLAY
            return

        # 2-1: {name}/ 서브디렉토리 탐색 (애니메이션)
        for char_subdir in (USER_CHARACTER_DIR / self.name, CHARACTER_DIR / self.name):
            if char_subdir.is_dir():
                numbered = _discover_numbered_frames(self.name, (char_subdir,))
                if numbered:
                    self.frames_by_index = numbered
                    self.has_numbered_frames = True
                    self.default_frame = numbered.get(0, numbered[min(numbered.keys())])
                    return

        # 2-2: {name}.png 단일 이미지 파일 탐색 (정적)
        for single in (USER_CHARACTER_DIR / f"{self.name}.png", CHARACTER_DIR / f"{self.name}.png"):
            if single.exists():
                self.default_frame = single
                return

        # 2-3: overlay.png fallback
        if USER_OVERLAY_RESOURCE.exists():
            self.default_frame = USER_OVERLAY_RESOURCE
        else:
            self.default_frame = RESOURCE_OVERLAY

    def build_sequence_paths(self) -> list[Path]:
        if not (self.sequence_enabled and self.has_numbered_frames and self.frames_by_index):
            return []

        if self.start_index <= self.end_index:
            order = list(range(self.start_index, self.end_index + 1))
        else:
            order = list(range(self.start_index, self.end_index - 1, -1))

        step_paths = [self.frames_by_index[i] for i in order if i in self.frames_by_index]
        if not step_paths:
            return []

        sequence: list[Path] = []
        for _ in range(self.repeat_count):
            sequence.extend(step_paths)
        return sequence


class CharacterOverlay:
    def __init__(
        self,
        root: tk.Tk,
        on_activate: Callable[[], None],
        on_set_provider: Callable[[str], None] | None = None,
        on_get_provider: Callable[[], str] | None = None,
        on_quit: Callable[[], None] | None = None,
        on_set_provider_model: Callable[[str, "str | None"], None] | None = None,
        on_get_ollama_models: Callable[[], list] | None = None,
        on_get_ollama_model: Callable[[], str] | None = None,
        on_reload_ollama_models: Callable[[], None] | None = None,
        on_settings: Callable[[], None] | None = None,
        on_restart: Callable[[], None] | None = None,
    ):
        self.root = root
        self.on_activate = on_activate
        self.on_set_provider = on_set_provider
        self.on_get_provider = on_get_provider
        self.on_quit = on_quit
        self.on_set_provider_model = on_set_provider_model
        self.on_get_ollama_models = on_get_ollama_models
        self.on_get_ollama_model = on_get_ollama_model
        self.on_reload_ollama_models = on_reload_ollama_models
        self.on_settings = on_settings
        self.on_restart = on_restart

        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", _CHROMA)
        self.root.configure(bg=_CHROMA)

        self._cfg = load_cfg()
        self._profile = _CharacterProfile(self._cfg)
        self._current_source = self._profile.default_frame
        self._sequence_queue: list[Path] = []
        self._context_menu_open = False

        self._load_image(source_path=self._current_source)
        self._place_default()
        self._bind_events()
        self._build_context_menu()
        self.root.deiconify()
        self._keep_topmost()
        self._schedule_animation_tick(initial=True)

    def _load_image(self, work_size: tuple[int, int] | None = None, source_path: Path | None = None):
        cfg = self._cfg["overlay"]
        if source_path is not None:
            self._current_source = source_path

        active_source = self._current_source if self._current_source.exists() else RESOURCE_OVERLAY
        if work_size:
            sw, sh = work_size
        else:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()

        # 짧은 축 기준 스케일링 (landscape→높이, portrait→너비)
        base = min(sw, sh)
        target_h = max(120, int(base * cfg["char_height_ratio"]))

        try:
            img = Image.open(active_source).convert("RGBA")
        except Exception:
            img = Image.open(RESOURCE_OVERLAY).convert("RGBA")

        scale = target_h / img.height
        target_w = int(img.width * scale)
        img = img.resize((target_w, target_h), Image.LANCZOS)
        self._img_w, self._img_h = target_w, target_h

        r, g, b, a = img.split()
        canvas = Image.new("RGB", img.size, (1, 1, 1))
        canvas.paste(Image.merge("RGB", (r, g, b)), mask=a)
        self._photo = ImageTk.PhotoImage(canvas)

        if hasattr(self, "_label") and self._label.winfo_exists():
            self._label.configure(image=self._photo)
            self._label.image = self._photo
            return

        self._label = tk.Label(
            self.root,
            image=self._photo,
            bg=_CHROMA,
            cursor="hand2",
            bd=0,
            highlightthickness=0,
        )
        self._label.pack()

    def _place_default(self):
        cfg = self._cfg["overlay"]
        sh = self.root.winfo_screenheight()
        x = cfg["char_margin_x"]
        y = sh - self._img_h - cfg["char_margin_y"]
        self.root.geometry(f"{self._img_w}x{self._img_h}+{x}+{y}")

    def _bind_events(self):
        self._press_x = 0
        self._press_y = 0
        self._moved = False

        self._label.bind("<ButtonPress-1>", self._on_press)
        self._label.bind("<B1-Motion>", self._on_drag)
        self._label.bind("<ButtonRelease-1>", self._on_release)
        self._label.bind("<Button-3>", self._on_context_menu_event)
        self.root.bind("<Button-3>", self._on_context_menu_event)

    def _on_context_menu_event(self, event):
        self._show_context_menu(event)
        # label/root 이중 바인딩 이벤트 전파를 막아 메뉴 중복 post를 방지한다.
        return "break"

    def _build_context_menu(self):
        self._provider_var = tk.StringVar(value=self._get_provider_value())
        self._claude_model_var = tk.StringVar()
        self._ollama_model_var = tk.StringVar()

        self._context_menu = tk.Menu(self.root, tearoff=0)
        self._context_menu.add_command(label="채팅 열기/닫기", command=self._invoke_activate)

        self._provider_menu = tk.Menu(self._context_menu, tearoff=0)
        self._claude_submenu = tk.Menu(self._provider_menu, tearoff=0)
        self._ollama_submenu = tk.Menu(self._provider_menu, tearoff=0)

        self._context_menu.add_cascade(label="CLI 공급자", menu=self._provider_menu)
        self._context_menu.add_separator()
        self._context_menu.add_command(label="설정", command=self._invoke_settings)
        self._context_menu.add_command(label="재시작", command=self._invoke_restart)
        self._context_menu.add_separator()
        self._context_menu.add_command(label="종료", command=self._invoke_quit)

    def _get_ollama_model_value(self) -> str:
        if self.on_get_ollama_model is None:
            return ""
        try:
            return str(self.on_get_ollama_model() or "")
        except Exception:
            return ""

    def _get_ollama_models_value(self) -> list:
        if self.on_get_ollama_models is None:
            return []
        try:
            return list(self.on_get_ollama_models() or [])
        except Exception:
            return []

    @staticmethod
    def _is_ollama_routing(model: str) -> bool:
        m = (model or "").lower().strip()
        claude_aliases = {"default", "best", "sonnet", "opus", "haiku", "opusplan", "sonnet[1m]", "opus[1m]"}
        return bool(m) and not m.startswith("claude-") and m not in claude_aliases

    def _rebuild_provider_menu(self):
        """팝업 직전에 현재 상태를 반영해 provider 서브메뉴를 재구성한다."""
        self._provider_menu.delete(0, "end")
        self._claude_submenu.delete(0, "end")
        self._ollama_submenu.delete(0, "end")

        current_provider = self._get_provider_value()
        current_model = self._get_ollama_model_value()
        models = self._get_ollama_models_value()

        # ── 상위 flat 항목: Copilot / Gemini ────────────────────────
        self._provider_var.set(current_provider)
        self._provider_menu.add_checkbutton(
            label="Copilot CLI",
            onvalue="copilot",
            offvalue="",
            variable=self._provider_var,
            command=lambda: self._select_provider_model("copilot", None),
        )
        self._provider_menu.add_checkbutton(
            label="Gemini CLI",
            onvalue="gemini",
            offvalue="",
            variable=self._provider_var,
            command=lambda: self._select_provider_model("gemini", None),
        )
        self._provider_menu.add_separator()

        # ── Claude Code 서브메뉴 ────────────────────────────────────
        if current_provider == "claude-code":
            self._claude_model_var.set(current_model if self._is_ollama_routing(current_model) else "direct")
        else:
            self._claude_model_var.set("")

        self._claude_submenu.add_checkbutton(
            label="claude (직접)",
            onvalue="direct",
            offvalue="",
            variable=self._claude_model_var,
            command=lambda: self._select_provider_model("claude-code", ""),
        )
        self._claude_submenu.add_separator()
        if models:
            for _m in models:
                self._claude_submenu.add_checkbutton(
                    label=f"ollama: {_m}",
                    onvalue=_m,
                    offvalue="",
                    variable=self._claude_model_var,
                    command=lambda mod=_m: self._select_provider_model("claude-code", mod),
                )
        else:
            self._claude_submenu.add_command(label="(Ollama 모델 없음)", state="disabled")
        self._claude_submenu.add_separator()
        self._claude_submenu.add_command(label="Ollama 새로고침", command=self._invoke_reload_ollama_models)

        claude_label = f"{'✓' if current_provider == 'claude-code' else ' '} Claude Code"
        self._provider_menu.add_cascade(label=claude_label, menu=self._claude_submenu)
        self._provider_menu.add_separator()

        # ── Ollama 서브메뉴 ─────────────────────────────────────────
        if current_provider == "ollama":
            self._ollama_model_var.set(current_model)
        else:
            self._ollama_model_var.set("")

        if models:
            for _m in models:
                self._ollama_submenu.add_checkbutton(
                    label=_m,
                    onvalue=_m,
                    offvalue="",
                    variable=self._ollama_model_var,
                    command=lambda mod=_m: self._select_provider_model("ollama", mod),
                )
        else:
            self._ollama_submenu.add_command(label="(Ollama 모델 없음)", state="disabled")
        self._ollama_submenu.add_separator()
        self._ollama_submenu.add_command(label="새로고침", command=self._invoke_reload_ollama_models)

        ollama_label = f"{'✓' if current_provider == 'ollama' else ' '} Ollama"
        self._provider_menu.add_cascade(label=ollama_label, menu=self._ollama_submenu)

    def _show_context_menu(self, event):
        if self._context_menu_open:
            return

        self._rebuild_provider_menu()
        was_topmost = bool(self.root.attributes("-topmost"))
        self._context_menu_open = True
        try:
            # popup 메뉴가 최상위로 유지되도록 오버레이 topmost를 잠시 양보한다.
            if was_topmost:
                self.root.attributes("-topmost", False)
                self.root.update_idletasks()
            self._context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                self._context_menu.grab_release()
            except Exception:
                pass
            self._context_menu_open = False
            if was_topmost:
                self.root.attributes("-topmost", True)
            self.root.lift()

    def _dismiss_context_menu(self):
        def _close_once():
            for menu in (getattr(self, "_provider_menu", None), getattr(self, "_context_menu", None)):
                if menu is None:
                    continue
                try:
                    menu.unpost()
                except Exception:
                    pass
                try:
                    menu.grab_release()
                except Exception:
                    pass

        _close_once()
        # 서브메뉴 command 콜백 중에는 즉시 unpost가 누락될 수 있어 지연 닫기를 추가한다.
        try:
            self.root.after(0, _close_once)
            self.root.after(25, _close_once)
        except Exception:
            pass
        self._context_menu_open = False
        try:
            self.root.attributes("-topmost", True)
            self.root.lift()
        except Exception:
            pass

    def _get_provider_value(self) -> str:
        if self.on_get_provider is None:
            return "copilot"
        try:
            return str(self.on_get_provider())
        except Exception:
            self._log_overlay_exception()
            return "copilot"

    def _select_provider_model(self, provider: str, model: "str | None"):
        self._dismiss_context_menu()
        if model and _SMALL_MODEL_RE.search(model):
            import tkinter.messagebox as mb

            proceed = mb.askyesno(
                "\u26a0\ufe0f 소형 모델 경고",
                f"'{model}'은(는) 소형 모델입니다.\n\n"
                "engram의 복잡한 시스템 프롬프트(정체성 + 페르소나 + 지침 + 기억)를\n"
                "처리하기에 파라미터가 부족해 지시 무시, 도구 호출 실패 등이\n"
                "발생할 수 있습니다. (권장: 7B 이상)\n\n"
                "계속 진행하시겠습니까?",
                parent=self.root,
            )
            if not proceed:
                return
        self._provider_var.set(provider)
        if self.on_set_provider_model is not None:
            try:
                self.on_set_provider_model(provider, model)
            except Exception:
                self._log_overlay_exception()
        elif self.on_set_provider is not None:
            try:
                self.on_set_provider(provider)
            except Exception:
                self._log_overlay_exception()

    def _select_provider(self, provider: str):
        """하위 호환 — on_set_provider_model 없을 때 fallback."""
        self._select_provider_model(provider, None)

    def _invoke_activate(self):
        self._dismiss_context_menu()
        try:
            self.on_activate()
        except Exception:
            self._log_overlay_exception()

    def _invoke_settings(self):
        self._dismiss_context_menu()
        if self.on_settings is None:
            return
        try:
            self.on_settings()
        except Exception:
            self._log_overlay_exception()

    def _invoke_restart(self):
        self._dismiss_context_menu()
        if self.on_restart is None:
            return
        try:
            self.on_restart()
        except Exception:
            self._log_overlay_exception()

    def _invoke_quit(self):
        self._dismiss_context_menu()
        if self.on_quit is None:
            return
        try:
            self.on_quit()
        except Exception:
            self._log_overlay_exception()

    def _invoke_reload_ollama_models(self):
        self._dismiss_context_menu()
        if self.on_reload_ollama_models is not None:
            try:
                self.on_reload_ollama_models()
            except Exception:
                self._log_overlay_exception()

    def _log_overlay_exception(self):
        import logging
        import traceback

        logging.basicConfig(filename=str(Path.home() / ".engram" / "overlay_error.log"), level=logging.ERROR)
        logging.error(traceback.format_exc())

    def _on_press(self, event):
        self._press_x = event.x_root
        self._press_y = event.y_root
        self._moved = False

    def _on_drag(self, event):
        dx = event.x_root - self._press_x
        dy = event.y_root - self._press_y
        if abs(dx) > 4 or abs(dy) > 4:
            self._moved = True
        x = self.root.winfo_x() + dx
        y = self.root.winfo_y() + dy
        self._press_x = event.x_root
        self._press_y = event.y_root
        self.root.geometry(f"+{x}+{y}")

    def _keep_topmost(self):
        """주기적으로 창을 맨 위로 올려 작업표시줄 등에 가리지 않게 유지."""
        if not self._context_menu_open:
            self.root.lift()
            self.root.attributes("-topmost", True)
        self.root.after(500, self._keep_topmost)

    def _set_frame(self, source_path: Path):
        self._reload_image_for_current_monitor(source_path=source_path)

    def _schedule_animation_tick(self, initial: bool = False):
        if initial:
            self.root.after(200, self._animation_tick)
            return
        idle_ms = int(self._profile.idle_check_interval_sec * 1000)
        self.root.after(max(100, idle_ms), self._animation_tick)

    def _animation_tick(self):
        if self._sequence_queue:
            next_frame = self._sequence_queue.pop(0)
            self._set_frame(next_frame)
            if self._sequence_queue:
                sec = random.uniform(self._profile.interval_min_sec, self._profile.interval_max_sec)
                self.root.after(max(50, int(sec * 1000)), self._animation_tick)
                return

            if self._current_source != self._profile.default_frame:
                self._set_frame(self._profile.default_frame)
            self._schedule_animation_tick()
            return

        if self._current_source != self._profile.default_frame:
            self._set_frame(self._profile.default_frame)

        if random.random() <= self._profile.trigger_chance:
            sequence = self._profile.build_sequence_paths()
            if sequence:
                self._sequence_queue = sequence
                self.root.after(0, self._animation_tick)
                return

        self._schedule_animation_tick()

    def _reload_image_for_current_monitor(self, source_path: Path | None = None):
        """드래그 후 현재 모니터 해상도 기준으로 캐릭터 이미지 재계산."""
        import win32api

        cx = self.root.winfo_x() + self._img_w // 2
        cy = self.root.winfo_y() + self._img_h // 2
        try:
            hmon = win32api.MonitorFromPoint((cx, cy), 2)
            mon_info = win32api.GetMonitorInfo(hmon)
            wl, wt, wr, wb = mon_info["Work"]
            work_size = (wr - wl, wb - wt)
        except Exception:
            work_size = (self.root.winfo_screenwidth(), self.root.winfo_screenheight())

        old_h = self._img_h
        self._load_image(work_size=work_size, source_path=source_path)

        # 창 크기 갱신 (y 위치는 비율 유지)
        x = self.root.winfo_x()
        y = self.root.winfo_y()
        if old_h > 0:
            y = y + old_h - self._img_h  # 하단 기준 유지
        self.root.geometry(f"{self._img_w}x{self._img_h}+{x}+{y}")

    def _on_release(self, event):
        if self._moved:
            self._reload_image_for_current_monitor()
        else:
            self._invoke_activate()

    def get_phys_rect(self):
        """tkinter 논리 좌표 반환 — wt --pos 와 동일한 좌표계."""
        return (
            self.root.winfo_x(),
            self.root.winfo_y(),
            self._img_w,
            self._img_h,
        )
