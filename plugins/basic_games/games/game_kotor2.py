import logging
import os
import struct
from pathlib import Path
import winreg

import mobase
from PyQt6.QtCore import QDateTime, QDir, QPoint, QTimer, QUrl, Qt
from PyQt6.QtGui import QColor, QBrush, QDesktopServices, QImage, QPixmap
from PyQt6.QtWidgets import (
    QHeaderView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from basic_games.basic_game import BasicGame
from basic_games.basic_features import (
    BasicLocalSavegames,
    BasicGameSaveGameInfo,
    BasicModDataChecker,
    GlobPatterns,
)
from basic_games.basic_features.basic_save_game_info import BasicGameSaveGame, format_date
from basic_games.basic_features.utils import is_directory

logger = logging.getLogger("mobase")


# Savegame handling


class Kotor2SaveGame(BasicGameSaveGame):
    """Represents a single KOTOR II save directory with thumbnail preview."""

    def getName(self) -> str:
        return Path(self._filepath).name

    def getCreationTime(self) -> QDateTime:
        newest = 0
        for f in Path(self._filepath).glob("*"):
            if f.is_file():
                newest = max(newest, f.stat().st_mtime)
        return QDateTime.fromSecsSinceEpoch(int(newest))

    def getScreenshot(self):
        """Decode and return the TGA save thumbnail as QPixmap."""

        for name in ["Screen.tga", "screen.tga", "SCREEN.TGA"]:
            tga_path = Path(self._filepath) / name
            if tga_path.exists():
                break
        else:
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

    # MO2 UI expects these for Save tab preview
    def _pixmap(self):
        if not hasattr(self, "_cached_pixmap"):
            self._cached_pixmap = self.getScreenshot()
        return self._cached_pixmap

    def isNull(self):
        return self._pixmap().isNull()

    def scaledToWidth(self, width, mode=None):
        pm = self._pixmap()
        try:
            return pm.scaledToWidth(width, mode) if not pm.isNull() else pm
        except Exception:
            return pm

    def scaledToHeight(self, height, mode=None):
        pm = self._pixmap()
        try:
            return pm.scaledToHeight(height, mode) if not pm.isNull() else pm
        except Exception:
            return pm


def parse_kotor2_save_metadata(save_path: Path, save: mobase.ISaveGame):
    files = [f.name for f in save_path.glob("*")]
    meta = {
        "Files": ", ".join(files[:5]) + ("..." if len(files) > 5 else ""),
        "Modified": format_date(save.getCreationTime(), "hh:mm:ss, d.M.yyyy"),
    }
    return meta


# Texture tab widget


class _TextureItem(QTreeWidgetItem):
    """Custom item to enforce Err column priority when sorting."""

    def __lt__(self, other: "QTreeWidgetItem") -> bool:  # type: ignore[override]
        tree = self.treeWidget()
        if tree and tree.sortColumn() == 0:
            my_weight = self.data(0, Qt.ItemDataRole.UserRole + 2)
            other_weight = other.data(0, Qt.ItemDataRole.UserRole + 2)
            try:
                return int(my_weight) < int(other_weight)
            except Exception:
                pass
        return super().__lt__(other)


class Kotor2TextureTab(QWidget):
    """Lists texture assets from active mods and the game override folder."""

    _EXTENSIONS = {".tga", ".tpc", ".txi", ".dds"}
    # Sort weights for the Err column (highest first)
    _WEIGHT_NONE = 0
    _WEIGHT_HIDDEN = 1  # hidden rows sit below conflicts
    _WEIGHT_MINOR = 2
    _WEIGHT_MAJOR = 3

    def __init__(
        self,
        parent: QWidget | None,
        organizer: mobase.IOrganizer,
        game: "StarWarsKotor2Game",
    ):
        super().__init__(parent)
        self._organizer = organizer
        self._game = game

        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        self._count_label = QLabel("0 texture files")
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh)  # type: ignore
        header.addWidget(self._count_label)
        header.addStretch()
        unhide_btn = QPushButton("Unhide all")
        unhide_btn.clicked.connect(self._unhide_all)  # type: ignore
        header.addWidget(unhide_btn)
        header.addWidget(refresh_btn)
        layout.addLayout(header)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(6)
        self._tree.setHeaderLabels(["Err", "Name", "Mod", "Type", "Size", "Date modified"])
        header_view = self._tree.header()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for col in range(2, 6):
            header_view.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self._tree.setRootIsDecorated(False)
        self._tree.setSortingEnabled(True)
        self._tree.itemDoubleClicked.connect(self._open_item)  # type: ignore
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._context_menu)  # type: ignore
        layout.addWidget(self._tree)

        organizer.onProfileChanged(lambda a, b: self.refresh())
        organizer.modList().onModInstalled(lambda mod: self.refresh())
        organizer.modList().onModRemoved(lambda mod: self.refresh())
        organizer.modList().onModStateChanged(lambda mods: self.refresh())

        self.refresh()

    def _iter_override_roots(self):
        """Yield override directories in mod priority order followed by game override."""
        for mod_path in self._game._active_mod_paths():
            for candidate in (mod_path / "override", mod_path / "Override"):
                if candidate.exists() and candidate.is_dir():
                    yield f"Mod: {mod_path.name}", candidate
                    break

        override_path = Path(self._game.overrideDirectory().absolutePath())
        if override_path.exists():
            yield "Game Override", override_path

    def refresh(self):
        """Rebuild the table: collect winners, detect conflicts, and flag hidden files."""
        # winners => key: relative path (lower), value: (source label, relative path)
        winners: dict[str, tuple[str, str]] = {}
        hidden_entries: list[tuple[str, str]] = []

        mod_entries: list[tuple[str, Path]] = []
        game_entry: tuple[str, Path] | None = None

        for source, root in self._iter_override_roots():
            if source.startswith("Mod: "):
                mod_entries.append((source, root))
            else:
                game_entry = (source, root)

        for source, root in reversed(mod_entries):
            for file in root.rglob("*"):
                if not file.is_file():
                    continue
                is_hidden = file.name.endswith(".mohidden")
                target_name = file.name[:-9] if is_hidden else file.name
                target_ext = Path(target_name).suffix.lower()
                if target_ext not in self._EXTENSIONS:
                    continue
                rel = (file.relative_to(root).parent / target_name).as_posix() if is_hidden else file.relative_to(root).as_posix()
                key = rel.lower()
                if is_hidden:
                    hidden_entries.append((source, rel + ".mohidden"))
                    continue
                if key not in winners:
                    winners[key] = (source, rel)

        if game_entry:
            source, root = game_entry
            for file in root.rglob("*"):
                if not file.is_file():
                    continue
                is_hidden = file.name.endswith(".mohidden")
                target_name = file.name[:-9] if is_hidden else file.name
                target_ext = Path(target_name).suffix.lower()
                if target_ext not in self._EXTENSIONS:
                    continue
                rel = (file.relative_to(root).parent / target_name).as_posix() if is_hidden else file.relative_to(root).as_posix()
                key = rel.lower()
                if is_hidden:
                    hidden_entries.append((source, rel + ".mohidden"))
                    continue
                if key not in winners:
                    winners[key] = (source, rel)

        items = sorted(winners.values(), key=lambda i: i[1].lower())
        entries: list[dict] = []
        base_exts: dict[str, set[str]] = {}
        hidden_count = 0

        for source, rel in items:
            mod_name = source.replace("Mod: ", "")
            name = Path(rel).name
            suffix = Path(rel).suffix.upper().lstrip(".")
            size_text = ""
            mtime_text = ""
            source_path = None

            if mod_name and mod_name != "Game Override":
                for mod_path in self._game._active_mod_paths():
                    if mod_path.name == mod_name:
                        source_path = mod_path / "override" / rel
                        break
            else:
                source_path = Path(self._game.overrideDirectory().absolutePath()) / rel

            if source_path and source_path.exists():
                stat = source_path.stat()
                size_text = self._format_size(stat.st_size)
                mtime_text = QDateTime.fromSecsSinceEpoch(int(stat.st_mtime)).toString("M/d/yyyy h:mm AP")

            base_key = Path(rel).with_suffix("").as_posix().lower()
            ext_lower = Path(rel).suffix.lower().lstrip(".")
            base_exts.setdefault(base_key, set()).add(ext_lower)

            entries.append(
                {
                    "name": name,
                    "mod": mod_name or "Game Override",
                    "type": f"{suffix} File" if suffix else "",
                    "size": size_text,
                    "date": mtime_text,
                    "rel": rel,
                    "base": base_key,
                    "ext": ext_lower,
                    "path": source_path,
                    "hidden": False,
                }
            )

        for source, rel in hidden_entries:
            hidden_count += 1
            mod_name = source.replace("Mod: ", "")
            source_path = None
            if mod_name and mod_name != "Game Override":
                for mod_path in self._game._active_mod_paths():
                    if mod_path.name == mod_name:
                        source_path = mod_path / "override" / rel
                        break
            else:
                source_path = Path(self._game.overrideDirectory().absolutePath()) / rel

            suffix = Path(rel[:-9]).suffix.upper().lstrip(".") if rel.lower().endswith(".mohidden") else ""
            entries.append(
                {
                    "name": Path(rel).name,
                    "mod": mod_name or "Game Override",
                    "type": f"{suffix} File (hidden)" if suffix else "Hidden",
                    "size": self._format_size(source_path.stat().st_size) if source_path and source_path.exists() else "",
                    "date": QDateTime.fromSecsSinceEpoch(int(source_path.stat().st_mtime)).toString("M/d/yyyy h:mm AP") if source_path and source_path.exists() else "",
                    "rel": rel,
                    "base": Path(rel[:-9]).with_suffix("").as_posix().lower() if rel.lower().endswith(".mohidden") else Path(rel).with_suffix("").as_posix().lower(),
                    "ext": Path(rel[:-9]).suffix.lower().lstrip(".") if rel.lower().endswith(".mohidden") else Path(rel).suffix.lower().lstrip("."),
                    "path": source_path,
                    "hidden": True,
                }
            )

        def conflict_color(exts: set[str]) -> QBrush | None:
            """Highlight known conflict combos."""
            has_tpc = "tpc" in exts
            has_txi = "txi" in exts
            has_tga = "tga" in exts
            if has_tpc and has_txi:
                return QBrush(QColor("#ffcccc"))
            if has_tpc and has_tga:
                return QBrush(QColor("#fff4cc"))
            return None

        conflict_brushes: dict[str, QBrush] = {}
        conflict_flags: dict[str, str] = {}
        for base, exts in base_exts.items():
            if len(exts) > 1:
                if brush := conflict_color(exts):
                    conflict_brushes[base] = brush
                has_tpc = "tpc" in exts
                has_txi = "txi" in exts
                has_tga = "tga" in exts
                if has_tpc and has_txi:
                    conflict_flags[base] = "!!"
                elif has_tpc and has_tga:
                    conflict_flags[base] = "!"
                else:
                    conflict_flags[base] = ""

        conflict_entries = [e for e in entries if e["base"] in conflict_brushes and not e["hidden"]]
        normal_entries = [e for e in entries if e["base"] not in conflict_brushes and not e["hidden"]]
        hidden_only_entries = [e for e in entries if e["hidden"]]
        conflict_entries.sort(key=lambda e: e["rel"].lower())
        normal_entries.sort(key=lambda e: e["rel"].lower())
        hidden_only_entries.sort(key=lambda e: e["rel"].lower())

        self._tree.clear()
        major_errors = 0
        minor_errors = 0
        for e in conflict_entries + normal_entries + hidden_only_entries:
            flag = "." if e["hidden"] else conflict_flags.get(e["base"], "")
            weight = self._WEIGHT_NONE
            if flag == "!!":
                major_errors += 1
                weight = self._WEIGHT_MAJOR
            elif flag == "!":
                minor_errors += 1
                weight = self._WEIGHT_MINOR
            elif flag == ".":
                weight = self._WEIGHT_HIDDEN
            row = _TextureItem(
                [flag, e["name"], e["mod"], e["type"], e["size"], e["date"]]
            )
            row.setToolTip(0, e["rel"])
            if not e["hidden"] and e["base"] in conflict_brushes:
                brush = conflict_brushes[e["base"]]
                for col in range(6):
                    row.setBackground(col, brush)
            if e["path"]:
                row.setData(0, Qt.ItemDataRole.UserRole, str(e["path"]))
            row.setData(0, Qt.ItemDataRole.UserRole + 1, e["hidden"])
            row.setData(0, Qt.ItemDataRole.UserRole + 2, weight)
            self._tree.addTopLevelItem(row)

        self._tree.sortItems(0, Qt.SortOrder.DescendingOrder)

        self._count_label.setText(
            f"{len(items) + hidden_count} texture files | Major: {major_errors} | Minor: {minor_errors} | Hidden: {hidden_count}"
        )

    def _item_path(self, item: QTreeWidgetItem) -> Path | None:
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data:
            return Path(str(data))
        return None

    def _open_item(self, item: QTreeWidgetItem, _column: int):
        path = self._item_path(item)
        if path and path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _context_menu(self, pos: QPoint):
        item = self._tree.itemAt(pos)
        if not item:
            return
        path = self._item_path(item)
        if not path:
            return
        is_hidden = bool(item.data(0, Qt.ItemDataRole.UserRole + 1))
        menu = QMenu(self)
        if is_hidden:
            action = menu.addAction("Unhide")
            action.triggered.connect(lambda: self._toggle_hidden(path, True))  # type: ignore
        else:
            action = menu.addAction("Hide (.mohidden)")
            action.triggered.connect(lambda: self._toggle_hidden(path, False))  # type: ignore
        menu.exec(self._tree.viewport().mapToGlobal(pos))

    def _toggle_hidden(self, path: Path, currently_hidden: bool):
        """Rename the file to toggle .mohidden suffix."""
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

    def _unhide_all(self):
        """Unhide every currently listed .mohidden file."""
        # Walk tree items to find hidden paths
        paths: list[Path] = []
        for i in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(i)
            is_hidden = bool(item.data(0, Qt.ItemDataRole.UserRole + 1))
            path = self._item_path(item)
            if is_hidden and path and path.name.endswith(".mohidden"):
                paths.append(path)

        for path in paths:
            try:
                path.rename(path.with_name(path.name[:-9]))
            except Exception as e:
                logger.warning(f"[KOTOR2] Failed to unhide {path}: {e}")

        self.refresh()

    @staticmethod
    def _format_size(size: int) -> str:
        units = ["B", "KB", "MB", "GB"]
        val = float(size)
        for u in units:
            if val < 1024 or u == units[-1]:
                text = f"{val:.2f}".rstrip("0").rstrip(".")
                return f"{text} {u}"
            val /= 1024


# Mod validation / fixer

class Kotor2ModDataChecker(BasicModDataChecker):
    """Validates and fixes mod folder layout for KOTOR II (TSL)."""

    _valid_map = {
        "override": (".tga", ".dds", ".mdl", ".mdx", ".uti", ".utc",
                     ".ncs", ".nss", ".2da", ".dlg", ".wav", ".mp3",
                     ".bik", ".txi", ".tpc"),
        "movies": (".bik",),
        "data": (".bif",),
        "lips": (".mod",),
        "modules": (".erf", ".rim", ".mod"),
        "streammusic": (".wav",),
        "streamsounds": (".wav",),
        "streamvoice": (".wav",),
        "texturepacks": (".erf",),
    }

    _ignored_exts = (
        ".txt", ".pdf", ".png", ".jpg", ".jpeg", ".bmp", ".gif",
        ".md", ".rtf", ".doc", ".docx", ".ini", ".html", ".url",
        ".log", ".bak", ".xml", ".docx#"
    )

    _restricted_dirs = {"data"}

    def __init__(self):
        all_exts = tuple(ext for exts in self._valid_map.values() for ext in exts)
        super().__init__(GlobPatterns(all_exts))

    # Helpers

    def _iter_dirs(self, node):
        for e in list(node):
            if is_directory(e):
                yield e
                yield from self._iter_dirs(e)

    def _find_dirs_named(self, node, name_lower: str):
        name_lower = name_lower.lower()
        return [d for d in self._iter_dirs(node) if d.name().lower() == name_lower]

    def _file_is_valid_for_path(self, file_node, path: str) -> bool:
        if is_directory(file_node):
            return False
        fname = file_node.name().lower()
        for folder, exts in self._valid_map.items():
            if folder in path.lower():
                return any(fname.endswith(ext) for ext in exts)
        return False
    
    def _cleanup_root(self, filetree: mobase.IFileTree):
        """
        Remove:
        - Any top-level entries that are NOT valid game dirs
        - Any INVALID files inside override/
        """

        valid_top = set(self._valid_map.keys()) | {"override", "tslpatchdata"}
        ignored = self._ignored_exts
        valid_exts = set(ext for exts in self._valid_map.values() for ext in exts)

        # Clean root: drop stray top-level folders/files
        for entry in list(filetree):
            name = entry.name().lower()

            # allowed top-level directories ONLY
            if name in valid_top:
                continue

            # remove everything else (leftover folder, readme, garbage)
            entry.detach()

        # Clean invalid files inside override/
        override_dirs = self._find_dirs_named(filetree, "override")
        if override_dirs:
            override = override_dirs[0]

            for child in list(override):
                if is_directory(child):
                    # remove nested directories inside override — not allowed
                    child.detach()
                    continue

                # extract ext properly
                _, ext = os.path.splitext(child.name().lower())

                # remove ignored + non-valid extensions
                if ext in ignored or ext not in valid_exts:
                    child.detach()



    # Validation

    def dataLooksValid(self, filetree: mobase.IFileTree) -> mobase.ModDataChecker.CheckReturn:
        tsl_dirs = self._find_dirs_named(filetree, "tslpatchdata")
        if tsl_dirs:
            return mobase.ModDataChecker.FIXABLE

        # Restricted directories immediately invalidate the mod
        for d in self._iter_dirs(filetree):
            if d.name().lower() in self._restricted_dirs:
                return mobase.ModDataChecker.INVALID

        # Root-level unknown folder with files => FIXABLE (single-folder flatten)
        ignored = self._ignored_exts

        def parent_has_valid_dir(node) -> bool:
            p = node.parent()
            while p is not None and p != filetree:
                if p.name().lower() in self._valid_map.keys():
                    return True
                p = p.parent()
            return False

        for entry in list(filetree):
            if not is_directory(entry):
                continue
            dname = entry.name().lower()
            if dname in self._valid_map.keys():
                continue

            # Only consider root-level unknown folders
            for child in list(entry):
                if not is_directory(child):
                    _, ext = os.path.splitext(child.name().lower())
                    if ext not in ignored:
                        # This is a root folder the user zipped up manually; needs flattening
                        return mobase.ModDataChecker.FIXABLE
            # If no mod files inside, skip it

            # Nested known game dir inside an extra folder (e.g., MyMod/Override/...)
            for child in list(entry):
                if not is_directory(child):
                    continue
                cname = child.name().lower()
                if cname in self._valid_map.keys():
                    # If that nested known dir has any files, we can fix by flattening
                    for grand in list(child):
                        if not is_directory(grand):
                            return mobase.ModDataChecker.FIXABLE

        # Any non-valid directory anywhere (not under a valid game dir) containing files => FIXABLE
        for d in self._iter_dirs(filetree):
            dname = d.name().lower()
            if dname in self._valid_map.keys():
                continue
            if parent_has_valid_dir(d):
                continue
            for child in list(d):
                if not is_directory(child):
                    _, ext = os.path.splitext(child.name().lower())
                    if ext not in ignored:
                        return mobase.ModDataChecker.FIXABLE

        # Normal valid folders (override/, modules/, etc.)
        for folder in self._valid_map.keys():
            found = self._find_dirs_named(filetree, folder)
            if found:
                return mobase.ModDataChecker.VALID

        # Loose files with known mod extensions are fixable
        all_valid_exts = tuple(ext for exts in self._valid_map.values() for ext in exts)
        if any(not is_directory(e) and e.name().lower().endswith(all_valid_exts) for e in filetree):
            return mobase.ModDataChecker.FIXABLE

        # dialog.tlk alone = valid
        if any(not is_directory(e) and e.name().lower() == "dialog.tlk" for e in filetree):
            return mobase.ModDataChecker.VALID

        return mobase.ModDataChecker.INVALID


    # Fixer

    def fix(self, filetree: mobase.IFileTree) -> mobase.IFileTree:
        tsl_dirs = self._find_dirs_named(filetree, "tslpatchdata")

        # Multiple tslpatchdata folders
        if len(tsl_dirs) > 1:
            options = []
            mapping = {}

            for d in tsl_dirs:
                parent = d.parent()
                if parent is not None:
                    display_name = parent.name()
                else:
                    display_name = d.name()
                options.append(display_name)
                mapping[display_name] = d

            choice, ok = QInputDialog.getItem(
                None, "Select TSLPatcher Folder",
                "Multiple TSLPatcher folders found. Choose which one to keep:",
                options, 0, False
            )

            if ok and choice:
                selected = mapping[choice]
                filetree.move(selected, "tslpatchdata")

                # Remove all other top-level entries
                for top in list(filetree):
                    if top.name().lower() != "tslpatchdata":
                        top.detach()

                return filetree

        # Single tslpatchdata folder
        if len(tsl_dirs) == 1:
            selected = tsl_dirs[0]
            filetree.move(selected, "tslpatchdata")
            for top in list(filetree):
                if top.name().lower() != "tslpatchdata":
                    top.detach()
            return filetree

        # Directories containing mod files (files NOT in ignored list)
        ignored = self._ignored_exts

        valid_dirs = []

        # Scan all subdirectories (excluding root)
        for d in self._iter_dirs(filetree):
            if d.parent() is None:
                continue  # skip the root archive node

            # does this directory contain at least one NON-ignored file?
            for child in list(d):
                if not is_directory(child) and not child.name().lower().endswith(ignored):
                    valid_dirs.append(d)
                    break

        # MULTIPLE valid dirs, user chooses which one is the mod folder
        if len(valid_dirs) > 1:
            options = [d.name() for d in valid_dirs]
            mapping = {d.name(): d for d in valid_dirs}

            choice, ok = QInputDialog.getItem(
                None,
                "Select Mod Folder",
                "Multiple folders contain mod files. Choose the correct one:",
                options,
                0,
                False
            )

            if ok and choice:
                keep = mapping[choice]

                # Move chosen folder to override/
                filetree.move(keep, "override")

                # Remove everything except override/
                for top in list(filetree):
                    if top.name().lower() != "override":
                        top.detach()

                return filetree

        # SIMPLE CASE: exactly ONE directory at the root → flatten into override
        root_dirs = [e for e in list(filetree) if is_directory(e)]

        if len(root_dirs) == 1:
            keep = root_dirs[0]

            # ensure override exists
            if not self._find_dirs_named(filetree, "override"):
                filetree.addDirectory("override")

            # move ALL files directly into override
            for child in list(keep):
                if not is_directory(child):
                    filetree.move(child, f"override/{child.name()}")



            self._cleanup_root(filetree)
            return filetree



        # Loose files in root that are NOT in ignored list → override
        ignored = self._ignored_exts

        root_files = [e for e in list(filetree) if not is_directory(e)]

        # valid = anything NOT in ignored list
        loose_valid = [
            f for f in root_files
            if not f.name().lower().endswith(ignored)
        ]

        if loose_valid:
            # ensure override exists
            if not self._find_dirs_named(filetree, "override"):
                filetree.addDirectory("override")

            for f in loose_valid:
                filetree.move(f, f"override/{f.name()}")

            self._cleanup_root(filetree)
            return filetree



        return filetree


# Main plugin

class StarWarsKotor2Game(BasicGame, mobase.IPluginFileMapper):
    """Mod Organizer 2 (dev build) plugin for STAR WARS Knights of the Old Republic II: TSL."""

    def __init__(self):
        BasicGame.__init__(self)
        mobase.IPluginFileMapper.__init__(self)
        self._texture_tab: Kotor2TextureTab | None = None
        self._platform_logged = False

    Name = "STAR WARS Knights of the Old Republic II The Sith Lords"
    Author = "J"
    Version = "1.4.1"

    GameName = Name
    GameShortName = "kotor2"
    GameNexusName = "kotor2"
    GameNexusId = 198
    GameSteamId = 208580
    GameGogId = 1421404581
    GameBinary = "swkotor2.exe"
    GameDataPath = "%GAME_PATH%"

    def init(self, organizer: mobase.IOrganizer) -> bool:
        """Initialize plugin, register saves + validation, and map directories."""
        super().init(organizer)
        self._organizer = organizer

        # Register features
        self._register_feature(BasicLocalSavegames(self.savesDirectory()))
        self._register_feature(
            BasicGameSaveGameInfo(Kotor2SaveGame, parse_kotor2_save_metadata)
        )
        self._register_feature(Kotor2ModDataChecker())
        organizer.onUserInterfaceInitialized(self._init_texture_tab)
        organizer.onAboutToRun(lambda app: self._log_platform_once())

        # If the managed game is already set (path chosen), log immediately.
        try:
            mg = self._organizer.managedGame()
            if mg and (mg == self or mg.gameName() == self.gameName()) and self.gameDirectory().exists():
                self._log_platform_once(force=True)
        except Exception:
            logger.info("[KOTOR2] Platform logging failed")

        # Bootstrap expected game directories for USVFS mapping
        if (
            self._organizer.managedGame()
            and self._organizer.managedGame().gameName() == self.gameName()
        ):
            for d in [
                self.dataDirectory(),
                self.lipsDirectory(),
                self.modulesDirectory(),
                self.moviesDirectory(),
                self.overrideDirectory(),
                self.streamMusicDirectory(),
                self.streamSoundsDirectory(),
                self.streamVoiceDirectory(),
                self.texturePacksDirectory(),
                self.savesDirectory(),
            ]:
                os.makedirs(d.absolutePath(), exist_ok=True)

        return True

    # Directory mappings
    def dataDirectory(self): return QDir(self.gameDirectory().absolutePath() + "/Data")
    def lipsDirectory(self): return QDir(self.gameDirectory().absolutePath() + "/Lips")
    def modulesDirectory(self): return QDir(self.gameDirectory().absolutePath() + "/Modules")
    def moviesDirectory(self): return QDir(self.gameDirectory().absolutePath() + "/Movies")
    def overrideDirectory(self): return QDir(self.gameDirectory().absolutePath() + "/Override")
    def streamMusicDirectory(self): return QDir(self.gameDirectory().absolutePath() + "/StreamMusic")
    def streamSoundsDirectory(self): return QDir(self.gameDirectory().absolutePath() + "/StreamSounds")
    def streamVoiceDirectory(self): return QDir(self.gameDirectory().absolutePath() + "/StreamVoice")
    def texturePacksDirectory(self): return QDir(self.gameDirectory().absolutePath() + "/TexturePacks")
    def savesDirectory(self): return QDir(self.gameDirectory().absolutePath() + "/saves")

    def getModMappings(self) -> dict[str, list[str]]:
        return {
            "Data": [self.dataDirectory().absolutePath()],
            "Lips": [self.lipsDirectory().absolutePath()],
            "Modules": [self.modulesDirectory().absolutePath()],
            "Movies": [self.moviesDirectory().absolutePath()],
            "Override": [self.overrideDirectory().absolutePath()],
            "StreamMusic": [self.streamMusicDirectory().absolutePath()],
            "StreamSounds": [self.streamSoundsDirectory().absolutePath()],
            "StreamVoice": [self.streamVoiceDirectory().absolutePath()],
            "TexturePacks": [self.texturePacksDirectory().absolutePath()],
        }

    def _active_mod_paths(self):
        mods_root = Path(self._organizer.modsPath())
        modlist = self._organizer.modList().allModsByProfilePriority()

        for mod_name in modlist:
            state = self._organizer.modList().state(mod_name)
            if state & mobase.ModState.ACTIVE:
                yield mods_root / mod_name

    def mappings(self) -> list[mobase.Mapping]:
        mappings = []
        game_path = Path(self.gameDirectory().absolutePath())

        # Look through all active mods
        for mod_path in self._active_mod_paths():
            if not mod_path.exists():
                continue

            for child in mod_path.iterdir():
                # ONLY map dialog.tlk
                if child.name.lower() != "dialog.tlk":
                    continue

                dest = game_path / "dialog.tlk"

                mappings.append(
                    mobase.Mapping(
                        source=str(child),
                        destination=str(dest),
                        is_directory=False,
                        create_target=False,
                    )
                )

        return mappings

    def _log_platform_once(self, force: bool = False) -> bool:
        """Log detected platform when the game path is known."""
        if self._platform_logged and not force:
            return True
        try:
            gd = self.gameDirectory()
            steam_root = self._detect_steam_root(Path(gd.absolutePath()))
            self._warn_if_workshop_present(steam_root)

            logger.info(
                "[KOTOR2] Steam detected:%s path:%s steam_root:%s"
                % (
                    self.is_steam(),
                    gd.absolutePath(),
                    steam_root,
                )
            )
            self._platform_logged = True
        except Exception as e:
            logger.info("[KOTOR2] Platform logging failed: %s", e)
        return True

    def _detect_steam_root(self, game_path: Path) -> str:
        """Try to infer Steam install from path, then registry."""
        try:
            parts = [p.lower() for p in game_path.parts]
            if "steamapps" in parts:
                idx = parts.index("steamapps")
                return str(Path(*game_path.parts[:idx]))
        except Exception:
            pass

        reg_paths = [
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
        ]
        for hive, key, value in reg_paths:
            try:
                with winreg.OpenKeyEx(hive, key, 0, winreg.KEY_READ) as k:
                    data, _ = winreg.QueryValueEx(k, value)
                    if data:
                        return str(Path(str(data)))
            except FileNotFoundError:
                continue
            except Exception:
                continue

        return "unknown"

    def _warn_if_workshop_present(self, steam_root: str):
        """Warn if Steam Workshop content for KOTOR2 is detected."""
        if steam_root.lower() == "unknown":
            return
        workshop_path = Path(steam_root) / "steamapps" / "workshop" / "content" / "208580"
        try:
            if workshop_path.exists() and any(workshop_path.iterdir()):
                logger.warning("[KOTOR2] Steam Workshop content detected")
                try:
                    QTimer.singleShot(
                        2000,  # give MO2 UI time to finish loading
                        lambda: QMessageBox.warning(
                            None,
                            "KOTOR II",
                            "Steam Workshop content detected for KOTOR II. Workshop mods are unsupported in Mod Orgainizer 2.",
                        ),
                    )
                except Exception:
                    pass
        except Exception as e:
            logger.debug("[KOTOR2] Workshop check failed: %s", e)

    def _init_texture_tab(self, main_window: QMainWindow):
        if self._organizer.managedGame() != self:
            return

        tab_widget: QTabWidget | None = main_window.findChild(QTabWidget, "tabWidget")
        if not tab_widget:
            return

        data_index = None
        saves_index = None
        for i in range(tab_widget.count()):
            text = tab_widget.tabText(i).lower()
            if text == "data":
                data_index = i
            if text == "saves":
                saves_index = i

        insert_index = tab_widget.count()
        if data_index is not None:
            insert_index = data_index + 1
        elif saves_index is not None:
            insert_index = saves_index

        self._texture_tab = Kotor2TextureTab(main_window, self._organizer, self)
        tab_widget.insertTab(insert_index, self._texture_tab, "Textures")

    # INI / executables
    def iniFiles(self):
        return [self.gameDirectory().absoluteFilePath("swkotor2.ini")]

    def executables(self):
        self._log_platform_once()
        exe_path = self.gameDirectory().absoluteFilePath(self.binaryName())
        hk_path = str((Path(__file__).resolve().parent / "kotor2" / "hk_reassembler.bat").absolute())
        logger.info(f"[KOTOR2 Plugin] registering executables: {exe_path}, {hk_path}")
        return [
            mobase.ExecutableInfo("KOTOR2", exe_path),
            mobase.ExecutableInfo("HK Reassembler", hk_path),
        ]

    # Save listing
    def listSaves(self, folder: QDir) -> list[mobase.ISaveGame]:
        saves = []
        root = Path(folder.absolutePath())
        for sub in root.iterdir():
            if sub.is_dir() and any(f.suffix == ".sav" for f in sub.iterdir()):
                saves.append(Kotor2SaveGame(sub))
        return saves


# Required entry point

def createPlugin() -> mobase.IPluginGame:
    return StarWarsKotor2Game()
