import logging
import struct
from pathlib import Path

import mobase
from PyQt6.QtCore import QDateTime
from PyQt6.QtGui import QImage, QPixmap

from basic_games.basic_features.basic_save_game_info import BasicGameSaveGame, format_date

logger = logging.getLogger("mobase")


# Represent a single KOTOR II save directory in the saves tab.
class Kotor2SaveGame(BasicGameSaveGame):
    # Cache the save path and lazy-loaded preview state.
    def __init__(self, filepath):
        super().__init__(filepath)
        self._path = Path(filepath)
        self._creation_time: QDateTime | None = None
        self._screenshot_path: Path | None = None
        self._screenshot_checked = False

    # Return the display name for the save slot.
    def getName(self) -> str:
        return self._path.name

    # Return the save directory modification time.
    def getCreationTime(self) -> QDateTime:
        if self._creation_time is None:
            try:
                self._creation_time = QDateTime.fromSecsSinceEpoch(
                    int(self._path.stat().st_mtime)
                )
            except Exception:
                self._creation_time = QDateTime()
        return self._creation_time

    # Find the screenshot file associated with the save.
    def _find_screenshot_path(self) -> Path | None:
        if not self._screenshot_checked:
            self._screenshot_checked = True
            for name in ("Screen.tga", "screen.tga", "SCREEN.TGA"):
                tga_path = self._path / name
                if tga_path.exists():
                    self._screenshot_path = tga_path
                    break
        return self._screenshot_path

    # Decode the save screenshot into a pixmap.
    def getScreenshot(self):
        tga_path = self._find_screenshot_path()
        if tga_path is None:
            return QPixmap()

        try:
            data = tga_path.read_bytes()
            width, height = struct.unpack_from("<HH", data, 12)
            bpp = data[16]
            if bpp not in (24, 32):
                return QPixmap()

            id_len = data[0]
            offset = 18 + id_len
            img = memoryview(data[offset:])
            bpp_bytes = bpp // 8
            row_size = width * bpp_bytes
            flipped = bytearray(len(img))

            for y in range(height):
                src_y = height - 1 - y
                flipped[y * row_size:(y + 1) * row_size] = img[src_y * row_size:(src_y + 1) * row_size]

            for i in range(0, len(flipped), bpp_bytes):
                flipped[i], flipped[i + 2] = flipped[i + 2], flipped[i]

            fmt = QImage.Format.Format_RGB888 if bpp == 24 else QImage.Format.Format_RGBA8888
            qimg = QImage(bytes(flipped), width, height, fmt)
            return QPixmap.fromImage(qimg.copy())
        except Exception as e:
            logger.warning(f"[KOTOR2] Screenshot decode failed: {e}")
            return QPixmap()

    # Cache the decoded screenshot pixmap.
    def _pixmap(self):
        if not hasattr(self, "_cached_pixmap"):
            self._cached_pixmap = self.getScreenshot()
        return self._cached_pixmap

    # Report whether the cached screenshot is empty.
    def isNull(self):
        return self._pixmap().isNull()

    # Scale the cached screenshot to the requested width.
    def scaledToWidth(self, width, mode=None):
        pm = self._pixmap()
        try:
            return pm.scaledToWidth(width, mode) if not pm.isNull() else pm
        except Exception:
            return pm

    # Scale the cached screenshot to the requested height.
    def scaledToHeight(self, height, mode=None):
        pm = self._pixmap()
        try:
            return pm.scaledToHeight(height, mode) if not pm.isNull() else pm
        except Exception:
            return pm


# Build the extra metadata shown for a save entry.
def parse_kotor2_save_metadata(save_path: Path, save: mobase.ISaveGame):
    files = [f.name for f in save_path.glob("*")]
    return {
        "Files": ", ".join(files[:5]) + ("..." if len(files) > 5 else ""),
        "Modified": format_date(save.getCreationTime(), "hh:mm:ss, d.M.yyyy"),
    }
