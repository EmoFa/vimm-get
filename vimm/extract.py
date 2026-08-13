"""
Archive extraction for downloaded games.

Vimm serves everything zipped (.zip for cartridge systems, .7z for the large
disc systems). Extraction lands next to the archive; on success the archive
is deleted along with any .txt files that came out of it (the site includes
an info text in some archives). On failure the archive is left untouched so
the operation can be retried.
"""

from __future__ import annotations

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

    if archive.suffix.lower() == ".zip":
        extracted = _extract_zip(archive, dest, progress)
    elif archive.suffix.lower() == ".7z":
        extracted = _extract_7z(archive, dest, progress, poll_interval)
    else:
        raise VimmError(f"not an archive I can extract: {archive.name}")

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


def _extract_zip(archive: Path, dest: Path, progress) -> list[Path]:
    written: list[Path] = []
    try:
        with zipfile.ZipFile(archive) as zf:
            entries = [i for i in zf.infolist() if not i.is_dir()]
            total = sum(i.file_size for i in entries) or 1
            done = 0
            for info in entries:
                target = _safe_member_path(dest, info.filename)
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as out:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(min(done, total), total)
                written.append(target)
        return written
    except (zipfile.BadZipFile, OSError) as exc:
        _cleanup_partial(written)
        raise VimmError(f"extraction failed: {exc}") from exc


def _extract_7z(archive: Path, dest: Path, progress,
                poll_interval: float = 0.3) -> list[Path]:
    if py7zr is None:
        raise VimmError("7z support needs py7zr:  py -m pip install py7zr")
    written: list[Path] = []
    try:
        with py7zr.SevenZipFile(archive) as zf:
            entries = [e for e in zf.list() if not e.is_directory]
            names = [e.filename for e in entries]
            total = sum(e.uncompressed for e in entries) or 1
            for name in names:
                _safe_member_path(dest, name)  # traversal check up front

            # extractall() is one long blocking call. py7zr's own callback
            # only fires once per archive member (measured), so a single
            # multi-gigabyte disc image - the common case here - would report
            # nothing at all until it finished. Watching the output files
            # grow gives real progress whatever the archive's shape.
            targets = [dest / name for name in names]
            with _SizeWatcher(progress, targets, total, poll_interval):
                zf.extractall(path=dest)

            for name in names:
                target = dest / name
                if target.is_file():
                    written.append(target)
        if progress:
            progress(total, total)
        return written
    except (py7zr.exceptions.ArchiveError, OSError) as exc:
        _cleanup_partial(written)
        raise VimmError(f"extraction failed: {exc}") from exc


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
