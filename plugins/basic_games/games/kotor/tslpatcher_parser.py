import configparser
from dataclasses import dataclass
from pathlib import Path


# Represent one parsed TSLPatcher operation.
@dataclass(frozen=True)
class TslPatcherOperation:
    resource_type: str
    action: str
    target: str
    location: str
    scope: tuple[str, ...]
    source_section: str

    # Build stable conflict keys for one parsed operation.
    def conflict_keys(self) -> tuple[str, ...]:
        if self.resource_type == "file" and self.action != "replace":
            return tuple()
        base_resource = self.action if self.resource_type == "file" else self.resource_type
        base = f"{base_resource}:{self.target}"
        if self.resource_type in {"file", "compile", "tlk", "ssf"}:
            return (base,)
        if self.resource_type == "2da":
            row_scopes = [
                scope
                for scope in self.scope
                if scope.startswith(
                    (
                        "rowlabel=",
                        "rowindex=",
                        "labelindex=",
                        "exclusivecolumn=",
                        "rowvalue=",
                        "rowsection=",
                        "2damemory=",
                    )
                )
            ]
            col_scopes = [scope for scope in self.scope if scope.startswith("col=")]
            action_scopes = [scope for scope in self.scope if scope.startswith(("addrow", "copyrow", "changerow", "modifyrow"))]
            row_scope = "|".join(row_scopes)
            action_scope = "|".join(action_scopes)
            if row_scope and col_scopes:
                return tuple(f"{base}:{action_scope}:{row_scope}:{col_scope}" for col_scope in col_scopes)
            if row_scope:
                return (f"{base}:{action_scope}:{row_scope}",)
            return (base,)
        if self.resource_type == "gff":
            if self.scope:
                return tuple(f"{base}:{path}" for path in self.scope)
            return (base,)
        if self.scope:
            return (f"{base}:{'|'.join(self.scope)}",)
        return (base,)


# Hold the parsed metadata extracted from one INI file.
@dataclass(frozen=True)
class ParsedIniData:
    description: str
    files: tuple[str, ...]
    install_paths: tuple[str, ...]
    required: tuple[str, ...]
    destinations: tuple[str, ...]
    operations: tuple[TslPatcherOperation, ...]


_SECTION_TYPES = {
    "installlist": "install",
    "compilelist": "compile",
    "tlklist": "tlk",
    "2dalist": "2da",
    "gfflist": "gff",
    "ssflist": "ssf",
}

_GFF_SUFFIXES = {
    ".are", ".dlg", ".git", ".ifo", ".jrl", ".pth", ".utc", ".utd", ".ute", ".uti", ".utm",
    ".utp", ".uts", ".utt", ".utw", ".fac", ".gui", ".res",
}

_IGNORED_SCOPE_KEYS = {
    "label", "exclusivecolumn", "rowlabel", "rowindex", "labelindex", "2damemory", "sortindex",
    "store2da", "copyrow", "addrow", "changerow", "modifyrow", "columnlabel", "index",
    "filename", "file", "replacefile", "saveas", "destination", "!destination", "lookupgamefolder",
}


# Strip trailing TSLPatcher comment markers from a line.
def _strip_ini_comment(line: str) -> str:
    for marker in ("//", ";"):
        idx = line.find(marker)
        if idx != -1:
            return line[:idx].strip()
    return line.strip()


# Normalize a value for case-insensitive comparisons.
def _normalize_entry(value: str) -> str:
    value = value.strip().strip('"').strip("'")
    return value.replace("/", "\\").lower()


# Normalize a path or filename target.
def _normalize_target(value: str) -> str:
    return _normalize_entry(value)


# Detect values that refer to files rather than labels.
def _looks_like_file(value: str) -> bool:
    if not value:
        return False
    if value.isdigit():
        return False
    path = Path(value)
    if path.suffix:
        return True
    filename = path.name.lower()
    return "\\" in value or "/" in value or filename == "dialog.tlk"


# Detect values that look like install destinations.
def _looks_like_install_path(value: str) -> bool:
    if not value or value.isdigit():
        return False
    return not _looks_like_file(value)


# Load the INI while dropping blank and commented lines.
def _iter_clean_lines(ini_path: Path) -> list[str]:
    return [
        trimmed
        for raw_line in ini_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        if (trimmed := _strip_ini_comment(raw_line))
    ]


# Parse the INI with case-preserving keys.
def _parse_config(ini_path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    clean_lines = _iter_clean_lines(ini_path)
    start_index = 0
    for idx, line in enumerate(clean_lines):
        if line.startswith("[") and line.endswith("]"):
            start_index = idx
            break
    parser.read_string("\n".join(clean_lines[start_index:]), source=str(ini_path))
    return parser


# Find a section by name without caring about case.
def _find_section(parser: configparser.ConfigParser, *names: str) -> str | None:
    lower_map = {section.lower(): section for section in parser.sections()}
    for name in names:
        if not name:
            continue
        hit = lower_map.get(name.lower())
        if hit:
            return hit
    return None


# Extract the row selector used by a 2DA patch section.
def _extract_row_selector(parser: configparser.ConfigParser, section_name: str) -> str | None:
    if not parser.has_section(section_name):
        return None
    section = parser[section_name]
    row_label = section.get("RowLabel", "").strip()
    if row_label:
        return f"rowlabel={_normalize_entry(row_label)}"

    row_index = section.get("RowIndex", "").strip()
    if row_index:
        return f"rowindex={_normalize_entry(row_index)}"

    label_index = section.get("LabelIndex", "").strip()
    if label_index:
        return f"labelindex={_normalize_entry(label_index)}"

    exclusive_column = section.get("ExclusiveColumn", "").strip()
    if exclusive_column:
        exclusive_value = section.get(exclusive_column, "").strip()
        if exclusive_value:
            return f"exclusivecolumn={_normalize_entry(exclusive_column)}:{_normalize_entry(exclusive_value)}"
        return f"exclusivecolumn={_normalize_entry(exclusive_column)}"
    return None


# Extract the 2DA columns that a section edits.
def _extract_2da_columns(parser: configparser.ConfigParser, section_name: str) -> tuple[str, ...]:
    if not parser.has_section(section_name):
        return tuple()
    cols: list[str] = []
    for key, value in parser.items(section_name):
        lower_key = key.lower()
        if lower_key in _IGNORED_SCOPE_KEYS:
            continue
        if lower_key.startswith("file") or lower_key.startswith("table"):
            continue
        if lower_key.startswith("replace") or lower_key.startswith("copyrow") or lower_key.startswith("addrow"):
            continue
        if lower_key.startswith("changerow") or lower_key.startswith("modifyrow"):
            continue
        if not value.strip():
            continue
        cols.append(lower_key)
    return tuple(dict.fromkeys(cols).keys())


# Extract a stable row identity for a 2DA patch section.
def _extract_2da_row_identity(
    parser: configparser.ConfigParser, section_name: str, action_key: str | None = None
) -> str | None:
    if not parser.has_section(section_name):
        return None

    selector = _extract_row_selector(parser, section_name)
    if selector:
        return selector

    section = parser[section_name]
    for key in ("label", "name", "resref", "template", "template resref"):
        value = section.get(key, "").strip()
        if value:
            return f"rowvalue={_normalize_entry(key)}:{_normalize_entry(value)}"

    for key, value in section.items():
        lower_key = key.lower()
        if lower_key in _IGNORED_SCOPE_KEYS:
            continue
        if not value.strip():
            continue
        if lower_key in {"label", "name"}:
            return f"rowvalue={lower_key}:{_normalize_entry(value)}"

    if action_key and action_key.lower().startswith(("addrow", "copyrow")):
        return f"rowsection={_normalize_entry(section_name)}"

    for key, value in section.items():
        lower_key = key.lower()
        if lower_key.startswith("2damemory") and value.strip():
            return f"2damemory={_normalize_entry(value)}"

    return None


# Extract edited field paths from a GFF patch section.
def _extract_gff_paths(parser: configparser.ConfigParser, section_name: str) -> tuple[str, ...]:
    if not parser.has_section(section_name):
        return tuple()
    paths: list[str] = []
    for key, value in parser.items(section_name):
        lower_key = key.lower()
        normalized_key = _normalize_entry(key)
        cleaned = _normalize_entry(value)
        if "\\" in normalized_key:
            paths.append(normalized_key)
            continue
        if not cleaned:
            continue
        if "fieldpath" in lower_key or lower_key in {"path", "!fieldpath"}:
            paths.append(cleaned)
        elif lower_key.startswith("label") and "\\" in cleaned:
            paths.append(cleaned)
    return tuple(dict.fromkeys(paths).keys())


# Extract the identity scope for a TLK patch section.
def _extract_tlk_scope(parser: configparser.ConfigParser, key: str, value: str) -> tuple[str, ...]:
    lower_key = key.lower().strip()
    stripped_value = value.strip()
    if lower_key.startswith("strref") and stripped_value.isdigit():
        return (f"strref={_normalize_entry(stripped_value)}",)

    section_name = _find_section(parser, value)
    if not section_name or not parser.has_section(section_name):
        return (f"entry={_normalize_entry(value)}",)

    section = parser[section_name]
    strref = section.get("StrRef", "").strip()
    if strref:
        return (f"strref={_normalize_entry(strref)}",)

    token = section.get("2DAMEMORY", "").strip() or section.get("TLKMemory", "").strip()
    if token:
        return (f"append-token={_normalize_entry(token)}",)

    text = section.get("Text", "").strip()
    sound = section.get("Sound", "").strip()
    if text or sound:
        return (f"append={_normalize_entry(section_name)}",)

    return (f"entry={_normalize_entry(section_name)}",)


# Choose the file target referenced by a list entry.
def _target_from_list_value(key: str, value: str, expected_suffixes: set[str] | None = None) -> str:
    key_norm = _normalize_target(key)
    value_norm = _normalize_target(value)
    for candidate in (value_norm, key_norm):
        if not candidate:
            continue
        if expected_suffixes is None:
            if _looks_like_file(candidate):
                return candidate
        elif Path(candidate).suffix.lower() in expected_suffixes:
            return candidate
    return value_norm or key_norm


# Join an install location and file target into one key.
def _join_location_target(location: str, target: str) -> str:
    location = _normalize_target(location)
    target = _normalize_target(target)
    if not location:
        return target
    if not target:
        return location
    return f"{location}::{target}"


# Parse install folder and destination directives from the INI.
def _parse_install_folder_and_destination(parser: configparser.ConfigParser) -> tuple[tuple[str, ...], tuple[str, ...]]:
    install_paths: list[str] = []
    destinations: list[str] = []
    for section_name in parser.sections():
        for key, value in parser.items(section_name):
            lower_key = key.lower()
            normalized = _normalize_entry(value)
            if not normalized:
                continue
            if lower_key.startswith("install_folder") and _looks_like_install_path(value.strip()):
                install_paths.append(value.strip())
            elif lower_key == "!destination" and _looks_like_install_path(normalized):
                destinations.append(normalized)
    return tuple(dict.fromkeys(install_paths).keys()), tuple(dict.fromkeys(destinations).keys())


# Parse Required entries from the INI.
def _parse_required(parser: configparser.ConfigParser) -> tuple[str, ...]:
    required: list[str] = []
    for section_name in parser.sections():
        for key, value in parser.items(section_name):
            if key.lower() == "required":
                normalized = _normalize_entry(value)
                if _looks_like_file(normalized):
                    required.append(normalized)
    return tuple(dict.fromkeys(required).keys())


# Parse all supported patch operations from the INI.
def _parse_operations(parser: configparser.ConfigParser) -> tuple[TslPatcherOperation, ...]:
    operations: list[TslPatcherOperation] = []

    for section_name in parser.sections():
        section_type = _SECTION_TYPES.get(section_name.lower())
        if section_type is None:
            continue

        if section_type == "tlk":
            for key, value in parser.items(section_name):
                if not value.strip():
                    continue
                operations.append(
                    TslPatcherOperation(
                        resource_type="tlk",
                        action="patch",
                        target="dialog.tlk",
                        location="global",
                        scope=_extract_tlk_scope(parser, key, value),
                        source_section=section_name,
                    )
                )
            continue

        for key, value in parser.items(section_name):
            if not value.strip():
                continue

            if section_type == "install":
                install_location = ""
                if key.lower().startswith("install_folder"):
                    install_location = _normalize_target(value)
                    referenced_section = _find_section(parser, key)
                    if referenced_section and parser.has_section(referenced_section):
                        for inner_key, inner_value in parser.items(referenced_section):
                            if not inner_value.strip():
                                continue
                            lower_inner = inner_key.lower()
                            target = _target_from_list_value(inner_key, inner_value)
                            if lower_inner.startswith(("replace", "file")) and _looks_like_file(target):
                                action = "replace" if lower_inner.startswith("replace") else "install"
                                operations.append(
                                    TslPatcherOperation(
                                        "file",
                                        action,
                                        _join_location_target(install_location, target),
                                        install_location or "override",
                                        tuple(),
                                        referenced_section,
                                    )
                                )
                        continue
                target = _target_from_list_value(key, value)
                if _looks_like_file(target):
                    action = "replace" if key.lower().startswith("replace") else "install"
                    operations.append(
                        TslPatcherOperation(
                            "file",
                            action,
                            _join_location_target(install_location, target),
                            install_location or "override",
                            tuple(),
                            section_name,
                        )
                    )
                continue

            if section_type == "compile":
                target = _target_from_list_value(key, value)
                target_path = Path(target)
                if target_path.suffix.lower() == ".nss":
                    target = str(target_path.with_suffix(".ncs")).replace("/", "\\").lower()
                elif not target_path.suffix:
                    target = f"{target}.ncs"
                operations.append(
                    TslPatcherOperation("compile", "compile", _normalize_target(target), "override", tuple(), section_name)
                )
                continue

            if section_type == "ssf":
                target = _target_from_list_value(key, value, {".ssf"})
                operations.append(
                    TslPatcherOperation("ssf", "patch", target, "override", tuple(), section_name)
                )
                continue

            if section_type == "2da":
                target = _target_from_list_value(key, value, {".2da"})
                data_section = _find_section(parser, value, target)
                if data_section and parser.has_section(data_section):
                    emitted = False
                    for inner_key, inner_value in parser.items(data_section):
                        lower_inner = inner_key.lower()
                        if not (
                            lower_inner.startswith("addrow")
                            or lower_inner.startswith("copyrow")
                            or lower_inner.startswith("changerow")
                            or lower_inner.startswith("modifyrow")
                        ):
                            continue
                        selector_section = _find_section(parser, inner_value)
                        scope_parts: list[str] = [lower_inner]
                        if selector_section:
                            selector = _extract_2da_row_identity(parser, selector_section, lower_inner)
                            columns = _extract_2da_columns(parser, selector_section)
                            if selector:
                                scope_parts.append(selector)
                            if columns:
                                scope_parts.extend(f"col={column}" for column in columns)
                        operations.append(
                            TslPatcherOperation("2da", "patch", target, "override", tuple(scope_parts), data_section)
                        )
                        emitted = True
                    if emitted:
                        continue
                operations.append(
                    TslPatcherOperation("2da", "patch", target, "override", tuple(), section_name)
                )
                continue

            if section_type == "gff":
                target = _target_from_list_value(key, value, _GFF_SUFFIXES)
                data_section = _find_section(parser, value, target)
                paths = _extract_gff_paths(parser, data_section) if data_section else tuple()
                location = "override"
                if data_section and parser.has_section(data_section):
                    destination = parser.get(data_section, "!Destination", fallback="").strip()
                    if destination:
                        location = _normalize_target(destination)
                operations.append(
                    TslPatcherOperation("gff", "patch", target, location, paths, data_section or section_name)
                )
                continue

    return tuple(operations)


# Derive the file list from parsed operations.
def _files_from_operations(operations: tuple[TslPatcherOperation, ...]) -> tuple[str, ...]:
    files: list[str] = []
    for operation in operations:
        if operation.resource_type == "tlk":
            files.append("dialog.tlk")
        else:
            files.append(operation.target)
    return tuple(dict.fromkeys(files).keys())


# Parse one TSLPatcher INI into the data used by the UI.
def parse_tslpatcher_ini(ini_path: Path) -> ParsedIniData:
    if not ini_path.exists():
        return ParsedIniData("", tuple(), tuple(), tuple(), tuple(), tuple())

    parser = _parse_config(ini_path)
    description = parser.get("Settings", "WindowCaption", fallback="").strip()
    install_paths, destinations = _parse_install_folder_and_destination(parser)
    required = _parse_required(parser)
    operations = _parse_operations(parser)
    files = _files_from_operations(operations)

    return ParsedIniData(
        description=description,
        files=files,
        install_paths=install_paths,
        required=required,
        destinations=destinations,
        operations=operations,
    )
