import configparser
import html
import json
import logging
import os
import re
import subprocess
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

import mobase
from PyQt6.QtCore import QObject, QPoint, QProcess, Qt, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from hash_utils import file_hash, xxh3_bytes
from sync_installer import SyncInstallResult, install_kson_build
from ui_theme import configure_download_button, configure_refresh_button, configure_tree_widget, refresh_mo2, set_header_resize_mode

logger = logging.getLogger("mobase")


_FIXED_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


# Convert a KSON filename timestamp to display text.
def _kson_version_text_from_name(name: str) -> str:
    match = re.search(r"(\d{8})[_-]?(\d{6})", Path(name).stem)
    if not match:
        return "unknown"
    try:
        parsed = datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S")
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return "unknown"


# Fetch KSON data off the UI thread.
class _FetchWorker(QObject):
    finished = pyqtSignal(object)
    failed = pyqtSignal(str, object)

    # Store fetch inputs for the worker.
    def __init__(self, cache_path: Path, build_key: str, game_name: str, repo: str, timeout: int):
        super().__init__()
        self._cache_path = cache_path
        self._build_key = build_key
        self._game_name = game_name
        self._repo = repo
        self._timeout = timeout

    # Fetch, cache, and select KSON data.
    def run(self):
        errors: list[str] = []
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            remote_kson, _source_url, remote_name = self._download_kson()
            remote_path = self._cache_path.parent / remote_name
            remote_path.write_text(json.dumps(remote_kson, indent=2), encoding="utf-8")
        except Exception as exc:
            errors.append(str(exc))

        try:
            selected_path, kson = self._latest_local_kson()
            kson["_selected_kson_name"] = selected_path.name
            self._cache_path.write_text(json.dumps(kson, indent=2), encoding="utf-8")
            mod_count = len([mod for mod in kson.get("mods", []) if Kotor2SyncTab._kson_mod_name(mod)])
            details = [
                f"Loaded {mod_count} mods for {kson.get('game') or self._build_key}.",
                f"KSON version: {_kson_version_text_from_name(selected_path.name)}",
                f"Selected KSON: {selected_path}",
                f"Source URL: {kson.get('_source_url') or '(local file)'}",
                f"Cache file: {self._cache_path}",
            ]
            if errors:
                details.extend(["", "Fetch warnings:", *errors])
            self.finished.emit(
                {
                    "selected_path": str(selected_path),
                    "kson": kson,
                    "mod_count": mod_count,
                    "details": "\n".join(details),
                    "warnings": errors,
                }
            )
        except Exception as exc:
            self.failed.emit(str(exc), errors)

    # Download the newest remote KSON.
    def _download_kson(self) -> tuple[dict, str, str]:
        errors: list[str] = []
        for branch in ("main", "master"):
            try:
                source_url, file_name = self._latest_kson_raw_url(branch)
                text = self._download_text(source_url)
                kson = json.loads(text)
                mods = kson.get("mods", [])
                if isinstance(mods, list) and any(Kotor2SyncTab._kson_mod_name(mod) for mod in mods):
                    kson["_source_url"] = source_url
                    kson["_fetched_at"] = datetime.now(timezone.utc).isoformat()
                    return kson, source_url, file_name
                errors.append(f"{source_url} -> no mod entries found")
            except Exception as exc:
                errors.append(f"{self._repo}/{branch} -> {exc}")
        raise RuntimeError("Unable to fetch a usable KSON manifest.\n\n" + "\n".join(errors))

    # Return the newest raw KSON URL.
    def _latest_kson_raw_url(self, branch: str) -> tuple[str, str]:
        tree_url = f"https://api.github.com/repos/{self._repo}/git/trees/{branch}?recursive=1"
        payload = json.loads(self._download_text(tree_url))
        files = [
            str(item.get("path", ""))
            for item in payload.get("tree", [])
            if item.get("type") == "blob" and self._is_game_kson_path(str(item.get("path", "")))
        ]
        if not files:
            raise RuntimeError(f"No {self._build_key} .kson files found on {branch}.")
        latest = max(files, key=self._kson_sort_key)
        return f"https://raw.githubusercontent.com/{self._repo}/{branch}/{quote(latest)}", Path(latest).name

    # Return the newest local KSON.
    def _latest_local_kson(self) -> tuple[Path, dict]:
        candidates = []
        for path in self._cache_path.parent.glob("*.kson"):
            if path.name == self._cache_path.name:
                continue
            if self._is_game_kson_path(path.name):
                candidates.append(path)
        if not candidates and self._cache_path.exists():
            candidates.append(self._cache_path)
        if not candidates:
            raise RuntimeError(f"No local {self._build_key} KSON files are available.")

        errors: list[str] = []
        for path in sorted(candidates, key=lambda item: self._kson_sort_key(item.name), reverse=True):
            try:
                kson = json.loads(path.read_text(encoding="utf-8"))
                mods = kson.get("mods", [])
                if isinstance(mods, list) and any(Kotor2SyncTab._kson_mod_name(mod) for mod in mods):
                    return path, kson
                errors.append(f"{path.name}: no mod entries found")
            except Exception as exc:
                errors.append(f"{path.name}: {exc}")
        raise RuntimeError("No usable local KSON files are available.\n\n" + "\n".join(errors))

    # Download one text payload.
    def _download_text(self, url: str) -> str:
        request = Request(url, headers={"User-Agent": "KOTORganizer-MO2-SyncTab/1.0"})
        try:
            with urlopen(request, timeout=self._timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, errors="replace")
        except URLError as exc:
            raise RuntimeError(str(exc)) from exc

    # Check if a path is for this game.
    def _is_game_kson_path(self, path: str) -> bool:
        name = Path(path).name.lower()
        if not name.endswith(".kson"):
            return False
        if self._build_key == "kotor2":
            return name.startswith("kotor2")
        return name.startswith("kotor") and not name.startswith("kotor2")

    # Sort KSON paths by timestamp.
    @staticmethod
    def _kson_sort_key(path: str) -> tuple[str, str]:
        name = Path(path).stem.lower()
        match = re.search(r"(\d{8}[_-]?\d{6})", name)
        timestamp = match.group(1).replace("_", "").replace("-", "") if match else ""
        return timestamp, name

# Sort rows by numeric values when present.
class _NumericTreeWidgetItem(QTreeWidgetItem):
    # Compare rows using stored numeric values.
    def __lt__(self, other):
        column = self.treeWidget().sortColumn() if self.treeWidget() else 0
        left = self.data(column, Qt.ItemDataRole.UserRole + 10)
        right = other.data(column, Qt.ItemDataRole.UserRole + 10)
        if isinstance(left, int) and isinstance(right, int):
            return left < right
        return super().__lt__(other)


# Run sync work off the UI thread.
class _SyncWorker(QObject):
    progress = pyqtSignal(int, int, str, str)
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    # Store sync paths for the worker.
    def __init__(self, kson_path: Path, downloads_path: Path, mods_path: Path, profile_path: Path):
        super().__init__()
        self._kson_path = kson_path
        self._downloads_path = downloads_path
        self._mods_path = mods_path
        self._profile_path = profile_path

    # Run the sync install.
    def run(self):
        try:
            result = install_kson_build(
                self._kson_path,
                self._downloads_path,
                self._mods_path,
                self._profile_path,
                progress=self.progress.emit,
            )
            self.finished.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


# Render the Sync tab inside MO2.
class Kotor2SyncTab(QWidget):
    _FETCH_TIMEOUT_SECONDS = 20
    _KSON_REPO = "J0-o/kson_modlist"

    # Build the sync tab UI scaffold.
    def __init__(self, parent: QWidget | None, organizer: mobase.IOrganizer, game):
        super().__init__(parent)
        self._organizer = organizer
        self._game = game
        self._download_queue: list[tuple[QTreeWidgetItem, dict]] = []
        self._download_process: QProcess | None = None
        self._browser_process: subprocess.Popen | None = None
        self._browser_waiting: tuple[QTreeWidgetItem, dict, Path, str, float, str, set[str]] | None = None
        self._fetch_thread: QThread | None = None
        self._fetch_worker: _FetchWorker | None = None
        self._sync_thread: QThread | None = None
        self._sync_worker: _SyncWorker | None = None
        self._sync_progress_lines: list[str] = []
        self._validated_for_sync = False

        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        self._summary_label = QLabel("0 mods")
        self._kson_version_label = QLabel("KSON: unknown")
        self._refresh_btn = QPushButton("Refresh")
        configure_refresh_button(self._refresh_btn)
        self._refresh_btn.clicked.connect(self._refresh_fetch_validate)
        self._download_btn = QPushButton("Download Missing")
        configure_download_button(self._download_btn)
        self._download_btn.clicked.connect(self._download_missing_archives)
        self._sync_btn = QPushButton("Sync")
        self._sync_btn.setEnabled(False)
        self._sync_btn.clicked.connect(self._sync_validated_build)
        header.addWidget(self._refresh_btn)
        header.addWidget(self._download_btn)
        header.addWidget(self._summary_label)
        header.addWidget(self._kson_version_label)
        header.addStretch()
        header.addWidget(self._sync_btn)
        layout.addLayout(header)

        self._tree = QTreeWidget(self)
        self._tree.setColumnCount(10)
        self._tree.setHeaderLabels(
            ["State", "Priority", "Mod", "Enabled", "Archive", "Version", "Release Date", "Source", "Files", "Actions"]
        )
        configure_tree_widget(
            self._tree,
            selection_mode=QAbstractItemView.SelectionMode.SingleSelection,
            uniform_row_heights=True,
        )
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._show_context_menu)
        header_view = self._tree.header()
        set_header_resize_mode(header_view, QHeaderView.ResizeMode.Interactive, 10)
        self._tree.setColumnWidth(0, 70)
        self._tree.setColumnWidth(1, 70)
        self._tree.setColumnWidth(2, 300)
        self._tree.setColumnWidth(3, 80)
        self._tree.setColumnWidth(4, 260)
        self._tree.setColumnWidth(5, 100)
        self._tree.setColumnWidth(6, 150)
        self._tree.setColumnWidth(7, 120)
        self._tree.setColumnWidth(8, 70)
        self._tree.setColumnWidth(9, 70)
        layout.addWidget(self._tree, 3)

        self._details = QPlainTextEdit(self)
        self._details.setReadOnly(True)
        self._details.setVisible(False)

        self.refresh()

    # Run the full sync-tab refresh flow from the single Refresh button.
    def _refresh_fetch_validate(self):
        if self._fetch_thread is not None:
            return
        self.refresh()
        self._start_fetch_latest_manifest()

    # Rebuild the sync list from the latest cached KSON.
    def refresh(self):
        self._tree.clear()
        self._validated_for_sync = False
        self._sync_btn.setEnabled(False)
        kson = self._read_cached_kson()
        version_text = self._cached_kson_version_text()
        self._kson_version_label.setText(f"KSON: {version_text}")
        if kson is None:
            row = QTreeWidgetItem(["Info", "", "No cached KSON", "", "", "", "", "Press Fetch", "", ""])
            row.setData(
                0,
                Qt.ItemDataRole.UserRole,
                "\n".join(
                    [
                        "No cached sync KSON exists yet.",
                        "",
                        f"Game: {self._game.gameName()}",
                        f"Cache path: {self._cache_path()}",
                        f"KSON version: {version_text}",
                        "",
                        "Press Fetch to download the latest KSON build for this game.",
                    ]
                ),
            )
            self._tree.addTopLevelItem(row)
            self._tree.setCurrentItem(row)
            self._summary_label.setText("0 mods")
            self._update_details()
            return

        mods = kson.get("mods", [])
        build_name = str(kson.get("game") or self._build_key())
        source_url = str(kson.get("_source_url") or "")
        fetched_at = str(kson.get("_fetched_at") or "")
        patch_order_count = self._patch_order_count(kson.get("tslpatch_order"))
        for mod in mods:
            mod_name = self._kson_mod_name(mod)
            if not mod_name:
                continue
            enabled_label = "Enabled" if self._kson_mod_enabled(mod) else "Disabled"
            priority = mod.get("priority") if isinstance(mod, dict) else None
            mod_url = str(mod.get("url") or "").strip() if isinstance(mod, dict) else ""
            archive_files = mod.get("archive_files", []) if isinstance(mod, dict) else []
            actions = mod.get("actions", []) if isinstance(mod, dict) else []
            row = _NumericTreeWidgetItem(
                [
                    "Ready",
                    str(priority if priority is not None else ""),
                    mod_name,
                    enabled_label,
                    str(mod.get("archive_name") or "").strip() if isinstance(mod, dict) else "",
                    str(mod.get("version") or "").strip() if isinstance(mod, dict) else "",
                    str(mod.get("release_date") or "").strip() if isinstance(mod, dict) else "",
                    self._source_label(mod_url),
                    str(len(archive_files)) if isinstance(archive_files, list) else "",
                    str(len(actions)) if isinstance(actions, list) else "",
                ]
            )
            row.setToolTip(7, mod_url)
            row.setData(0, Qt.ItemDataRole.UserRole + 1, mod)
            row.setData(1, Qt.ItemDataRole.UserRole + 10, int(priority) if str(priority).lstrip("-").isdigit() else -1)
            row.setData(8, Qt.ItemDataRole.UserRole + 10, len(archive_files) if isinstance(archive_files, list) else -1)
            row.setData(9, Qt.ItemDataRole.UserRole + 10, len(actions) if isinstance(actions, list) else -1)
            row.setData(
                0,
                Qt.ItemDataRole.UserRole,
                "\n".join(
                    [
                        f"Mod: {mod_name}",
                        f"Build: {build_name}",
                        f"Enabled: {enabled_label}",
                        f"Fetched: {fetched_at or '(unknown)'}",
                        f"KSON version: {version_text}",
                        f"Source URL: {source_url or '(unknown)'}",
                        f"TSLPatch order entries: {patch_order_count}",
                        f"Cache file: {self._cache_path()}",
                    ]
                ),
            )
            self._tree.addTopLevelItem(row)

        if self._tree.topLevelItemCount():
            self._tree.setCurrentItem(self._tree.topLevelItem(0))
        self._summary_label.setText(f"{self._tree.topLevelItemCount()} mods")
        self._update_details()

    # Download missing archives one at a time from the cached KSON.
    def _download_missing_archives(self):
        if self._download_process is not None or self._browser_waiting is not None:
            return
        self._download_queue = []
        for index in range(self._tree.topLevelItemCount()):
            row = self._tree.topLevelItem(index)
            mod = row.data(0, Qt.ItemDataRole.UserRole + 1)
            if not isinstance(mod, dict):
                continue
            archive_name = self._expected_archive_name(mod)
            url = str(mod.get("url") or "").strip()
            if archive_name and self._archive_path(archive_name) is None:
                self._download_queue.append((row, mod))
                continue
            if not archive_name and url:
                self._download_queue.append((row, mod))

        if not self._download_queue:
            self._details.setPlainText("No missing archives to download.")
            logger.info("[KOTOR2 Sync] No missing archives to download.")
            return

        self._download_btn.setEnabled(False)
        self._details.setPlainText(f"Downloading {len(self._download_queue)} missing archive(s) one at a time.")
        logger.info(f"[KOTOR2 Sync] Downloading {len(self._download_queue)} missing archive(s).")
        self._process_next_download()

    # Start the next queued missing archive download.
    def _process_next_download(self):
        if not self._download_queue:
            self._download_btn.setEnabled(True)
            self._summary_label.setText("Download queue complete")
            self._details.appendPlainText("\nDownload queue complete.")
            logger.info("[KOTOR2 Sync] Download queue complete.")
            return

        row, mod = self._download_queue.pop(0)
        mod_name = self._kson_mod_name(mod)
        archive_name = self._expected_archive_name(mod)
        if archive_name:
            existing_archive_path = self._archive_path(archive_name)
            if existing_archive_path is not None:
                self._mark_downloaded(row, mod, existing_archive_path, "Archive already exists in downloads.")
                QTimer.singleShot(0, self._process_next_download)
                return
        url = str(mod.get("url") or "").strip()
        host = urlparse(url).netloc.lower()
        row.setText(0, "Downloading")
        self._summary_label.setText(f"Downloading {mod_name}")
        self._details.setPlainText(
            "\n".join(
                [
                    f"Mod: {mod_name}",
                    f"Archive: {archive_name}",
                    f"URL: {url or '(none)'}",
                    "",
                    "Downloading one missing archive.",
                ]
            )
        )
        QApplication.processEvents()

        if not url:
            self._mark_download_failed(row, mod, "No URL in KSON.")
            QTimer.singleShot(0, self._process_next_download)
            return
        if "deadlystream.com" in host and self._start_deadlystream_download(row, mod, url, archive_name):
            return
        if "nexusmods.com" in host and self._start_nexus_download(row, mod, url):
            return
        self._start_browser_download(row, mod, url, "Browser fallback")

    # Download one DeadlyStream file with DeadlyScraper.exe.
    def _start_deadlystream_download(self, row: QTreeWidgetItem, mod: dict, url: str, archive_name: str) -> bool:
        scraper = Path(__file__).resolve().parent / "DeadlyScraper.exe"
        if not scraper.exists():
            self._append_download_detail("DeadlyScraper.exe is missing; using browser fallback.", warning=True)
            return False
        process = QProcess(self)
        self._download_process = process
        process.finished.connect(
            lambda _code, _status, row=row, mod=mod, process=process:
            self._finish_deadlystream_download(row, mod, process)
        )
        args = [url, "--download", str(self._downloads_path())]
        if archive_name and not self._is_tslrcm_expected_archive_name(archive_name):
            args.extend(["--select", archive_name])
        process.start(str(scraper), args)
        return True

    # Finish one DeadlyScraper download and fall back if the expected archive is still missing.
    def _finish_deadlystream_download(self, row: QTreeWidgetItem, mod: dict, process: QProcess):
        stdout = bytes(process.readAllStandardOutput()).decode("utf-8", errors="replace").strip()
        stderr = bytes(process.readAllStandardError()).decode("utf-8", errors="replace").strip()
        self._download_process = None
        archive_name = self._expected_archive_name(mod)
        archive_path = self._archive_path(archive_name)
        if archive_path is not None:
            self._mark_downloaded(row, mod, archive_path, "Downloaded with DeadlyScraper.")
            QTimer.singleShot(0, self._process_next_download)
            return
        downloaded_path = self._newest_download_for_url(str(mod.get("url") or ""))
        if downloaded_path is not None and archive_name:
            renamed_path = self._rename_download_to_expected(downloaded_path, archive_name)
            if renamed_path is not None:
                self._mark_downloaded(row, mod, renamed_path, "Downloaded with DeadlyScraper.")
                QTimer.singleShot(0, self._process_next_download)
                return
        self._append_download_detail(
            "\n".join(
                [
                    "DeadlyScraper did not produce the expected archive; using browser fallback.",
                    stdout,
                    stderr,
                ]
            ).strip()
            ,
            warning=True,
        )
        QTimer.singleShot(
            0,
            lambda row=row, mod=mod: self._start_browser_download(
                row,
                mod,
                str(mod.get("url") or ""),
                "DeadlyScraper fallback",
            ),
        )

    # Download Nexus files through MO2's nxm handler, with DownloadPopUp browser fallback.
    def _start_nexus_download(self, row: QTreeWidgetItem, mod: dict, url: str) -> bool:
        if self._start_nxm_download(row, mod, url):
            return True
        popup_url = self._nexus_download_popup_url(mod, url)
        if popup_url:
            self._start_browser_download(row, mod, popup_url, "Nexus DownloadPopUp fallback")
            return True
        self._append_download_detail(
            "Nexus file_id is missing from the KSON. Rebuild the KSON so Nexus archive.meta modID/fileID fields are included.",
            warning=True,
        )
        return False

    # Hand Nexus downloads to MO2 through the registered nxm:// protocol when file_id is known.
    def _start_nxm_download(self, row: QTreeWidgetItem, mod: dict, url: str) -> bool:
        mod_id = str(mod.get("mod_id") or mod.get("modID") or "").strip() or self._nexus_mod_id(url)
        file_id = str(mod.get("file_id") or mod.get("fileID") or "").strip()
        if not mod_id or not file_id or not file_id.isdigit() or int(file_id) <= 0:
            self._append_download_detail(
                "Nexus KSON entry has no usable file_id from archive.meta; cannot create nxm:// manager link.",
                warning=True,
            )
            return False

        nxm_url = f"nxm://{self._nexus_game_name()}/mods/{mod_id}/files/{file_id}"
        QDesktopServices.openUrl(QUrl(nxm_url))
        self._mark_download_pending(row, mod, f"Opened MO2/Nexus manager link: {nxm_url}")
        existing_names = {path.name for path in self._downloads_path().iterdir() if path.is_file()}
        self._browser_waiting = (
            row,
            mod,
            self._downloads_path() / html.unescape(self._expected_archive_name(mod)),
            "MO2 nxm download",
            time.monotonic() + 900,
            url,
            existing_names,
        )
        QTimer.singleShot(2000, self._poll_browser_download)
        return True

    # Build Nexus DownloadPopUp URL for browser-based fallback.
    def _nexus_download_popup_url(self, mod: dict, url: str) -> str:
        file_id = str(mod.get("file_id") or mod.get("fileID") or "").strip()
        if not file_id or not file_id.isdigit() or int(file_id) <= 0:
            return ""
        return (
            "https://www.nexusmods.com/Core/Libs/Common/Widgets/DownloadPopUp"
            f"?id={file_id}&game_id={self._nexus_game_id()}&nmm=1"
        )

    # Launch a sandboxed Edge profile for manual/fallback downloads and poll for the expected archive.
    def _start_browser_download(self, row: QTreeWidgetItem, mod: dict, url: str, reason: str):
        archive_name = self._expected_archive_name(mod)
        downloads_path = self._downloads_path()
        edge = self._edge_path()
        if not edge:
            self._mark_download_pending(row, mod, f"{reason}: Edge not found; opened default browser.")
            QDesktopServices.openUrl(QUrl(url))
            QTimer.singleShot(0, self._process_next_download)
            return

        profile_dir = Path(self._organizer.profilePath()) / "edge_profile"
        self._write_edge_preferences(profile_dir, downloads_path)
        try:
            self._browser_process = subprocess.Popen(
                [
                    str(edge),
                    f"--user-data-dir={profile_dir}",
                    "--no-first-run",
                    "--disable-sync",
                    "--new-window",
                    url,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            self._mark_download_pending(row, mod, f"{reason}: Edge launch failed ({exc}); opened default browser.")
            QDesktopServices.openUrl(QUrl(url))
            QTimer.singleShot(0, self._process_next_download)
            return

        existing_names = {path.name for path in downloads_path.iterdir() if path.is_file()}
        self._browser_waiting = (
            row,
            mod,
            downloads_path / html.unescape(archive_name),
            reason,
            time.monotonic() + 900,
            url,
            existing_names,
        )
        self._append_download_detail(f"{reason}: opened sandboxed Edge profile. Waiting for archive download.")
        QTimer.singleShot(2000, self._poll_browser_download)

    # Poll browser downloads and close the sandboxed Edge window after the expected file appears.
    def _poll_browser_download(self):
        if self._browser_waiting is None:
            return
        row, mod, expected_path, reason, deadline, url, existing_names = self._browser_waiting
        detected_path = self._detect_browser_download(expected_path, existing_names)
        if detected_path is not None:
            self._browser_waiting = None
            self._mark_downloaded(row, mod, detected_path, f"{reason}: browser download detected.")
            QTimer.singleShot(2500, self._close_browser_process)
            QTimer.singleShot(2600, self._process_next_download)
            return
        if self._browser_process is not None and self._browser_process.poll() is not None:
            self._browser_waiting = None
            self._close_browser_process()
            self._set_validation_row(
                row,
                "Skipped",
                self._kson_mod_name(mod),
                self._expected_archive_name(mod),
                str(mod.get("archive_xxh3") or "").strip().lower(),
                None,
                "",
                f"{reason}: browser window was closed before the download completed.",
            )
            QTimer.singleShot(0, self._process_next_download)
            return
        if time.monotonic() >= deadline:
            self._browser_waiting = None
            self._close_browser_process()
            self._mark_download_pending(row, mod, f"{reason}: sandboxed Edge did not finish within 15 minutes; opened default browser.")
            QDesktopServices.openUrl(QUrl(url))
            QTimer.singleShot(0, self._process_next_download)
            return
        QTimer.singleShot(2000, self._poll_browser_download)

    # Detect a completed browser download path.
    def _detect_browser_download(self, expected_path: Path, existing_names: set[str]) -> Path | None:
        if (
            expected_path.name
            and expected_path.is_file()
            and not self._is_incomplete_download_name(expected_path.name)
            and not (expected_path.parent / f"{expected_path.name}.crdownload").exists()
        ):
            return expected_path

        new_files = [
            path
            for path in expected_path.parent.iterdir()
            if path.is_file()
            and path.name not in existing_names
            and not self._is_incomplete_download_name(path.name)
        ]
        if len(new_files) != 1:
            return None

        return new_files[0]

    @staticmethod
    def _is_incomplete_download_name(name: str) -> bool:
        lower_name = name.casefold()
        return (
            lower_name.endswith(".crdownload")
            or lower_name.endswith(".tmp")
            or lower_name.endswith(".meta")
            or lower_name.endswith(".part")
            or lower_name.endswith(".partial")
            or lower_name.endswith(".download")
            or lower_name.endswith(".opdownload")
            or lower_name.endswith(".unfinished")
        )

    # Close only the Edge process started for this sandboxed profile when possible.
    def _close_browser_process(self):
        process = self._browser_process
        self._browser_process = None
        if process is not None and process.poll() is None:
            process.terminate()

    # Mark one archive as downloaded and validate its hash.
    def _mark_downloaded(self, row: QTreeWidgetItem, mod: dict, archive_path: Path, result: str):
        self._capture_downloaded_archive_metadata(mod, archive_path)
        row.setText(4, self._expected_archive_name(mod))
        archive_name = self._expected_archive_name(mod)
        archive_path, wrap_result = self._wrap_loose_download(archive_path, archive_name)
        if wrap_result:
            result = f"{result}\n{wrap_result}"
        self._write_archive_meta(mod, archive_path)
        validation_result = self._validate_archive_row_from_mod(row, mod)
        details = str(row.data(0, Qt.ItemDataRole.UserRole) or "")
        if details:
            details = details.replace(f"Result: {row.text(0)}", f"Result: {result}", 1)
            for column in range(row.columnCount()):
                row.setData(column, Qt.ItemDataRole.UserRole, details)
        if self._tree.currentItem() is row:
            self._details.setPlainText(str(row.data(0, Qt.ItemDataRole.UserRole) or ""))
        if validation_result == "ok":
            row.setText(0, "Hash OK")

    # Mark one download request as handed off to an external downloader/browser.
    def _mark_download_pending(self, row: QTreeWidgetItem, mod: dict, result: str):
        self._set_validation_row(
            row,
            "Pending",
            self._kson_mod_name(mod),
            self._expected_archive_name(mod),
            str(mod.get("archive_xxh3") or "").strip().lower(),
            None,
            "",
            result,
        )

    # Mark one download as failed and keep the queue moving.
    def _mark_download_failed(self, row: QTreeWidgetItem, mod: dict, result: str):
        self._set_validation_row(
            row,
            "Download Fail",
            self._kson_mod_name(mod),
            self._expected_archive_name(mod),
            str(mod.get("archive_xxh3") or "").strip().lower(),
            None,
            "",
            result,
        )

    # Append a message to the details box without replacing current context.
    def _append_download_detail(self, text: str, warning: bool = False):
        if text:
            self._details.appendPlainText(f"\n{text}")
            if warning:
                logger.warning(f"[KOTOR2 Sync] {text}")

    # Start fetching the latest KSON in a worker thread.
    def _start_fetch_latest_manifest(self):
        thread = QThread(self)
        worker = _FetchWorker(
            self._cache_path(),
            self._build_key(),
            self._game.gameName(),
            self._KSON_REPO,
            self._FETCH_TIMEOUT_SECONDS,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._finish_fetch_latest_manifest)
        worker.failed.connect(self._fail_fetch_latest_manifest)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._clear_fetch_worker)
        self._fetch_thread = thread
        self._fetch_worker = worker
        self._refresh_btn.setEnabled(False)
        self._download_btn.setEnabled(False)
        self._sync_btn.setEnabled(False)
        self._summary_label.setText("Fetching KSON...")
        self._details.setPlainText("Fetching latest KSON manifest...")
        logger.info("[KOTOR2 Sync] Starting KSON fetch.")
        thread.start()

    # Finish a successful KSON fetch.
    def _finish_fetch_latest_manifest(self, result: dict):
        self.refresh()
        details = str(result.get("details") or "")
        current = self._tree.currentItem()
        if current is not None:
            current.setData(0, Qt.ItemDataRole.UserRole, details)
        self._details.setPlainText(details)
        logger.info(f"[KOTOR2 Sync] Loaded {result.get('mod_count')} mods from {result.get('selected_path')}.")
        for warning in result.get("warnings", []):
            logger.warning(f"[KOTOR2 Sync] Fetch warning: {warning}")
        self._validate_archives()

    # Show a failed KSON fetch.
    def _fail_fetch_latest_manifest(self, message: str, errors: list[str]):
        logger.warning(f"[KOTOR2 Sync] Failed to load KSON: {message}")
        self._tree.clear()
        row = QTreeWidgetItem(["Error", "", "Fetch failed", "", "", "", "", "See details", "", ""])
        row.setData(
            0,
            Qt.ItemDataRole.UserRole,
            "\n".join(
                [
                    f"Failed to fetch the latest KSON for {self._game.gameName()}.",
                    "",
                    message,
                    *(["", "Fetch warnings:", *errors] if errors else []),
                ]
            ),
        )
        self._tree.addTopLevelItem(row)
        self._tree.setCurrentItem(row)
        self._summary_label.setText("0 mods")
        self._update_details()

    # Clear fetch worker references.
    def _clear_fetch_worker(self):
        self._fetch_thread = None
        self._fetch_worker = None
        self._refresh_btn.setEnabled(True)
        self._download_btn.setEnabled(True)

    # Show details for the current row.
    def _update_details(self):
        item = self._tree.currentItem()
        self._details.setPlainText(str(item.data(0, Qt.ItemDataRole.UserRole) or "") if item else "")

    # Show the row context menu.
    def _show_context_menu(self, pos: QPoint):
        row = self._tree.itemAt(pos)
        if row is None:
            return
        mod = row.data(0, Qt.ItemDataRole.UserRole + 1)
        if not isinstance(mod, dict):
            return

        archive_name = str(mod.get("archive_name") or "").strip()
        archive_path = self._archive_path(archive_name) if archive_name else None
        url = str(mod.get("url") or "").strip()

        menu = QMenu(self)
        download_action = menu.addAction("Download")
        webpage_action = menu.addAction("View Web Page")
        hash_action = menu.addAction("Hash Check")
        explorer_action = menu.addAction("Open in Explorer")

        if self._download_process is not None or self._browser_waiting is not None:
            download_action.setEnabled(False)
        if not url:
            webpage_action.setEnabled(False)
        if not archive_name:
            download_action.setEnabled(False)
            hash_action.setEnabled(False)
        if archive_path is None:
            explorer_action.setEnabled(False)

        chosen = menu.exec(self._tree.viewport().mapToGlobal(pos))
        if chosen is download_action:
            self._download_selected_row(row, mod)
        elif chosen is webpage_action and url:
            QDesktopServices.openUrl(QUrl(url))
        elif chosen is hash_action:
            self._validate_archive_row(row)
        elif chosen is explorer_action and archive_path is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(archive_path.parent)))

    # Download the archive for one selected row.
    def _download_selected_row(self, row: QTreeWidgetItem, mod: dict):
        if self._download_process is not None or self._browser_waiting is not None:
            return
        self._download_queue = [(row, mod)]
        self._download_btn.setEnabled(False)
        self._process_next_download()

    # Validate one archive row.
    def _validate_archive_row(self, row: QTreeWidgetItem):
        mod = row.data(0, Qt.ItemDataRole.UserRole + 1)
        if not isinstance(mod, dict):
            return
        self._validate_archive_row_from_mod(row, mod)
        self._update_details()

    # Validate one archive row from its KSON entry.
    def _validate_archive_row_from_mod(
        self,
        row: QTreeWidgetItem,
        mod: dict,
        hash_cache: dict[Path, str] | None = None,
    ) -> str:
        mod_name = self._kson_mod_name(mod)
        archive_name = str(mod.get("archive_name") or "").strip()
        expected_hash = str(mod.get("archive_xxh3") or "").strip().lower()
        if not archive_name:
            self._set_validation_row(row, "Empty OK", mod_name, archive_name, expected_hash, None, "", "Blank archive_name means this is a valid empty mod.")
            return "empty"
        if not expected_hash:
            self._set_validation_row(row, "Skipped", mod_name, archive_name, expected_hash, None, "", "No archive hash in KSON.")
            return "skipped"

        archive_path = self._archive_path(archive_name)
        if archive_path is None:
            self._set_validation_row(row, "Missing", mod_name, archive_name, expected_hash, None, "", "Archive not found in MO2 downloads.")
            return "missing"

        if hash_cache is not None:
            if archive_path not in hash_cache:
                hash_cache[archive_path] = file_hash(archive_path).lower()
            actual_hash = hash_cache[archive_path]
        else:
            actual_hash = file_hash(archive_path).lower()
        if actual_hash == expected_hash:
            self._set_validation_row(row, "Hash OK", mod_name, archive_name, expected_hash, archive_path, actual_hash, "Archive hash matches.")
            return "ok"

        archive_files_ok, result_text = self._archive_contents_hash_ok(mod, archive_path)
        if archive_files_ok:
            self._set_validation_row(row, "Hash OK", mod_name, archive_name, expected_hash, archive_path, actual_hash, result_text)
            return "ok"

        self._set_validation_row(row, "Hash Miss", mod_name, archive_name, expected_hash, archive_path, actual_hash, result_text)
        return "mismatch"

    # Validate local archive files against archive names and XXH3 hashes in the cached KSON.
    def _validate_archives(self):
        kson = self._read_cached_kson()
        if not kson:
            self._details.setPlainText("No cached KSON is loaded. Fetch or place a local KSON first.")
            logger.warning("[KOTOR2 Sync] Archive validation skipped: no cached KSON.")
            return

        self._prepare_tslrcm_archives_for_validation(kson)

        mods_by_name: dict[str, list[dict]] = {}
        for mod in kson.get("mods", []):
            if not isinstance(mod, dict):
                continue
            mod_name = self._kson_mod_name(mod)
            if mod_name:
                mods_by_name.setdefault(mod_name, []).append(mod)

        hash_cache: dict[Path, str] = {}
        counts = {"ok": 0, "empty": 0, "missing": 0, "mismatch": 0, "skipped": 0}
        sorting_enabled = self._tree.isSortingEnabled()
        self._tree.setSortingEnabled(False)
        try:
            rows = [self._tree.topLevelItem(index) for index in range(self._tree.topLevelItemCount())]
            for index, row in enumerate(rows):
                mod = row.data(0, Qt.ItemDataRole.UserRole + 1)
                if not isinstance(mod, dict):
                    matches = mods_by_name.get(row.text(2), [])
                    mod = matches.pop(0) if matches else None
                if not isinstance(mod, dict):
                    counts["skipped"] += 1
                    self._set_validation_row(row, "Skipped", row.text(2), "", "", None, "", "No matching KSON mod entry was found for this row.")
                    continue

                mod_name = self._kson_mod_name(mod)
                row.setText(0, "Checking")
                self._summary_label.setText(f"Validating {index + 1}/{len(rows)}: {mod_name}")
                QApplication.processEvents()
                result = self._validate_archive_row_from_mod(mod=mod, row=row, hash_cache=hash_cache)
                counts[result] += 1
        finally:
            self._tree.setSortingEnabled(sorting_enabled)

        self._summary_label.setText(
            f"{counts['ok']} ok | {counts['empty']} empty | {counts['missing']} missing | "
            f"{counts['mismatch']} mismatch | {counts['skipped']} skipped"
        )
        logger.info(
            f"[KOTOR2 Sync] Archive validation: {counts['ok']} ok, {counts['empty']} empty, "
            f"{counts['missing']} missing, {counts['mismatch']} mismatch, {counts['skipped']} skipped."
        )
        self._validated_for_sync = counts["missing"] == 0 and counts["mismatch"] == 0 and counts["skipped"] == 0
        self._sync_btn.setEnabled(self._validated_for_sync and self._sync_thread is None)
        self._update_details()

    def _prepare_tslrcm_archives_for_validation(self, kson: dict):
        mods = kson.get("mods", [])
        if not isinstance(mods, list):
            return
        for mod in mods:
            if not isinstance(mod, dict):
                continue
            archive_name = str(mod.get("archive_name") or "").strip()
            if not self._is_tslrcm_expected_archive_name(archive_name):
                continue
            try:
                self._convert_matching_tslrcm_installer(archive_name)
            except Exception as exc:
                logger.warning(f"[KOTOR2 Sync] TSLRCM pre-validation conversion failed for {archive_name}: {exc}")

    # Install the validated KSON into MO2 mods and update profile modlist.txt.
    def _sync_validated_build(self):
        if not self._validated_for_sync or self._sync_thread is not None:
            return
        kson_path = self._cache_path()
        if not kson_path.exists():
            self._details.setPlainText("No cached KSON is available to sync.")
            logger.warning("[KOTOR2 Sync] Sync skipped: no cached KSON.")
            return
        thread = QThread(self)
        worker = _SyncWorker(kson_path, self._downloads_path(), Path(self._organizer.modsPath()), Path(self._organizer.profilePath()))
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._update_sync_progress)
        worker.finished.connect(self._finish_sync)
        worker.failed.connect(self._fail_sync)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._clear_sync_worker)
        self._sync_thread = thread
        self._sync_worker = worker
        self._sync_progress_lines = []
        self._sync_btn.setEnabled(False)
        self._details.setPlainText("Starting sync...")
        logger.info(f"[KOTOR2 Sync] Starting sync from {kson_path}.")
        thread.start()

    # Show sync worker progress.
    def _update_sync_progress(self, current: int, total: int, mod_name: str, status: str):
        self._summary_label.setText(f"Syncing {current}/{total}: {mod_name}")
        self._sync_progress_lines.append(f"[{current}/{total}] {mod_name}: {status}")
        self._sync_progress_lines = self._sync_progress_lines[-80:]
        self._details.setPlainText("\n".join(self._sync_progress_lines))
        self._details.verticalScrollBar().setValue(self._details.verticalScrollBar().maximum())

    # Show sync completion details.
    def _finish_sync(self, result: SyncInstallResult):
        details = [f"Synced {result.mod_count} mod(s).", f"Updated: {Path(self._organizer.profilePath()) / 'modlist.txt'}"]
        if result.warnings:
            details.extend(["", "Warnings:", *result.warnings[:30]])
        self._details.setPlainText("\n".join(details))
        self._summary_label.setText(f"Synced {result.mod_count} mods")
        logger.info(f"[KOTOR2 Sync] Synced {result.mod_count} mod(s).")
        for warning in result.warnings[:30]:
            logger.warning(f"[KOTOR2 Sync] {warning}")
        refresh_mo2(self._organizer, self)
        QTimer.singleShot(750, self._run_post_sync_steps)

    # Show sync failure details.
    def _fail_sync(self, message: str):
        self._details.setPlainText(f"Sync failed:\n{message}")
        self._summary_label.setText("Sync failed")
        logger.warning(f"[KOTOR2 Sync] Sync failed: {message}")
        refresh_mo2(self._organizer, self)

    # Clear sync worker references.
    def _clear_sync_worker(self):
        self._sync_thread = None
        self._sync_worker = None
        self._sync_btn.setEnabled(self._validated_for_sync)

    # Run post-sync patcher and texture steps.
    def _run_post_sync_steps(self):
        patcher_tab = getattr(self._game, "_patcher_tab", None)
        run_after_sync = getattr(patcher_tab, "run_after_sync", None)
        if not callable(run_after_sync):
            logger.warning("[KOTOR2 Sync] Patcher tab is not available after sync.")
            self._refresh_related_tabs()
            return

        self._summary_label.setText("Running patcher")
        self._details.appendPlainText("\nRunning patcher...")
        try:
            run_after_sync()
            self._details.appendPlainText("Patcher finished.")
            logger.info("[KOTOR2 Sync] Patcher finished after sync.")
            self._run_texture_auto_fix_after_sync()
            self._summary_label.setText("Sync, patcher, and texture autofix complete")
        except Exception as exc:
            self._summary_label.setText("Sync complete; post-sync step failed")
            self._details.appendPlainText(f"Post-sync step failed:\n{exc}")
            logger.warning(f"[KOTOR2 Sync] Post-sync step failed: {exc}")
        self._refresh_related_tabs()

    # Refresh textures and run the auto-fix loop after patching.
    def _run_texture_auto_fix_after_sync(self):
        texture_tab = getattr(self._game, "_texture_tab", None)
        run_auto_fix = getattr(texture_tab, "run_auto_fix_after_sync", None)
        if not callable(run_auto_fix):
            logger.warning("[KOTOR2 Sync] Texture tab is not available after sync.")
            return

        self._summary_label.setText("Running texture autofix")
        self._details.appendPlainText("Running texture autofix...")
        run_auto_fix()
        self._details.appendPlainText("Texture autofix finished.")
        logger.info("[KOTOR2 Sync] Texture autofix finished after sync.")

    # Refresh custom tabs that read the mod list.
    def _refresh_related_tabs(self):
        for attr_name in ("_patcher_tab", "_texture_tab"):
            tab = getattr(self._game, attr_name, None)
            refresh = getattr(tab, "refresh", None)
            if callable(refresh):
                refresh()

    # Update one sync row after archive validation.
    def _set_validation_row(
        self,
        row: QTreeWidgetItem,
        state: str,
        mod_name: str,
        archive_name: str,
        expected_hash: str,
        archive_path: Path | None,
        actual_hash: str,
        result: str,
    ):
        row.setText(0, state)
        details = "\n".join(
            [
                f"Mod: {mod_name}",
                f"Validation: {state}",
                f"Result: {result}",
                "",
                f"Archive name: {archive_name or '(none)'}",
                f"Archive path: {archive_path or '(not found)'}",
                f"Expected archive XXH3: {expected_hash or '(none)'}",
                f"Actual archive XXH3: {actual_hash or '(none)'}",
                f"Cache file: {self._cache_path()}",
            ]
        )
        for column in range(row.columnCount()):
            row.setData(column, Qt.ItemDataRole.UserRole, details)
        if self._tree.currentItem() is row:
            self._details.setPlainText(details)

    # Return the current game build key used for remote lookup and cache naming.
    def _build_key(self) -> str:
        return "kotor2" if self._game.gameShortName().lower() == "kotor2" else "kotor"

    # Return the profile-local directory for downloaded and local KSON manifests.
    def _kson_dir(self) -> Path:
        return Path(self._organizer.profilePath()) / "kson"

    # Return the local cache path for the selected KSON.
    def _cache_path(self) -> Path:
        return self._kson_dir() / f"{self._build_key()}_latest_build.kson"

    # Return the displayed cached KSON timestamp.
    def _cached_kson_version_text(self) -> str:
        cache_path = self._cache_path()
        source_url = ""
        selected_name = ""
        if cache_path.exists():
            try:
                kson = json.loads(cache_path.read_text(encoding="utf-8"))
                source_url = str(kson.get("_source_url") or "")
                selected_name = str(kson.get("_selected_kson_name") or "")
            except Exception:
                source_url = ""
        for name in (selected_name, Path(source_url).name if source_url else "", cache_path.name):
            version_text = _kson_version_text_from_name(name)
            if version_text != "unknown":
                return version_text
        return self._latest_local_kson_version_text()

    # Return the newest timestamped local KSON version text.
    def _latest_local_kson_version_text(self) -> str:
        candidates = [
            _kson_version_text_from_name(path.name)
            for path in self._kson_dir().glob("*.kson")
            if path.name != self._cache_path().name
            and self._is_game_kson_path(path.name)
        ]
        known = [candidate for candidate in candidates if candidate != "unknown"]
        return max(known) if known else "unknown"

    # Check if a KSON filename is for this game.
    def _is_game_kson_path(self, path: str) -> bool:
        name = Path(path).name.lower()
        if not name.endswith(".kson"):
            return False
        if self._build_key() == "kotor2":
            return name.startswith("kotor2")
        return name.startswith("kotor") and not name.startswith("kotor2")

    # Resolve a KSON archive name to a file in MO2 downloads.
    def _archive_path(self, archive_name: str) -> Path | None:
        downloads_path = self._downloads_path()
        candidates = [
            archive_name,
            html.unescape(archive_name),
        ]
        seen: set[str] = set()
        for candidate in candidates:
            cleaned = candidate.strip().strip('"').strip("'")
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            for path in (downloads_path / cleaned, downloads_path / f"{cleaned}.zip"):
                if path.exists():
                    return path
        converted_path = self._convert_matching_tslrcm_installer(archive_name)
        if converted_path is not None and converted_path.exists():
            return converted_path
        return None

    # Allow an archive with matching contents to pass even if the container hash differs.
    def _archive_contents_hash_ok(self, mod: dict, archive_path: Path) -> tuple[bool, str]:
        archive_files = mod.get("archive_files")
        if not isinstance(archive_files, list) or not archive_files:
            return False, "Archive hash does not match."

        expected_by_path: dict[str, str] = {}
        for file_entry in archive_files:
            if not isinstance(file_entry, dict):
                return False, "Archive hash does not match."
            path = str(file_entry.get("path") or "").strip().replace("\\", "/")
            xxh3 = str(file_entry.get("xxh3") or "").strip().lower()
            if not path or not xxh3:
                return False, "Archive hash does not match."
            expected_by_path[path] = xxh3

        actual_by_path = self._archive_member_hashes(archive_path)
        if not actual_by_path:
            return False, "Archive hash does not match."

        expected_paths = set(expected_by_path)
        actual_paths = set(actual_by_path)
        if expected_paths != actual_paths:
            missing = sorted(expected_paths - actual_paths)
            extra = sorted(actual_paths - expected_paths)
            details: list[str] = []
            if missing:
                details.append(f"missing {len(missing)} file(s)")
            if extra:
                details.append(f"extra {len(extra)} file(s)")
            suffix = f" ({', '.join(details)})" if details else ""
            return False, f"Archive hash does not match, and archive contents differ from KSON{suffix}."

        mismatches = sorted(path for path in expected_paths if expected_by_path[path] != actual_by_path[path])
        if mismatches:
            return False, f"Archive hash does not match, and {len(mismatches)} archived file hash(es) differ from KSON."

        return True, "Archive hash mismatched, but all archived file hashes match the KSON contents."

    # Hash all files inside a ZIP archive by relative path.
    def _archive_member_hashes(self, archive_path: Path) -> dict[str, str]:
        if archive_path.suffix.lower() != ".zip":
            return {}
        try:
            with zipfile.ZipFile(archive_path) as archive:
                return {
                    info.filename.replace("\\", "/"): xxh3_bytes(archive.read(info)).lower()
                    for info in archive.infolist()
                    if not info.is_dir()
                }
        except Exception:
            return {}

    # Wrap a loose download in an uncompressed ZIP.
    def _wrap_loose_download(self, archive_path: Path, archive_name: str) -> tuple[Path, str]:
        converted_path, converted_result = self._convert_tslrcm_installer_if_needed(archive_path, archive_name)
        if converted_path != archive_path or converted_result:
            return converted_path, converted_result
        if self._is_known_archive(archive_path):
            return archive_path, ""
        expected_name = html.unescape(archive_name).strip()
        if expected_name:
            wrapped_path = archive_path.with_name(expected_name)
        else:
            wrapped_path = archive_path.with_name(f"{archive_path.name}.zip")
        if wrapped_path.suffix.lower() != ".zip":
            wrapped_path = wrapped_path.with_name(f"{wrapped_path.name}.zip")
        temp_path = wrapped_path.with_name(f"{wrapped_path.name}.tmp")
        seven_zip = self._seven_zip_exe()
        if seven_zip:
            result = subprocess.run(
                [seven_zip, "a", "-tzip", "-mx=0", str(temp_path), archive_path.name],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                startupinfo=self._subprocess_startupinfo(),
                creationflags=self._subprocess_creationflags(),
                cwd=str(archive_path.parent),
            )
            if result.returncode == 0 and temp_path.exists():
                temp_path.replace(wrapped_path)
                if archive_path != wrapped_path and archive_path.exists():
                    archive_path.unlink()
                return wrapped_path, f"Wrapped loose file as uncompressed ZIP with 7-Zip: {wrapped_path.name}"

        original_bytes = archive_path.read_bytes()
        with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr(archive_path.name, original_bytes, compress_type=zipfile.ZIP_STORED)
        temp_path.replace(wrapped_path)
        if archive_path != wrapped_path and archive_path.exists():
            archive_path.unlink()
        return wrapped_path, f"Wrapped loose file as uncompressed ZIP: {wrapped_path.name}"

    # Convert a downloaded TSLRCM 2022 installer into the expected archive when possible.
    def _convert_tslrcm_installer_if_needed(self, archive_path: Path, archive_name: str) -> tuple[Path, str]:
        if not self._should_convert_tslrcm_installer(archive_path, archive_name):
            return archive_path, ""
        target_path = archive_path.with_name(self._tslrcm_archive_output_name(archive_name))
        if target_path.exists() and self._is_known_archive(target_path):
            return target_path, f"Using converted TSLRCM archive: {target_path.name}"
        try:
            converted_path = self._convert_tslrcm_installer_to_archive(archive_path, target_path)
            return converted_path, f"Converted TSLRCM installer to archive: {converted_path.name}"
        except Exception as exc:
            return archive_path, f"TSLRCM installer conversion failed: {exc}"

    # Try to convert a matching TSLRCM installer already present in downloads.
    def _convert_matching_tslrcm_installer(self, archive_name: str) -> Path | None:
        expected_name = html.unescape(archive_name).strip()
        if not expected_name:
            return None
        for candidate in self._downloads_path().iterdir():
            if not candidate.is_file():
                continue
            if not self._is_tslrcm_installer_path(candidate):
                continue
            converted_path, _result = self._convert_tslrcm_installer_if_needed(candidate, archive_name)
            if converted_path.exists() and converted_path.suffix.lower() == ".zip":
                return converted_path
        return None

    # Check whether a path is the known TSLRCM 2022 installer.
    @staticmethod
    def _is_tslrcm_installer_path(path: Path) -> bool:
        if not path.exists() or not path.is_file():
            return False
        if path.suffix.lower() != ".exe":
            return False
        stem = path.stem.casefold().strip()
        normalized = "".join(ch for ch in stem if ch.isalnum())
        return normalized.startswith("tslrcm2022")

    @classmethod
    def _should_convert_tslrcm_installer(cls, path: Path, archive_name: str) -> bool:
        if cls._is_tslrcm_installer_path(path):
            return True
        if path.suffix.lower() != ".exe" or not path.exists() or not path.is_file():
            return False
        if not cls._is_tslrcm_expected_archive_name(archive_name):
            return False
        stem = path.stem.casefold().strip()
        normalized = "".join(ch for ch in stem if ch.isalnum())
        return normalized == "tslrcm"

    @staticmethod
    def _is_tslrcm_expected_archive_name(name: str) -> bool:
        cleaned = html.unescape(str(name or "")).strip().casefold()
        if not cleaned:
            return False
        stem = Path(cleaned).stem
        normalized = "".join(ch for ch in stem if ch.isalnum())
        return normalized.startswith("tslrcm2022")

    @staticmethod
    def _tslrcm_archive_output_name(name: str) -> str:
        cleaned = html.unescape(str(name or "")).strip()
        if cleaned:
            stem = Path(cleaned).stem
            normalized = "".join(ch for ch in stem.casefold() if ch.isalnum())
            if normalized.startswith("tslrcm2022"):
                return f"{stem}.zip"
            return cleaned if Path(cleaned).suffix.lower() == ".zip" else f"{cleaned}.zip"
        return "tslrcm2022.zip"

    # Convert the TSLRCM installer payload into a deterministic ZIP archive.
    def _convert_tslrcm_installer_to_archive(self, installer_path: Path, archive_path: Path) -> Path:
        script_path = Path(__file__).resolve().parent / "tslrcm-lzma.ps1"
        if not script_path.exists():
            raise RuntimeError(f"Missing converter script: {script_path}")
        with tempfile.TemporaryDirectory(prefix="kotorganizer_tslrcm_") as temp_dir:
            normalized_path = Path(temp_dir) / "normalized"
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script_path),
                    str(installer_path),
                    str(normalized_path),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                startupinfo=self._subprocess_startupinfo(),
                creationflags=self._subprocess_creationflags(),
            )
            if result.returncode != 0:
                raise RuntimeError((result.stderr or result.stdout or "converter failed").strip())
            files = sorted(path for path in normalized_path.rglob("*") if path.is_file())
            if not files:
                raise RuntimeError("converter produced no normalized files")
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            temp_zip = archive_path.with_name(f"{archive_path.name}.tmp")
            if temp_zip.exists():
                temp_zip.unlink()
            try:
                with zipfile.ZipFile(temp_zip, "w", compression=zipfile.ZIP_STORED) as archive:
                    for file_path in files:
                        relative = file_path.relative_to(normalized_path).as_posix()
                        info = zipfile.ZipInfo(relative)
                        info.date_time = _FIXED_ZIP_TIMESTAMP
                        info.compress_type = zipfile.ZIP_STORED
                        info.create_system = 0
                        archive.writestr(info, file_path.read_bytes())
                temp_zip.replace(archive_path)
            finally:
                if temp_zip.exists():
                    temp_zip.unlink(missing_ok=True)
            return archive_path

    # Return the bundled 7-Zip executable.
    @staticmethod
    def _seven_zip_exe() -> str:
        plugin_dir = Path(__file__).resolve().parent
        exe = plugin_dir / "7z.exe"
        dll = plugin_dir / "7z.dll"
        return str(exe) if exe.exists() and dll.exists() else ""

    # Return Windows subprocess startup info when needed.
    @staticmethod
    def _subprocess_startupinfo():
        if os.name != "nt":
            return None
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        return startupinfo

    # Return Windows subprocess creation flags when needed.
    @staticmethod
    def _subprocess_creationflags() -> int:
        return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    # Check whether a download is already an archive.
    @staticmethod
    def _is_archive_file(path: Path) -> bool:
        return path.suffix.lower() in {
            ".zip",
            ".7z",
            ".rar",
            ".tar",
            ".gz",
            ".bz2",
            ".xz",
            ".tgz",
            ".tbz2",
            ".txz",
        }

    # Check whether a path is a real archive, not just an archive-named file.
    def _is_known_archive(self, path: Path) -> bool:
        if not path.exists() or not self._is_archive_file(path):
            return False
        suffix = path.suffix.lower()
        if suffix == ".zip":
            return zipfile.is_zipfile(path)
        return True

    # Return MO2's downloads folder as a Path.
    def _downloads_path(self) -> Path:
        return Path(self._organizer.downloadsPath())

    # Return the Nexus game identifier used by MO2 where available.
    def _nexus_game_name(self) -> str:
        try:
            value = self._game.gameNexusName()
            if value:
                return str(value).strip().lower()
        except Exception:
            pass
        return self._game.gameShortName().lower()

    # Return Nexus numeric game id for DownloadPopUp URLs.
    def _nexus_game_id(self) -> str:
        game_name = self._nexus_game_name()
        if game_name == "kotor":
            return "234"
        if game_name == "kotor2":
            return "198"
        try:
            value = self._game.gameNexusID()
            if value:
                return str(value)
        except Exception:
            pass
        return "234" if self._build_key() == "kotor" else "198"

    # Extract a Nexus mod id from a KSON URL.
    # Extract the Nexus mod id from a URL.
    @staticmethod
    def _nexus_mod_id(url: str) -> str:
        match = re.search(r"/mods/(\d+)", url)
        return match.group(1) if match else ""

    # Locate Microsoft Edge for sandboxed browser fallback.
    # Find the Edge browser executable.
    @staticmethod
    def _edge_path() -> Path | None:
        candidates = [
            Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
            Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    # Seed the sandboxed Edge profile with the MO2 downloads folder.
    # Write Edge download preferences.
    @staticmethod
    def _write_edge_preferences(profile_dir: Path, downloads_path: Path):
        default_dir = profile_dir / "Default"
        default_dir.mkdir(parents=True, exist_ok=True)
        prefs_path = default_dir / "Preferences"
        prefs = {}
        if prefs_path.exists():
            try:
                prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
            except Exception:
                prefs = {}
        prefs.setdefault("download", {})
        prefs["download"]["default_directory"] = str(downloads_path)
        prefs["download"]["prompt_for_download"] = False
        prefs_path.write_text(json.dumps(prefs, indent=2), encoding="utf-8")

    # Write/update MO2 archive metadata for a downloaded archive.
    def _write_archive_meta(self, mod: dict, archive_path: Path):
        archive_name = archive_path.name
        meta_path = archive_path.with_name(f"{archive_name}.meta")
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        if meta_path.exists():
            try:
                parser.read(meta_path, encoding="utf-8")
            except Exception:
                parser = configparser.ConfigParser(interpolation=None)
                parser.optionxform = str
        if not parser.has_section("General"):
            parser.add_section("General")

        url = str(mod.get("url") or "").strip()
        host = urlparse(url).netloc.lower()
        repository = str(mod.get("repository") or "").strip()
        if not repository:
            if "nexusmods.com" in host:
                repository = "Nexus"
            elif "deadlystream.com" in host:
                repository = "DeadlyStream"

        fields = {
            "installed": "false",
            "uninstalled": "false",
            "gameName": self._nexus_game_name() if repository.lower() == "nexus" else self._build_key(),
            "name": archive_path.stem,
            "modName": self._kson_mod_name(mod),
            "version": str(mod.get("version") or "").strip(),
            "newestVersion": str(mod.get("version") or "").strip(),
            "manualURL": url,
            "url": url,
            "repository": repository,
            "ArchiveReleaseDate": str(mod.get("release_date") or "").strip(),
            "KsonArchiveXXH3": str(mod.get("archive_xxh3") or "").strip(),
        }

        mod_id = str(mod.get("mod_id") or mod.get("modID") or "").strip() or self._nexus_mod_id(url)
        file_id = str(mod.get("file_id") or mod.get("fileID") or "").strip()
        if mod_id:
            fields["modID"] = mod_id
        if file_id:
            fields["fileID"] = file_id

        for key, value in fields.items():
            if value:
                parser.set("General", key, value)
        with meta_path.open("w", encoding="utf-8") as handle:
            parser.write(handle)

    def _expected_archive_name(self, mod: dict) -> str:
        return str(mod.get("archive_name") or "").strip()

    def _capture_downloaded_archive_metadata(self, mod: dict, archive_path: Path):
        changed = False
        if not str(mod.get("archive_name") or "").strip():
            mod["archive_name"] = archive_path.name
            changed = True
        if not str(mod.get("archive_xxh3") or "").strip():
            try:
                mod["archive_xxh3"] = file_hash(archive_path).lower()
                changed = True
            except Exception:
                pass
        if changed:
            self._write_cached_kson_mod_update(mod)

    def _write_cached_kson_mod_update(self, mod: dict):
        cache_path = self._cache_path()
        kson = self._read_cached_kson()
        if kson is None:
            return
        mods = kson.get("mods", [])
        if not isinstance(mods, list):
            return
        target_name = self._kson_mod_name(mod)
        target_priority = str(mod.get("priority") or "").strip()
        updated = False
        for item in mods:
            if not isinstance(item, dict):
                continue
            if self._kson_mod_name(item) != target_name:
                continue
            if target_priority and str(item.get("priority") or "").strip() != target_priority:
                continue
            item["archive_name"] = str(mod.get("archive_name") or "").strip()
            item["archive_xxh3"] = str(mod.get("archive_xxh3") or "").strip()
            updated = True
            break
        if not updated:
            return
        try:
            cache_path.write_text(json.dumps(kson, indent=2), encoding="utf-8")
        except Exception:
            return

    def _newest_download_for_url(self, url: str) -> Path | None:
        host = urlparse(url).netloc.casefold()
        if "deadlystream.com" not in host:
            return None
        try:
            candidates = [
                path for path in self._downloads_path().iterdir()
                if path.is_file() and not path.name.casefold().endswith(".meta")
            ]
        except Exception:
            return None
        return max(candidates, key=lambda path: path.stat().st_mtime, default=None)

    def _rename_download_to_expected(self, archive_path: Path, expected_name: str) -> Path | None:
        if self._should_preserve_download_name_for_conversion(archive_path, expected_name):
            return archive_path
        target_path = archive_path.with_name(expected_name)
        if archive_path == target_path:
            return archive_path
        if target_path.exists():
            return target_path
        try:
            archive_path.rename(target_path)
            meta_path = archive_path.with_name(f"{archive_path.name}.meta")
            if meta_path.exists():
                meta_path.rename(target_path.with_name(f"{target_path.name}.meta"))
            return target_path
        except Exception:
            return None

    @classmethod
    def _should_preserve_download_name_for_conversion(cls, archive_path: Path, expected_name: str) -> bool:
        return (
            archive_path.exists()
            and archive_path.is_file()
            and archive_path.suffix.lower() == ".exe"
            and cls._is_tslrcm_expected_archive_name(expected_name)
        )

    # Read the cached KSON when present.
    def _read_cached_kson(self) -> dict | None:
        cache_path = self._cache_path()
        if not cache_path.exists():
            return None
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    # Return a mod name from KSON data.
    @staticmethod
    def _kson_mod_name(mod) -> str:
        if isinstance(mod, dict):
            name = mod.get("mod_name") or mod.get("name") or mod.get("Mod Name")
            return str(name).strip() if name else ""
        if isinstance(mod, str):
            return mod.strip()
        return ""

    # Return true if the KSON mod should be enabled in modlist.txt.
    # Return whether a KSON mod is enabled.
    @staticmethod
    def _kson_mod_enabled(mod) -> bool:
        if not isinstance(mod, dict):
            return True
        return bool(mod.get("enabled", True))

    # Build a short source label from a mod URL.
    # Build a short source label.
    @staticmethod
    def _source_label(url: str) -> str:
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        if "deadlystream.com" in host:
            return "DeadlyStream"
        if "nexusmods.com" in host:
            return "Nexus Mods"
        if "mega.nz" in host:
            return "MEGA"
        if "github.com" in host:
            return "GitHub"
        return host or "(none)"

    # Count embedded TSLPatcher order entries for display.
    # Count patch order entries.
    @staticmethod
    def _patch_order_count(value) -> int:
        if isinstance(value, dict):
            for key in ("mods", "order", "entries"):
                inner = value.get(key)
                if isinstance(inner, list):
                    return len(inner)
            return len(value)
        if isinstance(value, list):
            return len(value)
        return 0
