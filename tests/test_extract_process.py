"""Extraction happens in a child process, and still behaves identically.

py7zr is Python, so unpacking here would hold the GIL against the download
loop - measured at half the download throughput and 20x worse chunk gaps,
which is the stutter Nolan noticed. Doing it elsewhere costs 5%. These
checks pin the mechanism in place and prove nothing else changed.
"""
import os
import sys
import tempfile
import zipfile
from pathlib import Path

from pathlib import Path as _RepoPath
sys.path.insert(0, str(_RepoPath(__file__).resolve().parents[1]))

from vimm import extract as ex
from vimm.engine import VimmError

try:
    import py7zr
except ImportError:
    py7zr = None

failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    if not ok:
        failures.append(name)


def tempdir():
    return Path(tempfile.mkdtemp(prefix="vimmxp_"))


def make_zip(folder: Path, name="Game (USA).zip", wrap="", extras=()):
    archive = folder / name
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{wrap}Game (USA).bin", os.urandom(200_000))
        for extra in extras:
            z.writestr(f"{wrap}{extra}", "info")
    return archive


# ============================================================ out of process
print("=== the decompression runs in a child process ===")
d = tempdir()
archive = make_zip(d)

calls = []
real_spawn = ex._spawn_extract


def spy(a, dest):
    calls.append((a, dest))
    return real_spawn(a, dest)


ex._spawn_extract = spy
try:
    kept = ex.extract_archive(archive)
finally:
    ex._spawn_extract = real_spawn

check("a child was spawned", len(calls) == 1, str(calls))
check("it is a separate Python interpreter", Path(sys.executable).is_file())
check("the file came out", [p.name for p in kept] == ["Game (USA).bin"],
      str([p.name for p in kept]))
check("and the archive was removed", not archive.exists())

# The point of the exercise, asserted directly: no decompression may run on
# the calling thread, because that is what held the GIL against downloads.
here = []
real_extractall = zipfile.ZipFile.extractall
zipfile.ZipFile.extractall = lambda self, *a, **k: here.append(1)
try:
    d1b = tempdir()
    ex.extract_archive(make_zip(d1b))
finally:
    zipfile.ZipFile.extractall = real_extractall
check("no unpacking happened in this process", here == [], f"{len(here)} calls")
check("the child did the work anyway", (d1b / "Game (USA).bin").is_file())

# ================================================================= identical
print("=== the result is exactly what it was before ===")
d2 = tempdir()
a2 = make_zip(d2, extras=("readme.txt",))
kept2 = ex.extract_archive(a2)
check("the .txt that came out of the archive is dropped",
      [p.name for p in kept2] == ["Game (USA).bin"], str([p.name for p in kept2]))
check("no stray txt left behind", not (d2 / "readme.txt").exists())

print("=== a wrapping folder is still flattened ===")
d3 = tempdir()
a3 = make_zip(d3, wrap="Game (USA)/")
kept3 = ex.extract_archive(a3)
check("the disc sits in the system folder, not a subfolder",
      [p.parent for p in kept3] == [d3], str([str(p) for p in kept3]))
check("the wrapper is gone", not (d3 / "Game (USA)").exists())

progress_seen = []
print("=== progress is still reported ===")
d4 = tempdir()
a4 = make_zip(d4)
ex.extract_archive(a4, progress=lambda done, total: progress_seen.append((done, total)),
                   poll_interval=0.01)
check("progress was reported, ending at 100%",
      bool(progress_seen) and progress_seen[-1][0] == progress_seen[-1][1],
      str(progress_seen[-1:]))

# ==================================================================== 7z too
if py7zr is not None:
    print("=== 7z goes the same way ===")
    d5 = tempdir()
    payload = d5 / "src"
    payload.mkdir()
    (payload / "Game (USA).bin").write_bytes(os.urandom(200_000))
    a5 = d5 / "Game (USA).7z"
    with py7zr.SevenZipFile(a5, "w") as z:
        z.writeall(payload / "Game (USA).bin", "Game (USA).bin")
    calls.clear()
    ex._spawn_extract = spy
    try:
        kept5 = ex.extract_archive(a5)
    finally:
        ex._spawn_extract = real_spawn
    check("a child was spawned for the 7z", len(calls) == 1, str(len(calls)))
    check("the disc came out", [p.name for p in kept5] == ["Game (USA).bin"],
          str([p.name for p in kept5]))
    check("and the archive was removed", not a5.exists())

# ================================================================= fallback
print("=== if the child cannot be started, it still extracts ===")
d6 = tempdir()
a6 = make_zip(d6)


def cannot_spawn(a, dest):
    raise OSError("no interpreter here")


ex._spawn_extract = cannot_spawn
try:
    kept6 = ex.extract_archive(a6)
finally:
    ex._spawn_extract = real_spawn
check("extraction still succeeded in-process",
      [p.name for p in kept6] == ["Game (USA).bin"], str([p.name for p in kept6]))
check("the archive was still removed", not a6.exists())

# ============================================================ failure paths
print("=== a broken archive fails cleanly and keeps the archive ===")
d7 = tempdir()
a7 = d7 / "Broken (USA).zip"
a7.write_bytes(b"this is not a zip file at all")
try:
    ex.extract_archive(a7)
    check("a corrupt archive raises", False, "no error raised")
except VimmError as exc:
    check("a corrupt archive raises", "extraction failed" in str(exc), str(exc))
check("the corrupt archive is left for a retry", a7.is_file())

print("=== a member escaping the folder is refused ===")
d8 = tempdir()
a8 = d8 / "Evil (USA).zip"
with zipfile.ZipFile(a8, "w") as z:
    z.writestr("../escaped.txt", "nope")
outside = d8.parent / "escaped.txt"
outside.unlink(missing_ok=True)
try:
    ex.extract_archive(a8)
    check("traversal is refused", False, "no error raised")
except VimmError as exc:
    check("traversal is refused", "escapes destination" in str(exc), str(exc))
check("and nothing was written outside the folder", not outside.exists())
check("the archive is untouched", a8.is_file())

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
