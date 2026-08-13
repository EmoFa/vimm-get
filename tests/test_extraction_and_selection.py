"""Round-2 tests: 7z extraction progress (the reported bug), search tags,
disc overrides, m3u .m3u-folder layout, next-disc wait reason."""
import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

from pathlib import Path as _RepoPath
sys.path.insert(0, str(_RepoPath(__file__).resolve().parents[1]))
import py7zr

from vimm import m3u as vm3u
from vimm import search as vs
from vimm.engine import NEXT_DISC_WAIT, VaultPage, Media, make_options, select_media
from vimm.extract import extract_archive

failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    if not ok:
        failures.append(name)


def tempdir():
    return Path(tempfile.mkdtemp(prefix="vimmr2_"))


# ------------------------------------------------- 1. 7z extraction progress
print("=== 7z extraction progress (the reported bug) ===")

# 1a. The watcher mechanism itself, driven deterministically: a file grows
# while the watcher runs, and the watcher must report the growth.
from vimm.extract import _SizeWatcher

d = tempdir()
grow = d / "growing.iso"
grow.write_bytes(b"")
samples = []
with _SizeWatcher(lambda done, total: samples.append(done / total),
                  [grow], total=100_000, interval=0.02):
    with open(grow, "ab") as handle:
        for _ in range(10):
            handle.write(b"x" * 10_000)
            handle.flush()
            os.fsync(handle.fileno())
            time.sleep(0.03)
mid = sorted({round(s, 2) for s in samples if 0.0 < s < 1.0})
check("watcher reports a file growing", len(mid) >= 3, f"distinct fractions: {mid}")
check("watcher never claims 100% while working", all(s < 1.0 for s in samples))

# 1b. Realistic single-member archive: one big member, as a disc image is.
# Zero-filled so the archive is small but extraction still writes real bytes.
d = tempdir()
seven = d / "Big Game (USA).7z"
with py7zr.SevenZipFile(seven, "w") as z:
    z.writestr(b"\0" * 220_000_000, "Big Game (USA).iso")
samples = []
started = time.monotonic()
kept = extract_archive(seven, progress=lambda done, total: samples.append(done / total),
                       poll_interval=0.05)
elapsed = time.monotonic() - started
mid = [s for s in samples if 0.0 < s < 1.0]
check("single-member 7z: extracted", [p.name for p in kept] == ["Big Game (USA).iso"])
check("single-member 7z: intermediate progress reported", len(mid) > 0,
      f"{len(samples)} samples, {len(mid)} strictly between 0 and 1, {elapsed:.1f}s")
check("single-member 7z: ends at 100%", samples and abs(samples[-1] - 1.0) < 1e-9)
check("single-member 7z: monotonic", all(b >= a for a, b in zip(samples, samples[1:])))

# 1c. Multi-member archive still works and still strips the txt.
d = tempdir()
seven = d / "Multi (USA).7z"
with py7zr.SevenZipFile(seven, "w") as z:
    for n in (1, 2, 3):
        z.writestr(os.urandom(3_000_000), f"Track {n}.bin")
    z.writestr(b"info", "readme.txt")
samples = []
kept = extract_archive(seven, progress=lambda done, total: samples.append(done / total),
                       poll_interval=0.02)
check("multi-member 7z: txt removed, bins kept",
      sorted(p.name for p in kept) == ["Track 1.bin", "Track 2.bin", "Track 3.bin"])
check("multi-member 7z: reaches 100%", samples and abs(samples[-1] - 1.0) < 1e-9)

# ------------------------------------------------------------- 2. search tags
print("=== search tags ===")
# Captured verbatim from the live site, honeypot included.
ROW = """
<tr><td style="width:80px; text-align:center">PS1</td><td style="width:auto">
<a href="/vault/999999" style="display: none">9</a>
<a href= "/vault/57536">Akuji the Heartless (Trade Demo)</a>&nbsp;
<b class="redBorder" style="cursor:default" title="Demo">D</b></td>
<td style="width:65px; text-align:center"><img src="/images/flags/usa.png" class="flag" title="USA"></td>
<td>1.0</td><td>-</td></tr>
<tr><td style="text-align:center">PS1</td><td>
<a href="/vault/999999" style="display: none">9</a>
<a href= "/vault/6078">Akuji the Heartless</a></td>
<td><img title="USA" src="x"></td><td>1.0</td><td>-</td></tr>
<tr><td style="text-align:center">NES</td><td>
<a href="/vault/999999" style="display: none">9</a>
<a href= "/vault/4242">Some Unreleased Thing</a>&nbsp;
<span class="redBorder" title="Download unavailable - Please upload it! &#x26a0;">!</span></td>
<td><img title="Japan" src="x"></td><td>1.0</td><td>-</td></tr>
"""
hits = vs.parse_results(ROW)
check("3 rows parsed, honeypot rejected",
      [h.vault_id for h in hits] == [57536, 6078, 4242], str([h.vault_id for h in hits]))
check("demo row tagged", hits[0].tags == ["Demo"], str(hits[0].tags))
check("clean row untagged", hits[1].tags == [])
check("unavailable row flagged not-downloadable",
      not hits[2].downloadable and hits[1].downloadable)
check("title excludes the badge text", hits[0].title == "Akuji the Heartless (Trade Demo)",
      hits[0].title)

kept, hidden = vs.filter_by_tags(hits, vs.DEFAULT_HIDDEN_TAGS)
check("default filter hides demo + unavailable, keeps the real release",
      [h.vault_id for h in kept] == [6078] and len(hidden) == 2)
kept_all, hidden_none = vs.filter_by_tags(hits, [])
check("empty filter keeps everything", len(kept_all) == 3 and hidden_none == [])
check("vocabulary includes new tags",
      "Bonus disc" in vs.tag_vocabulary(hits))

# names like "Demon Sword" must never be caught (word-safety of the approach)
demon = vs.parse_results(
    '<tr><td>NES</td><td><a href="/vault/1">Demon Sword</a></td><td></td></tr>')
check("Demon Sword has no tags and survives the filter",
      demon[0].tags == [] and len(vs.filter_by_tags(demon, ["Demo"])[0]) == 1)

# ----------------------------------------------------------- 3. disc override
print("=== per-game disc overrides ===")


def media(disc):
    return Media(media_id=100 + disc, version="1.0", disc=disc,
                 filename=f"Game (USA) (Disc {disc}).bin", sizes=[1000, 0, 0],
                 size_texts=["1 KB", "0", "0"], formats=["PS1"],
                 crc32=None, md5=None, sha1=None)


page = VaultPage(vault_id=2826, title="Game (PS1)", download_host="http://x/",
                 media=[media(1), media(2), media(3)])

opts = make_options()
check("default: all discs", [m.disc for m, _ in select_media(page, opts)] == [1, 2, 3])

opts = make_options(disc_overrides={2826: [2]})
check("override picks disc 2 only", [m.disc for m, _ in select_media(page, opts)] == [2])

opts = make_options(disc_overrides={2826: [1, 3]})
check("override picks 1 and 3", [m.disc for m, _ in select_media(page, opts)] == [1, 3])

opts = make_options(disc_overrides={"2826": [3]})
check("string keys work (JSON round-trip)",
      [m.disc for m, _ in select_media(page, opts)] == [3])

opts = make_options(disc_overrides={9999: [1]}, discs="2")
check("unrelated override falls back to global --discs",
      [m.disc for m, _ in select_media(page, opts)] == [2])

# ------------------------------------------------------------- 4. m3u layout
print("=== m3u layout ===")
d = tempdir()
for n in (1, 2, 3):
    (d / f"Final Fantasy VII (USA) (Disc {n}).chd").write_bytes(b"x" * 10)
made = vm3u.make_playlists(d, "psx")
expected = d / "Final Fantasy VII (USA).m3u" / "Final Fantasy VII (USA).m3u"
check("folder carries .m3u and holds the playlist of the same name",
      made == [expected] and expected.is_file(), str(made))
check("folder is a directory named *.m3u",
      (d / "Final Fantasy VII (USA).m3u").is_dir())
check("discs moved inside",
      all((d / "Final Fantasy VII (USA).m3u" /
           f"Final Fantasy VII (USA) (Disc {n}).chd").is_file() for n in (1, 2, 3)))
check("re-running is a no-op", vm3u.make_playlists(d, "psx") == [])

# ------------------------------------------------------- 5. next-disc reason
print("=== next-disc wait reason ===")
check("engine exposes a distinct reason", NEXT_DISC_WAIT == "waiting for the next disc")

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
