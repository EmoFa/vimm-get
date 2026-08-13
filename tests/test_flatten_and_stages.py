"""Round-3: extraction flattening, the extract -> compress -> m3u order,
and the resume-by-stem lookup."""
import os
import sys
import tempfile
import zipfile
from pathlib import Path

from pathlib import Path as _RepoPath
sys.path.insert(0, str(_RepoPath(__file__).resolve().parents[1]))
import py7zr

import vimm.server as srv
from vimm import m3u as vm3u
from vimm.engine import find_download
from vimm.extract import extract_archive

failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    if not ok:
        failures.append(name)


def tempdir():
    return Path(tempfile.mkdtemp(prefix="vimmr3_"))


# ------------------------------------------------ 1. extraction flattening
print("=== extraction flattens the per-disc wrapper folder ===")
d = tempdir()
# Vimm wraps each disc's contents in a folder named after the disc.
for disc in (1, 2):
    archive = d / f"Skies of Arcadia (USA) (Disc {disc}).7z"
    with py7zr.SevenZipFile(archive, "w") as z:
        inner = f"Skies of Arcadia (USA) (Disc {disc})"
        z.writestr(os.urandom(20_000), f"{inner}/Disc {disc}.bin")
        z.writestr(b'FILE "Disc %d.bin" BINARY\n' % disc, f"{inner}/Disc {disc}.cue")
        z.writestr(b"info", f"{inner}/readme.txt")
    extract_archive(archive, poll_interval=0.02)

names = sorted(p.name for p in d.iterdir())
check("no wrapper folders remain", all((d / n).is_file() for n in names), str(names))
check("both discs sit side by side in the parent",
      names == ["Disc 1.bin", "Disc 1.cue", "Disc 2.bin", "Disc 2.cue"], str(names))
check("txt still stripped", not any(n.endswith(".txt") for n in names))

# A flat archive must be left alone.
d2 = tempdir()
flat = d2 / "Flat Game.7z"
with py7zr.SevenZipFile(flat, "w") as z:
    z.writestr(os.urandom(1000), "Flat Game.iso")
kept = extract_archive(flat, poll_interval=0.02)
check("already-flat archive unaffected",
      [p.name for p in kept] == ["Flat Game.iso"] and (d2 / "Flat Game.iso").is_file())

# ------------------------------------------- 2. m3u prefers chd, one per disc
print("=== m3u picks one file per disc, preferring .chd ===")
d = tempdir()
for disc in (1, 2, 3):
    # A half-converted folder: cue sheets alongside their finished chds.
    (d / f"Game (USA) (Disc {disc}).cue").write_text("x")
    (d / f"Game (USA) (Disc {disc}).chd").write_bytes(b"chd")
made = vm3u.make_playlists(d, "psx")
playlist = d / "Game (USA).m3u" / "Game (USA).m3u"
check("playlist written inside the .m3u folder",
      made == [playlist] and playlist.is_file(), str(made))
lines = playlist.read_text().strip().splitlines()
check("one line per disc, all .chd",
      lines == [f"Game (USA) (Disc {n}).chd" for n in (1, 2, 3)], str(lines))
check("chds moved into the folder",
      all((d / "Game (USA).m3u" / f"Game (USA) (Disc {n}).chd").is_file()
          for n in (1, 2, 3)))

# --------------------------------------------------- 3. stage gating order
print("=== m3u only unlocks after compression ===")
hub = srv.Hub()


GATE_DIR = tempdir()


def entry(**stages):
    base = {"archive": True, "extracted": False, "chd": False, "m3u": False}
    base.update(stages)
    return {"key": "k1", "system_folder": "psx", "dir": str(GATE_DIR),
            "title": "Gate Test",
            "files": [{"archive": "a.7z"}, {"archive": "b.7z"}],
            "stages": base}


check("downloaded only -> no compress, no m3u",
      not hub.can_chd(entry()) and not hub.can_m3u(entry()))
check("extracted -> compress yes, m3u not yet",
      hub.can_chd(entry(extracted=True)) and not hub.can_m3u(entry(extracted=True)))
check("compressed -> m3u unlocks",
      hub.can_m3u(entry(extracted=True, chd=True)))
check("single-disc game never offers m3u",
      not hub.can_m3u({"system_folder": "psx",
                       "files": [{"archive": "a.7z"}],
                       "stages": {"extracted": True, "chd": True}}))

# submit_stage must refuse an m3u that is not ready yet
refused = hub.submit_stage(entry(extracted=True), "m3u")
check("submit_stage refuses a premature m3u", refused is None)

# ------------------------------------------------ 4. resume lookup by stem
print("=== partial/finished files found whatever extension the server used ===")
d = tempdir()
(d / "Game (USA) (Disc 1).7z.part").write_bytes(b"x" * 100)
found = find_download(d, "Game (USA) (Disc 1)", partial=True)
check("finds a .7z.part when .zip was planned",
      found is not None and found.name == "Game (USA) (Disc 1).7z.part")
check("no false match on a similar stem",
      find_download(d, "Game (USA) (Disc 10)", partial=True) is None)

(d / "Other Game.7z").write_bytes(b"x")
check("finds a finished archive by stem",
      (find_download(d, "Other Game", partial=False) or Path("?")).name == "Other Game.7z")
(d / "Third Game.iso").write_bytes(b"x")
check("an extracted .iso is not mistaken for an archive",
      find_download(d, "Third Game", partial=False) is None)

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
