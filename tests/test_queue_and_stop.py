"""Queueing costs no requests, each page is fetched once, and Stop differs
from Pause. All against a local server that counts what it is asked for."""
import base64
import io
import json
import os
import socket
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

srv.DATA_DIR = Path(tempfile.mkdtemp(prefix="vimmqs_data_"))

from fastapi.testclient import TestClient

failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    if not ok:
        failures.append(name)


def wait_for(predicate, timeout=60, poll=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(poll)
    return False


# ------------------------------------------------------------ test server
PAYLOAD = os.urandom(900_000)
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
    z.writestr("Game (USA).gbc", PAYLOAD)
ZIP = buf.getvalue()

PAGE_HITS: list[int] = []          # vault ids requested
MODE = {"vault": "ok", "drip": False}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/vault/"):
            vault_id = int(self.path.rsplit("/", 1)[1])
            PAGE_HITS.append(vault_id)
            if MODE["vault"] == "busy":
                body = (b"<html><body>You're currently downloading something. "
                        b"Please wait for your download to finish.</body></html>")
                self.send_response(429)
                self.send_header("Content-Type", "text/html; charset=UTF-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            title = base64.b64encode(f"Game {vault_id} (USA).gbc".encode()).decode()
            host = f"http://127.0.0.1:{self.server.server_address[1]}/"
            html = (
                f"<html><head><title>The Vault: Game {vault_id} (GBC)</title></head>"
                f'<body><form action="{host}" method="POST" id="dl_form">'
                f'<input name="mediaId" value="{vault_id}"></form>'
                f'<script>let media=[{{"ID":{vault_id},"GoodTitle":"{title}",'
                f'"Serial":null,"SortOrder":1,"Version":"1.0",'
                f'"Zipped":"{len(ZIP)//1024}","AltZipped":"0","AltZipped2":"0",'
                f'"GoodHash":null,"GoodMd5":null,"GoodSha1":null,'
                f'"ZippedText":"0.9 MB","AltZippedText":"0","AltZipped2Text":"0",'
                f'"Mirror":["GBC"]}}];</script></body></html>').encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=UTF-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return

        start = 0
        if self.headers.get("Range"):
            start = int(self.headers["Range"].split("=")[1].split("-")[0])
        body = ZIP[start:]
        # Name the download after the media, as the real site does - that
        # correspondence is what lets a later run find its own partial file.
        media_id = self.path.split("mediaId=")[-1].split("&")[0]
        self.send_response(206 if start else 200)
        if start:
            self.send_header("Content-Range",
                             f"bytes {start}-{len(ZIP)-1}/{len(ZIP)}")
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition",
                         f'attachment; filename="Game {media_id} (USA).zip"')
        self.end_headers()
        if MODE["drip"]:
            # Slow enough that a pause or stop lands mid-transfer.
            for i in range(0, len(body), 32768):
                self.wfile.write(body[i:i + 32768])
                self.wfile.flush()
                time.sleep(0.05)
            return
        self.wfile.write(body)


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
SITE = f"http://127.0.0.1:{server.server_address[1]}"


def make_app(out_dir):
    app = srv.create_app()
    hub = app.state.hub
    hub.site_base_override = SITE
    hub.settings.update(out=str(out_dir), organize=False, delay=1.0,
                        sweeps=0, auto_extract=False)
    return app, hub


# =================================================== adding costs nothing
print("=== adding IDs makes no requests ===")
OUT = Path(tempfile.mkdtemp(prefix="vimmqs_out_"))
app, hub = make_app(OUT)
client = TestClient(app)

with client:
    PAGE_HITS.clear()
    ids = "\n".join(str(700 + n) for n in range(10))
    r = client.post("/api/queue", json={"text": ids})
    check("ten IDs queued", r.json()["added"] == 10)
    time.sleep(1.5)  # any background lookup would have fired by now
    check("no vault pages fetched on add", PAGE_HITS == [], str(PAGE_HITS))
    check("rows are unresolved but usable",
          all(q["title"].startswith("vault/") and not q["resolved"]
              for q in hub.queue))

    # ------------------------------------------- on-demand lookup, once
    print("=== looking one up, then running it, fetches its page once ===")
    PAGE_HITS.clear()
    r = client.post("/api/queue/700/resolve")
    check("resolve fills in the real title",
          r.json()["item"]["title"] == "Game 700 (GBC)", str(r.json()["item"]["title"]))
    check("exactly one page fetch", PAGE_HITS == [700], str(PAGE_HITS))

    # Trim the queue to just this game and run it.
    for n in range(701, 710):
        client.request("DELETE", f"/api/queue/{n}")
    PAGE_HITS.clear()
    client.post("/api/run/start")
    check("run finished", wait_for(lambda: hub.run_status.startswith("finished")),
          hub.run_status)
    check("the run reused the cached page, fetching nothing",
          PAGE_HITS == [], str(PAGE_HITS))
    check("the file downloaded", (OUT / "Game 700 (USA).zip").is_file())
    check("the finished game left the queue", hub.queue == [], str(hub.queue))

# ============================================== a run resolves as it goes
print("=== an unresolved queue resolves during the run, one fetch each ===")
OUT2 = Path(tempfile.mkdtemp(prefix="vimmqs_out2_"))
app2, hub2 = make_app(OUT2)
client2 = TestClient(app2)
with client2:
    PAGE_HITS.clear()
    client2.post("/api/queue", json={"text": "801\n802\n803"})
    check("start is available immediately, without waiting on lookups",
          client2.post("/api/run/start").json()["status"] == "started")
    check("run finished", wait_for(lambda: hub2.run_status.startswith("finished"), 90),
          hub2.run_status)
    check("three games, three page fetches - no duplicates",
          sorted(PAGE_HITS) == [801, 802, 803], str(PAGE_HITS))
    # Finished games live in the history now, so the names are checked there
    # rather than in a queue they have rightly left.
    check("the queue is empty once everything finished",
          hub2.queue == [], str(hub2.queue))
    # History is stored under a DATA_DIR shared by the whole suite, so earlier
    # sections' downloads are in here too - these three must be present, not
    # be the only ones.
    titles = {h["title"] for h in hub2.history}
    check("names filled in from the run",
          {"Game 801 (GBC)", "Game 802 (GBC)", "Game 803 (GBC)"} <= titles,
          str(sorted(titles)))

# ================================================ throttling is not failure
print("=== a 429 on lookup leaves the game queued, not failed ===")
OUT3 = Path(tempfile.mkdtemp(prefix="vimmqs_out3_"))
app3, hub3 = make_app(OUT3)
client3 = TestClient(app3)
with client3:
    client3.post("/api/queue", json={"text": "900"})
    MODE["vault"] = "busy"
    client3.post("/api/queue/900/resolve")
    MODE["vault"] = "ok"
    item = hub3.queue[0]
    check("still queued, not failed", item["status"] == "queued", item["status"])
    check("left unresolved so it can be tried again", not item["resolved"])
    check("says why", "busy" in item["message"].lower(), item["message"])

# ================================================== Pause keeps, Stop discards
print("=== Pause keeps partial files; Stop discards them ===")
MODE["drip"] = True

for label, pause in (("Pause", True), ("Stop", False)):
    out = Path(tempfile.mkdtemp(prefix=f"vimmqs_{label.lower()}_"))
    app4, hub4 = make_app(out)
    client4 = TestClient(app4)
    with client4:
        client4.post("/api/queue", json={"text": "950"})
        client4.post("/api/run/start")
        started = wait_for(lambda: any(q["status"] == "downloading"
                                       for q in hub4.queue), 30)
        check(f"{label}: download under way", started)
        time.sleep(0.6)

        if not pause:
            summary = client4.get("/api/run/partials").json()
            check("Stop: the prompt can name a real size",
                  summary["count"] == 1 and summary["bytes"] > 0, str(summary))

        client4.post("/api/run/pause" if pause else "/api/run/stop")
        wait_for(lambda: not hub4.run_status.endswith("ing"), 30)
        time.sleep(0.5)

        parts = list(out.glob("*.part"))
        item = hub4.queue[0]
        if pause:
            check("Pause: the .part is kept", len(parts) == 1, str(parts))
            check("Pause: item shows paused", item["status"] == "paused",
                  item["status"])
            # And Start picks it up again rather than starting over.
            MODE["drip"] = False
            client4.post("/api/run/start")
            check("Pause: Start finishes it",
                  wait_for(lambda: hub4.run_status.startswith("finished"), 60),
                  hub4.run_status)
            check("Pause: the finished game left the queue",
                  hub4.queue == [], str(hub4.queue))
            check("Pause: file is complete and correct",
                  (out / "Game 950 (USA).zip").read_bytes() == ZIP)
            MODE["drip"] = True
        else:
            check("Stop: the .part is gone", parts == [], str(parts))
            check("Stop: item is back to queued", item["status"] == "queued",
                  item["status"])
            check("Stop: says what it did",
                  "discard" in hub4.run_status, hub4.run_status)

MODE["drip"] = False

# A finished download must never be swept up by Stop.
print("=== Stop never touches a completed file ===")
out5 = Path(tempfile.mkdtemp(prefix="vimmqs_keep_"))
app5, hub5 = make_app(out5)
client5 = TestClient(app5)
with client5:
    client5.post("/api/queue", json={"text": "960"})
    client5.post("/api/run/start")
    wait_for(lambda: hub5.run_status.startswith("finished"), 60)
    check("downloaded", (out5 / "Game 960 (USA).zip").is_file())
    summary = client5.get("/api/run/partials").json()
    check("nothing reported as discardable", summary["count"] == 0, str(summary))
    client5.post("/api/run/stop")
    time.sleep(0.4)
    check("the finished file survives Stop", (out5 / "Game 960 (USA).zip").is_file())

# ==================================== adding to a queue that is already running
print("=== a game added mid-run is picked up without pressing Start again ===")
MODE["drip"] = True
out6 = Path(tempfile.mkdtemp(prefix="vimmqs_late_"))
app6, hub6 = make_app(out6)
client6 = TestClient(app6)
with client6:
    client6.post("/api/queue", json={"text": "970"})
    check("started", client6.post("/api/run/start").json()["status"] == "started")
    check("the first game is downloading",
          wait_for(lambda: any(q["status"] == "downloading" for q in hub6.queue), 30))

    # Add while the run is in flight. Start is deliberately not pressed again.
    client6.post("/api/queue", json={"text": "971"})
    MODE["drip"] = False

    check("run finished", wait_for(lambda: hub6.run_status.startswith("finished"), 120),
          hub6.run_status)
    check("both games downloaded, on one Start",
          "2 downloaded" in hub6.run_status, hub6.run_status)
    check("the first file is there", (out6 / "Game 970 (USA).zip").is_file())
    check("and so is the late arrival", (out6 / "Game 971 (USA).zip").is_file())
    check("the queue is empty", hub6.queue == [], str(hub6.queue))

print("=== the loop cannot take the same game twice ===")
# What bounds the loop: a game already attempted is never offered again, and
# only "queued" items are, so an id that ends up failed or paused does not
# keep coming back round.
out7 = Path(tempfile.mkdtemp(prefix="vimmqs_bound_"))
app7, hub7 = make_app(out7)
hub7.queue = [
    {"vault_id": 1, "status": "queued"},
    {"vault_id": 2, "status": "queued"},
    {"vault_id": 3, "status": "failed"},
    {"vault_id": 4, "status": "paused"},
    {"vault_id": 5, "status": "downloading"},
]
check("fresh queued games are offered",
      hub7._pending_ids(set()) == [1, 2], str(hub7._pending_ids(set())))
check("one already attempted is not offered again",
      hub7._pending_ids({1}) == [2], str(hub7._pending_ids({1})))
check("nothing is left once both have had a turn",
      hub7._pending_ids({1, 2}) == [], str(hub7._pending_ids({1, 2})))
check("failed, paused and in-flight games are never picked up",
      hub7._pending_ids(set()) == [1, 2])

print("=== Stop still ends the run between batches ===")
MODE["drip"] = True
out8 = Path(tempfile.mkdtemp(prefix="vimmqs_latestop_"))
app8, hub8 = make_app(out8)
client8 = TestClient(app8)
with client8:
    client8.post("/api/queue", json={"text": "990"})
    client8.post("/api/run/start")
    wait_for(lambda: any(q["status"] == "downloading" for q in hub8.queue), 30)
    client8.post("/api/queue", json={"text": "991"})     # queued behind it
    client8.post("/api/run/stop")
    check("the run stops promptly", wait_for(
        lambda: not hub8.run_status.endswith("ing"), 30), hub8.run_status)
    check("it says it discarded", "discard" in hub8.run_status, hub8.run_status)
    check("the late arrival was never started",
          not (out8 / "Game 991 (USA).zip").is_file())
MODE["drip"] = False

server.shutdown()
print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
