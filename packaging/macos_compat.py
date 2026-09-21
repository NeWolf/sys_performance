"""Shared macOS release baseline and Mach-O compatibility checks."""
from pathlib import Path
import platform
import re
import subprocess

MACOS_MIN_VERSION = "14.0"
MACHO_MAGICS = {
    b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca",
}


def version_tuple(value):
    if not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", value):
        raise RuntimeError(f"Invalid macOS version: {value!r}")
    parts = tuple(map(int, value.split(".")))
    return parts + (0,) * (3 - len(parts))


def check_load_commands(text, binary, minimum=MACOS_MIN_VERSION):
    """Check deployment versions, not SDK versions, in the selected CPU slice."""
    versions = []
    for block in re.split(r"Load command \d+", text):
        if re.search(r"\bcmd LC_BUILD_VERSION\b", block):
            target = re.search(r"^\s*platform\s+(\S+)", block, re.MULTILINE)
            if not target or target.group(1).lower() not in ("1", "macos"):
                raise RuntimeError(f"Non-macOS Mach-O platform: {binary}")
            match = re.search(r"^\s*minos\s+(\S+)", block, re.MULTILINE)
        elif re.search(r"\bcmd LC_VERSION_MIN_MACOSX\b", block):
            match = re.search(r"^\s*version\s+(\S+)", block, re.MULTILINE)
        else:
            continue
        if not match:
            raise RuntimeError(f"Missing Mach-O minimum version: {binary}")
        versions.append(match.group(1))
    if not versions:
        raise RuntimeError(f"No macOS deployment version found: {binary}")
    for version in versions:
        if version_tuple(version) > version_tuple(minimum):
            raise RuntimeError(
                f"{binary} requires macOS {version}, exceeding release baseline {minimum}. "
                "Use a compatible Python/dependency build; changing Info.plist or "
                "MACOSX_DEPLOYMENT_TARGET cannot downgrade prebuilt binaries.")


def verify_macos_binaries(payload, minimum=MACOS_MIN_VERSION):
    """Inspect every bundled Mach-O, including Python and extension libraries."""
    arch = platform.machine()
    seen = set()
    count = 0
    for path in sorted(Path(payload).rglob("*")):
        if not path.is_file():
            continue
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        with path.open("rb") as handle:
            if handle.read(4) not in MACHO_MAGICS:
                continue  # Assets and the Android ELF collector are not macOS code.
        subprocess.run(["lipo", str(path), "-verify_arch", arch], check=True,
                       capture_output=True, text=True)
        result = subprocess.run(["otool", "-arch", arch, "-l", str(path)],
                                check=True, capture_output=True, text=True)
        check_load_commands(result.stdout, path, minimum)
        count += 1
    if not count:
        raise RuntimeError(f"No Mach-O binaries found in {payload}")
    print(f"Verified {count} Mach-O binaries for {arch}, macOS {minimum} or earlier")
    return count


def verify_macos_metadata(info):
    if info.get("LSMinimumSystemVersion") != MACOS_MIN_VERSION:
        raise RuntimeError(f"macOS app must declare minimum version {MACOS_MIN_VERSION}")