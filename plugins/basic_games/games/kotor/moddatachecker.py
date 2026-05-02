import os

import mobase
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from basic_games.basic_features import BasicModDataChecker, GlobPatterns
from basic_games.basic_features.utils import is_directory


# Check and fix KOTOR mod archive layouts.
class KotorModDataCheckerBase(BasicModDataChecker):
    _valid_map = {
        "override": (
            ".2da", ".are", ".bik", ".dlg", ".dds", ".fac", ".git",
            ".ifo", ".jrl", ".lip", ".lyt", ".mdl", ".mdx", ".mp3",
            ".ncs", ".nss", ".ssf", ".tga", ".tpc", ".txi", ".utc",
            ".utd", ".ute", ".uti", ".utm", ".utp", ".uts", ".utt",
            ".utw", ".wav", ".wok",
        ),
        "movies": (".bik",),
        "data": (".bif",),
        "lips": (".mod",),
        "modules": (".erf", ".rim", ".mod"),
        "streammusic": (".wav",),
        "streamsounds": (".wav",),
        "streamwaves": (".wav",),
        "streamvoice": (".wav",),
        "texturepacks": (".erf",),
    }

    _ignored_exts = (
        ".txt", ".pdf", ".png", ".jpg", ".jpeg", ".bmp", ".gif",
        ".md", ".rtf", ".doc", ".docx", ".ini", ".html", ".url",
        ".log", ".bak", ".xml", ".docx#",
    )

    _restricted_dirs = {"data"}

    # Create the checker with valid extensions.
    def __init__(self):
        all_exts = tuple(ext for exts in self._valid_map.values() for ext in exts)
        self._all_valid_exts = {ext.lower() for ext in all_exts}
        super().__init__(GlobPatterns(all_exts))

    # Yield all directories below a node.
    def _iter_dirs(self, node):
        for entry in list(node):
            if is_directory(entry):
                yield entry
                yield from self._iter_dirs(entry)

    # Find directories with a matching name.
    def _find_dirs_named(self, node, name_lower: str):
        name_lower = name_lower.lower()
        return [directory for directory in self._iter_dirs(node) if directory.name().lower() == name_lower]

    # Find one file or directory by relative path below a tree node.
    def _find_entry_by_relpath(self, node, relpath: str):
        parts = [part for part in relpath.replace("\\", "/").split("/") if part]
        if not parts:
            return None
        current = node
        for index, part in enumerate(parts):
            match = next((child for child in list(current) if child.name().lower() == part.lower()), None)
            if match is None:
                return None
            if index == len(parts) - 1:
                return match
            if not is_directory(match):
                return None
            current = match
        return None

    # Check for ignored source folders.
    def _is_ignored_source_dir_name(self, name: str) -> bool:
        return name.lower().startswith("source")

    # Check whether a file is a KOTOR mod file.
    def _is_valid_mod_file(self, file_node) -> bool:
        if is_directory(file_node):
            return False
        file_name = file_node.name().lower()
        if file_name == "dialog.tlk":
            return True
        _, ext = os.path.splitext(file_name)
        if ext in self._ignored_exts:
            return False
        return ext in self._all_valid_exts

    # Remove invalid root entries.
    def _cleanup_root(self, filetree: mobase.IFileTree):
        valid_top = set(self._valid_map.keys()) | {"dialog.tlk", "override", "tslpatchdata"}
        ignored = self._ignored_exts
        valid_exts = set(ext for exts in self._valid_map.values() for ext in exts)

        for entry in list(filetree):
            if entry.name().lower() in valid_top:
                continue
            entry.detach()

        override_dirs = self._find_dirs_named(filetree, "override")
        if not override_dirs:
            return

        override = override_dirs[0]
        for child in list(override):
            if is_directory(child):
                child.detach()
                continue
            if child.name().lower() == "dialog.tlk":
                filetree.move(child, child.name())
                continue

            _, ext = os.path.splitext(child.name().lower())
            if ext in ignored or ext not in valid_exts:
                child.detach()

    # Check whether the mod data layout is valid.
    def dataLooksValid(self, filetree: mobase.IFileTree) -> mobase.ModDataChecker.CheckReturn:
        tsl_dirs = self._find_dirs_named(filetree, "tslpatchdata")
        if tsl_dirs:
            return mobase.ModDataChecker.FIXABLE

        for directory in self._iter_dirs(filetree):
            if directory.name().lower() in self._restricted_dirs:
                return mobase.ModDataChecker.INVALID

        ignored = self._ignored_exts

        # Check whether a node is under a valid folder.
        def parent_has_valid_dir(node) -> bool:
            parent = node.parent()
            while parent is not None and parent != filetree:
                if parent.name().lower() in self._valid_map:
                    return True
                parent = parent.parent()
            return False

        for entry in list(filetree):
            if not is_directory(entry):
                continue
            if self._is_ignored_source_dir_name(entry.name()):
                continue
            if entry.name().lower() in self._valid_map:
                continue

            for child in list(entry):
                if self._is_valid_mod_file(child):
                    return mobase.ModDataChecker.FIXABLE

            for child in list(entry):
                if not is_directory(child):
                    continue
                if child.name().lower() in self._valid_map:
                    for grandchild in list(child):
                        if self._is_valid_mod_file(grandchild):
                            return mobase.ModDataChecker.FIXABLE

        for directory in self._iter_dirs(filetree):
            if self._is_ignored_source_dir_name(directory.name()):
                continue
            if directory.name().lower() in self._valid_map:
                continue
            if parent_has_valid_dir(directory):
                continue
            for child in list(directory):
                if self._is_valid_mod_file(child):
                    return mobase.ModDataChecker.FIXABLE

        has_top_level_game_dir = False
        has_nested_game_dir = False
        for entry in list(filetree):
            if is_directory(entry) and entry.name().lower() in self._valid_map:
                has_top_level_game_dir = True
                continue
            if is_directory(entry):
                for directory in self._iter_dirs(entry):
                    if directory.name().lower() in self._valid_map:
                        has_nested_game_dir = True
                        break
            if has_nested_game_dir:
                break

        if has_nested_game_dir and not has_top_level_game_dir:
            return mobase.ModDataChecker.FIXABLE
        if has_top_level_game_dir:
            for entry in list(filetree):
                if is_directory(entry):
                    if entry.name().lower() not in self._valid_map:
                        return mobase.ModDataChecker.FIXABLE
                    continue
                if entry.name().lower() != "dialog.tlk":
                    return mobase.ModDataChecker.FIXABLE
            return mobase.ModDataChecker.VALID

        all_valid_exts = tuple(ext for exts in self._valid_map.values() for ext in exts)
        if any(not is_directory(entry) and entry.name().lower().endswith(all_valid_exts) for entry in filetree):
            return mobase.ModDataChecker.FIXABLE

        if any(not is_directory(entry) and entry.name().lower() == "dialog.tlk" for entry in filetree):
            return mobase.ModDataChecker.VALID

        return mobase.ModDataChecker.INVALID

    # Fix the mod data layout when possible.
    def fix(self, filetree: mobase.IFileTree) -> mobase.IFileTree | None:
        tsl_dirs = self._find_dirs_named(filetree, "tslpatchdata")
        valid_dirs = []

        # Build a display path for a tree node.
        def _display_path(node: mobase.IFileTree) -> str:
            parts = [node.name()]
            parent = node.parent()
            while parent is not None and parent.parent() is not None:
                parts.append(parent.name())
                parent = parent.parent()
            parts.reverse()
            return "/".join(parts)

        # Check whether a node is under tslpatchdata.
        def _is_under_tslpatchdata(node: mobase.IFileTree) -> bool:
            parent = node.parent()
            while parent is not None:
                if parent.name().lower() == "tslpatchdata":
                    return True
                parent = parent.parent()
            return False

        # Check whether a node is under a source folder.
        def _is_ignored_source_dir(node: mobase.IFileTree) -> bool:
            parent = node.parent()
            while parent is not None:
                if self._is_ignored_source_dir_name(parent.name()):
                    return True
                parent = parent.parent()
            return self._is_ignored_source_dir_name(node.name())

        # Check for direct valid mod files.
        def _directory_has_direct_valid_mod_file(node: mobase.IFileTree) -> bool:
            return any(self._is_valid_mod_file(child) for child in list(node) if not is_directory(child))

        # Check for valid mod files below a directory.
        def _directory_contains_valid_mod_file(node: mobase.IFileTree) -> bool:
            for child in list(node):
                if is_directory(child):
                    if (
                        child.name().lower() == "tslpatchdata"
                        or _is_under_tslpatchdata(child)
                        or _is_ignored_source_dir(child)
                    ):
                        continue
                    if _directory_contains_valid_mod_file(child):
                        return True
                    continue
                if self._is_valid_mod_file(child):
                    return True
            return False

        # Check for a valid child source directory.
        def _has_qualifying_child_directory(node: mobase.IFileTree) -> bool:
            for child in list(node):
                if not is_directory(child):
                    continue
                if (
                    child.name().lower() == "tslpatchdata"
                    or _is_under_tslpatchdata(child)
                    or _is_ignored_source_dir(child)
                ):
                    continue
                if _directory_contains_valid_mod_file(child):
                    return True
            return False

        # Move valid files into Override.
        def _move_valid_files_to_override(node: mobase.IFileTree):
            for child in list(node):
                if is_directory(child):
                    if (
                        child.name().lower() == "tslpatchdata"
                        or _is_under_tslpatchdata(child)
                        or _is_ignored_source_dir(child)
                    ):
                        continue
                    _move_valid_files_to_override(child)
                    continue
                if self._is_valid_mod_file(child):
                    if child.name().lower() == "dialog.tlk":
                        existing = self._find_entry_by_relpath(filetree, child.name())
                        if existing is not None and existing is not child:
                            existing.detach()
                        filetree.move(child, child.name())
                    else:
                        destination = f"override/{child.name()}"
                        existing = self._find_entry_by_relpath(filetree, destination)
                        if existing is not None and existing is not child:
                            existing.detach()
                        filetree.move(child, destination)

        # Yield valid file moves for one loose source.
        def _iter_valid_loose_moves(node: mobase.IFileTree | None):
            if node is None:
                for file_node in loose_valid:
                    if file_node.name().lower() == "dialog.tlk":
                        yield file_node, "dialog.tlk"
                    else:
                        yield file_node, f"override/{file_node.name()}"
                return

            def _walk(current: mobase.IFileTree):
                for child in list(current):
                    if is_directory(child):
                        if (
                            child.name().lower() == "tslpatchdata"
                            or _is_under_tslpatchdata(child)
                            or _is_ignored_source_dir(child)
                        ):
                            continue
                        yield from _walk(child)
                        continue
                    if self._is_valid_mod_file(child):
                        if child.name().lower() == "dialog.tlk":
                            yield child, "dialog.tlk"
                        else:
                            yield child, f"override/{child.name()}"

            yield from _walk(node)

        for directory in self._iter_dirs(filetree):
            if directory.parent() is None:
                continue
            if (
                directory.name().lower() == "tslpatchdata"
                or _is_under_tslpatchdata(directory)
                or _is_ignored_source_dir(directory)
            ):
                continue
            if _directory_has_direct_valid_mod_file(directory) or (
                _directory_contains_valid_mod_file(directory) and not _has_qualifying_child_directory(directory)
            ):
                valid_dirs.append(directory)

        root_files = [entry for entry in list(filetree) if not is_directory(entry)]
        loose_valid = [entry for entry in root_files if self._is_valid_mod_file(entry)]
        total_choices = len(valid_dirs) + (1 if loose_valid else 0)

        top_level_game_dirs = [
            entry
            for entry in list(filetree)
            if is_directory(entry) and entry.name().lower() in self._valid_map
        ]
        extra_valid_dirs = [
            directory
            for directory in valid_dirs
            if not (directory.parent() == filetree and directory.name().lower() in self._valid_map)
        ]
        if top_level_game_dirs and not loose_valid and not tsl_dirs and not extra_valid_dirs:
            self._cleanup_root(filetree)
            return filetree

        if tsl_dirs or total_choices > 1:
            tsl_options: list[str] = []
            tsl_mapping: dict[str, mobase.IFileTree] = {}
            loose_options: list[str] = []
            loose_mapping: dict[str, mobase.IFileTree | None] = {}

            for directory in tsl_dirs:
                display_name = _display_path(directory) if directory.parent() is not None else directory.name()
                name = display_name
                suffix = 2
                while name in tsl_mapping:
                    name = f"{display_name} ({suffix})"
                    suffix += 1
                tsl_options.append(name)
                tsl_mapping[name] = directory

            if loose_valid:
                loose_options.append("(root)")
                loose_mapping["(root)"] = None

            for directory in valid_dirs:
                name = _display_path(directory)
                suffix = 2
                while name in loose_mapping:
                    name = f"{_display_path(directory)} ({suffix})"
                    suffix += 1
                loose_options.append(name)
                loose_mapping[name] = directory

            # Ask the user which source to install.
            def _choose_install_sources(
                tsl_labels: list[str], loose_labels: list[str]
            ) -> tuple[str | None, list[str], bool]:
                dialog = QDialog()
                dialog.setWindowTitle("Select Install Sources")
                layout = QVBoxLayout(dialog)
                layout.addWidget(QLabel("Choose either one TSLPatcher folder or one or more loose-file sources. If more are needed, then install the archive again, as a separate mod."))

                content_row = QHBoxLayout()
                layout.addLayout(content_row)

                tsl_panel = QWidget(dialog)
                tsl_layout = QVBoxLayout(tsl_panel)
                tsl_layout.setContentsMargins(0, 0, 0, 0)
                tsl_layout.addWidget(QLabel("TSLPatcher folders"))
                tsl_group = QButtonGroup(dialog)
                tsl_group.setExclusive(True)
                tsl_buttons: list[QRadioButton] = []
                none_button = QRadioButton("None", tsl_panel)
                tsl_group.addButton(none_button)
                tsl_layout.addWidget(none_button)
                tsl_buttons.append(none_button)
                for label in tsl_labels:
                    button = QRadioButton(label, tsl_panel)
                    tsl_group.addButton(button)
                    tsl_layout.addWidget(button)
                    tsl_buttons.append(button)
                if len(tsl_labels) == 1 and not loose_labels:
                    tsl_buttons[-1].setChecked(True)
                else:
                    none_button.setChecked(True)
                tsl_layout.addStretch(1)
                content_row.addWidget(tsl_panel, 1)

                separator = QFrame(dialog)
                separator.setFrameShape(QFrame.Shape.VLine)
                separator.setFrameShadow(QFrame.Shadow.Sunken)
                content_row.addWidget(separator)

                loose_panel = QWidget(dialog)
                loose_layout = QVBoxLayout(loose_panel)
                loose_layout.setContentsMargins(0, 0, 0, 0)
                loose_layout.addWidget(QLabel("Loose-file mod sources"))
                loose_layout.addWidget(
                    QLabel("Check sources to install, then drag to set consolidation order.")
                )
                location_list = QListWidget(loose_panel)
                location_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
                location_list.setDefaultDropAction(Qt.DropAction.MoveAction)
                location_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
                location_list.setDragEnabled(True)
                location_list.setAcceptDrops(True)
                location_list.setDropIndicatorShown(True)
                location_list.setDragDropOverwriteMode(False)
                for label in loose_labels:
                    item = QListWidgetItem(label)
                    item.setFlags(
                        item.flags()
                        | Qt.ItemFlag.ItemIsUserCheckable
                        | Qt.ItemFlag.ItemIsDragEnabled
                        | Qt.ItemFlag.ItemIsSelectable
                        | Qt.ItemFlag.ItemIsEnabled
                    )
                    item.setCheckState(Qt.CheckState.Checked)
                    location_list.addItem(item)
                if not loose_labels:
                    location_list.setEnabled(False)
                loose_layout.addWidget(location_list)

                controls = QHBoxLayout()
                select_all_button = QPushButton("Select All", loose_panel)
                clear_all_button = QPushButton("Clear All", loose_panel)
                controls.addWidget(select_all_button)
                controls.addWidget(clear_all_button)
                controls.addStretch(1)
                loose_layout.addLayout(controls)
                content_row.addWidget(loose_panel, 1)

                # Set all loose-source checks.
                def _set_loose_checks(state: Qt.CheckState):
                    for i in range(location_list.count()):
                        location_list.item(i).setCheckState(state)

                # Disable loose sources when TSLPatcher is selected.
                def _sync_from_tsl():
                    tsl_selected = any(button.isChecked() and button is not none_button for button in tsl_buttons)
                    location_list.setEnabled(not tsl_selected and bool(loose_labels))
                    select_all_button.setEnabled(not tsl_selected and bool(loose_labels))
                    clear_all_button.setEnabled(not tsl_selected and bool(loose_labels))
                    if tsl_selected:
                        _set_loose_checks(Qt.CheckState.Unchecked)

                # Disable TSLPatcher when loose sources are selected.
                def _sync_from_loose(item: QListWidgetItem):
                    if item.checkState() == Qt.CheckState.Checked:
                        for button in tsl_buttons:
                            if button.isChecked():
                                button.setChecked(False)
                        none_button.setChecked(True)
                        _sync_from_tsl()

                for button in tsl_buttons:
                    button.toggled.connect(_sync_from_tsl)

                location_list.itemChanged.connect(_sync_from_loose)
                select_all_button.clicked.connect(lambda: _set_loose_checks(Qt.CheckState.Checked))
                clear_all_button.clicked.connect(lambda: _set_loose_checks(Qt.CheckState.Unchecked))
                _sync_from_tsl()

                buttons = QDialogButtonBox(
                    QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
                )
                buttons.accepted.connect(dialog.accept)
                buttons.rejected.connect(dialog.reject)
                layout.addWidget(buttons)

                if dialog.exec() == int(QDialog.DialogCode.Accepted):
                    selected_tsl = next(
                        (
                            button.text()
                            for button in tsl_buttons
                            if button.isChecked() and button is not none_button
                        ),
                        None,
                    )
                    selected_loose = [
                        location_list.item(i).text()
                        for i in range(location_list.count())
                        if location_list.item(i).checkState() == Qt.CheckState.Checked
                    ]
                    return selected_tsl, selected_loose, True
                return None, [], False

            selected_tsl, selected_loose, ok = _choose_install_sources(tsl_options, loose_options)
            if ok and selected_tsl:
                selected = tsl_mapping[selected_tsl]
                filetree.move(selected, "tslpatchdata")
                for top in list(filetree):
                    if top.name().lower() != "tslpatchdata":
                        top.detach()
                return filetree

            if ok and selected_loose:
                chosen_nodes = [loose_mapping[label] for label in selected_loose if label in loose_mapping]
                if not self._find_dirs_named(filetree, "override"):
                    filetree.addDirectory("override")

                final_moves: dict[str, mobase.IFileTree] = {}
                for node in chosen_nodes:
                    for file_node, destination in _iter_valid_loose_moves(node):
                        final_moves[destination.lower()] = file_node

                for destination_key, file_node in final_moves.items():
                    destination = "dialog.tlk" if destination_key == "dialog.tlk" else f"override/{file_node.name()}"
                    existing = self._find_entry_by_relpath(filetree, destination)
                    if existing is not None and existing is not file_node:
                        existing.detach()
                    filetree.move(file_node, destination)

                for top in list(filetree):
                    if top.name().lower() not in {"dialog.tlk", "override"}:
                        top.detach()

                return filetree

            if ok:
                return filetree

            return None

        root_dirs = [entry for entry in list(filetree) if is_directory(entry)]
        if len(root_dirs) == 1:
            keep = root_dirs[0]

            if not self._find_dirs_named(filetree, "override"):
                filetree.addDirectory("override")

            # Move all files from a single root folder.
            def _move_files_only(node: mobase.IFileTree):
                for child in list(node):
                    if is_directory(child):
                        _move_files_only(child)
                    else:
                        if child.name().lower() == "dialog.tlk":
                            existing = self._find_entry_by_relpath(filetree, child.name())
                            if existing is not None and existing is not child:
                                existing.detach()
                            filetree.move(child, child.name())
                        else:
                            destination = f"override/{child.name()}"
                            existing = self._find_entry_by_relpath(filetree, destination)
                            if existing is not None and existing is not child:
                                existing.detach()
                            filetree.move(child, destination)

            _move_files_only(keep)
            self._cleanup_root(filetree)
            return filetree

        if loose_valid:
            if not self._find_dirs_named(filetree, "override"):
                filetree.addDirectory("override")

            for file_node in loose_valid:
                if file_node.name().lower() == "dialog.tlk":
                    continue
                destination = f"override/{file_node.name()}"
                existing = self._find_entry_by_relpath(filetree, destination)
                if existing is not None and existing is not file_node:
                    existing.detach()
                filetree.move(file_node, destination)

            self._cleanup_root(filetree)
            return filetree

        return filetree
