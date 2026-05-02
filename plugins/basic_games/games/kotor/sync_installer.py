import base64
import configparser
import json
import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from archive_service import ArchiveService
from patcher_entries import PatchEntry, collect_patch_entries


SUBPROCESS_STARTUPINFO = None
SUBPROCESS_CREATIONFLAGS = 0
if os.name == "nt":
    SUBPROCESS_STARTUPINFO = subprocess.STARTUPINFO()
    SUBPROCESS_STARTUPINFO.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    SUBPROCESS_CREATIONFLAGS = subprocess.CREATE_NO_WINDOW


@dataclass
class SyncInstallResult:
    mod_count: int
    warnings: list[str]


def install_kson_build(
    kson_path: Path,
    downloads_path: Path,
    mods_path: Path,
    profile_path: Path,
    progress=None,
) -> SyncInstallResult:
    payload = json.loads(kson_path.read_text(encoding="utf-8"))
    mods = sorted(payload.get("mods", []), key=lambda item: int(item.get("priority", 0) or 0))
    warnings: list[str] = []
    _delete_patch_order(profile_path, warnings)
    preserved_mods, old_mods_path = _move_existing_mods(mods_path, warnings)
    total = len(mods)
    for index, mod in enumerate(mods, start=1):
        mod_name = str(mod.get("mod_name") or "").strip()
        if not mod_name:
            continue
        if progress:
            progress(index, total, mod_name, "Preparing mod folder")
        _install_mod(mod, downloads_path, mods_path, warnings, progress, index, total)

    # MO2's modlist.txt is stored in reverse priority order compared with the
    # numeric priority used for install application.
    _write_modlist(
        profile_path,
        [mod for mod in reversed(mods) if str(mod.get("mod_name") or "").strip()],
        preserved_mods,
    )
    _write_patch_order(profile_path, mods_path, mods, preserved_mods, payload.get("tslpatch_order"), warnings)
    _cleanup_old_mods(old_mods_path, warnings)
    return SyncInstallResult(mod_count=total, warnings=warnings)


def _delete_patch_order(profile_path: Path, warnings: list[str]) -> None:
    patch_order_path = profile_path / "tslpatch_order.json"
    if not patch_order_path.exists():
        return
    try:
        patch_order_path.unlink()
    except Exception as exc:
        warnings.append(f"failed to delete tslpatch_order.json: {exc}")


def _write_patch_order(
    profile_path: Path,
    mods_path: Path,
    mods: list[dict],
    preserved_mods: list[str],
    patch_order: object,
    warnings: list[str],
) -> None:
    try:
        ordered_mods = [mod for mod in reversed(mods) if str(mod.get("mod_name") or "").strip()]
        profile_order = [*sorted(preserved_mods, key=str.casefold), *[str(mod.get("mod_name") or "").strip() for mod in ordered_mods]]
        active_state = {mod_name: True for mod_name in preserved_mods}
        active_state.update({
            str(mod.get("mod_name") or "").strip(): bool(mod.get("enabled", True))
            for mod in mods
            if str(mod.get("mod_name") or "").strip()
        })
        entries = collect_patch_entries(mods_path, profile_order, _patch_order_enabled_state(patch_order), active_state)
        _write_patch_order_entries(profile_path / "tslpatch_order.json", entries)
    except Exception as exc:
        warnings.append(f"failed to write tslpatch_order.json: {exc}")


def _patch_order_enabled_state(patch_order: object) -> dict[tuple[str, str], bool]:
    rows = patch_order.get("patches", []) if isinstance(patch_order, dict) else patch_order
    if not isinstance(rows, list):
        return {}
    enabled_state: dict[tuple[str, str], bool] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        mod_name = str(row.get("mod_name") or "").strip()
        patch_name = str(row.get("patch_name") or "").strip()
        if mod_name and patch_name:
            enabled_state[(mod_name, patch_name)] = _truthy(row.get("enabled", False))
    return enabled_state


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _write_patch_order_entries(path: Path, entries: list[PatchEntry]) -> None:
    payload = {"patches": []}
    for entry in entries:
        payload["patches"].append({
            "enabled": entry.enabled,
            "priority": entry.priority,
            "mod_name": entry.mod_name,
            "patch_name": entry.patch_name,
            "description": entry.description,
            "ini_short_path": entry.ini_short_path,
            "destination": entry.destination,
            "install_paths": entry.install_paths,
            "files": entry.files,
            "required": entry.required,
        })
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _move_existing_mods(mods_path: Path, warnings: list[str]) -> tuple[list[str], Path | None]:
    if not mods_path.exists():
        return [], None
    preserved: list[str] = []
    old_mods_root = mods_path.parent / "_kotorganizer_sync_old_mods"
    _cleanup_old_mods(old_mods_root, warnings)
    old_mods_path = old_mods_root / datetime.now().strftime("%Y%m%d_%H%M%S")
    moved_any = False
    for mod_path in sorted(path for path in mods_path.iterdir() if path.is_dir()):
        if mod_path.name.startswith("[NODELETE]"):
            preserved.append(mod_path.name)
            continue
        try:
            old_mods_path.mkdir(parents=True, exist_ok=True)
            mod_path.rename(_unique_mod_path(old_mods_path, mod_path.name))
            moved_any = True
        except Exception as exc:
            warnings.append(f"{mod_path.name}: failed to move existing mod folder aside: {exc}")
    return preserved, old_mods_path if moved_any else None


def _cleanup_old_mods(old_mods_path: Path | None, warnings: list[str]) -> None:
    if old_mods_path is None or not old_mods_path.exists():
        return
    try:
        _remove_tree(old_mods_path)
        parent = old_mods_path.parent
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
    except Exception as exc:
        warnings.append(f"old mods cleanup failed for {old_mods_path}: {exc}")


def _remove_tree(path: Path) -> None:
    # Retry failed deletes after clearing read-only.
    def _retry_writeable(function, failed_path, exc_info):
        try:
            os.chmod(failed_path, stat.S_IWRITE)
            function(failed_path)
        except Exception:
            raise exc_info[1]

    shutil.rmtree(path, onerror=_retry_writeable)


def _unique_mod_path(mods_path: Path, name: str) -> Path:
    candidate = mods_path / name
    suffix = 2
    while candidate.exists():
        candidate = mods_path / f"{name} ({suffix})"
        suffix += 1
    return candidate


def _install_mod(mod: dict, downloads_path: Path, mods_path: Path, warnings: list[str], progress, index: int, total: int):
    mod_name = str(mod.get("mod_name") or "").strip()
    archive_service = ArchiveService(downloads_path)
    archive_name = archive_service.expected_archive_name(mod)
    selected_archive_name = archive_name
    mod_path = mods_path / mod_name
    if mod_path.exists():
        old_mods_path = mods_path.parent / "_kotorganizer_sync_old_mods" / f"reinstall_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        old_mods_path.mkdir(parents=True, exist_ok=True)
        try:
            mod_path.rename(_unique_mod_path(old_mods_path, mod_name))
            _cleanup_old_mods(old_mods_path, warnings)
        except Exception as exc:
            warnings.append(f"{mod_name}: failed to move existing mod folder aside: {exc}")
            return
    mod_path.mkdir(parents=True, exist_ok=True)

    if not selected_archive_name:
        return

    archive_path = archive_service.resolve_archive_path_for_mod(mod)
    if archive_path is None:
        warnings.append(f"{mod_name}: archive not found: {selected_archive_name}")
        return

    with tempfile.TemporaryDirectory(prefix="kotorganizer_sync_") as temp_dir:
        extract_path = Path(temp_dir)
        if progress:
            progress(index, total, mod_name, f"Extracting {archive_path.name}")
        if not _extract_archive(archive_path, extract_path):
            warnings.append(f"{mod_name}: failed to extract archive: {archive_path.name}")
            return
        if progress:
            progress(index, total, mod_name, "Applying KSON actions")
        _apply_actions(mod, extract_path, mod_path, warnings)
        if progress:
            progress(index, total, mod_name, "Writing MO2 metadata")
        _write_mod_meta(mod, mod_path, archive_path.name)
        _mark_archive_meta_installed(mod, archive_path, mod_name)


def _apply_actions(mod: dict, extract_path: Path, mod_path: Path, warnings: list[str]):
    mod_name = str(mod.get("mod_name") or "").strip()
    for action in mod.get("actions", []):
        action_type = str(action.get("action") or "").strip().lower()
        if action_type == "delete":
            continue
        source = action.get("source")
        destination = action.get("destination")
        if not destination:
            continue
        destination_path = _safe_join(mod_path, str(destination))
        destination_path.parent.mkdir(parents=True, exist_ok=True)

        if action_type in {"move", "rename"}:
            if not source:
                warnings.append(f"{mod_name}: generated file has no payload and cannot be recreated: {destination}")
                continue
            source_path = _safe_join(extract_path, str(source))
            if source_path.exists():
                shutil.copy2(source_path, destination_path)
            else:
                warnings.append(f"{mod_name}: source file missing in archive: {source}")
        elif action_type == "patched":
            if not source:
                warnings.append(f"{mod_name}: patched file has no source: {destination}")
                continue
            source_path = _safe_join(extract_path, str(source))
            if not source_path.exists():
                warnings.append(f"{mod_name}: patch source missing in archive: {source}")
                continue
            patched = _apply_binary_patch(source_path.read_bytes(), action.get("patch") or {})
            destination_path.write_bytes(patched)
        else:
            warnings.append(f"{mod_name}: unsupported action '{action_type}' for {destination}")


# Apply one binary patch payload.
def _apply_binary_patch(source: bytes, patch: dict) -> bytes:
    if patch.get("format") == "multi-span-binary-v1":
        result = bytearray(source)
        shift = 0
        for operation in patch.get("operations", []):
            offset = int(operation.get("offset", 0) or 0) + shift
            delete_count = int(operation.get("delete", 0) or 0)
            insert = base64.b64decode(str(operation.get("insert_base64") or ""))
            result[offset:offset + delete_count] = insert
            shift += len(insert) - delete_count
        return bytes(result)
    if patch.get("format") == "single-span-binary-v1":
        prefix = int(patch.get("prefix", 0) or 0)
        source_suffix = int(patch.get("source_suffix", 0) or 0)
        replacement = base64.b64decode(str(patch.get("replacement_base64") or ""))
        suffix = source[len(source) - source_suffix:] if source_suffix else b""
        return source[:prefix] + replacement + suffix
    return source


# Write the MO2 modlist.
def _write_modlist(profile_path: Path, mods: list[dict], preserved_mods: list[str]):
    profile_path.mkdir(parents=True, exist_ok=True)
    lines = ["# This file was automatically generated by Mod Organizer."]
    for mod_name in sorted(preserved_mods, key=str.casefold):
        lines.append(f"+{mod_name}")
    for mod in mods:
        name = str(mod.get("mod_name") or "").strip()
        enabled = bool(mod.get("enabled", True))
        lines.append(f"{'+' if enabled else '-'}{name}")
    (profile_path / "modlist.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


# Write MO2 metadata for one mod.
def _write_mod_meta(mod: dict, mod_path: Path, archive_name: str):
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    meta_path = mod_path / "meta.ini"
    if meta_path.exists():
        parser.read(meta_path, encoding="utf-8")
    if not parser.has_section("General"):
        parser.add_section("General")

    fields = {
        "installationFile": archive_name,
        "modName": str(mod.get("mod_name") or "").strip(),
        "buildName": str(mod.get("mod_name") or "").strip(),
        "buildURL": str(mod.get("url") or "").strip(),
        "manualURL": str(mod.get("url") or "").strip(),
        "url": str(mod.get("url") or "").strip(),
        "repository": str(mod.get("repository") or "").strip(),
        "version": str(mod.get("version") or "").strip(),
        "newestVersion": str(mod.get("version") or "").strip(),
        "ArchiveReleaseDate": str(mod.get("release_date") or "").strip(),
        "modID": str(mod.get("mod_id") or mod.get("modID") or "").strip(),
        "fileID": str(mod.get("file_id") or mod.get("fileID") or "").strip(),
    }
    for key, value in fields.items():
        if value:
            parser.set("General", key, value)
    with meta_path.open("w", encoding="utf-8") as handle:
        parser.write(handle, space_around_delimiters=False)


# Mark an archive as installed.
def _mark_archive_meta_installed(mod: dict, archive_path: Path, mod_name: str):
    meta_path = archive_path.with_name(f"{archive_path.name}.meta")
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    if meta_path.exists():
        parser.read(meta_path, encoding="utf-8")
    if not parser.has_section("General"):
        parser.add_section("General")

    parser.set("General", "installed", "true")
    parser.set("General", "uninstalled", "false")
    parser.set("General", "removed", "false")
    parser.set("General", "modName", mod_name)
    url = str(mod.get("url") or "").strip()
    if url:
        parser.set("General", "manualURL", url)
        parser.set("General", "url", url)
    for source, target in (
        ("version", "version"),
        ("version", "newestVersion"),
        ("release_date", "ArchiveReleaseDate"),
        ("repository", "repository"),
        ("mod_id", "modID"),
        ("file_id", "fileID"),
    ):
        value = str(mod.get(source) or "").strip()
        if value:
            parser.set("General", target, value)
    with meta_path.open("w", encoding="utf-8") as handle:
        parser.write(handle, space_around_delimiters=False)

# Extract one archive.
def _extract_archive(archive_path: Path, output_path: Path) -> bool:
    exe = _seven_zip_exe()
    if not exe:
        return False
    result = subprocess.run(
        [exe, "x", "-y", f"-o{output_path}", str(archive_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        startupinfo=SUBPROCESS_STARTUPINFO,
        creationflags=SUBPROCESS_CREATIONFLAGS,
    )
    return result.returncode == 0


# Find the bundled 7-Zip executable.
def _seven_zip_exe() -> str:
    plugin_dir = Path(__file__).resolve().parent
    exe = plugin_dir / "7z.exe"
    dll = plugin_dir / "7z.dll"
    return str(exe) if exe.exists() and dll.exists() else ""


# Join a path safely under root.
def _safe_join(root: Path, relative_path: str) -> Path:
    resolved = (root / relative_path.replace("\\", "/")).resolve()
    root_resolved = root.resolve()
    if root_resolved != resolved and root_resolved not in resolved.parents:
        raise ValueError(f"Unsafe path outside target folder: {relative_path}")
    return resolved
