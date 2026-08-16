"""Multi-disc post-processing: per disc as it lands, playlist once it is whole.

Two reported bugs live here.

1. Only disc 1 was compressed. `item_done` fires once per file, so starting
   the chain from the first one let extraction find no archive left, declare
   the whole game extracted, and hand half a game to CHD.

2. Nothing was compressed at all on a re-download. `add_history` reuses an
   existing entry *including its stage flags*, so a second download inherited
   "already extracted, already compressed, already playlisted" and the chain
   declined to start.

The wanted flow: extract and compress each disc as it arrives, overlapping
the next download; build the playlist only once every disc is settled - over
.chd files, or over cue sheets when compression is off.

Uses real chdman, as the other post-processing tests do.
"""
import base64
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from pathlib import Path as _RepoPath
sys.path.insert(0, str(_RepoPath(__file__).resolve().parents[1]))

import vimm.server as srv

srv.DATA_DIR = Path(tempfile.mkdtemp(prefix="vimmmd_data_"))

from vimm.chd import find_chdman as _find_chdman

if _find_chdman() is None:
    print("SKIPPED: chdman not found - install it, or run the app's "
          "COMPRESS action once to fetch it")
    raise SystemExit(0)

from fastapi.testclient import TestClient

failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    if not ok:
        failures.append(name)


def wait_for(predicate, timeout=180, poll=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(poll)
    return False


# ---------------------------------------------------- real multi-disc games
def disc_archive(stem: str) -> bytes:
    """A zip holding one genuinely valid single-track disc image."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr(f"{stem}.bin", os.urandom(2352 * 150))
        z.writestr(f"{stem}.cue",
                   f'FILE "{stem}.bin" BINARY\n  TRACK 01 MODE2/2352\n'
                   f"    INDEX 01 00:00:00\n")
    return buf.getvalue()


# vault id -> (system label, game name, disc count)
# One game per section: history is keyed by vault id and DATA_DIR is shared
# across the hubs here, so a reused id would inherit the previous section's
# tracked files.
GAMES = {
    4242: ("Dreamcast", "Skies of Arcadia (USA)", 2),
    4243: ("Dreamcast", "Grandia II (USA)", 2),
    4244: ("Dreamcast", "Shenmue (USA)", 3),
    4245: ("Dreamcast", "Resident Evil CV (USA)", 2),
    4246: ("Dreamcast", "D2 (USA)", 2),
}
ARCHIVES: dict[tuple[int, int], bytes] = {}


def stem_for(vault: int, disc: int) -> str:
    return f"{GAMES[vault][1]} (Disc {disc})"


for _vault, (_system, _name, _count) in GAMES.items():
    for _disc in range(1, _count + 1):
        ARCHIVES[(_vault, _disc)] = disc_archive(stem_for(_vault, _disc))

MODE = {"drip": False}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, headers=()):
        self.send_response(code)
        for key, value in headers:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/vault/"):
            vault = int(self.path.rsplit("/", 1)[1])
            system, name, count = GAMES[vault]
            media = [{
                "ID": vault * 10 + disc, "SortOrder": disc, "Version": "1.0",
                "GoodTitle": base64.b64encode(
                    f"{stem_for(vault, disc)}.cue".encode()).decode(),
                "Serial": None,
                "Zipped": str(len(ARCHIVES[(vault, disc)]) // 1024 or 1),
                "AltZipped": "0", "AltZipped2": "0",
                "GoodHash": None, "GoodMd5": None, "GoodSha1": None,
                "ZippedText": "1 MB", "AltZippedText": "0",
                "AltZipped2Text": "0", "Mirror": [system],
            } for disc in range(1, count + 1)]
            host = f"http://127.0.0.1:{self.server.server_address[1]}/"
            html = (
                f"<html><head><title>The Vault: {name} ({system})</title></head>"
                f'<body><form action="{host}" method="POST" id="dl_form">'
                f'<input name="mediaId" value="{vault}"></form>'
                f"<script>let media={json.dumps(media)};</script></body></html>"
            ).encode()
            self._send(200, html, [("Content-Type", "text/html; charset=UTF-8")])
            return

        raw = self.path.split("mediaId=")[-1].split("&")[0]
        if not raw.isdigit():
            self._send(200, b"<html><body>Vimm's Lair</body></html>",
                       [("Content-Type", "text/html; charset=UTF-8")])
            return
        media_id = int(raw)
        vault, disc = media_id // 10, media_id % 10
        body = ARCHIVES[(vault, disc)]
        stem = stem_for(vault, disc)

        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f'attachment; filename="{stem}.zip"')
        self.end_headers()
        # Only later discs drip, so the first one is finished and being
        # compressed while a later one is still on the wire.
        if MODE["drip"] and disc > 1:
            for i in range(0, len(body), 8192):
                self.wfile.write(body[i:i + 8192])
                self.wfile.flush()
                time.sleep(0.06)
            return
        self.wfile.write(body)


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
SITE = f"http://127.0.0.1:{server.server_address[1]}"


def make_app(out_dir, **settings):
    app = srv.create_app()
    hub = app.state.hub
    hub.site_base_override = SITE
    defaults = dict(out=str(out_dir), organize=True, delay=1.0, sweeps=0,
                    auto_extract=True, auto_compress=True, auto_m3u=True,
                    delete_chd_sources=True)
    hub.settings.update({**defaults, **settings})
    return app, hub


def idle(hub):
    return not any(j.status in ("queued", "running")
                   for j in hub.pipeline.jobs.values())


def entry_for(hub, vault):
    return next((h for h in hub.history if h["vault_id"] == vault), None)


def by_suffix(hub, entry):
    out = {}
    for path in hub.entry_files(entry):
        out.setdefault(path.suffix.lower(), []).append(path.name)
    return {k: sorted(v) for k, v in out.items()}


# ================================= the whole pipeline, every automation on
print("=== a two-disc Dreamcast game, every automation on ===")
OUT = Path(tempfile.mkdtemp(prefix="vimmmd_out_"))
app, hub = make_app(OUT)
client = TestClient(app)

with client:
    client.post("/api/queue", json={"text": "4242"})
    client.post("/api/run/start")
    check("both discs downloaded",
          wait_for(lambda: hub.run_status.startswith("finished"), 180),
          hub.run_status)

    entry = entry_for(hub, 4242)
    check("one history entry for the game", entry is not None)
    check("holding both discs", len(entry["files"]) == 2, str(len(entry["files"])))
    check("numbered 1 and 2",
          [f["disc"] for f in entry["files"]] == [1, 2],
          str([f["disc"] for f in entry["files"]]))

    check("the pipeline finished",
          wait_for(lambda: entry["stages"].get("m3u") and idle(hub), 300),
          str(entry["stages"]))

    found = by_suffix(hub, entry)
    check("BOTH discs were compressed to CHD",
          len(found.get(".chd", [])) == 2, str(found))
    check("no disc was left as a cue sheet", ".cue" not in found, str(found))
    check("a playlist was written", len(found.get(".m3u", [])) == 1, str(found))

    m3u = next(p for p in hub.entry_files(entry) if p.suffix.lower() == ".m3u")
    listed = [ln.strip() for ln in m3u.read_text(encoding="utf-8").splitlines()
              if ln.strip()]
    check("the playlist lists both discs, as .chd",
          len(listed) == 2 and all(ln.lower().endswith(".chd") for ln in listed),
          str(listed))
    check("the game left the queue only once it was wholly finished",
          hub.queue == [], str(hub.queue))

# ============================ disc 1 is compressed while disc 2 downloads
print("=== each disc is processed while the next one downloads ===")
OUT2 = Path(tempfile.mkdtemp(prefix="vimmmd_overlap_"))
app2, hub2 = make_app(OUT2)
client2 = TestClient(app2)
MODE["drip"] = True

with client2:
    client2.post("/api/queue", json={"text": "4243"})
    client2.post("/api/run/start")

    disc1_chd = OUT2 / "dreamcast" / f"{stem_for(4243, 1)}.chd"
    overlapped = wait_for(
        lambda: disc1_chd.is_file() and not hub2.run_status.startswith("finished"),
        120)
    check("disc 1 is already a .chd while the run is still going",
          overlapped, f"run={hub2.run_status} chd={disc1_chd.is_file()}")
    check("and disc 2 has not been recorded yet",
          len((entry_for(hub2, 4243) or {}).get("files", [])) == 1,
          str(len((entry_for(hub2, 4243) or {}).get("files", []))))

    check("the run finishes",
          wait_for(lambda: hub2.run_status.startswith("finished"), 180),
          hub2.run_status)
    entry2 = entry_for(hub2, 4243)
    check("the pipeline finishes",
          wait_for(lambda: entry2["stages"].get("m3u") and idle(hub2), 300),
          str(entry2["stages"]))
    check("both discs compressed", len(by_suffix(hub2, entry2).get(".chd", [])) == 2,
          str(by_suffix(hub2, entry2)))

MODE["drip"] = False

# ================================================= the reported re-download
print("=== re-downloading a game runs the whole chain again ===")
# Exactly what happened: the files were deleted, the app was restarted, and
# the same game was downloaded again into a clean folder.
shutil.rmtree(OUT / "dreamcast", ignore_errors=True)
app3, hub3 = make_app(OUT)          # a fresh Hub over the same DATA_DIR
client3 = TestClient(app3)

with client3:
    stale = entry_for(hub3, 4242)
    check("the old entry is still there, flagged complete",
          stale is not None and stale["stages"].get("m3u"), str(stale["stages"]))
    check("but none of its files are on disk", hub3.entry_files(stale) == [],
          str(hub3.entry_files(stale)))

    client3.post("/api/queue", json={"text": "4242"})
    client3.post("/api/run/start")
    check("both discs downloaded again",
          wait_for(lambda: hub3.run_status.startswith("finished"), 180),
          hub3.run_status)

    entry3 = entry_for(hub3, 4242)
    check("the stale flags were re-opened by the new download",
          wait_for(lambda: entry3["stages"].get("m3u") and idle(hub3), 300),
          str(entry3["stages"]))

    found = by_suffix(hub3, entry3)
    check("both discs compressed on the re-download",
          len(found.get(".chd", [])) == 2, str(found))
    check("and a playlist written", len(found.get(".m3u", [])) == 1, str(found))
    check("still exactly two disc rows, numbered 1 and 2",
          [f["disc"] for f in entry3["files"]] == [1, 2],
          str([f["disc"] for f in entry3["files"]]))

# ======================================== compression off: cue sheets in m3u
print("=== with compression off, the playlist holds the cue sheets ===")
OUT4 = Path(tempfile.mkdtemp(prefix="vimmmd_nochd_"))
app4, hub4 = make_app(OUT4, auto_compress=False)
client4 = TestClient(app4)

with client4:
    client4.post("/api/queue", json={"text": "4245"})
    client4.post("/api/run/start")
    wait_for(lambda: hub4.run_status.startswith("finished"), 180)
    entry4 = entry_for(hub4, 4245)
    check("the playlist is still built",
          wait_for(lambda: entry4["stages"].get("m3u") and idle(hub4), 180),
          str(entry4["stages"]))
    found = by_suffix(hub4, entry4)
    check("nothing was compressed", ".chd" not in found, str(found))
    check("both cue sheets are in the playlist folder",
          len(found.get(".cue", [])) == 2, str(found))
    m3u = next(p for p in hub4.entry_files(entry4) if p.suffix.lower() == ".m3u")
    listed = [ln.strip() for ln in m3u.read_text(encoding="utf-8").splitlines()
              if ln.strip()]
    check("and the playlist lists them",
          len(listed) == 2 and all(ln.lower().endswith(".cue") for ln in listed),
          str(listed))

# ============================ a system that does not use CHD still gets m3u
print("=== a multi-disc game on a non-CHD system still gets a playlist ===")
OUT5 = Path(tempfile.mkdtemp(prefix="vimmmd_nosys_"))
# Compression is on, but this system is not on the CHD list - as PS2 is not.
app5, hub5 = make_app(OUT5, chd_systems=["psx", "saturn"])
client5 = TestClient(app5)

with client5:
    client5.post("/api/queue", json={"text": "4246"})
    client5.post("/api/run/start")
    wait_for(lambda: hub5.run_status.startswith("finished"), 180)
    entry5 = entry_for(hub5, 4246)
    check("the playlist is built without waiting for a conversion",
          wait_for(lambda: entry5["stages"].get("m3u") and idle(hub5), 180),
          str(entry5["stages"]))
    found = by_suffix(hub5, entry5)
    check("the discs stayed as cue sheets", len(found.get(".cue", [])) == 2,
          str(found))

# =================================== the playlist waits for every disc
print("=== the playlist waits until every disc has arrived ===")
half = {
    "system_folder": "dreamcast",
    "files": [{"archive": "x", "disc": 1}],
    "discs_expected": 2,
    "stages": {"extracted": True, "chd": True, "m3u": False},
}
check("one disc of two is not ready for a playlist", not hub.can_m3u(half),
      str(hub.can_m3u(half)))
half["files"].append({"archive": "y", "disc": 2})
check("both discs are", hub.can_m3u(half), str(hub.can_m3u(half)))
half["discs_expected"] = 0        # unknown, e.g. after a restart
half["files"] = [{"archive": "x", "disc": 1}, {"archive": "y", "disc": 2}]
check("an unknown expectation does not block it", hub.can_m3u(half))

# ============================================ a chosen subset keeps its numbers
print("=== picking discs 1 and 3 records them as 1 and 3 ===")
OUT6 = Path(tempfile.mkdtemp(prefix="vimmmd_subset_"))
app6, hub6 = make_app(OUT6, auto_compress=False, auto_m3u=False)
client6 = TestClient(app6)

with client6:
    client6.post("/api/queue", json={"text": "4244"})     # three discs
    client6.post("/api/queue/4244/resolve")
    client6.post("/api/queue/4244/discs", json={"discs": [1, 3]})
    client6.post("/api/run/start")
    wait_for(lambda: hub6.run_status.startswith("finished"), 180)
    entry6 = entry_for(hub6, 4244)
    check("two discs downloaded", len(entry6["files"]) == 2,
          str(len(entry6["files"])))
    check("recorded as discs 1 and 3, not 1 and 2",
          [f["disc"] for f in entry6["files"]] == [1, 3],
          str([f["disc"] for f in entry6["files"]]))

# ==================== disc 1 converting while disc 2 waits to be extracted
print("=== disc 1's conversion finishing early must not close the game ===")
# The reported failure is a race the timing above cannot reach: with real
# multi-minute transfers, disc 1's CHD job is still running when disc 2
# finishes downloading, so disc 2's extract job sits behind it. Disc 1's
# conversion then completes at a moment when nothing compressible exists -
# because disc 2 is still a .7z - and the game gets flagged converted.
#
# Driven here through Hub._on_job_event directly, so it does not depend on
# how long chdman happens to take.
from vimm.pipeline import Job
from vimm.engine import Media, Result, VaultPage

OUT7 = Path(tempfile.mkdtemp(prefix="vimmmd_race_"))
app7, hub7 = make_app(OUT7)
RACE = 4247
DEST = OUT7 / "dreamcast"
DEST.mkdir(parents=True)


def fake_media(disc):
    return Media(media_id=RACE * 10 + disc, version="1.0", disc=disc,
                 filename=f"Race Game (USA) (Disc {disc}).cue",
                 sizes=[100, 0, 0], size_texts=["a", "b", "c"],
                 formats=["Dreamcast"], crc32=None, md5=None, sha1=None)


hub7._pages[RACE] = VaultPage(vault_id=RACE, title="Race Game (Dreamcast)",
                              download_host="http://x/", media=[fake_media(1),
                                                                fake_media(2)])
hub7._opts = hub7.build_options()
hub7._expected_discs[RACE] = 2


def lay_down_disc(disc):
    """The files an extract job would leave behind, plus its archive."""
    cue = DEST / f"Race Game (USA) (Disc {disc}).cue"
    binf = DEST / f"Race Game (USA) (Disc {disc}).bin"
    binf.write_bytes(os.urandom(2352 * 60))
    cue.write_text(f'FILE "{binf.name}" BINARY\n  TRACK 01 MODE2/2352\n'
                   f"    INDEX 01 00:00:00\n")
    return [str(cue), str(binf)]


def record(disc):
    """What item_done does when a disc finishes downloading."""
    archive = DEST / f"Race Game (USA) (Disc {disc}).7z"
    archive.write_bytes(b"archive")
    return hub7.add_history(Result(RACE, RACE * 10 + disc, archive.name,
                                   "ok", 7, "CRC ok"))


def finish(kind, label, target, item_key, **extra):
    job = Job(kind=kind, label=label, target=Path(target), item_key=item_key)
    job.status = "done"
    job.extra = {**job.extra, **extra}
    hub7._on_job_event(job)


def chd_jobs_for(name):
    return [j for j in hub7.pipeline.jobs.values()
            if j.kind == "chd" and Path(j.target).name == name]


# 1. disc 1 arrives and is unpacked
entry7 = record(1)
d1 = lay_down_disc(1)
(DEST / "Race Game (USA) (Disc 1).7z").unlink()      # extraction consumes it
finish("extract", "disc1", DEST / "Race Game (USA) (Disc 1).7z",
       entry7["key"], extracted=d1)
check("a conversion is queued for disc 1",
      len(chd_jobs_for("Race Game (USA) (Disc 1).cue")) == 1,
      str([j.label for j in hub7.pipeline.jobs.values()]))

# 2. disc 2 finishes downloading while disc 1 is still converting
record(2)
check("disc 2 is recorded but not yet unpacked",
      len(entry7["files"]) == 2 and
      (DEST / "Race Game (USA) (Disc 2).7z").is_file())

# 3. disc 1's conversion completes - the moment the bug struck
Path(d1[0]).unlink()                                  # chdman replaces them
Path(d1[1]).unlink()
disc1_chd = DEST / "Race Game (USA) (Disc 1).chd"
disc1_chd.write_bytes(b"chd")
finish("chd", "disc1", d1[0], entry7["key"], chd=str(disc1_chd))

check("the game is NOT flagged converted while disc 2 is still a .7z",
      not entry7["stages"].get("chd"), str(entry7["stages"]))
check("and no playlist has been queued",
      not any(j.kind == "m3u" for j in hub7.pipeline.jobs.values()),
      str([j.kind for j in hub7.pipeline.jobs.values()]))

# 4. disc 2 is unpacked
d2 = lay_down_disc(2)
(DEST / "Race Game (USA) (Disc 2).7z").unlink()
finish("extract", "disc2", DEST / "Race Game (USA) (Disc 2).7z",
       entry7["key"], extracted=d2)
check("disc 2 gets its own conversion, rather than being skipped",
      len(chd_jobs_for("Race Game (USA) (Disc 2).cue")) == 1,
      str([j.label for j in hub7.pipeline.jobs.values() if j.kind == "chd"]))
check("still no playlist over an unconverted disc",
      not any(j.kind == "m3u" for j in hub7.pipeline.jobs.values()),
      str([j.kind for j in hub7.pipeline.jobs.values()]))

# 5. disc 2's conversion completes - only now is the game done
Path(d2[0]).unlink()
Path(d2[1]).unlink()
disc2_chd = DEST / "Race Game (USA) (Disc 2).chd"
disc2_chd.write_bytes(b"chd")
finish("chd", "disc2", d2[0], entry7["key"], chd=str(disc2_chd))
check("now the game is flagged converted", entry7["stages"].get("chd"),
      str(entry7["stages"]))
check("and the playlist follows",
      any(j.kind == "m3u" for j in hub7.pipeline.jobs.values()),
      str([j.kind for j in hub7.pipeline.jobs.values()]))

server.shutdown()
print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
