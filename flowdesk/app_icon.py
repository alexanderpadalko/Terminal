"""The approved text-free FD monogram used in the Windows taskbar and executable.

The source image is kept as a compact base64 PNG because the GitHub text-only
contents connector cannot commit binary files directly. Both Qt and the EXE
icon are generated from exactly this same image, with no new design.
"""
from __future__ import annotations

import base64
import ctypes
from io import BytesIO
from pathlib import Path
import struct
import sys

from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import QIcon, QImage, QPixmap

_ASSET = Path(__file__).resolve().parent / "assets" / "flowdesk_icon.png.base64"
_ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)
_WINDOWS_APP_ID = "FlowDesk.TradingTerminal"


def source_png() -> bytes:
    """Return the original compact PNG without changing or decompressing OHLCV."""
    return base64.b64decode(_ASSET.read_text(encoding="ascii").strip(), validate=True)


def _source_image() -> QImage:
    image = QImage()
    if not image.loadFromData(source_png(), "PNG") or image.isNull():
        raise RuntimeError("Не удалось загрузить ресурс иконки FlowDesk")
    return image


def application_icon() -> QIcon:
    """A real QIcon with multi-resolution pixmaps for taskbar/window switching."""
    image = _source_image()
    icon = QIcon()
    for size in _ICON_SIZES:
        scaled = image.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
        icon.addPixmap(QPixmap.fromImage(scaled))
    return icon


def set_windows_application_id() -> None:
    """Keep a consistent Windows taskbar grouping separate from python.exe."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            ctypes.c_wchar_p(_WINDOWS_APP_ID)
        )
    except (AttributeError, OSError):
        # Never prevent starting the application on older Windows environments.
        pass


def write_executable_icon(destination: str | Path) -> Path:
    """Generate a Windows multi-size PNG-in-ICO file from the same approved logo.

    This runs BEFORE PyInstaller. There are no Pillow or extra runtime
    dependencies, and the version number is intentionally not changed.
    """
    image = _source_image()
    payloads: list[tuple[int, bytes]] = []
    for size in _ICON_SIZES:
        scaled = image.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
        buffer = QBuffer()
        if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
            raise RuntimeError("Не удалось создать PNG для иконки")
        if not scaled.save(buffer, "PNG"):
            raise RuntimeError(f"Не удалось сохранить PNG {size}x{size}")
        payloads.append((size, bytes(buffer.data())))
        buffer.close()

    directory = bytearray(struct.pack("<HHH", 0, 1, len(payloads)))
    images = bytearray()
    offset = 6 + 16 * len(payloads)
    for size, png in payloads:
        width = height = 0 if size == 256 else size
        directory.extend(struct.pack("<BBBBHHII", width, height, 0, 0,
                                     1, 32, len(png), offset))
        images.extend(png)
        offset += len(png)

    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(directory + images)
    return path
