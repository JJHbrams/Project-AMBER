"""'업데이트 확인' UI orchestration for the character overlay.

Network access and file downloads run on a background daemon thread; every
UI mutation (messagebox, progress dialog) is marshalled back onto the Tk
main thread via ``root.after(0, ...)``.  Widgets are never touched from the
worker thread directly.

Every branch of this flow ends in a messagebox visible to the user:
up-to-date, update installed, user-cancelled, network failure, rate limit,
missing asset, or disk-write failure.  A silent ``return`` here is exactly
the kind of defect this feature exists to avoid.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Optional

import urllib.request

from core.install.versioning import resolve_version
from core.update.github_release import (
    UpdateCheckError,
    UpdateResult,
    check_update,
)

log = logging.getLogger(__name__)

_DOWNLOAD_CHUNK_SIZE = 1 << 16


def _updates_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "engram-overlay" / "updates"


def _format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


class _DownloadCancelled(Exception):
    pass


class _ProgressDialog:
    def __init__(self, root: tk.Tk, total_size: int):
        self.cancelled = False
        self.window = tk.Toplevel(root)
        self.window.title("업데이트 다운로드 중")
        self.window.resizable(False, False)
        self.window.attributes("-topmost", True)
        self.window.protocol("WM_DELETE_WINDOW", self._on_cancel)

        self._label = tk.Label(self.window, text=f"0 / {_format_size(total_size)}")
        self._label.pack(padx=16, pady=(12, 4))

        self._progress = ttk.Progressbar(
            self.window, orient="horizontal", length=280, mode="determinate", maximum=100
        )
        self._progress.pack(padx=16, pady=4)

        cancel_button = tk.Button(self.window, text="취소", command=self._on_cancel)
        cancel_button.pack(pady=(4, 12))

        self.window.update_idletasks()
        self.window.grab_set()

    def _on_cancel(self):
        self.cancelled = True

    def update_progress(self, downloaded: int, total: int):
        percent = min(100, int(downloaded * 100 / max(total, 1)))
        self._progress["value"] = percent
        self._label.configure(text=f"{_format_size(downloaded)} / {_format_size(total)}")

    def close(self):
        try:
            self.window.grab_release()
        except Exception:
            pass
        try:
            self.window.destroy()
        except Exception:
            pass


def check_for_update(root: tk.Tk, parent: Optional[tk.Misc] = None) -> None:
    dialog_parent = parent if parent is not None else root

    try:
        current_version = resolve_version().version
    except (OSError, ValueError) as exc:
        log.warning("[update_check] 현재 버전 확인 실패: %s", exc)
        messagebox.showerror(
            "업데이트 확인 실패",
            f"현재 버전을 확인하지 못했습니다.\n{exc}",
            parent=dialog_parent,
        )
        return

    def _show_error(reason: str):
        messagebox.showerror("업데이트 확인 실패", reason, parent=dialog_parent)

    def _worker():
        try:
            result = check_update(current_version)
        except UpdateCheckError as exc:
            root.after(0, lambda reason=exc.reason: _show_error(reason))
            return
        except Exception as exc:
            log.exception("[update_check] 예상치 못한 오류")
            root.after(0, lambda exc=exc: _show_error(f"업데이트 확인 중 오류가 발생했습니다.\n{exc}"))
            return

        root.after(0, lambda: _handle_result(result))

    def _handle_result(result: UpdateResult):
        if not result.is_update_available:
            messagebox.showinfo(
                "업데이트 확인",
                f"현재 최신 버전입니다.\n\n현재 버전: {result.current_version}\n최신 버전: {result.latest_version}",
                parent=dialog_parent,
            )
            return

        asset = result.asset
        if asset is None:
            _show_error("릴리스에 설치 파일이 없습니다.")
            return

        proceed = messagebox.askyesno(
            "새 버전 발견",
            (
                f"현재 버전: {result.current_version}\n"
                f"최신 버전: {result.latest_version}\n"
                f"다운로드 크기: {_format_size(asset.size)}\n\n"
                "지금 다운로드하고 설치하시겠습니까?"
            ),
            parent=dialog_parent,
        )
        if not proceed:
            messagebox.showinfo(
                "업데이트 취소",
                "업데이트를 설치하지 않았습니다. 나중에 다시 확인할 수 있습니다.",
                parent=dialog_parent,
            )
            return

        _start_download(asset)

    def _start_download(asset):
        try:
            target_dir = _updates_dir()
            target_dir.mkdir(parents=True, exist_ok=True)
            target_path = target_dir / asset.name
        except OSError as exc:
            messagebox.showerror(
                "다운로드 실패",
                f"업데이트 파일을 저장할 폴더를 만들지 못했습니다.\n{exc}",
                parent=dialog_parent,
            )
            return

        dialog = _ProgressDialog(root, asset.size)

        def _download_worker():
            try:
                request = urllib.request.Request(
                    asset.download_url,
                    headers={"User-Agent": "ProjectIntelContinuum-UpdateCheck/1.0"},
                )
                downloaded = 0
                with urllib.request.urlopen(request, timeout=30) as response:
                    with open(target_path, "wb") as handle:
                        while True:
                            if dialog.cancelled:
                                raise _DownloadCancelled()
                            chunk = response.read(_DOWNLOAD_CHUNK_SIZE)
                            if not chunk:
                                break
                            handle.write(chunk)
                            downloaded += len(chunk)
                            root.after(0, lambda d=downloaded: dialog.update_progress(d, asset.size))
            except _DownloadCancelled:
                try:
                    target_path.unlink(missing_ok=True)
                except OSError:
                    pass
                root.after(0, lambda: _finish_cancelled(dialog))
                return
            except OSError as exc:
                try:
                    target_path.unlink(missing_ok=True)
                except OSError:
                    pass
                root.after(0, lambda exc=exc: _finish_download_failed(dialog, exc))
                return
            except Exception as exc:
                log.exception("[update_check] 다운로드 중 오류")
                try:
                    target_path.unlink(missing_ok=True)
                except OSError:
                    pass
                root.after(0, lambda exc=exc: _finish_download_failed(dialog, exc))
                return

            root.after(0, lambda: _finish_download_succeeded(dialog, target_path))

        threading.Thread(target=_download_worker, daemon=True).start()

    def _finish_cancelled(dialog: _ProgressDialog):
        dialog.close()
        messagebox.showinfo("업데이트 취소", "다운로드가 취소되었습니다.", parent=dialog_parent)

    def _finish_download_failed(dialog: _ProgressDialog, exc: Exception):
        dialog.close()
        messagebox.showerror(
            "다운로드 실패",
            f"업데이트 파일을 저장하지 못했습니다.\n{exc}",
            parent=dialog_parent,
        )

    def _finish_download_succeeded(dialog: _ProgressDialog, installer_path: Path):
        dialog.close()
        proceed = messagebox.askyesno(
            "다운로드 완료",
            "다운로드가 완료되었습니다. 지금 설치를 시작하시겠습니까?\n"
            "설치를 시작하면 오버레이가 종료됩니다.",
            parent=dialog_parent,
        )
        if not proceed:
            messagebox.showinfo(
                "설치 보류",
                f"설치를 취소했습니다. 받아둔 설치 파일은 여기 있습니다: {installer_path}\n"
                "나중에 직접 실행할 수 있습니다.",
                parent=dialog_parent,
            )
            return
        _launch_installer_and_quit(installer_path)

    def _launch_installer_and_quit(installer_path: Path):
        # Inno Setup's PrepareToInstall step terminates a running overlay
        # process before it proceeds with the install. Reversing this order
        # (quit first, then spawn) would race the installer against an
        # overlay process that is still shutting down, or leave nothing to
        # spawn the installer from. Spawn first, then quit -- the same
        # spawn-then-quit pattern as overlay/main.py's restart().
        try:
            subprocess.Popen([str(installer_path)], cwd=str(installer_path.parent))
        except OSError as exc:
            messagebox.showerror(
                "설치 실행 실패",
                f"설치 프로그램을 실행하지 못했습니다.\n{exc}\n\n파일 위치: {installer_path}",
                parent=dialog_parent,
            )
            return

        def _quit_after_spawn():
            try:
                root.quit()
            except Exception:
                pass
            try:
                root.destroy()
            except Exception:
                pass

        root.after(500, _quit_after_spawn)

    threading.Thread(target=_worker, daemon=True).start()
