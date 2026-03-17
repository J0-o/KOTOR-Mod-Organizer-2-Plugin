import configparser
import re
from pathlib import Path

from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QAbstractItemView, QHeaderView, QTreeWidget, QWidget


# Blend two colors by a fixed alpha amount.
def blend_colors(base: QColor, overlay: QColor, alpha: float) -> QColor:
    alpha = max(0.0, min(1.0, alpha))
    return QColor(
        int(base.red() * (1.0 - alpha) + overlay.red() * alpha),
        int(base.green() * (1.0 - alpha) + overlay.green() * alpha),
        int(base.blue() * (1.0 - alpha) + overlay.blue() * alpha),
    )


# Decode MO2's QVariant color serialization format.
def decode_qvariant_color(value: str) -> QColor | None:
    match = re.fullmatch(r"@Variant\((.*)\)", value.strip())
    if not match:
        return None

    raw = match.group(1)
    data = bytearray()
    i = 0
    while i < len(raw):
        if raw[i] == "\\" and i + 1 < len(raw):
            if raw[i + 1] == "0":
                data.append(0)
                i += 2
                continue
            if raw[i + 1] == "x" and i + 3 < len(raw):
                try:
                    data.append(int(raw[i + 2 : i + 4], 16))
                    i += 4
                    continue
                except ValueError:
                    pass
        data.append(ord(raw[i]) & 0xFF)
        i += 1

    if len(data) < 8:
        return None

    rgb16 = [int.from_bytes(data[-8 + j * 2 : -6 + j * 2], "big") for j in range(3)]
    return QColor(*(channel // 257 for channel in rgb16))


# Read a color setting from ModOrganizer.ini.
def mo2_setting_color(setting_name: str, fallback: QColor | None = None) -> QColor:
    ini_path = Path(__file__).resolve().parents[4] / "ModOrganizer.ini"
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    try:
        parser.read(ini_path, encoding="utf-8")
        value = parser.get("Settings", setting_name, fallback="")
    except Exception:
        value = ""

    color = decode_qvariant_color(value)
    if color and color.isValid():
        return color
    return fallback if fallback is not None else QColor(255, 0, 0)


# Return MO2's configured loose-file conflict color.
def mo2_conflict_red() -> QColor:
    return mo2_setting_color("overwritingLooseFilesColor", QColor(255, 0, 0))


# Return the tree base background color.
def tree_base_color(tree: QTreeWidget) -> QColor:
    return tree.palette().color(QPalette.ColorRole.Base)


# Return the tree alternate-row background color.
def tree_alt_base_color(tree: QTreeWidget) -> QColor:
    return tree.palette().color(QPalette.ColorRole.AlternateBase)


# Return the tree selection highlight color.
def tree_highlight_color(tree: QTreeWidget) -> QColor:
    return tree.palette().color(QPalette.ColorRole.Highlight)


# Return the tree text color.
def tree_text_color(tree: QTreeWidget) -> QColor:
    return tree.palette().color(QPalette.ColorRole.Text)


# Build the hover color used for tree rows.
def tree_hover_color(tree: QTreeWidget, alpha: float = 0.34) -> QColor:
    return blend_colors(tree_alt_base_color(tree), tree_highlight_color(tree), alpha)


# Build the shared conflict-row background color.
def tree_conflict_row_color(tree: QTreeWidget, conflict_color: QColor, alpha: float = 0.24) -> QColor:
    return blend_colors(tree_base_color(tree), conflict_color, alpha)


# Build the active conflict-row background color.
def tree_active_conflict_row_color(
    tree: QTreeWidget, conflict_color: QColor, alpha: float = 0.22
) -> QColor:
    return blend_colors(tree_base_color(tree), tree_highlight_color(tree), alpha)


# Return the marker color used for selected rows.
def tree_selected_marker_color(tree: QTreeWidget) -> QColor:
    return tree_highlight_color(tree)


# Build the major conflict brush color used by texture-like views.
def tree_major_conflict_color(tree: QTreeWidget, conflict_color: QColor | None = None, alpha: float = 0.34) -> QColor:
    return blend_colors(tree_alt_base_color(tree), conflict_color or mo2_conflict_red(), alpha)


# Build the minor conflict brush color used by texture-like views.
def tree_minor_conflict_color(tree: QTreeWidget, conflict_color: QColor | None = None, alpha: float = 0.20) -> QColor:
    return blend_colors(tree_base_color(tree), conflict_color or mo2_conflict_red(), alpha)


# Build a shared hover stylesheet for tree widgets.
def tree_hover_stylesheet(tree: QTreeWidget, alpha: float = 0.34) -> str:
    hover = tree_hover_color(tree, alpha)
    return (
        "QTreeWidget::item:hover {"
        f" background-color: rgba({hover.red()}, {hover.green()}, {hover.blue()}, 160);"
        "}"
    )


# Build a shared hover stylesheet for tree and text widgets.
def hover_stylesheet(widget: QWidget, alpha: float = 0.34) -> str:
    hover = tree_hover_color(widget, alpha)
    return (
        "QTreeWidget::item:hover, QPlainTextEdit:hover, QTextEdit:hover {"
        f" background-color: rgba({hover.red()}, {hover.green()}, {hover.blue()}, 160);"
        "}"
    )


# Apply the common base configuration for tree widgets.
def configure_tree_widget(
    tree: QTreeWidget,
    *,
    selection_mode: QAbstractItemView.SelectionMode,
    uniform_row_heights: bool = False,
    sorting_enabled: bool = True,
    alternating_rows: bool = True,
    root_decorated: bool = False,
    mouse_tracking: bool = False,
) -> None:
    tree.setRootIsDecorated(root_decorated)
    tree.setUniformRowHeights(uniform_row_heights)
    tree.setAlternatingRowColors(alternating_rows)
    tree.setSelectionMode(selection_mode)
    tree.setSortingEnabled(sorting_enabled)
    tree.setMouseTracking(mouse_tracking)


# Apply one resize mode to every header section.
def set_header_resize_mode(header: QHeaderView, mode: QHeaderView.ResizeMode, count: int) -> None:
    for col in range(count):
        header.setSectionResizeMode(col, mode)
