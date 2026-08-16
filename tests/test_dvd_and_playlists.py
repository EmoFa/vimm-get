"""PS2 compresses to CHD, and multi-disc GameCube games get a playlist.

Both were requested as settings-list additions, but the lists drive code that
only understood bin+cue: `compressible_among` matched .cue/.gdi only and
`compress_to_chd` ran `createcd`, while the playlist builder ignored anything
that was not .chd/.cue/.gdi. Ticking either box would have done nothing at
all. These cover the widened paths, and that the old ones are untouched.
"""
import os
import sys
import tempfile
from pathlib import Path

from pathlib import Path as _RepoPath
sys.path.insert(0, str(_RepoPath(__file__).resolve().parents[1]))

from vimm import chd as chd_mod
from vimm import m3u as m3u_mod
from vimm.engine import VimmError

failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    if not ok:
        failures.append(name)


def tempdir():
    return Path(tempfile.mkdtemp(prefix="vimmdvd_"))


# ================================================ which chdman subcommand
print("=== a bare ISO is a DVD image, and needs a different subcommand ===")
check("a cue sheet still uses createcd",
      chd_mod.chdman_subcommand(Path("Game (USA).cue")) == "createcd")
check("a gdi sheet still uses createcd",
      chd_mod.chdman_subcommand(Path("Game (USA).gdi")) == "createcd")
check("an iso uses createdvd",
      chd_mod.chdman_subcommand(Path("Game (USA).iso")) == "createdvd")
check("a .nkit.iso is an iso as far as the suffix goes",
      chd_mod.chdman_subcommand(Path("Game (USA).nkit.iso")) == "createdvd")

print("=== what counts as compressible ===")
d = tempdir()
cue = d / "PS1 Game (USA).cue"
cue.write_text('FILE "PS1 Game (USA).bin" BINARY\n')
binf = d / "PS1 Game (USA).bin"
binf.write_bytes(b"x" * 1000)
iso = d / "PS2 Game (USA).iso"
iso.write_bytes(b"y" * 1000)
rvz = d / "GC Game (USA).rvz"
rvz.write_bytes(b"z" * 1000)
done = d / "Already (USA).iso"
done.write_bytes(b"w" * 1000)
(d / "Already (USA).chd").write_bytes(b"chd")

found = [p.name for p in chd_mod.compressible_among(
    [cue, binf, iso, rvz, done])]
check("the cue sheet is compressible", "PS1 Game (USA).cue" in found, str(found))
check("the iso is compressible now too", "PS2 Game (USA).iso" in found, str(found))
check("a bin track is not offered on its own",
      "PS1 Game (USA).bin" not in found, str(found))
check("an .rvz is not compressed - Dolphin cannot read CHD",
      "GC Game (USA).rvz" not in found, str(found))
check("an image that already has a .chd is skipped",
      "Already (USA).iso" not in found, str(found))

print("=== compress_to_chd refuses what it cannot handle ===")
try:
    chd_mod.compress_to_chd(rvz)
    check("an .rvz is refused", False, "no error raised")
except VimmError as exc:
    check("an .rvz is refused", "must be a .cue, .gdi or .iso" in str(exc), str(exc))

# ================================================= real chdman on a real ISO
if chd_mod.find_chdman() is None:
    print("SKIPPED the live chdman check - not installed")
else:
    print("=== chdman really converts a PS2-style ISO ===")
    d2 = tempdir()
    real_iso = d2 / "PS2 Game (USA).iso"
    # A DVD image is a whole number of 2048-byte sectors.
    real_iso.write_bytes(os.urandom(2048 * 512))
    try:
        out = chd_mod.compress_to_chd(real_iso, delete_sources=True)
        check("a .chd came out", out.is_file() and out.suffix == ".chd", str(out))
        check("and the iso was replaced", not real_iso.exists())
    except VimmError as exc:
        check("chdman converted the iso", False, str(exc))

# ============================================ GameCube multi-disc playlists
print("=== a two-disc GameCube game gets a playlist over its .rvz files ===")
d3 = tempdir() / "gc"
d3.mkdir(parents=True)
for disc in (1, 2):
    (d3 / f"Resident Evil (USA) (Disc {disc}).rvz").write_bytes(b"rvz")

written = m3u_mod.make_playlists(d3, "gc")
check("one playlist was written", len(written) == 1, str(written))
if written:
    listed = [ln.strip() for ln in
              written[0].read_text(encoding="utf-8").splitlines() if ln.strip()]
    check("it lists both discs, as .rvz",
          listed == ["Resident Evil (USA) (Disc 1).rvz",
                     "Resident Evil (USA) (Disc 2).rvz"], str(listed))
    check("the discs moved into the playlist folder",
          (written[0].parent / "Resident Evil (USA) (Disc 1).rvz").is_file())

print("=== a single-disc GameCube game still gets nothing ===")
d4 = tempdir() / "gc"
d4.mkdir(parents=True)
(d4 / "Pikmin (USA).rvz").write_bytes(b"rvz")
check("no playlist for one disc", m3u_mod.make_playlists(d4, "gc") == [])

print("=== a system that does not read m3u is still skipped ===")
d5 = tempdir() / "gba"
d5.mkdir(parents=True)
for disc in (1, 2):
    (d5 / f"Thing (USA) (Disc {disc}).rvz").write_bytes(b"rvz")
check("no playlist for a system outside the list",
      m3u_mod.make_playlists(d5, "gba") == [])

print("=== a .chd still outranks the image it came from ===")
d6 = tempdir() / "ps2"
d6.mkdir(parents=True)
for disc in (1, 2):
    (d6 / f"Game (USA) (Disc {disc}).chd").write_bytes(b"chd")
    (d6 / f"Game (USA) (Disc {disc}).iso").write_bytes(b"iso")
sets = m3u_mod.find_disc_sets(d6)
picked = sorted(p.suffix for discs in sets.values() for p in discs)
check("the playlist would list the .chd, not the .iso",
      picked == [".chd", ".chd"], str(picked))

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
