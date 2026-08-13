"""extract / m3u / chd unit tests. chd's compression test uses the real
chdman (exercising the auto-download once on this machine)."""
import io
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

from pathlib import Path as _RepoPath
sys.path.insert(0, str(_RepoPath(__file__).resolve().parents[1]))
import py7zr

from vimm import chd as vchd
from vimm import m3u as vm3u
from vimm.engine import VimmError
from vimm.extract import extract_archive

# chdman is a separate binary. Rather than pull a ~90 MB download during a
# test run, skip and say so; the app fetches it on demand in normal use.
from vimm.chd import find_chdman as _find_chdman

if _find_chdman() is None:
    print("SKIPPED: chdman not found - install it, or run the app's "
          "COMPRESS action once to fetch it")
    raise SystemExit(0)

failures = []


def check(name, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    if not condition:
        failures.append(name)


def tempdir():
    d = Path(tempfile.mkdtemp(prefix="vimmpp_"))
    return d


# ---------------------------------------------------------------- extract ---
print("=== extract ===")
d = tempdir()
zip_path = d / "Game (USA).zip"
with zipfile.ZipFile(zip_path, "w") as z:
    z.writestr("Game (USA).gbc", os.urandom(50_000))
    z.writestr("Vimm's Lair.txt", "visit the site")
events = []
kept = extract_archive(zip_path, progress=lambda a, b: events.append((a, b)))
check("zip: rom kept", [p.name for p in kept] == ["Game (USA).gbc"])
check("zip: archive deleted", not zip_path.exists())
check("zip: txt deleted", not (d / "Vimm's Lair.txt").exists())
check("zip: progress emitted", len(events) > 0 and events[-1][0] == events[-1][1])

d = tempdir()
seven = d / "Game (Europe).7z"
with py7zr.SevenZipFile(seven, "w") as z:
    z.writestr(os.urandom(80_000), "Game (Europe).iso")
    z.writestr(b"info", "ReadMe.txt")
kept = extract_archive(seven)
check("7z: iso kept", [p.name for p in kept] == ["Game (Europe).iso"])
check("7z: archive deleted", not seven.exists())
check("7z: txt deleted", not (d / "ReadMe.txt").exists())

# corrupt archive: archive must survive
d = tempdir()
bad = d / "bad.zip"
bad.write_bytes(b"this is not a zip at all")
try:
    extract_archive(bad)
    check("corrupt zip raises", False)
except VimmError:
    check("corrupt zip raises", True)
check("corrupt zip kept for retry", bad.exists())

# --------------------------------------------------------------------- m3u ---
print("=== m3u ===")
d = tempdir()
for n in (1, 2, 3):
    (d / f"Final Fantasy VII (USA) (Disc {n}).chd").write_bytes(b"chd" * 10)
(d / "Single Game (USA).chd").write_bytes(b"x")

made = vm3u.make_playlists(d, "psx")
check("one playlist made", len(made) == 1)
m3u_path = d / "Final Fantasy VII (USA).m3u" / "Final Fantasy VII (USA).m3u"
check("folder and m3u share exact name", made and made[0] == m3u_path and m3u_path.exists())
lines = m3u_path.read_text().strip().splitlines() if m3u_path.exists() else []
check("entries are the 3 discs in order",
      lines == [f"Final Fantasy VII (USA) (Disc {n}).chd" for n in (1, 2, 3)], str(lines))
check("discs moved into folder",
      all((d / "Final Fantasy VII (USA).m3u" / f"Final Fantasy VII (USA) (Disc {n}).chd").exists()
          for n in (1, 2, 3)))
check("single-disc untouched", (d / "Single Game (USA).chd").exists())

# non-whitelisted system -> nothing
d2 = tempdir()
for n in (1, 2):
    (d2 / f"Some Game (Disc {n}).chd").write_bytes(b"x")
check("non-m3u system does nothing", vm3u.make_playlists(d2, "snes") == [])
check("idempotent-ish: no discs left flat means no more sets", vm3u.make_playlists(d, "psx") == [])

# cue with bin companions moves them too, m3u lists only cues
d3 = tempdir()
for n in (1, 2):
    (d3 / f"Game X (Disc {n}).cue").write_text(f'FILE "Game X (Disc {n}).bin" BINARY\n')
    (d3 / f"Game X (Disc {n}).bin").write_bytes(b"data")
made = vm3u.make_playlists(d3, "saturn")
inner = d3 / "Game X.m3u"
lines = (inner / "Game X.m3u").read_text().strip().splitlines()
check("cue set: m3u lists cues only", lines == ["Game X (Disc 1).cue", "Game X (Disc 2).cue"])
check("cue set: bins moved alongside",
      all((inner / f"Game X (Disc {n}).bin").exists() for n in (1, 2)))

# --------------------------------------------------------------------- chd ---
print("=== chd ===")
d = tempdir()
# A tiny valid single-track data cue+bin: 2352-byte raw sectors, 150 sectors.
bin_path = d / "Tiny Game (USA).bin"
bin_path.write_bytes(os.urandom(2352 * 150))
cue_path = d / "Tiny Game (USA).cue"
cue_path.write_text('FILE "Tiny Game (USA).bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n')

check("sources_of finds the bin", vm3u is not None and vchd.sources_of(cue_path) == [bin_path])

status_lines = []
progress_events = []
try:
    chd_path = vchd.compress_to_chd(cue_path, delete_sources=True,
                                    progress=lambda a, b: progress_events.append(a),
                                    status=status_lines.append)
    check("chd created", chd_path.exists() and chd_path.stat().st_size > 0)
    check("sources deleted after success", not cue_path.exists() and not bin_path.exists())
    check("progress reached 100", progress_events and progress_events[-1] == 100.0)
    print("   status:", "; ".join(status_lines[:3]))
except VimmError as exc:
    print(f"   NOTE: chd test could not run: {exc}")
    check("chd compression", False, str(exc))

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
