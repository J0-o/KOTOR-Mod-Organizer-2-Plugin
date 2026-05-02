import html
import logging
import os
import subprocess
import tempfile
import zipfile
from pathlib import Path

from hash_utils import file_hash


logger = logging.getLogger("mobase")

_FIXED_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


class ArchiveService:
    def __init__(self, downloads_path: Path, cache_path: Path | None = None):
        self._downloads_path = downloads_path
        self._cache_path = cache_path

    def prepare_tslrcm_archives_for_validation(self, kson: dict):
        mods = kson.get("mods", [])
        if not isinstance(mods, list):
            return
        for mod in mods:
            if not isinstance(mod, dict):
                continue
            archive_name = str(mod.get("archive_name") or "").strip()
            if not self.is_tslrcm_expected_archive_name(archive_name):
                continue
            try:
                self.convert_matching_tslrcm_installer(archive_name)
            except Exception as exc:
                logger.warning(f"[KOTOR2 Sync] TSLRCM pre-validation conversion failed for {archive_name}: {exc}")

    def validate_mod(self, mod: dict, hash_cache: dict[Path, str] | None = None) -> dict:
        mod_name = self.kson_mod_name(mod)
        archive_name = self.display_archive_name(mod)
        expected_hash = str(mod.get("archive_xxh3") or "").strip().lower()
        if not archive_name:
            return self._result(
                "empty",
                "Empty OK",
                mod_name,
                archive_name,
                expected_hash,
                None,
                "",
                "Blank archive_name means this is a valid empty mod.",
            )
        if not expected_hash:
            return self._result("skipped", "Skipped", mod_name, archive_name, expected_hash, None, "", "No archive hash in KSON.")

        named_archive_path = self.resolve_named_archive_path(self.expected_archive_name(mod))
        if named_archive_path is not None:
            return self.validate_archive_path(mod, named_archive_path, hash_cache=hash_cache, allow_content_hash=True)

        archive_path = self.resolve_archive_path_by_hash(expected_hash, hash_cache=hash_cache)
        if archive_path is None:
            return self._result("missing", "Missing", mod_name, archive_name, expected_hash, None, "", "Archive not found in MO2 downloads.")
        return self.validate_archive_path(mod, archive_path, hash_cache=hash_cache, allow_content_hash=False)

    def validate_archive_path(
        self,
        mod: dict,
        archive_path: Path,
        hash_cache: dict[Path, str] | None = None,
        allow_content_hash: bool = False,
    ) -> dict:
        mod_name = self.kson_mod_name(mod)
        archive_name = self.display_archive_name(mod)
        expected_hash = str(mod.get("archive_xxh3") or "").strip().lower()
        if hash_cache is not None:
            if archive_path not in hash_cache:
                hash_cache[archive_path] = file_hash(archive_path).lower()
            actual_hash = hash_cache[archive_path]
        else:
            actual_hash = file_hash(archive_path).lower()
        name_matches = self.archive_name_matches(archive_path, archive_name)
        if actual_hash == expected_hash:
            result_text = "Archive hash matches."
            if not name_matches:
                result_text = f"Archive name did not match, but archive hash matched: {archive_path.name}"
            return self._result("ok", "Hash OK", mod_name, archive_name, expected_hash, archive_path, actual_hash, result_text)

        if allow_content_hash and name_matches:
            archive_files_ok, result_text = self.archive_contents_hash_ok(mod, archive_path)
            if archive_files_ok:
                return self._result("ok", "Hash OK", mod_name, archive_name, expected_hash, archive_path, actual_hash, result_text)
            return self._result("mismatch", "Hash Miss", mod_name, archive_name, expected_hash, archive_path, actual_hash, result_text)

        return self._result(
            "mismatch",
            "Hash Miss",
            mod_name,
            archive_name,
            expected_hash,
            archive_path,
            actual_hash,
            "Archive name did not match the KSON archive name, and archive hash did not match.",
        )

    def resolve_archive_path_for_mod(self, mod: dict, hash_cache: dict[Path, str] | None = None) -> Path | None:
        archive_path = self.resolve_named_archive_path(self.expected_archive_name(mod))
        if archive_path is not None:
            return archive_path
        expected_hash = str(mod.get("archive_xxh3") or "").strip().lower()
        if not expected_hash:
            return None
        return self.resolve_archive_path_by_hash(expected_hash, hash_cache=hash_cache)

    def resolve_named_archive_path(self, *archive_names: str) -> Path | None:
        candidates: list[str] = []
        for archive_name in archive_names:
            candidates.extend([archive_name, html.unescape(archive_name)])
        seen: set[str] = set()
        for candidate in candidates:
            cleaned = candidate.strip().strip('"').strip("'")
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            for path in (self._downloads_path / cleaned, self._downloads_path / f"{cleaned}.zip"):
                if path.exists():
                    return path
        expected_archive_name = next((str(name).strip() for name in archive_names if str(name).strip()), "")
        if self.is_tslrcm_expected_archive_name(expected_archive_name):
            converted_path = self.convert_matching_tslrcm_installer(expected_archive_name)
            if converted_path is not None and converted_path.exists():
                return converted_path
        return None

    def resolve_archive_path_by_hash(self, expected_hash: str, hash_cache: dict[Path, str] | None = None) -> Path | None:
        if not expected_hash:
            return None
        try:
            candidates = sorted(
                path for path in self._downloads_path.iterdir()
                if path.is_file() and not path.name.casefold().endswith(".meta")
            )
        except Exception:
            return None
        for candidate in candidates:
            if not self.is_known_archive(candidate):
                continue
            if hash_cache is not None:
                if candidate not in hash_cache:
                    hash_cache[candidate] = file_hash(candidate).lower()
                actual_hash = hash_cache[candidate]
            else:
                actual_hash = file_hash(candidate).lower()
            if actual_hash == expected_hash:
                return candidate
        return None

    def archive_contents_hash_ok(self, mod: dict, archive_path: Path) -> tuple[bool, str]:
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

        actual_by_path, extraction_error = self.archive_member_hashes(archive_path)
        if extraction_error:
            return False, f"Archive hash does not match, and archive contents could not be extracted for comparison ({extraction_error})."

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

    def archive_member_hashes(self, archive_path: Path) -> tuple[dict[str, str], str]:
        seven_zip = self.seven_zip_exe()
        if seven_zip:
            try:
                with tempfile.TemporaryDirectory(prefix="kotorganizer_archive_hash_") as temp_dir:
                    extract_root = Path(temp_dir) / "extract"
                    extract_root.mkdir(parents=True, exist_ok=True)
                    result = subprocess.run(
                        [seven_zip, "x", "-y", f"-o{extract_root}", str(archive_path)],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        check=False,
                        startupinfo=self.subprocess_startupinfo(),
                        creationflags=self.subprocess_creationflags(),
                    )
                    if result.returncode != 0:
                        detail = (result.stderr or result.stdout or "").strip()
                        return {}, detail or f"7-Zip extraction failed with exit code {result.returncode}"
                    return (
                        {
                            path.relative_to(extract_root).as_posix(): file_hash(path).lower()
                            for path in sorted(extract_root.rglob("*"))
                            if path.is_file()
                        },
                        "",
                    )
            except Exception as exc:
                return {}, str(exc)

        if archive_path.suffix.lower() != ".zip":
            return {}, "7-Zip is unavailable for non-ZIP archive comparison"
        try:
            with zipfile.ZipFile(archive_path) as archive:
                with tempfile.TemporaryDirectory(prefix="kotorganizer_archive_hash_") as temp_dir:
                    extract_root = Path(temp_dir) / "extract"
                    extract_root.mkdir(parents=True, exist_ok=True)
                    archive.extractall(extract_root)
                    return (
                        {
                            path.relative_to(extract_root).as_posix(): file_hash(path).lower()
                            for path in sorted(extract_root.rglob("*"))
                            if path.is_file()
                        },
                        "",
                    )
        except Exception as exc:
            return {}, str(exc)

    def detect_browser_download(self, expected_path: Path, existing_names: set[str]) -> Path | None:
        if (
            expected_path.name
            and expected_path.is_file()
            and not self.is_incomplete_download_name(expected_path.name)
            and not (expected_path.parent / f"{expected_path.name}.crdownload").exists()
        ):
            return expected_path
        return self.detect_new_download(existing_names)

    def detect_new_download(self, existing_names: set[str]) -> Path | None:
        try:
            new_files = [
                path
                for path in self._downloads_path.iterdir()
                if path.is_file()
                and path.name not in existing_names
                and not self.is_incomplete_download_name(path.name)
            ]
        except Exception:
            return None
        if len(new_files) != 1:
            return None
        return new_files[0]

    def convert_matching_tslrcm_installer(self, archive_name: str) -> Path | None:
        expected_name = html.unescape(archive_name).strip()
        if not expected_name or not self.is_tslrcm_expected_archive_name(expected_name):
            return None
        for candidate in self._downloads_path.iterdir():
            if not candidate.is_file():
                continue
            if not self.is_tslrcm_installer_path(candidate):
                continue
            converted_path, _result = self.convert_tslrcm_installer_if_needed(candidate, archive_name)
            if converted_path.exists() and converted_path.suffix.lower() == ".zip":
                return converted_path
        return None

    def convert_tslrcm_installer_if_needed(self, archive_path: Path, archive_name: str) -> tuple[Path, str]:
        if not self.should_convert_tslrcm_installer(archive_path, archive_name):
            return archive_path, ""
        target_path = archive_path.with_name(self.tslrcm_archive_output_name(archive_name))
        if target_path.exists() and self.is_known_archive(target_path):
            return target_path, f"Using converted TSLRCM archive: {target_path.name}"
        try:
            converted_path = self.convert_tslrcm_installer_to_archive(archive_path, target_path)
            return converted_path, f"Converted TSLRCM installer to archive: {converted_path.name}"
        except Exception as exc:
            return archive_path, f"TSLRCM installer conversion failed: {exc}"

    def convert_tslrcm_installer_to_archive(self, installer_path: Path, archive_path: Path) -> Path:
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
                startupinfo=self.subprocess_startupinfo(),
                creationflags=self.subprocess_creationflags(),
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

    def _result(
        self,
        bucket: str,
        state: str,
        mod_name: str,
        archive_name: str,
        expected_hash: str,
        archive_path: Path | None,
        actual_hash: str,
        result: str,
    ) -> dict:
        return {
            "bucket": bucket,
            "state": state,
            "mod_name": mod_name,
            "archive_name": archive_name,
            "expected_hash": expected_hash,
            "archive_path": str(archive_path) if archive_path is not None else "",
            "actual_hash": actual_hash,
            "result": result,
            "cache_path": str(self._cache_path) if self._cache_path is not None else "",
        }

    @staticmethod
    def expected_archive_name(mod: dict) -> str:
        return html.unescape(str(mod.get("archive_name") or "")).strip().strip('"').strip("'")

    def display_archive_name(self, mod: dict) -> str:
        return self.expected_archive_name(mod)

    @staticmethod
    def archive_name_matches(archive_path: Path, archive_name: str) -> bool:
        cleaned = html.unescape(str(archive_name or "")).strip().strip('"').strip("'")
        if not cleaned:
            return False
        return archive_path.name in {cleaned, f"{cleaned}.zip"}

    @staticmethod
    def kson_mod_name(mod) -> str:
        if isinstance(mod, dict):
            name = mod.get("mod_name") or mod.get("name") or mod.get("Mod Name")
            return str(name).strip() if name else ""
        if isinstance(mod, str):
            return mod.strip()
        return ""

    @staticmethod
    def _kson_mod_name(mod) -> str:
        return ArchiveService.kson_mod_name(mod)

    @staticmethod
    def is_tslrcm_installer_path(path: Path) -> bool:
        if not path.exists() or not path.is_file():
            return False
        if path.suffix.lower() != ".exe":
            return False
        stem = path.stem.casefold().strip()
        normalized = "".join(ch for ch in stem if ch.isalnum())
        return normalized.startswith("tslrcm2022")

    @classmethod
    def should_convert_tslrcm_installer(cls, path: Path, archive_name: str) -> bool:
        if cls.is_tslrcm_installer_path(path):
            return True
        if path.suffix.lower() != ".exe" or not path.exists() or not path.is_file():
            return False
        if not cls.is_tslrcm_expected_archive_name(archive_name):
            return False
        stem = path.stem.casefold().strip()
        normalized = "".join(ch for ch in stem if ch.isalnum())
        return normalized == "tslrcm"

    @staticmethod
    def is_tslrcm_expected_archive_name(name: str) -> bool:
        cleaned = html.unescape(str(name or "")).strip().casefold()
        if not cleaned:
            return False
        stem = Path(cleaned).stem
        normalized = "".join(ch for ch in stem if ch.isalnum())
        return normalized.startswith("tslrcm2022")

    @staticmethod
    def tslrcm_archive_output_name(name: str) -> str:
        cleaned = html.unescape(str(name or "")).strip()
        if cleaned:
            stem = Path(cleaned).stem
            normalized = "".join(ch for ch in stem.casefold() if ch.isalnum())
            if normalized.startswith("tslrcm2022"):
                return f"{stem}.zip"
            return cleaned if Path(cleaned).suffix.lower() == ".zip" else f"{cleaned}.zip"
        return "tslrcm2022.zip"

    @staticmethod
    def is_archive_file(path: Path) -> bool:
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

    def is_known_archive(self, path: Path) -> bool:
        if not path.exists() or not self.is_archive_file(path):
            return False
        if path.suffix.lower() == ".zip":
            return zipfile.is_zipfile(path)
        return True

    @staticmethod
    def seven_zip_exe() -> str:
        plugin_dir = Path(__file__).resolve().parent
        exe = plugin_dir / "7z.exe"
        dll = plugin_dir / "7z.dll"
        return str(exe) if exe.exists() and dll.exists() else ""

    @staticmethod
    def subprocess_startupinfo():
        if os.name != "nt":
            return None
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        return startupinfo

    @staticmethod
    def subprocess_creationflags() -> int:
        return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    @staticmethod
    def is_incomplete_download_name(name: str) -> bool:
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
