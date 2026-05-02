import os
import shutil
import subprocess
import zlib
from functools import lru_cache
from pathlib import Path


SUBPROCESS_STARTUPINFO = None
SUBPROCESS_CREATIONFLAGS = 0
if os.name == "nt":
    SUBPROCESS_STARTUPINFO = subprocess.STARTUPINFO()
    SUBPROCESS_STARTUPINFO.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    SUBPROCESS_CREATIONFLAGS = subprocess.CREATE_NO_WINDOW


# Hash one file with xxhsum when available.
def file_hash(path: Path) -> str:
    exe = xxhsum_exe()
    if exe:
        result = subprocess.run(
            [exe, "-H3", str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            startupinfo=SUBPROCESS_STARTUPINFO,
            creationflags=SUBPROCESS_CREATIONFLAGS,
        )
        if result.returncode == 0:
            return parse_xxhsum_output(result.stdout)
    return xxh3_bytes(path.read_bytes())


# Hash bytes with XXH3 or a CRC fallback.
def xxh3_bytes(data: bytes) -> str:
    exe = xxhsum_exe()
    if exe:
        result = subprocess.run(
            [exe, "-H3", "-"],
            input=data,
            capture_output=True,
            check=False,
            startupinfo=SUBPROCESS_STARTUPINFO,
            creationflags=SUBPROCESS_CREATIONFLAGS,
        )
        if result.returncode == 0:
            return parse_xxhsum_output(result.stdout.decode("utf-8", errors="replace"))
    return f"crc32:{zlib.crc32(data) & 0xFFFFFFFF:08x}"


# Find the bundled or system xxhsum executable.
@lru_cache(maxsize=1)
def xxhsum_exe() -> str:
    for parent in (Path(__file__).resolve().parent, *Path(__file__).resolve().parents):
        for name in ("xxhsum.exe", "xxhsun.exe"):
            candidate = parent / name
            if candidate.exists():
                return str(candidate)
    exe = shutil.which("xxhsum") or shutil.which("xxhsum.exe") or shutil.which("xxhsun.exe")
    return exe or ""


# Parse the hash value from xxhsum output.
def parse_xxhsum_output(output: str) -> str:
    first = output.strip().split()[0].strip("\\/")
    upper = first.upper()
    for prefix in ("XXH3_", "XXH128_", "XXH64_", "XXH32_"):
        if upper.startswith(prefix):
            return first[len(prefix):].lower()
    return first.lower()
