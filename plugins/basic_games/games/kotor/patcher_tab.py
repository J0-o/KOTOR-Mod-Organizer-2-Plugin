import configparser
import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
import time

import mobase
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QBrush, QColor, QPainter, QPalette, QDesktopServices
from PyQt6.QtCore import QUrl
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QApplication,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
    QStyle,
    QStyleOptionSlider,
)
from tslpatcher_parser import TslPatcherOperation, parse_tslpatcher_ini
from ui_theme import (
    configure_tree_widget,
    mo2_conflict_red,
    tree_base_color,
    tree_conflict_row_color,
    tree_highlight_color,
    tree_active_conflict_row_color,
    tree_hover_stylesheet,
    set_header_resize_mode,
    tree_selected_marker_color,
)

logger = logging.getLogger("mobase")
PATCHER_MOD_NAME = "[ PATCHER FILES ]"


# Convert a subset of RTF into readable plain text.
def _rtf_to_text(rtf: str) -> str:
    out: list[str] = []
    stack: list[tuple[bool, bool]] = []
    ignorable = False
    uc_skip = 1
    skip = 0
    i = 0
    length = len(rtf)
    destinations = {
        "fonttbl",
        "colortbl",
        "stylesheet",
        "info",
        "pict",
        "object",
        "header",
        "footer",
        "headerl",
        "headerr",
        "footerl",
        "footerr",
        "ftnsep",
        "ftnsepc",
        "ftncn",
        "annotation",
        "xmlopen",
        "xmlattrname",
        "xmlattrvalue",
        "xmlclose",
        "fldinst",
        "fldrslt",
    }

    while i < length:
        ch = rtf[i]
        if skip:
            skip -= 1
        elif ch == "{":
            stack.append((ignorable, False))
        elif ch == "}":
            if stack:
                ignorable, _ = stack.pop()
        elif ch == "\\":
            i += 1
            if i >= length:
                break
            ch = rtf[i]
            if ch in "\\{}":
                if not ignorable:
                    out.append(ch)
            elif ch == "*":
                ignorable = True
            elif ch == "'":
                if i + 2 < length and not ignorable:
                    try:
                        out.append(bytes.fromhex(rtf[i + 1 : i + 3]).decode("cp1252", errors="ignore"))
                    except ValueError:
                        pass
                i += 2
            else:
                start = i
                while i < length and rtf[i].isalpha():
                    i += 1
                word = rtf[start:i]
                sign = 1
                if i < length and rtf[i] == "-":
                    sign = -1
                    i += 1
                num_start = i
                while i < length and rtf[i].isdigit():
                    i += 1
                num = sign * int(rtf[num_start:i]) if i > num_start else None
                if i < length and rtf[i] == " ":
                    pass
                else:
                    i -= 1

                if word in destinations:
                    ignorable = True
                if word == "par" or word == "line":
                    if not ignorable:
                        out.append("\n")
                elif word == "tab":
                    if not ignorable:
                        out.append("\t")
                elif word == "uc" and num is not None:
                    uc_skip = num
                elif word == "u":
                    if not ignorable and num is not None:
                        if num < 0:
                            num += 65536
                        out.append(chr(num))
                    skip = uc_skip
        elif ch in "\r\n":
            pass
        elif not ignorable:
            out.append(ch)
        i += 1

    text = "".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# Paint row markers next to the patch list scrollbar.
class _HKConflictOverview(QWidget):
    # Cache the tree and the row colors to paint.
    def __init__(self, tree: QTreeWidget, parent: QWidget | None = None):
        super().__init__(parent)
        self._tree = tree
        self._row_colors: list[QColor | None] = []
        self.setMinimumWidth(8)
        self.setMaximumWidth(8)

    # Update the colors used for the overview strip.
    def set_row_colors(self, row_colors: list[QColor | None]):
        self._row_colors = row_colors
        self.update()

    # Return the visible scrollbar track bounds.
    def _track_rect(self) -> tuple[int, int]:
        scroll_bar = self._tree.verticalScrollBar()
        if scroll_bar is None:
            return 0, self.height()

        option = QStyleOptionSlider()
        scroll_bar.initStyleOption(option)
        style = scroll_bar.style()
        sub_line_rect = style.subControlRect(
            QStyle.ComplexControl.CC_ScrollBar,
            option,
            QStyle.SubControl.SC_ScrollBarSubLine,
            scroll_bar,
        )
        add_line_rect = style.subControlRect(
            QStyle.ComplexControl.CC_ScrollBar,
            option,
            QStyle.SubControl.SC_ScrollBarAddLine,
            scroll_bar,
        )

        top = max(0, sub_line_rect.height())
        bottom = self.height() - max(0, add_line_rect.height())
        if self._tree.horizontalScrollBar().isVisible():
            bottom -= self._tree.horizontalScrollBar().height()
        if bottom <= top:
            return 0, self.height()
        return top, bottom

    # Paint the overview strip beside the tree.
    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.palette().color(QPalette.ColorRole.Base))
        row_count = len(self._row_colors)
        if row_count == 0 or self.height() <= 0:
            return

        track_top, track_bottom = self._track_rect()
        track_height = track_bottom - track_top
        if track_height <= 0:
            return

        width = self.width()
        for index, color in enumerate(self._row_colors):
            if color is None:
                continue
            top = track_top + int(index * track_height / row_count)
            bottom = track_top + int((index + 1) * track_height / row_count)
            height = max(2, bottom - top)
            painter.fillRect(0, top, width, height, color)


# Sort patch rows by numeric priority when needed.
class _HKPatchItem(QTreeWidgetItem):
    # Compare two rows using the active tree sort column.
    def __lt__(self, other: "QTreeWidgetItem") -> bool:
        tree = self.treeWidget()
        if tree and tree.sortColumn() == 4:
            try:
                return int(self.text(4)) < int(other.text(4))
            except Exception:
                pass
        return super().__lt__(other)


# Hold one parsed patch entry shown in the tree.
@dataclass
class _HKPatchEntry:
    enabled: bool
    priority: int
    mod_name: str
    patch_name: str
    description: str
    ini_short_path: str
    destination: str
    install_paths: str
    files: str
    required: str
    operations: tuple[TslPatcherOperation, ...]


# Show full details for one patch entry.
class _HKPatchDetailsDialog(QDialog):
    # Build the patch details dialog UI.
    def __init__(
        self,
        parent: QWidget | None,
        entry: _HKPatchEntry,
        conflict_rows: list[tuple[str, str]],
        info_text: str,
        info_path: Path | None,
        ini_text: str,
        log_text: str,
    ):
        super().__init__(parent)
        self.setWindowTitle(f"{entry.mod_name} / {entry.patch_name}")
        self.resize(880, 620)

        layout = QVBoxLayout(self)
        tabs = QTabWidget(self)
        layout.addWidget(tabs)

        info_tab = QWidget(self)
        info_layout = QVBoxLayout(info_tab)
        info_meta = QPlainTextEdit(self)
        info_meta.setReadOnly(True)
        info_meta.setPlainText(
            "\n".join(
                [
                    f"Mod: {entry.mod_name}",
                    f"Patch: {entry.patch_name}",
                    f"Description: {entry.description or '(none)'}",
                    f"Priority: {entry.priority}",
                    f"Enabled: {entry.enabled}",
                    f"INI: {entry.ini_short_path}",
                ]
            )
        )
        info_rtf = QPlainTextEdit(self)
        info_rtf.setReadOnly(True)
        info_rtf.setPlainText(info_text or "No info file found.")
        info_layout.addWidget(info_meta, 1)
        if info_path and info_path.exists():
            open_btn = QPushButton("Open info file", self)
            open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(info_path))))
            info_layout.addWidget(open_btn, 0)
        info_layout.addWidget(info_rtf, 4)
        tabs.addTab(info_tab, "Info")

        ini_view = QPlainTextEdit(self)
        ini_view.setReadOnly(True)
        ini_view.setPlainText(ini_text or "No INI text found.")
        tabs.addTab(ini_view, "Ini")

        operations = QPlainTextEdit(self)
        operations.setReadOnly(True)
        operations.setPlainText(
            "\n\n".join(
                [
                    "\n".join(
                        [
                            f"Type: {operation.resource_type}",
                            f"Action: {operation.action}",
                            f"Target: {operation.target}",
                            f"Location: {operation.location}",
                            f"Scope: {', '.join(operation.scope) if operation.scope else '(none)'}",
                            f"Section: {operation.source_section}",
                        ]
                    )
                    for operation in entry.operations
                ]
            )
            or "No parsed operations."
        )
        tabs.addTab(operations, "Operations")

        conflicts_tab = QWidget(self)
        conflicts_layout = QVBoxLayout(conflicts_tab)
        conflicts_tree = QTreeWidget(self)
        conflicts_tree.setColumnCount(2)
        conflicts_tree.setHeaderLabels(["Conflicting Mod", "Patch"])
        configure_tree_widget(
            conflicts_tree,
            selection_mode=QAbstractItemView.SelectionMode.SingleSelection,
            uniform_row_heights=True,
        )
        conflicts_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        conflicts_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        conflicts_view = QPlainTextEdit(self)
        conflicts_view.setReadOnly(True)
        conflicts_view.setPlaceholderText("Select a conflicting patch to view shared operations.")
        for label, details in conflict_rows:
            if " / " in label:
                mod_name, patch_name = label.split(" / ", 1)
            else:
                mod_name, patch_name = label, ""
            row = QTreeWidgetItem([mod_name, patch_name])
            row.setData(0, Qt.ItemDataRole.UserRole, details)
            conflicts_tree.addTopLevelItem(row)
        if conflict_rows:
            conflicts_tree.setCurrentItem(conflicts_tree.topLevelItem(0))
            conflicts_view.setPlainText(str(conflicts_tree.topLevelItem(0).data(0, Qt.ItemDataRole.UserRole) or ""))
        else:
            conflicts_view.setPlainText("No enabled HK conflicts for this patch.")
        conflicts_tree.itemClicked.connect(
            lambda item, _column: conflicts_view.setPlainText(str(item.data(0, Qt.ItemDataRole.UserRole) or ""))
        )
        conflicts_layout.addWidget(conflicts_tree, 2)
        conflicts_layout.addWidget(conflicts_view, 3)
        tabs.addTab(conflicts_tab, "Conflicts")

        log_view = QPlainTextEdit(self)
        log_view.setReadOnly(True)
        log_view.setPlainText(log_text or "No log file found for this patch.")
        tabs.addTab(log_view, "Log")


# Show prepare and run controls for the patcher.
class _HKRunnerDialog(QDialog):
    # Build the runner dialog and wire its buttons.
    def __init__(self, parent: QWidget | None, owner: "Kotor2HKReassemblerTab"):
        super().__init__(parent)
        self._owner = owner
        self.setWindowTitle("Patcher")
        self.resize(860, 620)

        layout = QVBoxLayout(self)
        buttons = QHBoxLayout()
        self._prepare_btn = QPushButton("Prepare", self)
        self._prepare_btn.clicked.connect(self._owner._prepare_hk_mod)
        self._run_hk_btn = QPushButton("Start", self)
        self._run_hk_btn.clicked.connect(self._owner._run_hk)
        self._stop_btn = QPushButton("Stop", self)
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._owner._stop_hk)
        buttons.addWidget(self._prepare_btn)
        buttons.addWidget(self._run_hk_btn)
        buttons.addWidget(self._stop_btn)
        buttons.addStretch()
        layout.addLayout(buttons)

        self._log_box = QPlainTextEdit(self)
        self._log_box.setReadOnly(True)
        self._log_box.setPlaceholderText("Patcher prepare/run logs will appear here.")
        layout.addWidget(self._log_box, 1)

    # Replace the runner log text.
    def set_log_text(self, text: str):
        self._log_box.setPlainText(text)
        self._log_box.verticalScrollBar().setValue(self._log_box.verticalScrollBar().maximum())

    # Toggle the runner button state.
    def set_running(self, running: bool):
        self._prepare_btn.setEnabled(not running)
        self._run_hk_btn.setEnabled(not running)
        self._stop_btn.setEnabled(running)


# Render the main patcher tab inside MO2.
class Kotor2HKReassemblerTab(QWidget):
    # Build the patcher tab UI and event hooks.
    def __init__(self, parent: QWidget | None, organizer: mobase.IOrganizer, game):
        super().__init__(parent)
        self._organizer = organizer
        self._game = game
        self._json_path = Path(__file__).resolve().parent / "tslpatch_order.json"
        self._active_conflict_key: str | None = None
        self._entries: list[_HKPatchEntry] = []
        self._last_profile_order: tuple[str, ...] = tuple()
        self._pending_checkbox_sync = False
        self._pending_click_entry_key: str | None = None
        self._stop_hk_requested = False
        self._current_hk_process: subprocess.Popen[str] | None = None
        self._runner_dialog: _HKRunnerDialog | None = None
        self._runner_log_text = ""

        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        self._summary_label = QLabel("No patches loaded")
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._parse_and_refresh)
        runner_btn = QPushButton("Run")
        runner_btn.clicked.connect(self._open_runner_dialog)
        header.addWidget(self._summary_label)
        header.addStretch()
        header.addWidget(refresh_btn)
        header.addWidget(runner_btn)
        layout.addLayout(header)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(5)
        self._tree.setHeaderLabels(["Ena", "Mod", "Patch", "Description", "Priority"])
        configure_tree_widget(
            self._tree,
            selection_mode=QAbstractItemView.SelectionMode.NoSelection,
            uniform_row_heights=True,
            mouse_tracking=True,
        )
        self._apply_tree_style()
        self._tree.itemChanged.connect(self._on_item_changed)
        self._tree.itemClicked.connect(self._on_item_clicked)
        self._tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        header_view = self._tree.header()
        header_view.setSectionsClickable(True)
        set_header_resize_mode(header_view, QHeaderView.ResizeMode.Interactive, 5)
        self._tree.setColumnWidth(0, 42)
        self._tree.setColumnWidth(1, 220)
        self._tree.setColumnWidth(2, 130)
        self._tree.setColumnWidth(3, 560)
        self._tree.setColumnWidth(4, 56)
        self._tree.sortItems(4, Qt.SortOrder.AscendingOrder)
        self._conflict_overview = _HKConflictOverview(self._tree)
        tree_layout = QHBoxLayout()
        tree_layout.setContentsMargins(0, 0, 0, 0)
        tree_layout.setSpacing(2)
        tree_layout.addWidget(self._tree, 1)
        tree_layout.addWidget(self._conflict_overview, 0)
        layout.addLayout(tree_layout, 3)
        header_view.sortIndicatorChanged.connect(self._update_conflict_overview)

        self._order_watch_timer = QTimer(self)
        self._order_watch_timer.setInterval(500)
        self._order_watch_timer.timeout.connect(self._check_mod_order_changed)
        self._checkbox_sync_timer = QTimer(self)
        self._checkbox_sync_timer.setSingleShot(True)
        self._checkbox_sync_timer.setInterval(120)
        self._checkbox_sync_timer.timeout.connect(self._flush_item_changes)
        self._click_select_timer = QTimer(self)
        self._click_select_timer.setSingleShot(True)
        self._click_select_timer.setInterval(180)
        self._click_select_timer.timeout.connect(self._flush_pending_click)

        organizer.onProfileChanged(lambda a, b: self.refresh())
        organizer.modList().onModInstalled(lambda mod: self.refresh())
        organizer.modList().onModRemoved(lambda mod: self.refresh())
        organizer.modList().onModStateChanged(lambda mods: self.refresh())

        self.refresh()

    # Parse patch entries and refresh the tab.
    def _parse_and_refresh(self):
        if self._tree.topLevelItemCount():
            self._write_json()
        self.refresh()
        QMessageBox.information(self, "Patcher", f"Parsed and saved:\n{self._json_path}")

    # Return mods in profile priority order.
    def _profile_mod_order(self) -> list[str]:
        return list(self._organizer.modList().allModsByProfilePriority())

    # Return the tree base color.
    def _theme_base_color(self) -> QColor:
        return tree_base_color(self._tree)

    # Return the tree highlight color.
    def _theme_highlight_color(self) -> QColor:
        return tree_highlight_color(self._tree)

    # Return MO2's conflict red color.
    def _mo2_conflict_red(self) -> QColor:
        return mo2_conflict_red()

    # Return the base conflict color for the patcher tab.
    def _theme_conflict_color(self) -> QColor:
        return self._mo2_conflict_red()

    # Return the active conflict row color.
    def _theme_active_conflict_color(self) -> QColor:
        return tree_active_conflict_row_color(self._tree, self._theme_conflict_color(), 0.22)

    # Return the passive conflict row color.
    def _theme_conflict_background(self) -> QColor:
        return tree_conflict_row_color(self._tree, self._theme_conflict_color(), 0.24)

    # Apply the hover styling for the patch tree.
    def _apply_tree_style(self):
        self._tree.setStyleSheet(tree_hover_stylesheet(self._tree, 0.34))

    # Refresh tree styling when the Qt palette changes.
    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (event.Type.PaletteChange, event.Type.StyleChange):
            self._apply_tree_style()
            self._rebuild_tree_from_entries()

    # Start watching mod order changes while visible.
    def showEvent(self, event):
        super().showEvent(event)
        self._last_profile_order = tuple(self._profile_mod_order())
        self._order_watch_timer.start()

    # Stop watching mod order changes while hidden.
    def hideEvent(self, event):
        self._order_watch_timer.stop()
        super().hideEvent(event)

    # Refresh the tab if MO2 mod priority changes.
    def _check_mod_order_changed(self):
        current_order = tuple(self._profile_mod_order())
        if current_order == self._last_profile_order:
            return
        self._last_profile_order = current_order
        self.refresh()

    # Find the patch data folder inside one mod.
    def _find_patch_dir(self, mod_path: Path) -> Path | None:
        for name in ("tslpatchdata", "TSLPatcherData", "patchdata"):
            candidate = mod_path / name
            if candidate.exists() and candidate.is_dir():
                return candidate
        return None

    # Disable active TSLPatcher mods before preparing the patcher mod.
    def _disable_active_tslpatcher_mods(self) -> list[str]:
        disabled: list[str] = []
        mod_list = self._organizer.modList()
        mods_root = Path(self._organizer.modsPath())
        for mod_name in self._profile_mod_order():
            if mod_name == PATCHER_MOD_NAME:
                continue
            if not (mod_list.state(mod_name) & mobase.ModState.ACTIVE):
                continue
            mod_path = mods_root / mod_name
            if not mod_path.exists() or not mod_path.is_dir():
                continue
            if self._find_patch_dir(mod_path) is None:
                continue
            mod_list.setActive(mod_name, False)
            disabled.append(mod_name)
        return disabled

    # Load the saved enabled-state map from disk.
    def _load_enabled_state(self) -> dict[tuple[str, str], bool]:
        enabled: dict[tuple[str, str], bool] = {}
        if self._json_path.exists():
            try:
                data = json.loads(self._json_path.read_text(encoding="utf-8"))
                for row in data.get("patches", []):
                    key = (str(row.get("mod_name", "")), str(row.get("patch_name", "")))
                    enabled[key] = bool(row.get("enabled", False))
                return enabled
            except Exception as e:
                logger.warning("[KOTOR2] Failed to read HK JSON state: %s", e)
        return enabled

    # Collect patch entries from active patcher mods.
    def _collect_patch_entries(self) -> list[_HKPatchEntry]:
        mods_root = Path(self._organizer.modsPath())
        if not mods_root.exists():
            return []

        order = self._profile_mod_order()
        enabled_state = self._load_enabled_state()
        order_index = {name: index for index, name in enumerate(order)}
        patch_mods = [
            mod_path for mod_path in mods_root.iterdir()
            if mod_path.is_dir() and self._find_patch_dir(mod_path) is not None
        ]
        patch_mods.sort(key=lambda path: order_index.get(path.name, -1), reverse=True)

        entries: list[_HKPatchEntry] = []
        for mod_path in patch_mods:
            patch_dir = self._find_patch_dir(mod_path)
            if patch_dir is None:
                continue

            namespaces_ini = patch_dir / "namespaces.ini"
            if namespaces_ini.exists():
                parser = configparser.ConfigParser(interpolation=None)
                parser.optionxform = str
                try:
                    parser.read(namespaces_ini, encoding="utf-8")
                except Exception as e:
                    logger.warning("[KOTOR2] Failed to read namespaces.ini for %s: %s", mod_path.name, e)
                    continue

                if not parser.has_section("Namespaces"):
                    continue
                namespace_names = [
                    value.strip()
                    for key, value in parser.items("Namespaces")
                    if key.lower().startswith("namespace") and value.strip()
                ]
                for ns_name in namespace_names:
                    if not parser.has_section(ns_name):
                        continue

                    ini_name = parser.get(ns_name, "IniName", fallback="").strip()
                    data_path = parser.get(ns_name, "DataPath", fallback="").strip()
                    description = parser.get(ns_name, "Description", fallback="").strip()
                    final_path = patch_dir / data_path if data_path else patch_dir
                    ini_path = final_path / ini_name if ini_name else final_path / "changes.ini"
                    if not ini_path.exists():
                        fallback = final_path / "changes.ini"
                        if fallback.exists():
                            ini_path = fallback
                        else:
                            continue

                    parsed = parse_tslpatcher_ini(ini_path)
                    entries.append(
                        _HKPatchEntry(
                            enabled=enabled_state.get((mod_path.name, ns_name), False),
                            priority=order_index.get(mod_path.name, -1),
                            mod_name=mod_path.name,
                            patch_name=ns_name,
                            description=description or parsed.description,
                            ini_short_path=str((Path(data_path) / ini_name).as_posix() if data_path else ini_name or "changes.ini"),
                            destination="; ".join(parsed.destinations),
                            install_paths="; ".join(parsed.install_paths),
                            files="; ".join(parsed.files),
                            required="; ".join(parsed.required),
                            operations=parsed.operations,
                        )
                    )
            else:
                ini_path = patch_dir / "changes.ini"
                if not ini_path.exists():
                    continue
                parsed = parse_tslpatcher_ini(ini_path)
                entries.append(
                    _HKPatchEntry(
                        enabled=enabled_state.get((mod_path.name, "Default"), False),
                        priority=order_index.get(mod_path.name, -1),
                        mod_name=mod_path.name,
                        patch_name="Default",
                        description=parsed.description,
                        ini_short_path="changes.ini",
                        destination="; ".join(parsed.destinations),
                        install_paths="; ".join(parsed.install_paths),
                        files="; ".join(parsed.files),
                        required="; ".join(parsed.required),
                        operations=parsed.operations,
                    )
                )
        return entries

    # Build human-readable duplicate conflict text.
    def _build_duplicate_text(self, entries: list[_HKPatchEntry]) -> str:
        dup_map: dict[str, set[str]] = {}
        for entry in entries:
            for operation in entry.operations:
                for conflict_key in operation.conflict_keys():
                    dup_map.setdefault(conflict_key, set()).add(f"{entry.mod_name} / {entry.patch_name}")
        duplicates = sorted((name, mods) for name, mods in dup_map.items() if len(mods) > 1)
        if not duplicates:
            return "No parser-detected TSLPatcher conflicts found."
        return "\n\n".join(f"{name} - {'; '.join(sorted(mods))}" for name, mods in duplicates)

    # Split a stored semicolon-delimited field.
    @staticmethod
    def _split_semicolon_list(value: str) -> list[str]:
        return [part.strip() for part in value.split(";") if part.strip()]

    # Normalize a relative path for lookups.
    @staticmethod
    def _normalize_relpath(value: str) -> str:
        return value.strip().strip("\\/").replace("/", "\\").lower()

    # Detect texture-like targets that should be treated specially.
    @staticmethod
    def _is_texture_target(target: str) -> bool:
        suffix = Path(target).suffix.lower()
        return suffix in {".tpc", ".tga", ".txi", ".mdl", ".mdx", ".wav"}

    # Build the set of virtual file targets needed by one patch.
    def _entry_vfs_targets(self, entry: _HKPatchEntry) -> set[str]:
        targets: set[str] = set()
        required_targets = {
            self._normalize_relpath(required)
            for required in self._split_semicolon_list(entry.required)
        }

        for destination in self._split_semicolon_list(entry.destination):
            normalized = self._normalize_relpath(destination)
            if normalized and (normalized in required_targets or not self._is_texture_target(normalized)):
                targets.add(normalized)

        for operation in entry.operations:
            if operation.resource_type == "tlk":
                targets.add("dialog.tlk")
                continue

            target = self._normalize_relpath(operation.target)
            location = self._normalize_relpath(operation.location)

            if operation.resource_type == "file" and "::" in target:
                container, inner_target = target.split("::", 1)
                if Path(container).suffix:
                    if container in required_targets or not self._is_texture_target(container):
                        targets.add(container)
                elif container:
                    combined = self._normalize_relpath(f"{container}\\{inner_target}")
                    if combined in required_targets or not self._is_texture_target(combined):
                        targets.add(combined)
                else:
                    if inner_target in required_targets or not self._is_texture_target(inner_target):
                        targets.add(inner_target)
                continue

            if location and Path(location).suffix:
                combined = location
            elif location in {"", "global"}:
                combined = target
            else:
                combined = self._normalize_relpath(f"{location}\\{target}")
            if combined in required_targets or not self._is_texture_target(combined):
                targets.add(combined)

        targets.update(target for target in required_targets if target)

        return {target for target in targets if target and "::" not in target}

    # Resolve one target against the active mod stack and game roots.
    def _resolve_vfs_file(self, target: str) -> tuple[Path | None, str, str]:
        normalized = self._normalize_relpath(target)
        if not normalized:
            return None, "", "target='' -> not found"

        parts = [part for part in normalized.split("\\") if part]
        if not parts:
            return None, normalized, f"target='{normalized}' -> not found"

        trace = [f"target='{normalized}'"]
        mods_root = Path(self._organizer.modsPath())
        active_mods: list[Path] = []
        for mod_name in reversed(self._profile_mod_order()):
            if mod_name == PATCHER_MOD_NAME:
                continue
            if not (self._organizer.modList().state(mod_name) & mobase.ModState.ACTIVE):
                continue
            mod_path = mods_root / mod_name
            if mod_path.exists() and mod_path.is_dir():
                active_mods.append(mod_path)

        for mod_path in active_mods:
            if len(parts) == 1:
                direct_candidate = mod_path / parts[0]
                if direct_candidate.exists() and direct_candidate.is_file():
                    trace.append(f"resolved in active mod: {direct_candidate}")
                    return direct_candidate, normalized, "\n".join(trace)

                for root_name in self._game.getModMappings().keys():
                    candidate = mod_path / root_name / parts[0]
                    if candidate.exists() and candidate.is_file():
                        resolved = self._normalize_relpath(f"{root_name}\\{parts[0]}")
                        trace.append(f"resolved in active mod root '{root_name}': {candidate}")
                        return candidate, resolved, "\n".join(trace)
            else:
                candidate = mod_path.joinpath(*parts)
                if candidate.exists() and candidate.is_file():
                    trace.append(f"resolved in active mod: {candidate}")
                    return candidate, normalized, "\n".join(trace)

        game_roots = {key.lower(): Path(path_list[0]) for key, path_list in self._game.getModMappings().items() if path_list}
        if len(parts) > 1 and parts[0].lower() in game_roots:
            game_candidate = game_roots[parts[0].lower()].joinpath(*parts[1:])
            if game_candidate.exists() and game_candidate.is_file():
                trace.append(f"resolved in mapped game root '{parts[0].lower()}': {game_candidate}")
                return game_candidate, normalized, "\n".join(trace)

        if len(parts) == 1:
            dialog_path = Path(self._game.gameDirectory().absolutePath()) / parts[0]
            if dialog_path.exists() and dialog_path.is_file():
                trace.append(f"resolved in game dir: {dialog_path}")
                return dialog_path, normalized, "\n".join(trace)

            for root_name, root_path in game_roots.items():
                game_candidate = root_path / parts[0]
                if game_candidate.exists() and game_candidate.is_file():
                    resolved = self._normalize_relpath(f"{root_name}\\{parts[0]}")
                    trace.append(f"resolved in mapped game root '{root_name}': {game_candidate}")
                    return game_candidate, resolved, "\n".join(trace)

        trace.append("not found in active mods or mapped game roots")
        return None, normalized, "\n".join(trace)

    # Clear the generated patcher mod directory.
    def _clear_hk_mod_dir(self, hk_dir: Path):
        hk_dir.mkdir(parents=True, exist_ok=True)
        for child in hk_dir.iterdir():
            if child.name.lower() == "meta.ini":
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                try:
                    child.unlink()
                except FileNotFoundError:
                    pass

    # Create dummy game executables required by HoloPatcher.
    @staticmethod
    def _ensure_dummy_game_exes(hk_dir: Path):
        dummy_bytes = bytes(range(256))
        for exe_name in ("swkotor2.exe", "swkotor.exe"):
            exe_path = hk_dir / exe_name
            if exe_path.exists():
                continue
            exe_path.write_bytes(dummy_bytes)

    # Replace the runner status text.
    def _set_status_text(self, text: str):
        self._runner_log_text = text
        if self._runner_dialog is not None:
            self._runner_dialog.set_log_text(text)
        QApplication.processEvents()

    # Replace the runner status text with a preserved prefix block.
    def _set_status_with_prefix(self, prefix: str, text: str):
        combined = f"{prefix.rstrip()}\n\n{text}" if prefix else text
        self._set_status_text(combined)

    # Append text to the runner log.
    def _append_status_text(self, text: str):
        if self._runner_log_text:
            self._runner_log_text = f"{self._runner_log_text.rstrip()}\n\n{text}"
        else:
            self._runner_log_text = text
        if self._runner_dialog is not None:
            self._runner_dialog.set_log_text(self._runner_log_text)
        QApplication.processEvents()

    # Toggle the runner dialog busy state.
    def _set_runner_busy(self, running: bool):
        if self._runner_dialog is not None:
            self._runner_dialog.set_running(running)

    # Show the runner dialog.
    def _open_runner_dialog(self):
        if self._runner_dialog is None:
            self._runner_dialog = _HKRunnerDialog(self, self)
            if self._runner_log_text:
                self._runner_dialog.set_log_text(self._runner_log_text)
        self._runner_dialog.show()
        self._runner_dialog.raise_()
        self._runner_dialog.activateWindow()

    # Request cancellation of the current prepare or run.
    def _stop_hk(self):
        self._stop_hk_requested = True
        process = self._current_hk_process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except Exception:
                pass

    # Prepare the generated patcher mod before running patches.
    def _prepare_hk_mod(self, silent: bool = False, manage_busy: bool = True):
        enabled_entries = [entry for entry in self._entries if entry.enabled]
        if not enabled_entries:
            self._set_status_text("No enabled HK patches to prepare.")
            return

        hk_dir = Path(self._organizer.modsPath()) / PATCHER_MOD_NAME
        self._stop_hk_requested = False
        if manage_busy:
            self._set_runner_busy(True)
        try:
            self._set_status_text(f"Preparing {PATCHER_MOD_NAME}...\nClearing target folder...")
            disabled_mods = self._disable_active_tslpatcher_mods()
            if disabled_mods:
                self.refresh()
                self._append_status_text(
                    "Disabled active TSLPatcher mods in MO2 before prepare:\n" + "\n".join(disabled_mods)
                )
            log_prefix = self._runner_log_text
            self._clear_hk_mod_dir(hk_dir)
            self._ensure_dummy_game_exes(hk_dir)

            targets_by_entry = [
                (entry, sorted(self._entry_vfs_targets(entry)))
                for entry in enabled_entries
            ]
            total_targets = sum(len(targets) for _, targets in targets_by_entry)
            copied = 0
            processed = 0
            seen_destinations: set[str] = set()
            resolution_log: list[str] = []
            resolution_cache: dict[str, tuple[Path | None, str, str]] = {}

            for entry_index, (entry, targets) in enumerate(targets_by_entry, start=1):
                if self._stop_hk_requested:
                    self._set_status_with_prefix(log_prefix, "Prepare stopped by user.")
                    return

                label = f"{entry.mod_name} / {entry.patch_name}"
                self._set_status_with_prefix(
                    log_prefix,
                    "\n".join(
                        [
                            "Preparing Patcher...",
                            f"Patch {entry_index}/{len(targets_by_entry)}: {label}",
                            f"Targets processed: {processed}/{total_targets}",
                            f"Files copied: {copied}",
                        ]
                    )
                )

                for target in targets:
                    if self._stop_hk_requested:
                        self._set_status_with_prefix(log_prefix, "Prepare stopped by user.")
                        return

                    normalized_target = self._normalize_relpath(target)
                    cached_result = resolution_cache.get(normalized_target)
                    if cached_result is None:
                        cached_result = self._resolve_vfs_file(normalized_target)
                        resolution_cache[normalized_target] = cached_result
                    source, relative, resolution = cached_result
                    processed += 1
                    if not source or not source.exists():
                        resolution_log.append(f"[MISS] {resolution}")
                        if processed % 10 == 0:
                            self._set_status_with_prefix(
                                log_prefix,
                                "\n".join(
                                    [
                                        f"Preparing {PATCHER_MOD_NAME}...",
                                        f"Patch {entry_index}/{len(targets_by_entry)}: {label}",
                                        f"Targets processed: {processed}/{total_targets}",
                                        f"Files copied: {copied}",
                                    ]
                                )
                            )
                        continue

                    destination = hk_dir / relative
                    destination_key = str(destination).lower()
                    if destination_key in seen_destinations:
                        if processed % 10 == 0:
                            self._set_status_with_prefix(
                                log_prefix,
                                "\n".join(
                                    [
                                        f"Preparing {PATCHER_MOD_NAME}...",
                                        f"Patch {entry_index}/{len(targets_by_entry)}: {label}",
                                        f"Targets processed: {processed}/{total_targets}",
                                        f"Files copied: {copied}",
                                    ]
                                )
                            )
                        continue

                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
                    seen_destinations.add(destination_key)
                    copied += 1
                    resolution_log.append(f"[COPY] {resolution}\ncopy target='{relative}'")

                    if processed % 10 == 0:
                        self._set_status_with_prefix(
                            log_prefix,
                            "\n".join(
                                [
                                    f"Preparing {PATCHER_MOD_NAME}...",
                                    f"Patch {entry_index}/{len(targets_by_entry)}: {label}",
                                    f"Targets processed: {processed}/{total_targets}",
                                    f"Files copied: {copied}",
                                ]
                            )
                        )

            self._set_status_with_prefix(
                log_prefix,
                "\n".join(
                    [
                        f"Prepared {PATCHER_MOD_NAME}.",
                        f"Patches scanned: {len(targets_by_entry)}",
                        f"Targets processed: {processed}",
                        f"Files copied: {copied}",
                        "",
                        "Resolution log:",
                        *resolution_log,
                    ]
                )
            )
        finally:
            if manage_busy:
                self._set_runner_busy(False)

    # Sanitize a string for temp file and log names.
    @staticmethod
    def _safe_name(value: str) -> str:
        return re.sub(r"[^\w\-.]+", "_", value)

    # Build a natural sort key for multipart names.
    @staticmethod
    def _natural_sort_key(value: str) -> tuple[object, ...]:
        parts = re.split(r"(\d+)", value.lower())
        key: list[object] = []
        for part in parts:
            if not part:
                continue
            if part.isdigit():
                key.append(int(part))
            else:
                key.append(part)
        return tuple(key)

    # Return enabled entries in the current tree order.
    def _run_order_entries(self) -> list[_HKPatchEntry]:
        by_key = {f"{entry.mod_name}::{entry.patch_name}": entry for entry in self._entries}
        ordered_entries: list[_HKPatchEntry] = []
        for i in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(i)
            if item.checkState(0) != Qt.CheckState.Checked:
                continue
            entry = by_key.get(self._entry_key(item))
            if entry is not None:
                ordered_entries.append(entry)
        return ordered_entries

    # Parse the summary values from one HoloPatcher log.
    @staticmethod
    def _parse_install_log_summary(install_log_text: str) -> tuple[str, int, int, bool]:
        cleaned_lines: list[str] = []
        error_count = 0
        warning_count = 0
        aborted = False

        match = re.search(
            r"installation is complete with\s+(\d+)\s+errors?\s+and\s+(\d+)\s+warnings?",
            install_log_text,
            flags=re.IGNORECASE,
        )
        if match:
            error_count = int(match.group(1))
            warning_count = int(match.group(2))

        for line in install_log_text.splitlines():
            stripped = line.strip()
            lower = stripped.lower()
            if lower.startswith("total patches:"):
                continue
            if "installation was aborted with errors" in lower or "importerror:" in lower:
                aborted = True
            cleaned_lines.append(line)

        cleaned_text = "\n".join(cleaned_lines).strip()
        return cleaned_text, error_count, warning_count, aborted

    # Find the base patch directory for an entry.
    def _find_entry_patch_dir(self, entry: _HKPatchEntry) -> Path | None:
        mod_path = Path(self._organizer.modsPath()) / entry.mod_name
        return self._find_patch_dir(mod_path)

    # Resolve the INI path for an entry.
    def _entry_ini_path(self, entry: _HKPatchEntry) -> Path | None:
        patch_dir = self._find_entry_patch_dir(entry)
        if patch_dir is None:
            return None
        ini_path = patch_dir / Path(entry.ini_short_path.replace("/", "\\"))
        if ini_path.exists():
            return ini_path
        fallback = patch_dir / "changes.ini"
        return fallback if fallback.exists() else None

    # Read the namespace-specific info filename for an entry.
    def _entry_namespace_info_name(self, entry: _HKPatchEntry) -> str:
        patch_dir = self._find_entry_patch_dir(entry)
        if patch_dir is None:
            return ""

        namespaces_ini = patch_dir / "namespaces.ini"
        if not namespaces_ini.exists():
            return ""

        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        try:
            parser.read(namespaces_ini, encoding="utf-8")
        except Exception:
            return ""

        if not parser.has_section(entry.patch_name):
            return ""

        return parser.get(entry.patch_name, "InfoName", fallback="").strip()

    # Resolve the best info.rtf candidate for an entry.
    def _entry_info_rtf_path(self, entry: _HKPatchEntry) -> Path | None:
        ini_path = self._entry_ini_path(entry)
        patch_dir = self._find_entry_patch_dir(entry)
        info_name = self._entry_namespace_info_name(entry)
        candidates: list[Path | None] = []

        if info_name:
            info_rel = Path(info_name.replace("/", "\\"))
            if info_rel.is_absolute():
                candidates.append(info_rel)
            else:
                if ini_path:
                    candidates.append(ini_path.parent / info_rel)
                if patch_dir:
                    candidates.append(patch_dir / info_rel)

        candidates.extend(
            [
                (ini_path.parent / "info.rtf") if ini_path else None,
                (patch_dir / "info.rtf") if patch_dir else None,
            ]
        )

        for candidate in candidates:
            if candidate and candidate.exists():
                return candidate
        return None

    # Return the stored log path for an entry.
    def _entry_log_path(self, entry: _HKPatchEntry) -> Path:
        log_dir = Path(__file__).resolve().parent / "logs"
        return log_dir / f"{self._safe_name(f'{entry.mod_name} / {entry.patch_name}')}.txt"

    # Extract plain text from an RTF info file.
    def _extract_rtf_text(self, rtf_path: Path) -> str | None:
        if not rtf_path.exists():
            return None

        try:
            return _rtf_to_text(rtf_path.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            return None

    # Stage one patch into a temp folder for execution.
    def _stage_patch_for_run(self, entry: _HKPatchEntry, temp_root: Path) -> tuple[Path | None, str]:
        patch_dir = self._find_entry_patch_dir(entry)
        if patch_dir is None:
            return None, "No tslpatchdata folder found"

        ini_rel = Path(entry.ini_short_path.replace("/", "\\"))
        ini_abs = patch_dir / ini_rel
        if not ini_abs.exists():
            fallback = patch_dir / "changes.ini"
            if fallback.exists():
                ini_abs = fallback
            else:
                return None, f"INI not found: {entry.ini_short_path}"

        temp_mod = temp_root / self._safe_name(f"{entry.mod_name}_{entry.patch_name}")
        temp_patch = temp_mod / "tslpatchdata"
        if temp_mod.exists():
            shutil.rmtree(temp_mod, ignore_errors=True)
        temp_patch.mkdir(parents=True, exist_ok=True)

        ini_folder = ini_abs.parent
        shutil.copytree(ini_folder, temp_patch, dirs_exist_ok=True)

        info_path = temp_patch / "info.rtf"
        if not info_path.exists():
            info_path.write_text(r"{\rtf1\ansi Patcher auto-generated info.rtf}", encoding="ascii")

        namespace_path = temp_patch / "namespaces.ini"
        if namespace_path.exists():
            try:
                namespace_path.unlink()
            except OSError:
                pass

        copied_ini = temp_patch / ini_abs.name
        fixed_ini = temp_patch / "changes.ini"
        if not copied_ini.exists():
            return None, f"INI missing after copy: {copied_ini}"
        if copied_ini.name.lower() != "changes.ini":
            if fixed_ini.exists():
                fixed_ini.unlink()
            copied_ini.rename(fixed_ini)

        return temp_mod, ""

    # Run the enabled patch entries through HoloPatcher.
    def _run_hk(self):
        enabled_entries = self._run_order_entries()
        if not enabled_entries:
            self._set_status_text("No enabled HK patches to run.")
            return

        hk_dir = Path(self._organizer.modsPath()) / PATCHER_MOD_NAME
        exe_path = Path(__file__).resolve().parent / "HoloPatcher.exe"
        temp_root = Path(__file__).resolve().parent / "temp"
        log_dir = Path(__file__).resolve().parent / "logs"

        if not exe_path.exists():
            self._set_status_text(f"HoloPatcher not found:\n{exe_path}")
            return

        self._stop_hk_requested = False
        self._set_runner_busy(True)
        try:
            self._prepare_hk_mod(silent=True, manage_busy=False)
            if self._stop_hk_requested:
                self._append_status_text("Run stopped by user during prepare.")
                return
            temp_root.mkdir(parents=True, exist_ok=True)
            log_dir.mkdir(parents=True, exist_ok=True)

            lines = ["=== HK Run ===", ""]
            self._append_status_text("\n".join(lines))
            failures = 0
            warning_count = 0
            error_count = 0
            warning_mods: list[str] = []
            error_mods: list[str] = []

            for index, entry in enumerate(enabled_entries, start=1):
                if self._stop_hk_requested:
                    lines.append("Run stopped by user.")
                    break
                label = f"{entry.mod_name} / {entry.patch_name}"
                lines.append(f"[{index}/{len(enabled_entries)}] {label}")
                self._set_status_text(f"{self._runner_log_text.rstrip()}\n[{index}/{len(enabled_entries)}] {label}")

                temp_mod, error = self._stage_patch_for_run(entry, temp_root)
                if temp_mod is None:
                    lines.append(f"  SKIPPED: {error}")
                    failures += 1
                    self._append_status_text(f"[{index}/{len(enabled_entries)}] {label}\n  SKIPPED: {error}")
                    continue

                temp_patch = temp_mod / "tslpatchdata"
                cmd = [
                    str(exe_path),
                    "--install",
                    "--game-dir",
                    str(hk_dir),
                    "--tslpatchdata",
                    str(temp_patch),
                ]
                try:
                    process = subprocess.Popen(
                        cmd,
                    )
                    self._current_hk_process = process
                    while process.poll() is None:
                        QApplication.processEvents()
                        if self._stop_hk_requested:
                            try:
                                process.terminate()
                            except Exception:
                                pass
                            break
                        time.sleep(0.05)
                    install_log = temp_mod / "installlog.txt"
                    install_log_text = ""
                    patch_aborted = False
                    patch_error_count = 0
                    patch_warning_count = 0
                    if install_log.exists():
                        shutil.copy2(install_log, log_dir / f"{self._safe_name(label)}.txt")
                        raw_install_log = install_log.read_text(encoding="utf-8", errors="ignore").strip()
                        install_log_text, patch_error_count, patch_warning_count, patch_aborted = self._parse_install_log_summary(raw_install_log)
                        warning_count += patch_warning_count
                        error_count += patch_error_count
                        if patch_warning_count and label not in warning_mods:
                            warning_mods.append(label)
                        if patch_error_count and label not in error_mods:
                            error_mods.append(label)
                        if patch_aborted and label not in error_mods:
                            error_mods.append(label)
                    if self._stop_hk_requested:
                        lines.append("  STOPPED")
                        failures += 1
                        block = f"[{index}/{len(enabled_entries)}] {label}"
                        if install_log_text:
                            block += f"\n\nHoloPatcher log:\n{install_log_text}"
                        block += "\n\n  STOPPED"
                        self._append_status_text(block)
                        break
                    if patch_aborted or patch_error_count > 0:
                        lines.append("  FAILED: install log reported errors")
                        failures += 1
                        status_line = "  FAILED: install log reported errors"
                    elif process.returncode == 0:
                        lines.append("  SUCCESS")
                        status_line = "  SUCCESS"
                    else:
                        lines.append(f"  FAILED: exit {process.returncode}")
                        failures += 1
                        status_line = f"  FAILED: exit {process.returncode}"
                    block = f"[{index}/{len(enabled_entries)}] {label}"
                    if install_log_text:
                        block += f"\n\nHoloPatcher log:\n{install_log_text}"
                    block += f"\n\n{status_line}"
                    self._append_status_text(block)
                except Exception as exc:
                    lines.append(f"  ERROR: {exc}")
                    failures += 1
                    self._append_status_text(f"[{index}/{len(enabled_entries)}] {label}\n  ERROR: {exc}")
                finally:
                    self._current_hk_process = None
                    shutil.rmtree(temp_mod, ignore_errors=True)

            shutil.rmtree(temp_root, ignore_errors=True)
            lines.append("")
            lines.append(f"Completed with {failures} failure(s).")
            summary_lines = [f"Completed with {failures} failure(s).", ""]
            summary_lines.append(f"Total errors: {error_count}")
            if error_mods:
                summary_lines.append("Mods with errors:")
                summary_lines.extend(error_mods)
            else:
                summary_lines.append("Mods with errors: none")
            summary_lines.append("")
            summary_lines.append(f"Total warnings: {warning_count}")
            if warning_mods:
                summary_lines.append("Mods with warnings:")
                summary_lines.extend(warning_mods)
            else:
                summary_lines.append("Mods with warnings: none")
            self._append_status_text("\n".join(summary_lines))
        finally:
            self._set_runner_busy(False)
            self._current_hk_process = None

    # Collapse operation conflict keys into a stored string.
    @staticmethod
    def _conflict_key_string(operations: tuple[TslPatcherOperation, ...]) -> str:
        keys: list[str] = []
        seen: set[str] = set()
        for operation in operations:
            for conflict_key in operation.conflict_keys():
                if conflict_key not in seen:
                    seen.add(conflict_key)
                    keys.append(conflict_key)
        return "; ".join(keys)

    # Split a stored conflict-key string.
    @staticmethod
    def _split_conflict_keys(value: str) -> set[str]:
        return {part.strip() for part in value.split(";") if part.strip()}

    # Build the stable key for one tree row.
    @staticmethod
    def _entry_key(item: QTreeWidgetItem) -> str:
        return f"{item.text(1)}::{item.text(2)}"

    # Build the conflict summary text for the selected row.
    def _selected_conflict_text(self, active_item: QTreeWidgetItem) -> str:
        rows = self._selected_conflict_rows(active_item)
        active_label = f"{active_item.text(1)} / {active_item.text(2)}"
        if not rows:
            if active_item.checkState(0) != Qt.CheckState.Checked:
                return "Selected patch is disabled. Enable it to inspect active conflicts."
            active_keys = self._split_conflict_keys(str(active_item.data(0, Qt.ItemDataRole.UserRole + 5) or ""))
            if not active_keys:
                return "Selected patch does not expose any parser-detected operations."
            return f"No enabled HK conflicts for {active_label}."

        return f"Conflicts for {active_label}:\n\n" + "\n\n".join(
            f"{label}\nShared operations:\n{details}" for label, details in rows
        )

    # Collect the rows that conflict with the selected row.
    def _selected_conflict_rows(self, active_item: QTreeWidgetItem) -> list[tuple[str, str]]:
        if active_item.checkState(0) != Qt.CheckState.Checked:
            return []

        active_keys = self._split_conflict_keys(str(active_item.data(0, Qt.ItemDataRole.UserRole + 5) or ""))
        if not active_keys:
            return []

        conflicts: list[tuple[str, str]] = []
        for i in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(i)
            if item is active_item:
                continue
            if item.checkState(0) != Qt.CheckState.Checked:
                continue
            shared_keys = sorted(
                active_keys.intersection(
                    self._split_conflict_keys(str(item.data(0, Qt.ItemDataRole.UserRole + 5) or ""))
                )
            )
            if not shared_keys:
                continue
            other_label = f"{item.text(1)} / {item.text(2)}"
            conflicts.append((other_label, "\n".join(shared_keys)))
        return conflicts

    # Resolve conflict text by entry key.
    def _selected_conflict_text_by_key(self, entry_key: str) -> str:
        for i in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(i)
            if self._entry_key(item) == entry_key:
                return self._selected_conflict_text(item)
        return "Selected patch is no longer present in the current HK list."

    # Refresh the scrollbar overview colors.
    def _update_conflict_overview(self, *_args):
        if not hasattr(self, "_conflict_overview"):
            return
        row_colors: list[QColor | None] = []
        selected_item = self._tree.currentItem()
        selected_marker = tree_selected_marker_color(self._tree)
        for i in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(i)
            color_name = item.data(0, Qt.ItemDataRole.UserRole + 6)
            color = QColor(str(color_name)) if color_name else None
            if item is selected_item:
                color = color if color is not None else selected_marker
            row_colors.append(color)
        self._conflict_overview.set_row_colors(row_colors)

    # Build row brushes for the active conflict set.
    def _build_conflict_styles(self, entries: list[_HKPatchEntry]) -> tuple[dict[str, QBrush], dict[str, QColor]]:
        conflict_brushes: dict[str, QBrush] = {}
        overview_colors: dict[str, QColor] = {}
        if not self._active_conflict_key:
            return conflict_brushes, overview_colors

        active_entry = next(
            (
                entry for entry in entries
                if f"{entry.mod_name}::{entry.patch_name}" == self._active_conflict_key and entry.enabled
            ),
            None,
        )
        if active_entry is None:
            return conflict_brushes, overview_colors

        active_keys = {key for op in active_entry.operations for key in op.conflict_keys()}
        if not active_keys:
            return conflict_brushes, overview_colors

        active_color = self._theme_active_conflict_color()
        conflict_brushes[self._active_conflict_key] = QBrush(active_color)
        overview_colors[self._active_conflict_key] = active_color
        for entry in entries:
            if not entry.enabled:
                continue
            entry_key = f"{entry.mod_name}::{entry.patch_name}"
            if entry_key == self._active_conflict_key:
                continue
            entry_keys = {key for op in entry.operations for key in op.conflict_keys()}
            if active_keys.intersection(entry_keys):
                conflict_color = self._theme_conflict_background()
                conflict_brushes[entry_key] = QBrush(conflict_color)
                overview_colors[entry_key] = conflict_color
        return conflict_brushes, overview_colors

    # Rebuild the visible patch tree from the entry list.
    def _rebuild_tree_from_entries(self):
        conflict_brushes, overview_colors = self._build_conflict_styles(self._entries)

        self._tree.blockSignals(True)
        self._tree.clear()
        for entry in self._entries:
            item = _HKPatchItem(["", entry.mod_name, entry.patch_name, entry.description, str(entry.priority)])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Checked if entry.enabled else Qt.CheckState.Unchecked)
            item.setData(4, Qt.ItemDataRole.UserRole, entry.priority)
            item.setData(0, Qt.ItemDataRole.UserRole, entry.ini_short_path)
            item.setData(0, Qt.ItemDataRole.UserRole + 1, entry.destination)
            item.setData(0, Qt.ItemDataRole.UserRole + 2, entry.install_paths)
            item.setData(0, Qt.ItemDataRole.UserRole + 3, entry.required)
            item.setData(0, Qt.ItemDataRole.UserRole + 4, entry.files)
            item.setData(0, Qt.ItemDataRole.UserRole + 5, self._conflict_key_string(entry.operations))
            item.setToolTip(3, entry.description)
            item.setToolTip(3, entry.description if not entry.files else f"{entry.description}\n\nFiles: {entry.files}")
            item_key = f"{entry.mod_name}::{entry.patch_name}"
            brush = conflict_brushes.get(item_key)
            overview_color = overview_colors.get(item_key)
            item.setData(0, Qt.ItemDataRole.UserRole + 6, overview_color.name() if overview_color else "")
            if brush is not None:
                for col in range(5):
                    item.setBackground(col, brush)
            self._tree.addTopLevelItem(item)
        self._tree.blockSignals(False)
        self._tree.sortItems(self._tree.sortColumn(), self._tree.header().sortIndicatorOrder())
        self._update_conflict_overview()

    # Persist the current patch tree state to JSON.
    def _write_json(self):
        payload = {"patches": []}
        for i in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(i)
            payload["patches"].append({
                "enabled": item.checkState(0) == Qt.CheckState.Checked,
                "priority": int(item.text(4)) if item.text(4).isdigit() else -1,
                "mod_name": item.text(1),
                "patch_name": item.text(2),
                "description": item.text(3),
                "ini_short_path": item.data(0, Qt.ItemDataRole.UserRole) or "",
                "destination": item.data(0, Qt.ItemDataRole.UserRole + 1) or "",
                "install_paths": item.data(0, Qt.ItemDataRole.UserRole + 2) or "",
                "files": item.data(0, Qt.ItemDataRole.UserRole + 4) or "",
                "required": item.data(0, Qt.ItemDataRole.UserRole + 3) or "",
            })
        self._json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # Reload entries and refresh the patch tree.
    def refresh(self):
        self._last_profile_order = tuple(self._profile_mod_order())
        self._entries = self._collect_patch_entries()
        self._rebuild_tree_from_entries()
        self._update_summary()
        self._write_json()

    # Enable or disable every visible row.
    def _set_all_enabled(self, enabled: bool):
        self._tree.blockSignals(True)
        state = Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked
        for i in range(self._tree.topLevelItemCount()):
            self._tree.topLevelItem(i).setCheckState(0, state)
        self._tree.blockSignals(False)
        self._update_summary()
        self._write_json()

    # Refresh the summary label text.
    def _update_summary(self):
        total = self._tree.topLevelItemCount()
        enabled = sum(1 for i in range(total) if self._tree.topLevelItem(i).checkState(0) == Qt.CheckState.Checked)
        self._summary_label.setText(f"{enabled}/{total} patches enabled")
    # Queue a state write after a checkbox change.
    def _on_item_changed(self, _item: QTreeWidgetItem, _column: int):
        self._update_summary()
        self._pending_checkbox_sync = True
        self._checkbox_sync_timer.start()

    # Flush pending checkbox changes to disk and memory.
    def _flush_item_changes(self):
        if not self._pending_checkbox_sync:
            return
        self._pending_checkbox_sync = False
        self._write_json()
        enabled_by_key = {
            self._entry_key(self._tree.topLevelItem(i)): self._tree.topLevelItem(i).checkState(0) == Qt.CheckState.Checked
            for i in range(self._tree.topLevelItemCount())
        }
        for entry in self._entries:
            entry.enabled = enabled_by_key.get(f"{entry.mod_name}::{entry.patch_name}", entry.enabled)
        self._rebuild_tree_from_entries()

    # Apply the delayed row click selection.
    def _flush_pending_click(self):
        if not self._pending_click_entry_key:
            return
        self._active_conflict_key = self._pending_click_entry_key
        self._pending_click_entry_key = None
        self._rebuild_tree_from_entries()

    # Queue a conflict selection when a row is clicked.
    def _on_item_clicked(self, item: QTreeWidgetItem, _column: int):
        if _column == 0:
            return
        self._pending_click_entry_key = self._entry_key(item)
        self._click_select_timer.start()

    # Open the patch details dialog for a row.
    def _on_item_double_clicked(self, item: QTreeWidgetItem, _column: int):
        self._click_select_timer.stop()
        self._pending_click_entry_key = None
        entry_key = self._entry_key(item)
        entry = next(
            (entry for entry in self._entries if f"{entry.mod_name}::{entry.patch_name}" == entry_key),
            None,
        )
        if entry is None:
            return
        info_path = self._entry_info_rtf_path(entry)
        ini_path = self._entry_ini_path(entry)
        log_path = self._entry_log_path(entry)
        info_text = self._extract_rtf_text(info_path) if info_path else ""
        ini_text = ini_path.read_text(encoding="utf-8", errors="ignore") if ini_path else ""
        log_text = log_path.read_text(encoding="utf-8", errors="ignore") if log_path.exists() else ""
        conflict_rows = self._selected_conflict_rows(item)
        dialog = _HKPatchDetailsDialog(self, entry, conflict_rows, info_text, info_path, ini_text, log_text)
        dialog.exec()
