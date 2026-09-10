"""Owned, click-through Win32 per-pixel shadow; never samples the desktop."""

import ctypes
from ctypes import wintypes as wt
from PIL import Image, ImageDraw, ImageFilter
import win32api
import win32gui


def _window_proc(hwnd, message, wparam, lparam):
    if message == 0x84:  # WM_NCHITTEST
        return -1  # HTTRANSPARENT
    if message == 0x21:  # WM_MOUSEACTIVATE
        return 3  # MA_NOACTIVATE
    return win32gui.DefWindowProc(hwnd, message, wparam, lparam)


class _BitmapHeader(ctypes.Structure):
    _fields_ = [
        ("size", wt.DWORD),
        ("width", wt.LONG),
        ("height", wt.LONG),
        ("planes", wt.WORD),
        ("bits", wt.WORD),
        ("compression", wt.DWORD),
        ("image_size", wt.DWORD),
        ("x", wt.LONG),
        ("y", wt.LONG),
        ("used", wt.DWORD),
        ("important", wt.DWORD),
    ]


class _Blend(ctypes.Structure):
    _fields_ = [
        ("operation", wt.BYTE),
        ("flags", wt.BYTE),
        ("alpha", wt.BYTE),
        ("format", wt.BYTE),
    ]


class SessionShadow:
    """One native owned surface behind the current center, no timer or focus."""

    def __init__(self):
        self.hwnd = 0
        self.bounds = None
        self.image = None
        self.visible = False
        self._signature = None
        self._behind = None
        self._cache = {}
        name = "EngramSessionShadow"
        instance = win32api.GetModuleHandle(None)
        try:
            cls = win32gui.WNDCLASS()
            cls.hInstance, cls.lpszClassName = instance, name
            cls.lpfnWndProc = _window_proc
            win32gui.RegisterClass(cls)
        except win32gui.error as error:
            if error.winerror != 1410:  # Class already exists in this process.
                raise
        self.hwnd = win32gui.CreateWindowEx(
            0x80000 | 0x20 | 0x08000000 | 0x80,
            name,
            "",
            0x80000000,
            0,
            0,
            1,
            1,
            # Lifecycle-owned by SessionStackWidget, intentionally no GW_OWNER.
            # Tk periodically lifts its root and would promote this native
            # owned-popup group above the independently ordered card HWNDs.
            0,
            0,
            instance,
            None,
        )

    def update(self, center_bounds, work_rect, opacity=1.0):
        signature = (center_bounds, work_rect, round(opacity, 3))
        if signature == self._signature:
            return
        x, y, width, height = center_bounds
        margin = 16
        size = (width + margin * 2, height + margin * 2)
        if size not in self._cache:
            mask = Image.new("L", size)
            ImageDraw.Draw(mask).rounded_rectangle(
                (margin, margin + 4, margin + width - 1, margin + height + 3),
                radius=14,
                fill=95,
            )
            mask = mask.filter(ImageFilter.GaussianBlur(6))
            shadow = Image.new("RGBA", size, (0, 0, 0, 0))
            shadow.putalpha(mask)
            if len(self._cache) > 24:
                self._cache.clear()
            self._cache[size] = shadow
        left, top = x - margin, y - margin
        clip = (
            max(left, work_rect[0]),
            max(top, work_rect[1]),
            min(left + size[0], work_rect[2]),
            min(top + size[1], work_rect[3]),
        )
        if clip[2] <= clip[0] or clip[3] <= clip[1]:
            self.hide()
            return
        self.image = self._cache[size].crop(
            (clip[0] - left, clip[1] - top, clip[2] - left, clip[3] - top)
        )
        if opacity != 1:
            self.image.putalpha(
                self.image.getchannel("A").point(lambda a: int(a * opacity))
            )
        self.bounds = (clip[0], clip[1], clip[2] - clip[0], clip[3] - clip[1])
        self._upload()
        self._signature = signature

    def _upload(self):
        gdi, user = ctypes.windll.gdi32, ctypes.windll.user32
        gdi.CreateCompatibleDC.argtypes, gdi.CreateCompatibleDC.restype = [
            wt.HDC
        ], wt.HDC
        gdi.CreateDIBSection.argtypes = [
            wt.HDC,
            ctypes.POINTER(_BitmapHeader),
            wt.UINT,
            ctypes.POINTER(ctypes.c_void_p),
            wt.HANDLE,
            wt.DWORD,
        ]
        gdi.CreateDIBSection.restype = wt.HBITMAP
        gdi.SelectObject.argtypes, gdi.SelectObject.restype = [
            wt.HDC,
            wt.HANDLE,
        ], wt.HANDLE
        gdi.DeleteObject.argtypes = [wt.HANDLE]
        gdi.DeleteDC.argtypes = [wt.HDC]
        user.UpdateLayeredWindow.argtypes = [
            wt.HWND,
            wt.HDC,
            ctypes.POINTER(wt.POINT),
            ctypes.POINTER(wt.SIZE),
            wt.HDC,
            ctypes.POINTER(wt.POINT),
            wt.DWORD,
            ctypes.POINTER(_Blend),
            wt.DWORD,
        ]
        x, y, width, height = self.bounds
        info = _BitmapHeader(
            ctypes.sizeof(_BitmapHeader), width, -height, 1, 32, 0, 0, 0, 0, 0, 0
        )
        bits = ctypes.c_void_p()
        dc = gdi.CreateCompatibleDC(None)
        if not dc:
            raise ctypes.WinError()
        bitmap = previous = None
        try:
            bitmap = gdi.CreateDIBSection(
                dc, ctypes.byref(info), 0, ctypes.byref(bits), None, 0
            )
            if not bitmap or not bits.value:
                raise ctypes.WinError()
            previous = gdi.SelectObject(dc, bitmap)
            if not previous or previous == ctypes.c_void_p(-1).value:
                raise ctypes.WinError()
            # RGB is black, already premultiplied for every alpha value.
            data = self.image.tobytes("raw", "BGRA")
            ctypes.memmove(bits, data, len(data))
            destination, source, size = (
                wt.POINT(x, y),
                wt.POINT(0, 0),
                wt.SIZE(width, height),
            )
            blend = _Blend(0, 0, 255, 1)
            if not user.UpdateLayeredWindow(
                self.hwnd,
                None,
                ctypes.byref(destination),
                ctypes.byref(size),
                dc,
                ctypes.byref(source),
                0,
                ctypes.byref(blend),
                2,
            ):
                raise ctypes.WinError()
        finally:
            if previous and previous != ctypes.c_void_p(-1).value:
                gdi.SelectObject(dc, previous)
            if bitmap:
                gdi.DeleteObject(bitmap)
            gdi.DeleteDC(dc)

    def place_behind(self, center_hwnd, force=False):
        if not self.visible or force or center_hwnd != self._behind:
            win32gui.SetWindowPos(
                self.hwnd, center_hwnd, 0, 0, 0, 0, 0x1 | 0x2 | 0x10 | 0x40
            )
            self.visible, self._behind = True, center_hwnd

    def hide(self):
        if self.hwnd and self.visible:
            win32gui.ShowWindow(self.hwnd, 0)
        self.visible = False
        self._signature = None

    def destroy(self):
        self.hide()
        if self.hwnd:
            win32gui.DestroyWindow(self.hwnd)
            self.hwnd = 0
