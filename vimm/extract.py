"""
Archive extraction for downloaded games.

Vimm serves everything zipped (.zip for cartridge systems, .7z for the large
disc systems). Extraction lands next to the archive; on success the archive
is deleted along with any .txt files that came out of it (the site includes
an info text in some archives). On failure the archive is left untouched so
the operation can be retried.

The decompression itself runs in a **child process**. py7zr is Python, so
doing it here would hold the GIL against the download loop, which reads the
socket 64 KB at a time and needs the lock for every chunk. Measured over
loopback with an extraction running alongside, that cost the download half
its throughput (2814 -> 1381 MB/s) and pushed its worst chunk gaps from
0.1 ms to 2.2 ms - a visible stutter. The same extraction in another process
cost 5%. chdman never had the problem because it was always a subprocess.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import zipfile
from pathlib import Path

from .engine import VimmError

try:
    import py7zr
except ImportError:  # pragma: no cover - listed in requirements
    py7zr = None

ARCHIVE_SUFFIXES = {".zip", ".7z"}


def is_archive(path: Path) -> bool:
    return path.suffix.lower() in ARCHIVE_SUFFIXES


def extract_archive(archive: Path, progress=None,
                    poll_interval: float = 0.3) -> list[Path]:
    """Extract `archive` into its own directory and clean up.

    Returns the kept files (extracted contents minus deleted .txt files).
    `progress(done_bytes, total_bytes)` is called as the extraction advances.
    On success the archive and extracted .txt files are deleted; on any
    failure the archive stays and partial output is removed.

    `poll_interval` is how often 7z output is measured on disk (see
    `_SizeWatcher`); the default suits multi-gigabyte archives.
    """
    archive = Path(archive)
    if not archive.is_file():
        raise VimmError(f"archive not found: {archive}")
    dest = archive.parent

    suffix = archive.suffix.lower()
    if suffix not in ARCHIVE_SUFFIXES:
        raise VimmError(f"not an archive I can extract: {archive.name}")

    # Reading the index is a header parse, cheap for both formats, and it is
    # what lets the traversal check happen before anything is written and the
    # progress watcher know what to look for.
    names, total = _read_index(archive)
    targets = [_safe_member_path(dest, name) for name in names]

    extracted = _extract(archive, dest, targets, total, progress, poll_interval)

    kept: list[Path] = []
    for path in extracted:
        # The site tucks an info .txt into many archives; nobody wants it in
        # a rom folder. (Only texts that came out of this archive are
        # touched - never pre-existing files.)
        if path.suffix.lower() == ".txt":
            path.unlink(missing_ok=True)
        else:
            kept.append(path)

    kept = _flatten(dest, kept)
    archive.unlink()
    return kept


def _flatten(dest: Path, extracted: list[Path]) -> list[Path]:
    """Lift a single wrapping directory's contents into `dest`.

    Vimm's disc archives wrap their contents in a folder per disc, so a
    multi-disc game would end up as several sibling folders. The CHD and
    playlist steps both expect the discs side by side in the system folder,
    so the wrapper is removed here. Anything that would collide with an
    existing file is left where it is rather than overwriting it.
    """
    tops = set()
    for path in extracted:
        try:
            tops.add(path.relative_to(dest).parts[0])
        except ValueError:
            return extracted
    if len(tops) != 1:
        return extracted

    wrapper = dest / next(iter(tops))
    if not wrapper.is_dir():
        return extracted  # already flat

    moved: list[Path] = []
    for path in extracted:
        target = dest / path.name
        if target.exists():
            moved.append(path)  # name already taken; leave it alone
            continue
        path.rename(target)
        moved.append(target)

    # Drop the wrapper only once nothing of value is left inside it.
    for directory in sorted((p for p in wrapper.rglob("*") if p.is_dir()),
                            reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass
    try:
        wrapper.rmdir()
    except OSError:
        pass  # something unexpected remains; keep it rather than delete it
    return moved


def _cleanup_partial(paths: list[Path]) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _safe_member_path(dest: Path, name: str) -> Path:
    """Refuse traversal: every member must land inside `dest`."""
    target = (dest / name).resolve()
    if not target.is_relative_to(dest.resolve()):
        raise VimmError(f"archive member escapes destination: {name}")
    return target


def _read_index(archive: Path) -> tuple[list[str], int]:
    """Member names and total uncompressed size, from the archive header."""
    try:
        if archive.suffix.lower() == ".zip":
            with zipfile.ZipFile(archive) as zf:
                entries = [i for i in zf.infolist() if not i.is_dir()]
                return ([i.filename for i in entries],
                        sum(i.file_size for i in entries) or 1)
        if py7zr is None:
            raise VimmError("7z support needs py7zr:  py -m pip install py7zr")
        with py7zr.SevenZipFile(archive) as zf:
            entries = [e for e in zf.list() if not e.is_directory]
            return ([e.filename for e in entries],
                    sum(e.uncompressed for e in entries) or 1)
    except VimmError:
        raise
    except Exception as exc:  # noqa: BLE001 - any unreadable archive reads alike
        raise VimmError(f"extraction failed: {exc}") from exc


# Deliberately imports nothing of this package, so it does not care how the
# app is installed or launched - only that `sys.executable` can run Python.
_CHILD = """
import sys, zipfile
archive, dest = sys.argv[1], sys.argv[2]
if archive.lower().endswith(".zip"):
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(dest)
else:
    import py7zr
    with py7zr.SevenZipFile(archive) as zf:
        zf.extractall(path=dest)
"""


def _spawn_extract(archive: Path, dest: Path) -> subprocess.CompletedProcess:
    """Run the decompression in a child process. Separated for testing."""
    return subprocess.run(
        [sys.executable, "-c", _CHILD, str(archive), str(dest)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def _extract(archive: Path, dest: Path, targets: list[Path], total: int,
             progress, poll_interval: float) -> list[Path]:
    """Unpack `archive` into `dest`, out of process, and report progress.

    The watcher polls the output files rather than relying on the archive
    library's callbacks - py7zr reports once per member, so one big disc
    image would report nothing until it finished. Polling the filesystem also
    happens to work perfectly well across a process boundary, which is what
    makes running the decompression elsewhere essentially free.
    """
    try:
        with _SizeWatcher(progress, targets, total, poll_interval):
            try:
                result = _spawn_extract(archive, dest)
            except OSError:
                # No usable interpreter to re-invoke. Falling back keeps the
                # app working; the download just stutters as it used to.
                _extract_here(archive, dest)
            else:
                if result.returncode != 0:
                    detail = (result.stderr or "").strip().splitlines()
                    raise VimmError("extraction failed: "
                                    + (detail[-1] if detail else
                                       f"exit code {result.returncode}"))
    except VimmError:
        _cleanup_partial(targets)
        raise
    except OSError as exc:
        _cleanup_partial(targets)
        raise VimmError(f"extraction failed: {exc}") from exc

    written = [path for path in targets if path.is_file()]
    if progress:
        progress(total, total)
    return written


def _extract_here(archive: Path, dest: Path) -> None:
    """The same work in this process. Only used when spawning is impossible."""
    if archive.suffix.lower() == ".zip":
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)
        return
    if py7zr is None:
        raise VimmError("7z support needs py7zr:  py -m pip install py7zr")
    with py7zr.SevenZipFile(archive) as zf:
        zf.extractall(path=dest)


class _SizeWatcher:
    """Reports extraction progress by watching the output files grow.

    Used as a context manager around a blocking extract call. Polls rather
    than relying on the archive library's callbacks, which are too coarse
    (py7zr reports once per member, so one big file reports nothing until
    it is done). Never reports 100% itself - the caller does that once the
    files are actually complete.
    """

    def __init__(self, progress, targets: list[Path], total: int,
                 interval: float = 0.3):
        self._progress = progress
        self._targets = targets
        self._total = max(total, 1)
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _bytes_on_disk(self) -> int:
        total = 0
        for path in self._targets:
            try:
                total += path.stat().st_size
            except OSError:
                pass  # not created yet, or vanished
        return total

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            done = self._bytes_on_disk()
            # Cap just below completion so the bar never claims to be
            # finished while the extractor is still working.
            self._progress(min(done, self._total - 1), self._total)

    def __enter__(self):
        if self._progress:
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="extract-progress")
            self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        return False
