"""A two-disc game must be post-processed as one game, not twice as half.

Reproduces the reported bug first: Skies of Arcadia (Dreamcast, 2 discs)
downloaded and extracted both discs, but only disc 1 was compressed to CHD
before the playlist folder was built.

`item_done` fires once per file. Starting the extract stage from the first
one lets extraction find no archives left to unpack, declare the whole game
extracted, and hand a half-finished game to CHD - which compresses the only
disc it can see and marks itself done, so disc 2 is never eligible again.

Uses real chdman, as the other post-processing tests do.
"""
import base64
import io
import json
import os
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


def wait_for(predicate, timeout=180, poll=0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(poll)
    return False


# ---------------------------------------------------- a real two-disc game
def disc_archive(stem: str) -> bytes:
    """A zip holding one genuinely valid single-track disc image."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr(f"{stem}.bin", os.urandom(2352 * 150))
        z.writestr(f"{stem}.cue",
                   f'FILE "{stem}.bin" BINARY\n  TRACK 01 MODE2/2352\n'
                   f"    INDEX 01 00:00:00\n")
    return buf.getvalue()


VAULT = 4242
STEMS = {1: "Skies of Arcadia (USA) (Disc 1)", 2: "Skies of Arcadia (USA) (Disc 2)"}
ARCHIVES = {disc: disc_archive(stem) for disc, stem in STEMS.items()}


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
            media = [{
                "ID": VAULT * 10 + disc, "SortOrder": disc, "Version": "1.0",
                "GoodTitle": base64.b64encode(f"{stem}.cue".encode()).decode(),
                "Serial": None,
                "Zipped": str(len(ARCHIVES[disc]) // 1024 or 1),
                "AltZipped": "0", "AltZipped2": "0",
                "GoodHash": None, "GoodMd5": None, "GoodSha1": None,
                "ZippedText": "1 MB", "AltZippedText": "0",
                "AltZipped2Text": "0", "Mirror": ["Dreamcast"],
            } for disc, stem in STEMS.items()]
            host = f"http://127.0.0.1:{self.server.server_address[1]}/"
            html = (
                f"<html><head><title>The Vault: Skies of Arcadia (Dreamcast)"
                f'</title></head><body><form action="{host}" method="POST" '
                f'id="dl_form"><input name="mediaId" value="{VAULT}"></form>'
                f"<script>let media={json.dumps(media)};</script></body></html>"
            ).encode()
            self._send(200, html, [("Content-Type", "text/html; charset=UTF-8")])
            return

        raw = self.path.split("mediaId=")[-1].split("&")[0]
        if not raw.isdigit():
            self._send(200, b"<html><body>Vimm's Lair</body></html>",
                       [("Content-Type", "text/html; charset=UTF-8")])
            return
        disc = int(raw) % 10
        stem = STEMS[disc]
        self._send(200, ARCHIVES[disc], [
            ("Content-Type", "application/zip"),
            ("Content-Disposition", f'attachment; filename="{stem}.zip"')])


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
SITE = f"http://127.0.0.1:{server.server_address[1]}"

OUT = Path(tempfile.mkdtemp(prefix="vimmmd_out_"))
app = srv.create_app()
hub = app.state.hub
hub.site_base_override = SITE
hub.settings.update(out=str(OUT), organize=True, delay=1.0, sweeps=0,
                    auto_extract=True, auto_compress=True, auto_m3u=True,
                    delete_chd_sources=True)
client = TestClient(app)

print("=== a two-disc Dreamcast game, every automation on ===")
with client:
    client.post("/api/queue", json={"text": str(VAULT)})
    client.post("/api/run/start")
    check("both discs downloaded",
          wait_for(lambda: hub.run_status.startswith("finished"), 180),
          hub.run_status)

    entry = next((h for h in hub.history if h["vault_id"] == VAULT), None)
    check("one history entry for the game", entry is not None)
    check("holding both discs", len(entry["files"]) == 2, str(len(entry["files"])))

    # The whole pipeline: extract -> chd -> m3u, chained automatically.
    def idle():
        return not any(j.status in ("queued", "running")
                       for j in hub.pipeline.jobs.values())

    check("the pipeline finished",
          wait_for(lambda: entry["stages"].get("m3u") and idle(), 300),
          str(entry["stages"]))

    on_disk = hub.entry_files(entry)
    chds = sorted(p.name for p in on_disk if p.suffix.lower() == ".chd")
    sheets = sorted(p.name for p in on_disk if p.suffix.lower() in (".cue", ".gdi"))
    playlists = sorted(p.name for p in on_disk if p.suffix.lower() == ".m3u")

    # This is the reported bug: one .chd and one leftover .cue.
    check("BOTH discs were compressed to CHD", len(chds) == 2, str(chds))
    check("no disc was left as a cue sheet", sheets == [], str(sheets))
    check("a playlist was written", len(playlists) == 1, str(playlists))

    if playlists:
        m3u = next(p for p in on_disk if p.suffix.lower() == ".m3u")
        listed = [line.strip() for line in
                  m3u.read_text(encoding="utf-8").splitlines() if line.strip()]
        check("the playlist lists both discs", len(listed) == 2, str(listed))
        check("and lists the .chd files, not the sheets",
              all(line.lower().endswith(".chd") for line in listed), str(listed))

    check("every stage is flagged done",
          all(entry["stages"].get(s) for s in ("extracted", "chd", "m3u")),
          str(entry["stages"]))
    check("the game left the queue only once it was wholly finished",
          hub.queue == [], str(hub.queue))

server.shutdown()
print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
