import configparser
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path

import mobase
from PyQt6.QtCore import QPoint, QTimer, Qt, QUrl
from PyQt6.QtGui import QBrush, QColor, QDesktopServices, QPainter, QPalette
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStyle,
    QStyleOptionSlider,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from patcher_entries import PatchEntry as _PatcherEntry
from patcher_entries import collect_patch_entries, find_patch_dir, read_ini_with_fallbacks
from tslpatcher_parser import TslPatcherOperation
from ui_theme import (
    configure_refresh_button,
    configure_tree_widget,
    mo2_conflict_red,
    refresh_mo2,
    set_header_resize_mode,
    tree_active_conflict_row_color,
    tree_base_color,
    tree_conflict_row_color,
    tree_highlight_color,
    tree_hover_stylesheet,
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
class _PatcherConflictOverview(QWidget):
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
class _PatcherItem(QTreeWidgetItem):
    # Compare two rows using the active tree sort column.
    def __lt__(self, other: "QTreeWidgetItem") -> bool:
        tree = self.treeWidget()
        if tree and tree.sortColumn() == 4:
            try:
                return int(self.text(4)) < int(other.text(4))
            except Exception:
                pass
        return super().__lt__(other)


# Show full details for one patch entry.
class _PatcherDetailsDialog(QDialog):
    # Build the patch details dialog UI.
    def __init__(
        self,
        parent: QWidget | None,
        owner: "Kotor2PatcherTab",
        entry: _PatcherEntry,
        conflict_rows: list[tuple[str, str]],
        info_text: str,
        info_path: Path | None,
        ini_text: str,
        log_text: str,
    ):
        super().__init__(parent)
        self._owner = owner
        self._entry = entry
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
            conflicts_view.setPlainText("No enabled patch conflicts for this patch.")
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

        test_tab = QWidget(self)
        test_layout = QVBoxLayout(test_tab)
        test_buttons = QHBoxLayout()
        prepare_test_btn = QPushButton("Prepare Test", self)
        prepare_test_btn.clicked.connect(self._prepare_test_install)
        run_test_btn = QPushButton("Run Test", self)
        run_test_btn.clicked.connect(self._run_test_install)
        open_test_btn = QPushButton("Open Test Folder", self)
        open_test_btn.clicked.connect(self._open_test_folder)
        test_buttons.addWidget(prepare_test_btn)
        test_buttons.addWidget(run_test_btn)
        test_buttons.addWidget(open_test_btn)
        test_buttons.addStretch()
        test_layout.addLayout(test_buttons)

        test_log = QPlainTextEdit(self)
        test_log.setReadOnly(True)
        test_log.setPlaceholderText("Single-patch prepare/run logs will appear here.")
        self._test_log = test_log
        test_layout.addWidget(test_log, 1)
        tabs.addTab(test_tab, "Test")

    # Prepare the selected test install.
    def _prepare_test_install(self):
        self._test_log.setPlainText(self._owner._prepare_test_entry(self._entry))

    # Run the selected test install.
    def _run_test_install(self):
        self._test_log.setPlainText(self._owner._run_test_entry(self._entry))

    # Open the test output folder.
    def _open_test_folder(self):
        test_dir = self._owner._test_entry_target_dir(self._entry)
        if test_dir.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(test_dir)))


# Show prepare and run controls for the patcher.
class _PatcherRunnerDialog(QDialog):
    # Build the runner dialog and wire its buttons.
    def __init__(self, parent: QWidget | None, owner: "Kotor2PatcherTab"):
        super().__init__(parent)
        self._owner = owner
        self.setWindowTitle("Patcher")
        self.resize(860, 620)

        layout = QVBoxLayout(self)
        buttons = QHBoxLayout()
        self._prepare_btn = QPushButton("Prepare", self)
        self._prepare_btn.clicked.connect(self._owner._prepare_patcher_mod)
        self._run_patcher_btn = QPushButton("Start", self)
        self._run_patcher_btn.clicked.connect(self._owner._run_patcher)
        self._stop_btn = QPushButton("Stop", self)
        self._stop_btn.setAutoDefault(False)
        self._stop_btn.setDefault(False)
        self._stop_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._owner._stop_patcher)
        buttons.addWidget(self._prepare_btn)
        buttons.addWidget(self._run_patcher_btn)
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
        self._run_patcher_btn.setEnabled(not running)
        self._stop_btn.setEnabled(running)


# Render the main patcher tab inside MO2.
class Kotor2PatcherTab(QWidget):
    # Build the patcher tab UI and event hooks.
    def __init__(self, parent: QWidget | None, organizer: mobase.IOrganizer, game):
        super().__init__(parent)
        self._organizer = organizer
        self._game = game
        self._json_path = Path(self._organizer.profilePath()) / "tslpatch_order.json"
        self._active_conflict_key: str | None = None
        self._entries: list[_PatcherEntry] = []
        self._last_profile_order: tuple[str, ...] = tuple()
        self._pending_checkbox_sync = False
        self._pending_click_entry_key: str | None = None
        self._stop_patcher_requested = False
        self._current_patcher_process: subprocess.Popen[str] | None = None
        self._runner_dialog: _PatcherRunnerDialog | None = None
        self._runner_log_text = ""
        self._refresh_pending = False

        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        self._summary_label = QLabel("No patches loaded")
        refresh_btn = QPushButton("Refresh")
        configure_refresh_button(refresh_btn)
        refresh_btn.clicked.connect(self._parse_and_refresh)
        runner_btn = QPushButton("Patch")
        runner_btn.clicked.connect(self._open_runner_dialog)
        header.addWidget(refresh_btn)
        header.addWidget(self._summary_label)
        header.addStretch()
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
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        header_view = self._tree.header()
        header_view.setSectionsClickable(True)
        set_header_resize_mode(header_view, QHeaderView.ResizeMode.Interactive, 5)
        self._tree.setColumnWidth(0, 42)
        self._tree.setColumnWidth(1, 220)
        self._tree.setColumnWidth(2, 130)
        self._tree.setColumnWidth(3, 560)
        self._tree.setColumnWidth(4, 56)
        self._tree.sortItems(4, Qt.SortOrder.AscendingOrder)
        self._conflict_overview = _PatcherConflictOverview(self._tree)
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
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(250)
        self._refresh_timer.timeout.connect(self._refresh_now)

        organizer.onProfileChanged(lambda a, b: self.schedule_refresh())
        organizer.modList().onModInstalled(lambda mod: self.schedule_refresh())
        organizer.modList().onModRemoved(lambda mod: self.schedule_refresh())
        organizer.modList().onModStateChanged(lambda mods: self.schedule_refresh())

        self.schedule_refresh(immediate=True)

    # Parse patch entries and refresh the tab.
    def _parse_and_refresh(self):
        if self._tree.topLevelItemCount():
            self._write_json()
        self.schedule_refresh(immediate=True)

    # Return mods in profile priority order.
    def _profile_mod_order(self) -> list[str]:
        return list(self._organizer.modList().allModsByProfilePriority())

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
        if self._refresh_pending or not self._tree.topLevelItemCount():
            self.schedule_refresh(immediate=True)

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
        self.schedule_refresh()

    # Find the patch data folder inside one mod.
    def _find_patch_dir(self, mod_path: Path) -> Path | None:
        return find_patch_dir(mod_path)

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
        mod_list = self._organizer.modList()
        for mod_name in self._profile_mod_order():
            enabled[(mod_name, "Default")] = bool(mod_list.state(mod_name) & mobase.ModState.ACTIVE)
        if self._json_path.exists():
            try:
                data = json.loads(self._json_path.read_text(encoding="utf-8"))
                for row in data.get("patches", []):
                    key = (str(row.get("mod_name", "")), str(row.get("patch_name", "")))
                    enabled[key] = bool(row.get("enabled", False))
                return enabled
            except Exception as e:
                logger.warning("[KOTOR2] Failed to read patcher JSON state: %s", e)
        return enabled

    # Collect patch entries from active patcher mods.
    def _collect_patch_entries(self) -> list[_PatcherEntry]:
        mods_root = Path(self._organizer.modsPath())
        order = self._profile_mod_order()
        enabled_state = self._load_enabled_state()
        mod_list = self._organizer.modList()
        active_state = {
            mod_name: bool(mod_list.state(mod_name) & mobase.ModState.ACTIVE)
            for mod_name in order
        }
        return collect_patch_entries(mods_root, order, enabled_state, active_state)

    # Build human-readable duplicate conflict text.
    def _build_duplicate_text(self, entries: list[_PatcherEntry]) -> str:
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
    def _entry_vfs_targets(self, entry: _PatcherEntry) -> set[str]:
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
    def _clear_patcher_mod_dir(self, patcher_dir: Path):
        patcher_dir.mkdir(parents=True, exist_ok=True)
        for child in patcher_dir.iterdir():
            if child.name.lower() == "meta.ini":
                continue
            if child.is_dir():
                self._remove_tree(child)
            else:
                try:
                    os.chmod(child, stat.S_IWRITE)
                    child.unlink()
                except FileNotFoundError:
                    pass

    # Create dummy game executables required by HoloPatcher.
    @staticmethod
    def _ensure_dummy_game_exes(patcher_dir: Path):
        dummy_bytes = bytes(range(256))
        for exe_name in ("swkotor2.exe", "swkotor.exe"):
            exe_path = patcher_dir / exe_name
            if exe_path.exists():
                continue
            exe_path.write_bytes(dummy_bytes)

    # Remove dummy game executables after patching.
    @staticmethod
    def _remove_dummy_game_exes(patcher_dir: Path):
        for exe_name in ("swkotor2.exe", "swkotor.exe"):
            exe_path = patcher_dir / exe_name
            try:
                if exe_path.exists():
                    os.chmod(exe_path, stat.S_IWRITE)
                    exe_path.unlink()
            except FileNotFoundError:
                pass

    # Prepare one target folder with the virtual files required by the given entries.
    def _prepare_target_dir_for_entries(
        self,
        target_dir: Path,
        entries: list[_PatcherEntry],
        target_name: str,
        log_prefix: str = "",
        update_runner_log: bool = True,
    ) -> str:
        self._clear_patcher_mod_dir(target_dir)
        self._ensure_dummy_game_exes(target_dir)

        targets_by_entry = [
            (entry, sorted(self._entry_vfs_targets(entry)))
            for entry in entries
        ]
        total_targets = sum(len(targets) for _, targets in targets_by_entry)
        copied = 0
        processed = 0
        seen_destinations: set[str] = set()
        resolution_log: list[str] = []
        resolution_cache: dict[str, tuple[Path | None, str, str]] = {}

        for entry_index, (entry, targets) in enumerate(targets_by_entry, start=1):
            if self._stop_patcher_requested:
                return f"{log_prefix.rstrip()}\n\nPrepare stopped by user." if log_prefix else "Prepare stopped by user."

            label = f"{entry.mod_name} / {entry.patch_name}"
            progress = "\n".join(
                [
                    f"Preparing {target_name}...",
                    f"Patch {entry_index}/{len(targets_by_entry)}: {label}",
                    f"Targets processed: {processed}/{total_targets}",
                    f"Files copied: {copied}",
                ]
            )
            if update_runner_log:
                self._set_status_with_prefix(log_prefix, progress)

            for target in targets:
                if self._stop_patcher_requested:
                    return f"{log_prefix.rstrip()}\n\nPrepare stopped by user." if log_prefix else "Prepare stopped by user."

                normalized_target = self._normalize_relpath(target)
                cached_result = resolution_cache.get(normalized_target)
                if cached_result is None:
                    cached_result = self._resolve_vfs_file(normalized_target)
                    resolution_cache[normalized_target] = cached_result
                source, relative, resolution = cached_result
                processed += 1
                if not source or not source.exists():
                    resolution_log.append(f"[MISS] {resolution}")
                    continue

                destination = target_dir / relative
                destination_key = str(destination).lower()
                if destination_key in seen_destinations:
                    continue

                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                seen_destinations.add(destination_key)
                copied += 1
                resolution_log.append(f"[COPY] {resolution}\ncopy target='{relative}'")

        return "\n".join(
            [
                f"Prepared {target_name}.",
                f"Patches scanned: {len(targets_by_entry)}",
                f"Targets processed: {processed}",
                f"Files copied: {copied}",
                "",
                "Resolution log:",
                *resolution_log,
            ]
        )

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
            self._runner_dialog = _PatcherRunnerDialog(self, self)
            if self._runner_log_text:
                self._runner_dialog.set_log_text(self._runner_log_text)
        self._runner_dialog.show()
        self._runner_dialog.raise_()
        self._runner_dialog.activateWindow()
        self.setFocus(Qt.FocusReason.OtherFocusReason)

    # Request cancellation of the current prepare or run.
    def _stop_patcher(self):
        self._stop_patcher_requested = True
        self._append_status_text("Stop requested.")
        process = self._current_patcher_process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except Exception:
                pass

    # Prepare the generated patcher mod before running patches.
    def _prepare_patcher_mod(self, silent: bool = False, manage_busy: bool = True):
        enabled_entries = [entry for entry in self._entries if entry.enabled]
        if not enabled_entries:
            self._set_status_text("No enabled patches to prepare.")
            return

        patcher_dir = Path(self._organizer.modsPath()) / PATCHER_MOD_NAME
        self._stop_patcher_requested = False
        if manage_busy:
            self._set_runner_busy(True)
        try:
            self._set_status_text(f"Preparing {PATCHER_MOD_NAME}...\nClearing target folder...")
            disabled_mods = self._disable_active_tslpatcher_mods()
            if disabled_mods:
                self._refresh_now()
                self._append_status_text(
                    "Disabled active TSLPatcher mods in MO2 before prepare:\n" + "\n".join(disabled_mods)
                )
            log_prefix = self._runner_log_text
            prepare_log = self._prepare_target_dir_for_entries(patcher_dir, enabled_entries, PATCHER_MOD_NAME, log_prefix)
            self._set_status_with_prefix(log_prefix, prepare_log)
        finally:
            if manage_busy:
                self._set_runner_busy(False)

    # Clear the generated [ PATCHER FILES ] folder without restaging patch files.
    def clear_generated_patcher_mod(self):
        patcher_dir = Path(self._organizer.modsPath()) / PATCHER_MOD_NAME
        self._clear_patcher_mod_dir(patcher_dir)

    # Return the isolated test target folder for one entry.
    def _test_entry_target_dir(self, entry: _PatcherEntry) -> Path:
        return Path(__file__).resolve().parent / "test" / self._safe_name(f"{entry.mod_name}_{entry.patch_name}")

    # Prepare one patch entry into its isolated test target folder.
    def _prepare_test_entry(self, entry: _PatcherEntry) -> str:
        self._stop_patcher_requested = False
        test_dir = self._test_entry_target_dir(entry)
        return self._prepare_target_dir_for_entries(
            test_dir,
            [entry],
            f"test folder for {entry.mod_name} / {entry.patch_name}",
            update_runner_log=False,
        )

    # Run one patch entry against its isolated test target folder.
    def _run_test_entry(self, entry: _PatcherEntry) -> str:
        exe_path = Path(__file__).resolve().parent / "HoloPatcher.exe"
        temp_root = Path(__file__).resolve().parent / "temp"
        log_dir = Path(__file__).resolve().parent / "logs"
        label = f"{entry.mod_name} / {entry.patch_name}"
        test_dir = self._test_entry_target_dir(entry)

        if not exe_path.exists():
            return f"HoloPatcher not found:\n{exe_path}"

        self._stop_patcher_requested = False
        prepare_log = self._prepare_test_entry(entry)
        if self._stop_patcher_requested:
            return f"{prepare_log}\n\nRun stopped by user during prepare."

        temp_root.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)

        lines = [
            f"=== Test Run: {label} ===",
            "",
            prepare_log,
            "",
            f"Installing into: {test_dir}",
        ]

        try:
            temp_mod, error = self._stage_patch_for_run(entry, temp_root)
            if temp_mod is None:
                lines.extend(["", f"SKIPPED: {error}"])
                return "\n".join(lines)

            temp_patch = temp_mod / "tslpatchdata"
            cmd = [
                str(exe_path),
                "--install",
                "--game-dir",
                str(test_dir),
                "--tslpatchdata",
                str(temp_patch),
            ]
            process = subprocess.Popen(cmd)
            self._current_patcher_process = process
            while process.poll() is None:
                QApplication.processEvents()
                if self._stop_patcher_requested:
                    try:
                        process.terminate()
                    except Exception:
                        pass
                    break
                time.sleep(0.05)

            install_log = temp_mod / "installlog.txt"
            if install_log.exists():
                shutil.copy2(install_log, log_dir / f"{self._safe_name(label)}_test.txt")
                raw_install_log = install_log.read_text(encoding="utf-8", errors="ignore").strip()
                install_log_text, patch_error_count, patch_warning_count, patch_aborted = self._parse_install_log_summary(raw_install_log)
                if install_log_text:
                    lines.extend(["", "HoloPatcher log:", install_log_text])
            else:
                patch_error_count = 0
                patch_warning_count = 0
                patch_aborted = False

            lines.append("")
            if self._stop_patcher_requested:
                lines.append("STOPPED")
            elif patch_aborted or patch_error_count > 0:
                lines.append("FAILED: install log reported errors")
            elif process.returncode == 0:
                status = "SUCCESS"
                if patch_warning_count > 0:
                    status += f" ({patch_warning_count} warning(s))"
                lines.append(status)
            else:
                lines.append(f"FAILED: exit {process.returncode}")
            return "\n".join(lines)
        except Exception as exc:
            lines.extend(["", f"ERROR: {exc}"])
            return "\n".join(lines)
        finally:
            self._current_patcher_process = None
            self._remove_tree_if_exists(temp_root)

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

    # Remove a folder tree when it exists.
    @classmethod
    def _remove_tree_if_exists(cls, path: Path) -> None:
        if path.exists():
            cls._remove_tree(path)

    # Remove a folder tree with Windows read-only retry.
    @staticmethod
    def _remove_tree(path: Path) -> None:
        def _retry_writeable(function, failed_path, exc_info):
            try:
                os.chmod(failed_path, stat.S_IWRITE)
                function(failed_path)
            except Exception:
                raise exc_info[1]

        shutil.rmtree(path, onerror=_retry_writeable)

    # Return enabled entries in the current tree order.
    def _run_order_entries(self) -> list[_PatcherEntry]:
        by_key = {f"{entry.mod_name}::{entry.patch_name}": entry for entry in self._entries}
        ordered_entries: list[_PatcherEntry] = []
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
    def _find_entry_patch_dir(self, entry: _PatcherEntry) -> Path | None:
        mod_path = Path(self._organizer.modsPath()) / entry.mod_name
        return self._find_patch_dir(mod_path)

    # Resolve the INI path for an entry.
    def _entry_ini_path(self, entry: _PatcherEntry) -> Path | None:
        patch_dir = self._find_entry_patch_dir(entry)
        if patch_dir is None:
            return None
        ini_path = patch_dir / Path(entry.ini_short_path.replace("/", "\\"))
        if ini_path.exists():
            return ini_path
        fallback = patch_dir / "changes.ini"
        return fallback if fallback.exists() else None

    # Resolve the best folder to reveal for an entry.
    def _entry_open_folder_path(self, entry: _PatcherEntry) -> Path | None:
        ini_path = self._entry_ini_path(entry)
        if ini_path is not None:
            return ini_path.parent
        return self._find_entry_patch_dir(entry)

    # Read the namespace-specific info filename for an entry.
    def _entry_namespace_info_name(self, entry: _PatcherEntry) -> str:
        patch_dir = self._find_entry_patch_dir(entry)
        if patch_dir is None:
            return ""

        namespaces_ini = patch_dir / "namespaces.ini"
        if not namespaces_ini.exists():
            return ""

        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        try:
            read_ini_with_fallbacks(parser, namespaces_ini)
        except Exception:
            return ""

        if not parser.has_section(entry.patch_name):
            return ""

        return parser.get(entry.patch_name, "InfoName", fallback="").strip()

    # Resolve the best info.rtf candidate for an entry.
    def _entry_info_rtf_path(self, entry: _PatcherEntry) -> Path | None:
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
    def _entry_log_path(self, entry: _PatcherEntry) -> Path:
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
    def _stage_patch_for_run(self, entry: _PatcherEntry, temp_root: Path) -> tuple[Path | None, str]:
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
            try:
                self._remove_tree(temp_mod)
            except Exception as exc:
                return None, f"Failed to clear temp folder: {exc}"
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
    def _run_patcher(self):
        enabled_entries = self._run_order_entries()
        if not enabled_entries:
            self._set_status_text("No enabled patches to run.")
            return

        patcher_dir = Path(self._organizer.modsPath()) / PATCHER_MOD_NAME
        exe_path = Path(__file__).resolve().parent / "HoloPatcher.exe"
        temp_root = Path(__file__).resolve().parent / "temp"
        log_dir = Path(__file__).resolve().parent / "logs"

        if not exe_path.exists():
            self._set_status_text(f"HoloPatcher not found:\n{exe_path}")
            return

        self._stop_patcher_requested = False
        self._set_runner_busy(True)
        try:
            self._prepare_patcher_mod(silent=True, manage_busy=False)
            if self._stop_patcher_requested:
                self._append_status_text("Run stopped by user during prepare.")
                return
            temp_root.mkdir(parents=True, exist_ok=True)
            log_dir.mkdir(parents=True, exist_ok=True)

            lines = ["=== Patcher Run ===", ""]
            self._append_status_text("\n".join(lines))
            failures = 0
            warning_count = 0
            error_count = 0
            warning_mods: list[str] = []
            error_mods: list[str] = []

            for index, entry in enumerate(enabled_entries, start=1):
                if self._stop_patcher_requested:
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
                    str(patcher_dir),
                    "--tslpatchdata",
                    str(temp_patch),
                ]
                try:
                    process = subprocess.Popen(
                        cmd,
                    )
                    self._current_patcher_process = process
                    while process.poll() is None:
                        QApplication.processEvents()
                        if self._stop_patcher_requested:
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
                    if self._stop_patcher_requested:
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
                    self._current_patcher_process = None
                    self._remove_tree_if_exists(temp_mod)

            self._remove_tree_if_exists(temp_root)
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
            self._current_patcher_process = None
            self._remove_dummy_game_exes(patcher_dir)
            refresh_mo2(self._organizer, self)

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
            return f"No enabled patch conflicts for {active_label}."

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
        return "Selected patch is no longer present in the current patch list."

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
    def _build_conflict_styles(self, entries: list[_PatcherEntry]) -> tuple[dict[str, QBrush], dict[str, QColor]]:
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
            item = _PatcherItem(["", entry.mod_name, entry.patch_name, entry.description, str(entry.priority)])
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
        self._json_path.parent.mkdir(parents=True, exist_ok=True)
        self._json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # Queue or trigger a patch list refresh.
    def schedule_refresh(self, immediate: bool = False):
        self._refresh_pending = True
        if not self.isVisible() and not immediate:
            return
        self._refresh_timer.start(0 if immediate else self._refresh_timer.interval())

    # Preserve the public refresh entry point for explicit callers.
    def refresh(self):
        self.schedule_refresh(immediate=True)

    # Refresh patch entries and run them after sync.
    def run_after_sync(self):
        self._open_runner_dialog()
        self._refresh_pending = False
        self._last_profile_order = tuple(self._profile_mod_order())
        self._entries = self._collect_patch_entries()
        self._rebuild_tree_from_entries()
        self._update_summary()
        self._write_json()
        self._run_patcher()

    # Reload entries and refresh the patch tree.
    def _refresh_now(self):
        if not self.isVisible() and self._tree.topLevelItemCount():
            return
        self._refresh_pending = False
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
        self._show_item_information(item)

    # Show the row context menu for the patcher list.
    def _on_tree_context_menu(self, pos: QPoint):
        item = self._tree.itemAt(pos)
        if item is None:
            return

        self._click_select_timer.stop()
        self._pending_click_entry_key = None
        entry_key = self._entry_key(item)
        entry = next(
            (entry for entry in self._entries if f"{entry.mod_name}::{entry.patch_name}" == entry_key),
            None,
        )
        menu = QMenu(self)
        info_action = menu.addAction("Information")
        open_folder_action = menu.addAction("Open in Explorer")
        if entry is None or self._entry_open_folder_path(entry) is None:
            open_folder_action.setEnabled(False)
        chosen_action = menu.exec(self._tree.viewport().mapToGlobal(pos))
        if chosen_action is info_action:
            self._show_item_information(item)
        elif chosen_action is open_folder_action and entry is not None:
            folder_path = self._entry_open_folder_path(entry)
            if folder_path is not None:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder_path)))

    # Open the patch details dialog for a row.
    def _show_item_information(self, item: QTreeWidgetItem):
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
        dialog = _PatcherDetailsDialog(self, self, entry, conflict_rows, info_text, info_path, ini_text, log_text)
        dialog.exec()

