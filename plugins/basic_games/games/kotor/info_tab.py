from pathlib import Path

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices, QFont, QPixmap
from PyQt6.QtWidgets import (
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

# Show plugin info and tool links.
class KotorInfoTab(QWidget):
    _TOOLS = (
        (
            "HoloPatcher",
            "https://github.com/NickHugi/PyKotor",
        ),
        (
            "DeadlyScraper",
            "https://github.com/search?q=DeadlyScraper&type=repositories",
        ),
        (
            "7-Zip",
            "https://github.com/ip7z/7zip",
        ),
        (
            "xxHash",
            "https://github.com/Cyan4973/xxHash",
        ),
    )

    # Build the centered info tab.
    def __init__(self, parent: QWidget | None, organizer, game):
        super().__init__(parent)
        self._organizer = organizer
        self._game = game
        self._plugin_dir = Path(__file__).resolve().parent

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addStretch(1)
        layout.addWidget(self._logo_widget(), 0, Qt.AlignmentFlag.AlignCenter)

        version = QLabel(f"{self._game_short_name()} Plugin {self._game_version()}")
        version.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(version, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addSpacing(18)

        for name, url in self._TOOLS:
            button = QPushButton(name)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setMinimumWidth(220)
            button.clicked.connect(lambda _checked=False, link=url: QDesktopServices.openUrl(QUrl(link)))
            layout.addWidget(button, 0, Qt.AlignmentFlag.AlignCenter)

        layout.addStretch(1)

    # Build the logo label.
    def _logo_widget(self) -> QLabel:
        label = QLabel("KOTORganizer")
        label.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        logo_path = self._logo_path()
        if logo_path is not None:
            pixmap = QPixmap(str(logo_path))
            if not pixmap.isNull():
                label.setText("")
                label.setPixmap(
                    pixmap.scaledToHeight(
                        56,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
                label.setToolTip(str(logo_path))
                return label

        logo_font = QFont()
        logo_font.setPointSize(18)
        logo_font.setBold(True)
        label.setFont(logo_font)
        return label

    # Find the logo image file.
    def _logo_path(self) -> Path | None:
        candidates = (
            "logo.png",
            "kotorganizer.png",
            "kotor_organizer.png",
            "kotor-logo.png",
            "kotor_logo.png",
        )
        search_dirs = (self._plugin_dir, self._plugin_dir.parent)
        for directory in search_dirs:
            for name in candidates:
                path = directory / name
                if path.exists():
                    return path
        for directory in search_dirs:
            matches = sorted(directory.glob("*.png"))
            if matches:
                return matches[0]
        return None

    # Return the active game short name.
    def _game_short_name(self) -> str:
        try:
            return str(self._game.gameShortName())
        except Exception:
            return str(getattr(self._game, "GameShortName", "KOTOR"))

    # Return the active plugin version.
    def _game_version(self) -> str:
        try:
            version = self._game.version()
            for attr in ("canonicalString", "displayString"):
                method = getattr(version, attr, None)
                if callable(method):
                    return str(method())
            return str(version)
        except Exception:
            return str(getattr(self._game, "Version", ""))
