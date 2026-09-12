"""Private rich-input limits shared by the native shell host boundary.

This module does not decode clipboard files or persist them.  It validates the
already-private data URLs delivered by the shell before a provider adapter sees
them.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from io import BytesIO
from PIL import Image, UnidentifiedImageError

MIB = 1024 * 1024
MAX_ATTACHMENTS = 4
MAX_ATTACHMENT_BYTES = 5 * MIB
MAX_TOTAL_BYTES = 20 * MIB
MAX_DECODED_PIXELS = 40_000_000
_MIMES = {"image/png": "png", "image/jpeg": "jpeg"}


@dataclass(frozen=True, slots=True)
class Attachment:
    mime_type: str
    data: bytes = field(repr=False)


def validate_attachments(items: object) -> tuple[Attachment, ...]:
    """Decode bounded PNG/JPEG data URLs without logging their content."""
    if not isinstance(items, list) or len(items) > MAX_ATTACHMENTS:
        raise ValueError("attachment count is invalid")
    result: list[Attachment] = []
    total = 0
    for item in items:
        if not isinstance(item, str) or not item.startswith("data:"):
            raise ValueError("attachment must be a data URL")
        try:
            header, encoded = item.split(",", 1)
            mime = header[5:].split(";", 1)[0].lower()
            if mime not in _MIMES or ";base64" not in header:
                raise ValueError("unsupported attachment type")
            if len(encoded) > ((MAX_ATTACHMENT_BYTES + 2) // 3) * 4:
                raise ValueError("attachment exceeds size limit")
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("invalid attachment encoding") from exc
        if not raw or len(raw) > MAX_ATTACHMENT_BYTES:
            raise ValueError("attachment exceeds size limit")
        _validate_dimensions(raw, mime)
        total += len(raw)
        if total > MAX_TOTAL_BYTES:
            raise ValueError("attachments exceed total limit")
        result.append(Attachment(mime, raw))
    return tuple(result)


def _validate_dimensions(raw: bytes, mime: str) -> None:
    # PNG has a fixed IHDR; JPEG dimensions require a small marker walk.
    if mime == "image/png":
        if len(raw) < 24:
            raise ValueError("invalid PNG")
        width, height = int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")
    else:
        width, height = _jpeg_dimensions(raw)
    if not width or not height or width * height > MAX_DECODED_PIXELS:
        raise ValueError("image dimensions exceed limit")
    try:
        with Image.open(BytesIO(raw)) as image:
            if image.format not in {"PNG", "JPEG"} or image.size != (width, height):
                raise ValueError("invalid image")
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValueError("invalid image") from exc


def _jpeg_dimensions(raw: bytes) -> tuple[int, int]:
    pos = 2
    while pos + 9 < len(raw):
        if raw[pos] != 0xff:
            pos += 1; continue
        marker = raw[pos + 1]; pos += 2
        while marker == 0xff and pos < len(raw): marker, pos = raw[pos], pos + 1
        if marker in (0xd8, 0xd9): continue
        if pos + 2 > len(raw): break
        length = int.from_bytes(raw[pos:pos + 2], "big")
        if length < 2 or pos + length > len(raw): break
        if 0xc0 <= marker <= 0xc3 or 0xc5 <= marker <= 0xc7 or 0xc9 <= marker <= 0xcb or 0xcd <= marker <= 0xcf:
            return int.from_bytes(raw[pos + 5:pos + 7], "big"), int.from_bytes(raw[pos + 3:pos + 5], "big")
        pos += length
    raise ValueError("invalid JPEG")
