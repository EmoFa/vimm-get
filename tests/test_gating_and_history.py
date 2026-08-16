"""Gating matrix, history grouping + migration, and the search filter through
the server API. No live traffic."""
import json
import sys
import tempfile
from pathlib import Path

from pathlib import Path as _RepoPath
sys.path.insert(0, str(_RepoPath(__file__).resolve().parents[1]))

import vimm.server as srv

TESTDATA = Path(tempfile.mkdtemp(prefix="vimmr2b_"))
srv.DATA_DIR = TESTDATA

from fastapi.testclient import TestClient

failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    if not ok:
        failures.append(name)


# ------------------------------------------------------------ gating matrix
print("=== compress / m3u button gating ===")
hub = srv.Hub()


def entry(system, discs=1, extracted=True, chd=True):
    # m3u comes after compression, so the matrix below assumes a fully
    # processed game unless a case says otherwise.
    return {
        "system_folder": system,
        "files": [{"archive": f"x{i}.zip"} for i in range(discs)],
        "stages": {"extracted": extracted, "chd": chd},
    }


# can_chd / can_m3u mean "this action is offerable now", which includes not
# already being done - so each is checked at the point in the pipeline where
# it would actually be offered.
MATRIX = [
    # system,        discs, chd offered once extracted, m3u offered once compressed
    ("psx",           2,     True,   True),
    ("psx",           1,     True,   False),   # single disc: no playlist
    ("dreamcast",     2,     True,   True),
    ("saturn",        2,     True,   True),
    ("segacd",        2,     True,   True),
    # Nothing to collapse - one ISO - but PCSX2 reads .chd and the saving is
    # large, so PS2 is compressed via chdman createdvd.
    ("ps2",           1,     True,   False),
    ("psp",           1,     False,  False),
    ("atarijaguar",   1,     False,  False),   # BigPEmu cannot read CHD
    ("atarijaguarcd", 2,     False,  False),
    ("snes",          1,     False,  False),
    # Dolphin cannot read CHD (.rvz is the equivalent, offered under formats)
    # but it does read m3u, so multi-disc GameCube games get a playlist.
    ("gc",            1,     False,  False),
    ("gc",            2,     False,  True),
    ("ps3",           1,     False,  False),
]
for system, discs, want_chd, want_m3u in MATRIX:
    got = (hub.can_chd(entry(system, discs, chd=False)),   # freshly extracted
           hub.can_m3u(entry(system, discs, chd=True)))    # freshly compressed
    check(f"{system} x{discs} -> chd={want_chd} m3u={want_m3u}",
          got == (want_chd, want_m3u), f"got {got}")

check("not extracted yet -> no buttons",
      hub.can_chd(entry("psx", 2, extracted=False, chd=False)) is False
      and hub.can_m3u(entry("psx", 2, extracted=False, chd=False)) is False)
# A playlist waits for compression only when compression is actually going to
# happen. With it switched off - as it is by default here - the discs stay as
# cue sheets and the playlist is built over those instead of never at all.
check("extracted, compression off -> both offered",
      hub.can_chd(entry("psx", 2, chd=False)) is True
      and hub.can_m3u(entry("psx", 2, chd=False)) is True)
hub.settings["auto_compress"] = True
check("extracted, compression on and still to do -> compress only",
      hub.can_chd(entry("psx", 2, chd=False)) is True
      and hub.can_m3u(entry("psx", 2, chd=False)) is False)
hub.settings["auto_compress"] = False
check("already compressed -> compress no longer offered",
      hub.can_chd(entry("psx", 2, chd=True)) is False)

# --------------------------------------------------------- history grouping
print("=== history grouping ===")
from vimm.engine import Result, VaultPage, Media, make_options

OUT = Path(tempfile.mkdtemp(prefix="vimmr2bout_"))
hub._opts = make_options(out=str(OUT), organize=True)
page = VaultPage(vault_id=555, title="Skies of Arcadia (Dreamcast)",
                 download_host="http://x/",
                 media=[Media(1, "1.0", 1, "Skies of Arcadia (USA) (Disc 1).gdi",
                              [1000, 0, 0], ["1 KB", "0", "0"], ["DC"], None, None, None)])
hub._pages[555] = page

for disc in (1, 2):
    hub.add_history(Result(555, disc, f"Skies of Arcadia (USA) (Disc {disc}).zip",
                           "ok", 5_000_000 * disc, "CRC ok"))

check("two discs collapse into one history entry", len(hub.history) == 1,
      f"{len(hub.history)} entries")
files = hub.history[0]["files"]
check("both discs listed under the parent", len(files) == 2,
      str([f["filename"] for f in files]))
check("disc numbers assigned", [f["disc"] for f in files] == [1, 2])
check("system folder recorded", hub.history[0]["system_folder"] == "dreamcast",
      hub.history[0]["system_folder"])

# re-downloading a disc replaces rather than duplicates
hub.add_history(Result(555, 1, "Skies of Arcadia (USA) (Disc 1).zip", "ok",
                       5_000_000, "CRC ok"))
check("re-download replaces its row, no duplicate",
      len(hub.history[0]["files"]) == 2, str(len(hub.history[0]["files"])))

# ------------------------------------------------------- history migration
print("=== history migration from the old per-file shape ===")
old = [
    {"key": "a1", "vault_id": 99, "title": "Two Disc Game (PS1)",
     "system_folder": "psx", "dir": str(OUT), "archive": str(OUT / "Game (Disc 1).zip"),
     "bytes": 10, "stages": {"archive": True, "extracted": False},
     "message": "CRC ok", "when": 1},
    {"key": "a2", "vault_id": 99, "title": "Two Disc Game (PS1)",
     "system_folder": "psx", "dir": str(OUT), "archive": str(OUT / "Game (Disc 2).zip"),
     "bytes": 20, "stages": {"archive": True, "extracted": False},
     "message": "CRC ok", "when": 2},
    {"key": "b1", "vault_id": 100, "title": "Solo Game (SNES)",
     "system_folder": "snes", "dir": str(OUT), "archive": str(OUT / "Solo.zip"),
     "bytes": 5, "stages": {"archive": True, "extracted": True},
     "message": "CRC ok", "when": 3},
]
migrated = srv._migrate_history(old)
check("old entries merge by vault_id", len(migrated) == 2, f"{len(migrated)} entries")
check("two-disc game gained both files",
      len(migrated[0]["files"]) == 2 and migrated[0]["vault_id"] == 99)
check("solo game preserved", migrated[1]["vault_id"] == 100
      and len(migrated[1]["files"]) == 1)
check("already-migrated data passes through unchanged",
      srv._migrate_history(migrated) == migrated)

# ------------------------------------------------ search filter via the API
print("=== search filtering through the API ===")
import vimm.search as vs

ROW = """
<tr><td>PS1</td><td><a href="/vault/999999" style="display: none">9</a>
<a href="/vault/1">Real Game</a></td><td><img title="USA" src="x"></td><td>1.0</td><td>-</td></tr>
<tr><td>PS1</td><td><a href="/vault/999999" style="display: none">9</a>
<a href="/vault/2">Real Game (Trade Demo)</a>&nbsp;
<b class="redBorder" title="Demo">D</b></td><td><img title="USA" src="x"></td><td>1.0</td><td>-</td></tr>
<tr><td>NES</td><td><a href="/vault/999999" style="display: none">9</a>
<a href="/vault/3">Missing Game</a>&nbsp;
<span class="redBorder" title="Download unavailable - Please upload it! &#x26a0;">!</span></td>
<td><img title="Japan" src="x"></td><td>1.0</td><td>-</td></tr>
"""

app = srv.create_app()
app.state.hub.search_session = None
client = TestClient(app)
with client:
    # Patch the module-level search to return our fixture rows.
    original = vs.search
    vs.search = lambda session, q, timeout=30: vs.parse_results(ROW)
    srv.search_mod.search = vs.search
    try:
        r = client.get("/api/search?q=real").json()
        check("default filter keeps only the real release",
              [h["vault_id"] for h in r["hits"]] == [1], str(r["hits"]))
        check("hidden list carries the demo and the unavailable entry",
              sorted(h["vault_id"] for h in r["hidden"]) == [2, 3])
        check("tags surface to the client",
              r["hidden"][0]["tags"] == ["Demo"] or r["hidden"][1]["tags"] == ["Demo"])
        check("unavailable entry marked not downloadable",
              any(h["downloadable"] is False for h in r["hidden"]))
        check("vocabulary returned", "Demo" in r["tag_vocabulary"])

        client.put("/api/settings", json={"hidden_tags": []})
        r = client.get("/api/search?q=real").json()
        check("clearing the filter shows everything",
              len(r["hits"]) == 3 and r["hidden"] == [])
    finally:
        vs.search = original
        srv.search_mod.search = original

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
