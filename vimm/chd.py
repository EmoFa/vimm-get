"""
CHD compression via chdman (the MAME project's tool).

chdman is the standard for turning bin+cue / gdi disc images into a single
compressed .chd that emulators read directly. It is a separate binary:

  * looked for in the project's tools/ folder, then on PATH
  * on Windows, auto-downloaded on demand: the official MAME release from
    github.com/mamedev/mame is fetched, its SHA-256 checked against the
    SHA256SUMS published in the same release, and ONLY chdman.exe is
    extracted from it (the release is a self-extracting 7z; we locate the
    embedded archive and pull one file). One-time ~90 MB download.
  * on macOS/Linux there are no official prebuilt binaries; the error
    message gives the one-line package-manager install instead.
"""

from __future__ import annotations

import hashlib
import io
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

import requests

from .engine import VimmError

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
_7Z_MAGIC = b"7z\xbc\xaf\x27\x1c"
_GITHUB_LATEST = "https://api.github.com/repos/mamedev/mame/releases/latest"

# chdman puts two percentages on a progress line and one on its closing
# summary:
#     Compressing, 75.0% complete... (ratio=0.4%)
#     Compression complete ... final ratio = 0.3%
# Matching any percentage picked up the ratio, so the bar leapt backwards to
# it the moment compression finished. Only "N% complete" is progress.
_PCT = re.compile(r"(\d+(?:\.\d+)?)%\s*complete")


def progress_percent(line: str) -> float | None:
    """The completion percentage chdman is reporting, if this line has one."""
    match = _PCT.search(line)
    return float(match.group(1)) if match else None


def find_chdman() -> Path | None:
    exe = "chdman.exe" if sys.platform == "win32" else "chdman"
    local = TOOLS_DIR / exe
    if local.is_file():
        return local
    on_path = shutil.which("chdman")
    return Path(on_path) if on_path else None


def ensure_chdman(progress=None, status=None) -> Path:
    """Return a chdman path, downloading it first on Windows if needed."""
    found = find_chdman()
    if found is not None:
        return found
    if sys.platform != "win32":
        hint = ("brew install rom-tools" if sys.platform == "darwin"
                else "sudo apt install mame-tools   (or dnf install mame-tools)")
        raise VimmError(
            f"chdman not found. Install it with:  {hint}\n"
            f"or place a chdman binary in {TOOLS_DIR}"
        )
    return _download_chdman_windows(progress, status)


def _download_chdman_windows(progress=None, status=None) -> Path:
    def say(text):
        if status:
            status(text)

    say("chdman not found - fetching the official MAME release...")
    release = requests.get(_GITHUB_LATEST, timeout=30).json()
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    tag = release.get("tag_name", "?")
    asset = next((a for a in release.get("assets", [])
                  if a["name"].endswith(f"b_{arch}.exe")), None)
    sums = next((a for a in release.get("assets", [])
                 if a["name"] == "SHA256SUMS"), None)
    if asset is None or sums is None:
        raise VimmError(f"could not find a MAME {arch} build in release {tag}")

    say(f"downloading {asset['name']} ({asset['size'] / 1e6:.0f} MB, one-time)...")
    digest = hashlib.sha256()
    buffer = io.BytesIO()
    with requests.get(asset["browser_download_url"], stream=True, timeout=60) as r:
        r.raise_for_status()
        done = 0
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            buffer.write(chunk)
            digest.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, asset["size"])

    # Verify against the checksums published in the same official release.
    sums_text = requests.get(sums["browser_download_url"], timeout=30).text
    expected = None
    for line in sums_text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == asset["name"]:
            expected = parts[0].lower()
            break
    if expected is None:
        raise VimmError(f"release {tag} has no checksum for {asset['name']}")
    if digest.hexdigest().lower() != expected:
        raise VimmError("MAME download failed its SHA-256 check - not keeping it")
    say("checksum verified - extracting chdman.exe...")

    # The release is a self-extracting 7z: an exe stub with a 7z archive
    # embedded. Find the archive and pull just the one file we need.
    data = buffer.getvalue()
    offset = data.find(_7Z_MAGIC)
    if offset < 0:
        raise VimmError("unexpected MAME archive layout (no embedded 7z found)")

    import py7zr
    TOOLS_DIR.mkdir(exist_ok=True)
    with py7zr.SevenZipFile(io.BytesIO(data[offset:])) as archive:
        names = [n for n in archive.getnames() if Path(n).name.lower() == "chdman.exe"]
        if not names:
            raise VimmError("chdman.exe not present in the MAME archive")
        archive.extract(path=TOOLS_DIR, targets=names[:1])

    extracted = TOOLS_DIR / names[0]
    target = TOOLS_DIR / "chdman.exe"
    if extracted != target:
        extracted.replace(target)
    say(f"chdman ready ({tag})")
    return target


# --------------------------------------------------------------------------
# Compression
# --------------------------------------------------------------------------


# What chdman will take as input here, and which of its subcommands suits
# each. A cue or gdi describes a CD; a bare .iso from PS2 is a DVD image, and
# createcd would refuse it.
CHD_INPUT_SUFFIXES = (".cue", ".gdi", ".iso")


def chdman_subcommand(image: Path) -> str:
    """`createcd` for a cue/gdi sheet, `createdvd` for a bare ISO."""
    return "createdvd" if Path(image).suffix.lower() == ".iso" else "createcd"


def sources_of(sheet: Path) -> list[Path]:
    """The image files a .cue or .gdi sheet references (bin tracks etc.).

    An .iso has no sheet to parse and must be rejected *before* the read.
    Sheets are a few hundred bytes, but a PS2 .iso is gigabytes, and
    decoding one as UTF-8 to discover it lists nothing took 11 seconds for
    1.5 GB - stalling every other thread for 5 of them, because the decode
    holds the GIL. That was the freeze at the end of a conversion.
    """
    if sheet.suffix.lower() not in (".cue", ".gdi"):
        return []
    text = sheet.read_text(encoding="utf-8", errors="replace")
    names: list[str] = []
    if sheet.suffix.lower() == ".cue":
        names = re.findall(r'FILE\s+"([^"]+)"', text, re.I)
        names += [m for m in re.findall(r"FILE\s+(\S+)\s+\w+", text, re.I)
                  if not m.startswith('"')]
    elif sheet.suffix.lower() == ".gdi":
        for line in text.splitlines()[1:]:
            quoted = re.search(r'"([^"]+)"', line)
            if quoted:
                names.append(quoted.group(1))
            else:
                fields = line.split()
                if len(fields) >= 5:
                    names.append(fields[4])
    seen: list[Path] = []
    for name in dict.fromkeys(names):
        path = sheet.parent / name
        if path.is_file():
            seen.append(path)
    return seen


def compress_to_chd(sheet: Path, delete_sources: bool = True,
                    progress=None, status=None) -> Path:
    """bin+cue or gdi -> a single .chd next to the input.

    On success (chdman exit 0) the sheet and its referenced images are
    deleted when `delete_sources` - the .chd replaces them. On failure a
    half-written .chd is removed and sources stay untouched.
    """
    sheet = Path(sheet)
    if sheet.suffix.lower() not in CHD_INPUT_SUFFIXES:
        raise VimmError(
            f"CHD input must be a .cue, .gdi or .iso, got {sheet.name}")
    if not sheet.is_file():
        raise VimmError(f"not found: {sheet}")

    chdman = ensure_chdman(progress=progress, status=status)
    out = sheet.with_suffix(".chd")
    if out.exists():
        raise VimmError(f"already exists: {out.name}")

    subcommand = chdman_subcommand(sheet)
    command = [str(chdman), subcommand, "-i", str(sheet), "-o", str(out)]
    if status:
        status(f"chdman {subcommand} {sheet.name} -> {out.name}")

    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=0,
    )
    tail: list[str] = []
    line = ""
    highest = 0.0
    assert process.stdout is not None
    while True:
        ch = process.stdout.read(1)
        if ch == "":
            break
        if ch in "\r\n":
            if line.strip():
                tail.append(line.strip())
                tail[:] = tail[-5:]
                percent = progress_percent(line)
                # Never let the bar run backwards. Parsing the ratio was the
                # known cause and is fixed above; this makes the symptom
                # impossible even if chdman's wording changes again.
                if percent is not None and progress and percent >= highest:
                    highest = percent
                    progress(percent, 100.0)
            line = ""
        else:
            line += ch
    code = process.wait()

    if code != 0:
        out.unlink(missing_ok=True)
        detail = "; ".join(tail[-2:]) or f"exit code {code}"
        raise VimmError(f"chdman failed: {detail}")

    if delete_sources:
        for source in sources_of(sheet):
            source.unlink(missing_ok=True)
        sheet.unlink(missing_ok=True)
    if progress:
        progress(100.0, 100.0)
    return out


def compressible_among(paths) -> list[Path]:
    """The disc images in `paths` that don't already have a .chd.

    Takes an explicit file list rather than a directory: a system folder holds
    every game for that system, so scanning it would sweep up other games'
    discs.

    `.iso` counts as well as the cue/gdi sheets, for the DVD-based systems -
    only ever reached for systems ticked in the CHD list, and none of the
    others arrive as an .iso.
    """
    sheets = []
    for raw in paths:
        path = Path(raw)
        if (path.suffix.lower() in CHD_INPUT_SUFFIXES and path.is_file()
                and not path.with_suffix(".chd").exists()):
            sheets.append(path)
    return sorted(set(sheets))


def compressible_sheets(folder: Path) -> list[Path]:
    """Every compressible sheet under `folder`.

    Only for whole-folder operations - per-game work must use
    `compressible_among` with that game's own files.
    """
    return compressible_among(folder.rglob("*"))
