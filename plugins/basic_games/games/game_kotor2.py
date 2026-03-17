import logging
import os
import sys
from pathlib import Path
import winreg

_plugin_file = Path(__file__).resolve()
_plugin_dir = _plugin_file.parent
_plugin_dir_str = str(_plugin_dir)
_shared_dir = _plugin_dir / "kotor"
_shared_dir_str = str(_shared_dir)
_plugin_dir_added = False
if _plugin_dir_str not in sys.path:
    sys.path.insert(0, _plugin_dir_str)
    _plugin_dir_added = True
_shared_dir_added = False
if _shared_dir_str not in sys.path:
    sys.path.insert(0, _shared_dir_str)
    _shared_dir_added = True

import mobase
from PyQt6.QtCore import QDir, QTimer
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QCheckBox,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QTabWidget,
    QVBoxLayout,
)

from basic_games.basic_game import BasicGame
from basic_games.basic_features import (
    BasicLocalSavegames,
    BasicGameSaveGameInfo,
    BasicModDataChecker,
    GlobPatterns,
)
from basic_games.basic_features.utils import is_directory
from patcher_tab import Kotor2HKReassemblerTab as Kotor2PatcherTab
from import_probe import KOTOR2_IMPORT_PROBE
from saves_tab import Kotor2SaveGame, parse_kotor2_save_metadata
from texture_tab import Kotor2TextureTab

logger = logging.getLogger("mobase")
if _plugin_dir_added:
    logger.info(f"[KOTOR2] inserted plugin dir into sys.path: {_plugin_dir_str}")
if _shared_dir_added:
    logger.info(f"[KOTOR2] inserted shared dir into sys.path: {_shared_dir_str}")
logger.info(f"[KOTOR2] plugin file path: {_plugin_file} | plugin dir: {_plugin_dir}")
for _idx, _entry in enumerate(sys.path):
    logger.info(f"[KOTOR2] sys.path[{_idx}]: {_entry}")
logger.info(f"[KOTOR2] import probe: {KOTOR2_IMPORT_PROBE}")

# Validate and fix mod archive layouts for KOTOR II.
class Kotor2ModDataChecker(BasicModDataChecker):
    # Configure the allowed file extensions and directory rules.
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

    # Build the checker with the full allowed extension list.
    def __init__(self):
        all_exts = tuple(ext for exts in self._valid_map.values() for ext in exts)
        super().__init__(GlobPatterns(all_exts))

    # Yield every directory node under the given file tree node.
    def _iter_dirs(self, node):
        for e in list(node):
            if is_directory(e):
                yield e
                yield from self._iter_dirs(e)

    # Find every directory whose name matches the requested value.
    def _find_dirs_named(self, node, name_lower: str):
        name_lower = name_lower.lower()
        return [d for d in self._iter_dirs(node) if d.name().lower() == name_lower]

    # Check whether a file belongs under the given destination path.
    def _file_is_valid_for_path(self, file_node, path: str) -> bool:
        if is_directory(file_node):
            return False
        fname = file_node.name().lower()
        for folder, exts in self._valid_map.items():
            if folder in path.lower():
                return any(fname.endswith(ext) for ext in exts)
        return False

    # Remove invalid top-level entries and invalid override files.
    def _cleanup_root(self, filetree: mobase.IFileTree):
        valid_top = set(self._valid_map.keys()) | {"override", "tslpatchdata"}
        ignored = self._ignored_exts
        valid_exts = set(ext for exts in self._valid_map.values() for ext in exts)

        for entry in list(filetree):
            name = entry.name().lower()
            if name in valid_top:
                continue
            entry.detach()

        override_dirs = self._find_dirs_named(filetree, "override")
        if override_dirs:
            override = override_dirs[0]

            for child in list(override):
                if is_directory(child):
                    child.detach()
                    continue

                _, ext = os.path.splitext(child.name().lower())
                if ext in ignored or ext not in valid_exts:
                    child.detach()

    # Decide whether the mod data looks valid, fixable, or invalid.
    def dataLooksValid(self, filetree: mobase.IFileTree) -> mobase.ModDataChecker.CheckReturn:
        tsl_dirs = self._find_dirs_named(filetree, "tslpatchdata")
        if tsl_dirs:
            return mobase.ModDataChecker.FIXABLE

        for d in self._iter_dirs(filetree):
            if d.name().lower() in self._restricted_dirs:
                return mobase.ModDataChecker.INVALID

        ignored = self._ignored_exts

        # Detect whether a directory is already nested under a valid game folder.
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

            for child in list(entry):
                if not is_directory(child):
                    _, ext = os.path.splitext(child.name().lower())
                    if ext not in ignored:
                        return mobase.ModDataChecker.FIXABLE

            for child in list(entry):
                if not is_directory(child):
                    continue
                cname = child.name().lower()
                if cname in self._valid_map.keys():
                    for grand in list(child):
                        if not is_directory(grand):
                            return mobase.ModDataChecker.FIXABLE

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

        for folder in self._valid_map.keys():
            found = self._find_dirs_named(filetree, folder)
            if found:
                return mobase.ModDataChecker.VALID

        all_valid_exts = tuple(ext for exts in self._valid_map.values() for ext in exts)
        if any(not is_directory(e) and e.name().lower().endswith(all_valid_exts) for e in filetree):
            return mobase.ModDataChecker.FIXABLE

        if any(not is_directory(e) and e.name().lower() == "dialog.tlk" for e in filetree):
            return mobase.ModDataChecker.VALID

        return mobase.ModDataChecker.INVALID

    # Fix common archive layouts by moving files into valid locations.
    def fix(self, filetree: mobase.IFileTree) -> mobase.IFileTree:
        tsl_dirs = self._find_dirs_named(filetree, "tslpatchdata")

        if len(tsl_dirs) > 1:
            options = []
            mapping = {}

            # Build a readable path label for the folder picker.
            def _display_path(node: mobase.IFileTree) -> str:
                parts = [node.name()]
                parent = node.parent()
                while parent is not None and parent.parent() is not None:
                    parts.append(parent.name())
                    parent = parent.parent()
                parts.reverse()
                return "/".join(parts)

            for d in tsl_dirs:
                parent = d.parent()
                display_name = _display_path(d) if parent is not None else d.name()
                name = display_name
                suffix = 2
                while name in mapping:
                    name = f"{display_name} ({suffix})"
                    suffix += 1
                options.append(name)
                mapping[name] = d

            choice, ok = QInputDialog.getItem(
                None, "Select TSLPatcher Folder",
                "Multiple TSLPatcher folders found. Choose which one to keep:",
                options, 0, False
            )

            if ok and choice:
                selected = mapping[choice]
                filetree.move(selected, "tslpatchdata")
                for top in list(filetree):
                    if top.name().lower() != "tslpatchdata":
                        top.detach()
                return filetree

        if len(tsl_dirs) == 1:
            selected = tsl_dirs[0]
            filetree.move(selected, "tslpatchdata")
            for top in list(filetree):
                if top.name().lower() != "tslpatchdata":
                    top.detach()
            return filetree

        ignored = self._ignored_exts
        valid_dirs = []

        for d in self._iter_dirs(filetree):
            if d.parent() is None:
                continue
            for child in list(d):
                if not is_directory(child) and not child.name().lower().endswith(ignored):
                    valid_dirs.append(d)
                    break

        root_files = [e for e in list(filetree) if not is_directory(e)]
        loose_valid = [f for f in root_files if not f.name().lower().endswith(ignored)]
        total_choices = len(valid_dirs) + (1 if loose_valid else 0)

        if total_choices > 1:
            options: list[str] = []
            mapping: dict[str, mobase.IFileTree | None] = {}

            def _display_path(node: mobase.IFileTree) -> str:
                parts = [node.name()]
                parent = node.parent()
                while parent is not None and parent.parent() is not None:
                    parts.append(parent.name())
                    parent = parent.parent()
                parts.reverse()
                return "/".join(parts)

            # Ask the user which content folders should be kept.
            def _choose_locations(labels: list[str]) -> list[str]:
                dlg = QDialog()
                dlg.setWindowTitle("Select Mod Folders")
                layout = QVBoxLayout(dlg)
                layout.addWidget(QLabel("Multiple locations contain mod files. Choose which ones to install:"))
                checks: list[QCheckBox] = []
                for label in labels:
                    cb = QCheckBox(label)
                    cb.setChecked(True)
                    layout.addWidget(cb)
                    checks.append(cb)

                buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
                buttons.accepted.connect(dlg.accept)
                buttons.rejected.connect(dlg.reject)
                layout.addWidget(buttons)

                if dlg.exec() == int(QDialog.DialogCode.Accepted):
                    return [cb.text() for cb in checks if cb.isChecked()]
                return []

            if loose_valid:
                options.append("(root)")
                mapping["(root)"] = None

            for d in valid_dirs:
                name = _display_path(d)
                suffix = 2
                while name in mapping:
                    name = f"{_display_path(d)} ({suffix})"
                    suffix += 1
                options.append(name)
                mapping[name] = d

            selected = _choose_locations(options)

            if selected:
                chosen_nodes = [mapping[label] for label in selected if label in mapping]

                if not self._find_dirs_named(filetree, "override"):
                    filetree.addDirectory("override")

                for node in chosen_nodes:
                    if node is None:
                        for f in loose_valid:
                            filetree.move(f, f"override/{f.name()}")
                    else:
                        for child in list(node):
                            if not is_directory(child):
                                filetree.move(child, f"override/{child.name()}")

                for top in list(filetree):
                    if top.name().lower() != "override":
                        top.detach()

                return filetree

        root_dirs = [e for e in list(filetree) if is_directory(e)]

        if len(root_dirs) == 1:
            keep = root_dirs[0]

            if not self._find_dirs_named(filetree, "override"):
                filetree.addDirectory("override")

            # Move only files from nested directories into override.
            def _move_files_only(node: mobase.IFileTree):
                for child in list(node):
                    if is_directory(child):
                        _move_files_only(child)
                    else:
                        filetree.move(child, f"override/{child.name()}")

            _move_files_only(keep)
            self._cleanup_root(filetree)
            return filetree

        if loose_valid:
            if not self._find_dirs_named(filetree, "override"):
                filetree.addDirectory("override")

            for f in loose_valid:
                filetree.move(f, f"override/{f.name()}")

            self._cleanup_root(filetree)
            return filetree

        return filetree

# Implement the MO2 game plugin for KOTOR II.
class StarWarsKotor2Game(BasicGame, mobase.IPluginFileMapper):
    # Initialize plugin state and custom tabs.
    def __init__(self):
        BasicGame.__init__(self)
        mobase.IPluginFileMapper.__init__(self)
        self._texture_tab: Kotor2TextureTab | None = None
        self._patcher_tab: Kotor2PatcherTab | None = None
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

    # Register MO2 features and create required game folders.
    def init(self, organizer: mobase.IOrganizer) -> bool:
        super().init(organizer)
        self._organizer = organizer

        self._register_feature(BasicLocalSavegames(self.savesDirectory()))
        self._register_feature(BasicGameSaveGameInfo(Kotor2SaveGame, parse_kotor2_save_metadata))
        self._register_feature(Kotor2ModDataChecker())
        organizer.onUserInterfaceInitialized(self._init_custom_tabs)
        organizer.onAboutToRun(lambda app: self._log_platform_once())

        try:
            mg = self._organizer.managedGame()
            if mg and (mg == self or mg.gameName() == self.gameName()) and self.gameDirectory().exists():
                self._log_platform_once(force=True)
        except Exception:
            logger.info("[KOTOR2] Platform logging failed")

        if self._organizer.managedGame() and self._organizer.managedGame().gameName() == self.gameName():
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

    # Return the game's Data directory.
    def dataDirectory(self): return QDir(self.gameDirectory().absolutePath() + "/Data")
    # Return the game's Lips directory.
    def lipsDirectory(self): return QDir(self.gameDirectory().absolutePath() + "/Lips")
    # Return the game's Modules directory.
    def modulesDirectory(self): return QDir(self.gameDirectory().absolutePath() + "/Modules")
    # Return the game's Movies directory.
    def moviesDirectory(self): return QDir(self.gameDirectory().absolutePath() + "/Movies")
    # Return the game's Override directory.
    def overrideDirectory(self): return QDir(self.gameDirectory().absolutePath() + "/Override")
    # Return the game's StreamMusic directory.
    def streamMusicDirectory(self): return QDir(self.gameDirectory().absolutePath() + "/StreamMusic")
    # Return the game's StreamSounds directory.
    def streamSoundsDirectory(self): return QDir(self.gameDirectory().absolutePath() + "/StreamSounds")
    # Return the game's StreamVoice directory.
    def streamVoiceDirectory(self): return QDir(self.gameDirectory().absolutePath() + "/StreamVoice")
    # Return the game's TexturePacks directory.
    def texturePacksDirectory(self): return QDir(self.gameDirectory().absolutePath() + "/TexturePacks")
    # Return the profile saves directory.
    def savesDirectory(self): return QDir(self.gameDirectory().absolutePath() + "/saves")

    # Return the virtual folder mappings exposed to MO2.
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

    # Yield active mod paths in profile priority order.
    def _active_mod_paths(self):
        mods_root = Path(self._organizer.modsPath())
        modlist = self._organizer.modList().allModsByProfilePriority()

        for mod_name in modlist:
            state = self._organizer.modList().state(mod_name)
            if state & mobase.ModState.ACTIVE:
                yield mods_root / mod_name

    # Provide loose-file mappings that MO2 should inject.
    def mappings(self) -> list[mobase.Mapping]:
        mappings = []
        game_path = Path(self.gameDirectory().absolutePath())

        for mod_path in self._active_mod_paths():
            if not mod_path.exists():
                continue
            for child in mod_path.iterdir():
                if child.name.lower() != "dialog.tlk":
                    continue
                mappings.append(
                    mobase.Mapping(
                        source=str(child),
                        destination=str(game_path / "dialog.tlk"),
                        is_directory=False,
                        create_target=False,
                    )
                )

        return mappings

    # Log platform details once per session.
    def _log_platform_once(self, force: bool = False) -> bool:
        if self._platform_logged and not force:
            return True
        try:
            gd = self.gameDirectory()
            steam_root = self._detect_steam_root(Path(gd.absolutePath()))
            self._warn_if_workshop_present(steam_root)
            logger.info(
                "[KOTOR2] Steam detected:%s path:%s steam_root:%s"
                % (self.is_steam(), gd.absolutePath(), steam_root)
            )
            self._platform_logged = True
        except Exception as e:
            logger.info("[KOTOR2] Platform logging failed: %s", e)
        return True

    # Detect the Steam root for the managed game install.
    def _detect_steam_root(self, game_path: Path) -> str:
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

    # Warn if Steam Workshop content is present for the game.
    def _warn_if_workshop_present(self, steam_root: str):
        if steam_root.lower() == "unknown":
            return
        workshop_path = Path(steam_root) / "steamapps" / "workshop" / "content" / "208580"
        try:
            if workshop_path.exists() and any(workshop_path.iterdir()):
                logger.warning("[KOTOR2] Steam Workshop content detected")
                try:
                    QTimer.singleShot(
                        2000,
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

    # Insert the custom saves, textures, and patcher tabs into MO2.
    def _init_custom_tabs(self, main_window: QMainWindow):
        if self._organizer.managedGame() != self:
            return

        tab_widget: QTabWidget | None = main_window.findChild(QTabWidget, "tabWidget")
        if not tab_widget:
            return

        data_index = None
        saves_index = None
        textures_index = None
        for i in range(tab_widget.count()):
            text = tab_widget.tabText(i).lower()
            if text == "data":
                data_index = i
            if text == "saves":
                saves_index = i
            if text == "textures":
                textures_index = i

        insert_index = tab_widget.count()
        if data_index is not None:
            insert_index = data_index + 1
        elif saves_index is not None:
            insert_index = saves_index

        if textures_index is None:
            self._texture_tab = Kotor2TextureTab(main_window, self._organizer, self)
            tab_widget.insertTab(insert_index, self._texture_tab, "Textures")
            textures_index = insert_index

        patcher_index = None
        for i in range(tab_widget.count()):
            if tab_widget.tabText(i).lower() == "patcher":
                patcher_index = i
                break
        if patcher_index is None:
            self._patcher_tab = Kotor2PatcherTab(main_window, self._organizer, self)
            tab_widget.insertTab(textures_index + 1, self._patcher_tab, "Patcher")

    # Return the INI files associated with the game.
    def iniFiles(self):
        return [self.gameDirectory().absoluteFilePath("swkotor2.ini")]

    # Return the main executable registered for launch.
    def executables(self):
        self._log_platform_once()
        exe_path = self.gameDirectory().absoluteFilePath(self.binaryName())
        logger.info(f"[KOTOR2 Plugin] registering executables: {exe_path}")
        return [
            mobase.ExecutableInfo("KOTOR2", exe_path),
        ]

    # Enumerate save directories visible to MO2.
    def listSaves(self, folder: QDir) -> list[mobase.ISaveGame]:
        saves = []
        root = Path(folder.absolutePath())
        for sub in root.iterdir():
            if sub.is_dir() and any(f.suffix == ".sav" for f in sub.iterdir()):
                saves.append(Kotor2SaveGame(sub))
        return saves


# Construct the MO2 plugin instance.
def createPlugin() -> mobase.IPluginGame:
    return StarWarsKotor2Game()
