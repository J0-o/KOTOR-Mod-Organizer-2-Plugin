import configparser
import logging
from dataclasses import dataclass
from pathlib import Path

from tslpatcher_parser import TslPatcherOperation, parse_tslpatcher_ini


logger = logging.getLogger("mobase")


# Hold one parsed patch entry shown in the patcher tab.
@dataclass
class PatchEntry:
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


# Read an INI with common encodings.
def read_ini_with_fallbacks(parser: configparser.ConfigParser, ini_path: Path) -> None:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            parser.read(ini_path, encoding=encoding)
            return
        except UnicodeError as exc:
            last_error = exc
    if last_error:
        raise last_error


# Find the TSLPatcher data folder.
def find_patch_dir(mod_path: Path) -> Path | None:
    for name in ("tslpatchdata", "TSLPatcherData", "patchdata"):
        candidate = mod_path / name
        if candidate.exists() and candidate.is_dir():
            return candidate
    return None


# Collect all patch entries from mods.
def collect_patch_entries(
    mods_root: Path,
    profile_order: list[str],
    enabled_state: dict[tuple[str, str], bool],
    active_state: dict[str, bool],
) -> list[PatchEntry]:
    if not mods_root.exists():
        return []

    order_index = {name: index for index, name in enumerate(profile_order)}
    patch_mods = [
        mod_path
        for mod_path in mods_root.iterdir()
        if mod_path.is_dir() and find_patch_dir(mod_path) is not None
    ]
    patch_mods.sort(key=lambda path: order_index.get(path.name, -1), reverse=True)

    entries: list[PatchEntry] = []
    for mod_path in patch_mods:
        patch_dir = find_patch_dir(mod_path)
        if patch_dir is None:
            continue

        namespaces_ini = patch_dir / "namespaces.ini"
        if namespaces_ini.exists():
            entries.extend(
                _collect_namespaced_entries(mod_path, patch_dir, namespaces_ini, order_index, enabled_state, active_state)
            )
        else:
            default_entry = _collect_default_entry(mod_path, patch_dir, order_index, enabled_state, active_state)
            if default_entry is not None:
                entries.append(default_entry)
    return entries


# Collect namespaced patch entries.
def _collect_namespaced_entries(
    mod_path: Path,
    patch_dir: Path,
    namespaces_ini: Path,
    order_index: dict[str, int],
    enabled_state: dict[tuple[str, str], bool],
    active_state: dict[str, bool],
) -> list[PatchEntry]:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    try:
        read_ini_with_fallbacks(parser, namespaces_ini)
    except Exception as exc:
        logger.warning("[KOTOR2] Failed to read namespaces.ini for %s: %s", mod_path.name, exc)
        return []

    if not parser.has_section("Namespaces"):
        return []

    entries: list[PatchEntry] = []
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
        ini_path = _find_ini_path(patch_dir, final_path, ini_name)
        if ini_path is None:
            continue

        parsed = parse_tslpatcher_ini(ini_path)
        entries.append(
            PatchEntry(
                enabled=enabled_state.get((mod_path.name, ns_name), active_state.get(mod_path.name, False)),
                priority=order_index.get(mod_path.name, -1),
                mod_name=mod_path.name,
                patch_name=ns_name,
                description=description or parsed.description,
                ini_short_path=str(ini_path.relative_to(patch_dir).as_posix()),
                destination="; ".join(parsed.destinations),
                install_paths="; ".join(parsed.install_paths),
                files="; ".join(parsed.files),
                required="; ".join(parsed.required),
                operations=parsed.operations,
            )
        )
    return entries


# Collect the default patch entry.
def _collect_default_entry(
    mod_path: Path,
    patch_dir: Path,
    order_index: dict[str, int],
    enabled_state: dict[tuple[str, str], bool],
    active_state: dict[str, bool],
) -> PatchEntry | None:
    ini_path = patch_dir / "changes.ini"
    if not ini_path.exists():
        return None
    parsed = parse_tslpatcher_ini(ini_path)
    return PatchEntry(
        enabled=enabled_state.get((mod_path.name, "Default"), active_state.get(mod_path.name, False)),
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


# Find the INI file for a patch.
def _find_ini_path(patch_dir: Path, final_path: Path, ini_name: str) -> Path | None:
    ini_candidates: list[Path] = []
    if ini_name:
        ini_candidates.extend(
            [
                final_path / ini_name,
                patch_dir / ini_name,
            ]
        )
    ini_candidates.extend(
        [
            final_path / "changes.ini",
            patch_dir / "changes.ini",
        ]
    )
    return next((candidate for candidate in ini_candidates if candidate.exists()), None)
