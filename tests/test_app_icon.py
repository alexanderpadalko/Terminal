"""Regression tests for the unchanged FlowDesk 0.2.26 app identity/icon.

The same approved raster is used by the window/taskbar and PyInstaller icon.
"""
import hashlib
import os
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage

from flowdesk import __version__
from flowdesk.app_icon import application_icon, source_png, write_executable_icon


class FlowDeskIconTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_version_remains_026_and_approved_image_is_unchanged(self):
        self.assertEqual(__version__, "0.2.26")
        source = source_png()
        self.assertTrue(source.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(
            hashlib.sha256(source).hexdigest(),
            "7cc2273f78b117cd648026fb68691502e5b2ee801ddcb551145f2222d2d0d966"
        )
        image = QImage.fromData(source, "PNG")
        self.assertFalse(image.isNull())
        self.assertEqual((image.width(), image.height()), (96, 96))

    def test_qt_app_icon_has_multiple_taskbar_sizes(self):
        icon = application_icon()
        self.assertFalse(icon.isNull())
        for size in (16, 24, 32, 48, 256):
            pixmap = icon.pixmap(size, size)
            self.assertFalse(pixmap.isNull(), size)
            self.assertEqual((pixmap.width(), pixmap.height()), (size, size))

    def test_generated_windows_exe_ico_contains_all_resolutions(self):
        with TemporaryDirectory() as directory:
            output = write_executable_icon(Path(directory) / "flowdesk.ico")
            data = output.read_bytes()
            reserved, kind, count = struct.unpack_from("<HHH", data, 0)
            self.assertEqual((reserved, kind, count), (0, 1, 7))
            sizes = []
            for i in range(count):
                width, height, _, _, planes, bpp, data_len, offset = (
                    struct.unpack_from("<BBBBHHII", data, 6 + i * 16)
                )
                size = 256 if width == 0 else width
                sizes.append(size)
                self.assertEqual(height or 256, size)
                self.assertEqual((planes, bpp), (1, 32))
                self.assertTrue(data[offset:offset + data_len].startswith(
                    b"\x89PNG\r\n\x1a\n"
                ))
            self.assertEqual(sizes, [16, 24, 32, 48, 64, 128, 256])

    def test_icon_is_on_main_window(self):
        from flowdesk.gui import MainWindow
        window = MainWindow(startup=False, watchlist_service=False)
        try:
            self.assertFalse(window.windowIcon().isNull())
            self.assertIn("0.2.26", window.windowTitle())
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
