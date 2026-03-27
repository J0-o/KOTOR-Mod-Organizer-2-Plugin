import logging
from pathlib import Path

import mobase
from PyQt6.QtCore import QDateTime, QPoint, QTimer, QUrl, Qt
from PyQt6.QtGui import QBrush, QDesktopServices
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from ui_theme import (
    configure_tree_widget,
    set_header_resize_mode,
    tree_major_conflict_color,
    tree_minor_conflict_color,
)

logger = logging.getLogger("mobase")


# Sort texture rows by the custom error and priority columns.
class _TextureItem(QTreeWidgetItem):
    # Compare rows using the active sort column.
    def __lt__(self, other: "QTreeWidgetItem") -> bool:
        tree = self.treeWidget()
        if tree:
            if tree.sortColumn() == 0:
                my_weight = self.data(0, Qt.ItemDataRole.UserRole + 2)
                other_weight = other.data(0, Qt.ItemDataRole.UserRole + 2)
                try:
                    return int(my_weight) < int(other_weight)
                except Exception:
                    pass
            if tree.sortColumn() == 6:
                my_priority = self.data(6, Qt.ItemDataRole.UserRole)
                other_priority = other.data(6, Qt.ItemDataRole.UserRole)
                try:
                    return int(my_priority) < int(other_priority)
                except Exception:
                    pass
        return super().__lt__(other)


# Render the texture conflict browser tab.
class Kotor2TextureTab(QWidget):
    _EXTENSIONS = {".tga", ".tpc", ".txi", ".dds"}
    _WEIGHT_NONE = 0
    _WEIGHT_HIDDEN = 1
    _WEIGHT_MINOR = 2
    _WEIGHT_MAJOR = 3

    # Build the textures tab UI and refresh hooks.
    def __init__(self, parent: QWidget | None, organizer: mobase.IOrganizer, game):
        super().__init__(parent)
        self._organizer = organizer
        self._game = game
        self._refresh_pending = False
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(150)
        self._refresh_timer.timeout.connect(self._refresh_now)

        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        self._count_label = QLabel("0 texture files")
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh)
        auto_fix_btn = QPushButton("Auto Fix")
        auto_fix_btn.clicked.connect(self._auto_fix)
        unhide_btn = QPushButton("Unhide all")
        unhide_btn.clicked.connect(self._unhide_all)
        header.addWidget(self._count_label)
        header.addStretch()
        header.addWidget(auto_fix_btn)
        header.addWidget(unhide_btn)
        header.addWidget(refresh_btn)
        layout.addLayout(header)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(7)
        self._tree.setHeaderLabels(["Err", "Name", "Mod", "Type", "Size", "Date modified", "Priority"])
        header_view = self._tree.header()
        set_header_resize_mode(header_view, QHeaderView.ResizeMode.Interactive, 7)
        self._tree.setColumnWidth(1, 300)
        self._tree.setColumnWidth(0, 48)
        self._tree.setColumnWidth(2, 180)
        self._tree.setColumnWidth(3, 110)
        self._tree.setColumnWidth(4, 90)
        self._tree.setColumnWidth(5, 150)
        self._tree.setColumnWidth(6, 80)
        configure_tree_widget(
            self._tree,
            selection_mode=QAbstractItemView.SelectionMode.ExtendedSelection,
        )
        self._tree.itemDoubleClicked.connect(self._open_item)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._context_menu)
        self._tree.itemSelectionChanged.connect(self._sync_base_selection)
        layout.addWidget(self._tree)

        self._syncing_selection = False

        organizer.onProfileChanged(lambda a, b: self.schedule_refresh())
        organizer.modList().onModInstalled(lambda mod: self.schedule_refresh())
        organizer.modList().onModRemoved(lambda mod: self.schedule_refresh())
        organizer.modList().onModStateChanged(lambda mods: self.schedule_refresh())
        self.schedule_refresh()

    # Return the major conflict highlight brush.
    def _major_conflict_brush(self) -> QBrush:
        return QBrush(tree_major_conflict_color(self._tree))

    # Return the minor conflict highlight brush.
    def _minor_conflict_brush(self) -> QBrush:
        return QBrush(tree_minor_conflict_color(self._tree))

    # Refresh immediately when the tab becomes visible.
    def showEvent(self, event):
        super().showEvent(event)
        if self._refresh_pending or not self._tree.topLevelItemCount():
            self.schedule_refresh(immediate=True)

    # Queue or trigger a tab refresh.
    def schedule_refresh(self, immediate: bool = False):
        self._refresh_pending = True
        if not self.isVisible() and not immediate:
            return
        self._refresh_timer.start(0 if immediate else self._refresh_timer.interval())

    # Yield active override roots in winner-resolution order.
    def _iter_override_roots(self):
        for mod_path in self._game._active_mod_paths():
            for candidate in (mod_path / "override", mod_path / "Override"):
                if candidate.exists() and candidate.is_dir():
                    yield f"Mod: {mod_path.name}", candidate
                    break
        override_path = Path(self._game.overrideDirectory().absolutePath())
        if override_path.exists():
            yield "Game Override", override_path

    # Force an immediate refresh pass.
    def refresh(self):
        self.schedule_refresh(immediate=True)

    # Scan override roots and rebuild the texture table.
    def _refresh_now(self):
        if not self.isVisible():
            return
        self._refresh_pending = False
        winners: dict[str, tuple[str, str]] = {}
        hidden_entries: list[tuple[str, str]] = []
        source_roots: dict[str, Path] = {}
        mod_entries: list[tuple[str, Path]] = []
        game_entry: tuple[str, Path] | None = None
        profile_order = list(self._organizer.modList().allModsByProfilePriority())
        priority_map = {name: index for index, name in enumerate(profile_order)}

        for source, root in self._iter_override_roots():
            source_roots[source] = root
            if source.startswith("Mod: "):
                mod_entries.append((source, root))
            else:
                game_entry = (source, root)

        for source, root in reversed(mod_entries):
            self._scan_root(source, root, winners, hidden_entries)
        if game_entry:
            self._scan_root(game_entry[0], game_entry[1], winners, hidden_entries)

        items = sorted(winners.values(), key=lambda i: i[1].lower())
        entries: list[dict] = []
        base_exts: dict[str, set[str]] = {}
        hidden_count = 0

        for source, rel in items:
            root = source_roots.get(source)
            source_path = root / rel if root else None
            size_text = ""
            mtime_text = ""
            if source_path and source_path.exists():
                stat = source_path.stat()
                size_text = self._format_size(stat.st_size)
                mtime_text = QDateTime.fromSecsSinceEpoch(int(stat.st_mtime)).toString("M/d/yyyy h:mm AP")
            base_key = Path(rel).with_suffix("").as_posix().lower()
            ext_lower = Path(rel).suffix.lower().lstrip(".")
            base_exts.setdefault(base_key, set()).add(ext_lower)
            entries.append(
                {
                    "name": Path(rel).name,
                    "priority": priority_map.get(source.replace("Mod: ", ""), -1) if source.startswith("Mod: ") else -1,
                    "mod": source.replace("Mod: ", "") or "Game Override",
                    "type": f"{Path(rel).suffix.upper().lstrip('.')} File" if Path(rel).suffix else "",
                    "size": size_text,
                    "date": mtime_text,
                    "rel": rel,
                    "base": base_key,
                    "path": source_path,
                    "hidden": False,
                }
            )

        for source, rel in hidden_entries:
            hidden_count += 1
            root = source_roots.get(source)
            source_path = root / rel if root else None
            stripped = rel[:-9] if rel.lower().endswith(".mohidden") else rel
            entries.append(
                {
                    "name": Path(rel).name,
                    "priority": priority_map.get(source.replace("Mod: ", ""), -1) if source.startswith("Mod: ") else -1,
                    "mod": source.replace("Mod: ", "") or "Game Override",
                    "type": f"{Path(stripped).suffix.upper().lstrip('.')} File (hidden)" if Path(stripped).suffix else "Hidden",
                    "size": self._format_size(source_path.stat().st_size) if source_path and source_path.exists() else "",
                    "date": QDateTime.fromSecsSinceEpoch(int(source_path.stat().st_mtime)).toString("M/d/yyyy h:mm AP") if source_path and source_path.exists() else "",
                    "rel": rel,
                    "base": Path(stripped).with_suffix("").as_posix().lower(),
                    "path": source_path,
                    "hidden": True,
                }
            )

        major_brush = self._major_conflict_brush()
        minor_brush = self._minor_conflict_brush()
        conflict_brushes: dict[str, QBrush] = {}
        conflict_flags: dict[str, str] = {}
        for base, exts in base_exts.items():
            has_tpc = "tpc" in exts
            has_txi = "txi" in exts
            has_tga = "tga" in exts
            if len(exts) > 1:
                if has_tpc and has_txi:
                    conflict_brushes[base] = major_brush
                    conflict_flags[base] = "!!"
                elif has_tpc and has_tga:
                    conflict_brushes[base] = minor_brush
                    conflict_flags[base] = "!"
                else:
                    conflict_flags[base] = ""

        self._tree.clear()
        major_errors = 0
        minor_errors = 0
        grouped = (
            sorted([e for e in entries if e["base"] in conflict_brushes and not e["hidden"]], key=lambda e: e["rel"].lower())
            + sorted([e for e in entries if e["base"] not in conflict_brushes and not e["hidden"]], key=lambda e: e["rel"].lower())
            + sorted([e for e in entries if e["hidden"]], key=lambda e: e["rel"].lower())
        )
        for entry in grouped:
            flag = "." if entry["hidden"] else conflict_flags.get(entry["base"], "")
            weight = self._WEIGHT_NONE
            if flag == "!!":
                major_errors += 1
                weight = self._WEIGHT_MAJOR
            elif flag == "!":
                minor_errors += 1
                weight = self._WEIGHT_MINOR
            elif flag == ".":
                weight = self._WEIGHT_HIDDEN
            priority_text = str(entry["priority"]) if entry["priority"] >= 0 else ""
            row = _TextureItem([flag, entry["name"], entry["mod"], entry["type"], entry["size"], entry["date"], priority_text])
            row.setToolTip(0, entry["rel"])
            if not entry["hidden"] and entry["base"] in conflict_brushes:
                brush = conflict_brushes[entry["base"]]
                for col in range(7):
                    row.setBackground(col, brush)
                row.setData(0, Qt.ItemDataRole.UserRole + 3, brush.color().name())
            if entry["path"]:
                row.setData(0, Qt.ItemDataRole.UserRole, str(entry["path"]))
            row.setData(0, Qt.ItemDataRole.UserRole + 1, entry["hidden"])
            row.setData(0, Qt.ItemDataRole.UserRole + 2, weight)
            row.setData(0, Qt.ItemDataRole.UserRole + 4, entry["base"])
            row.setData(6, Qt.ItemDataRole.UserRole, entry["priority"])
            self._tree.addTopLevelItem(row)

        self._tree.sortItems(0, Qt.SortOrder.DescendingOrder)
        self._count_label.setText(
            f"{len(items) + hidden_count} texture files | Major: {major_errors} | Minor: {minor_errors} | Hidden: {hidden_count}"
        )

    # Record winning visible files and hidden entries from one root.
    def _scan_root(self, source: str, root: Path, winners: dict[str, tuple[str, str]], hidden_entries: list[tuple[str, str]]):
        for file in root.rglob("*"):
            if not file.is_file():
                continue
            is_hidden = file.name.endswith(".mohidden")
            target_name = file.name[:-9] if is_hidden else file.name
            if Path(target_name).suffix.lower() not in self._EXTENSIONS:
                continue
            rel = (file.relative_to(root).parent / target_name).as_posix() if is_hidden else file.relative_to(root).as_posix()
            key = rel.lower()
            if is_hidden:
                hidden_entries.append((source, rel + ".mohidden"))
            elif key not in winners:
                winners[key] = (source, rel)

    # Resolve the source path stored on a row.
    def _item_path(self, item: QTreeWidgetItem) -> Path | None:
        data = item.data(0, Qt.ItemDataRole.UserRole)
        return Path(str(data)) if data else None

    # Open the clicked texture file externally.
    def _open_item(self, item: QTreeWidgetItem, _column: int):
        path = self._item_path(item)
        if path and path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    # Select all rows that share the same normalized texture base.
    def _sync_base_selection(self):
        if self._syncing_selection:
            return
        selected_items = self._tree.selectedItems()
        if not selected_items:
            return

        selected_bases = {
            item.data(0, Qt.ItemDataRole.UserRole + 4)
            for item in selected_items
            if item.data(0, Qt.ItemDataRole.UserRole + 4)
        }
        self._syncing_selection = True
        try:
            for i in range(self._tree.topLevelItemCount()):
                item = self._tree.topLevelItem(i)
                item_base = item.data(0, Qt.ItemDataRole.UserRole + 4)
                item.setSelected(bool(item_base and item_base in selected_bases))
        finally:
            self._syncing_selection = False

    # Show the hide/unhide context menu for a row.
    def _context_menu(self, pos: QPoint):
        item = self._tree.itemAt(pos)
        if not item:
            return
        path = self._item_path(item)
        if not path:
            return
        is_hidden = bool(item.data(0, Qt.ItemDataRole.UserRole + 1))
        menu = QMenu(self)
        action = menu.addAction("Unhide" if is_hidden else "Hide (.mohidden)")
        action.triggered.connect(lambda: self._toggle_hidden(path, is_hidden))
        menu.exec(self._tree.viewport().mapToGlobal(pos))

    # Toggle the .mohidden suffix for a texture file.
    def _toggle_hidden(self, path: Path, currently_hidden: bool):
        try:
            if currently_hidden and path.name.endswith(".mohidden"):
                new_path = path.with_name(path.name[:-9])
            elif not currently_hidden:
                new_path = path.with_name(path.name + ".mohidden")
            else:
                return
            path.rename(new_path)
        except Exception as e:
            logger.warning(f"[KOTOR2] Failed to toggle hidden state for {path}: {e}")
        finally:
            self.refresh()

    # Hide lower-priority visible texture variants for each texture base.
    def _auto_fix(self):
        visible_by_base: dict[str, dict[str, Path]] = {}
        for i in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(i)
            if bool(item.data(0, Qt.ItemDataRole.UserRole + 1)):
                continue
            path = self._item_path(item)
            base = item.data(0, Qt.ItemDataRole.UserRole + 4)
            if not path or not base:
                continue
            visible_by_base.setdefault(str(base), {})[path.suffix.lower()] = path

        to_hide: list[Path] = []
        for files in visible_by_base.values():
            winner_exts = self._winner_extensions(files)
            if not winner_exts:
                continue
            for ext, path in files.items():
                if ext not in winner_exts:
                    to_hide.append(path)

        for path in to_hide:
            try:
                path.rename(path.with_name(path.name + ".mohidden"))
            except Exception as e:
                logger.warning(f"[KOTOR2] Failed to auto-hide {path}: {e}")
        self.refresh()

    # Unhide every hidden texture in the active mod stack.
    def _unhide_all(self):
        paths: list[Path] = []
        for i in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(i)
            path = self._item_path(item)
            if bool(item.data(0, Qt.ItemDataRole.UserRole + 1)) and path and path.name.endswith(".mohidden"):
                paths.append(path)
        for path in paths:
            try:
                path.rename(path.with_name(path.name[:-9]))
            except Exception as e:
                logger.warning(f"[KOTOR2] Failed to unhide {path}: {e}")
        self.refresh()

    # Return the visible extension set to keep for one texture base.
    @staticmethod
    def _winner_extensions(files: dict[str, Path]) -> set[str]:
        if ".tpc" in files:
            return {".tpc"}
        if ".tga" in files and ".txi" not in files:
            return {".tga"}
        if ".tga" in files and ".txi" in files:
            return {".tga", ".txi"}
        if ".dds" in files and ".txi" not in files:
            return {".dds"}
        if ".dds" in files and ".txi" in files:
            return {".dds", ".txi"}
        if ".txi" in files:
            return {".txi"}
        return set()

    # Format a byte count for display.
    @staticmethod
    def _format_size(size: int) -> str:
        units = ["B", "KB", "MB", "GB"]
        val = float(size)
        for unit in units:
            if val < 1024 or unit == units[-1]:
                text = f"{val:.2f}".rstrip("0").rstrip(".")
                return f"{text} {unit}"
            val /= 1024
