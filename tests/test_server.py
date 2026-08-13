"""Server tests: queue/settings/run lifecycle against a local scripted vault
server (one mid-stream kill to prove resume through the whole web stack),
then a manual extract stage. Never touches the live site."""
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

# Isolate server state from the real data/ dir.
import vimm.server as srv

TESTDATA = Path(tempfile.mkdtemp(prefix="vimmsrvdata_"))
srv.DATA_DIR = TESTDATA

from fastapi.testclient import TestClient

import base64
import socket

failures = []


def check(name, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    if not condition:
        failures.append(name)


# ---- scripted vault server ---------------------------------------------------
PAYLOAD = os.urandom(1_500_000)
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
    z.writestr("Game 7 (USA).gbc", PAYLOAD)
    z.writestr("Vimm info.txt", "hello")
ZIP = buf.getvalue()
KILLS = {"remaining": 1}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/vault/"):
            vid = int(self.path.rsplit("/", 1)[1])
            b64 = base64.b64encode(f"Game {vid} (USA).gbc".encode()).decode()
            host = f"http://127.0.0.1:{self.server.server_address[1]}/"
            html = (f"<html><head><title>The Vault: Game {vid} (GBC)</title></head><body>"
                    f'<form action="{host}" method="POST" id="dl_form">'
                    f'<input name="mediaId" value="{vid * 10}"></form>'
                    f'<script>let media=[{{"ID":{vid * 10},"GoodTitle":"{b64}","Serial":null,'
                    f'"SortOrder":1,"Version":"1.0","Zipped":"{len(ZIP) // 1024}",'
                    f'"AltZipped":"0","AltZipped2":"0","GoodHash":null,"GoodMd5":null,'
                    f'"GoodSha1":null,"ZippedText":"1.4 MB","AltZippedText":"0",'
                    f'"AltZipped2Text":"0","Mirror":["GBC"]}}];</script></body></html>')
            body = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=UTF-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        start = 0
        rng = self.headers.get("Range")
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
        body = ZIP[start:]
        self.send_response(206 if start else 200)
        if start:
            self.send_header("Content-Range", f"bytes {start}-{len(ZIP) - 1}/{len(ZIP)}")
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition", 'attachment; filename="Game 7 (USA).zip"')
        self.end_headers()
        if KILLS["remaining"] > 0:
            KILLS["remaining"] -= 1
            cut = len(body) // 2
            self.wfile.write(body[:cut])
            self.wfile.flush()
            time.sleep(0.2)
            try:
                self.connection.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            self.connection.close()
            return
        self.wfile.write(body)


game_server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=game_server.serve_forever, daemon=True).start()
site = f"http://127.0.0.1:{game_server.server_address[1]}/"

# ---- app under test ----------------------------------------------------------
app = srv.create_app()
hub = app.state.hub
hub.site_base_override = site
OUT = Path(tempfile.mkdtemp(prefix="vimmsrvout_"))
client = TestClient(app)

with client:  # runs startup so hub.loop exists
    # settings round-trip
    r = client.put("/api/settings", json={
        "out": str(OUT), "organize": False, "delay": 1.0, "sweeps": 0,
        "auto_extract": False,
    })
    check("settings saved", r.json()["out"] == str(OUT))
    check("settings persisted", json.loads((TESTDATA / "settings.json").read_text())["sweeps"] == 0)

    # queue: text parse with a junk line
    r = client.post("/api/queue", json={"text": "7\nnot-an-id\n7\n"})
    check("queue add + dedup", r.json()["added"] == 1 and len(r.json()["queue"]) == 1)

    # queue: from search-hit shape
    r = client.post("/api/queue", json={"hits": [{"vault_id": 9, "title": "Game 9", "system": "GBC"}]})
    check("queue add from hits", r.json()["added"] == 1)
    r = client.post("/api/queue/reorder", json={"order": [9, 7]})
    check("reorder", [q["vault_id"] for q in r.json()["queue"]] == [9, 7])
    r = client.request("DELETE", "/api/queue/9")
    check("remove", [q["vault_id"] for q in r.json()["queue"]] == [7])

    # run: one mid-stream kill happens; engine must resume and finish
    with client.websocket_connect("/ws") as ws:
        r = client.post("/api/run/start")
        check("run started", r.json()["status"] == "started")
        deadline = time.time() + 60
        finished = False
        saw_progress = False
        while time.time() < deadline and not finished:
            event = json.loads(ws.receive_text())
            if event["type"] == "item" and event["item"].get("status") == "downloading":
                saw_progress = True
            if event["type"] == "run" and event["status"].startswith("finished"):
                finished = True
        check("run finished over WS", finished, hub.run_status)
        check("progress events over WS", saw_progress)

    item = hub.queue[0]
    check("item done after mid-stream kill (resume worked)", item["status"] == "done",
          item["message"])
    check("history entry created", len(hub.history) == 1)
    entry = hub.history[0]
    archive = Path(entry["files"][0]["archive"])
    check("archive on disk", archive.is_file() and archive.stat().st_size == len(ZIP))

    # manual extract stage through the API
    r = client.post(f"/api/items/{entry['key']}/extract")
    check("extract job accepted", r.json()["job"] is not None)
    deadline = time.time() + 30
    while time.time() < deadline and not hub.history[0]["stages"]["extracted"]:
        time.sleep(0.1)
    check("extract stage completed", hub.history[0]["stages"]["extracted"])
    check("rom extracted", (OUT / "Game 7 (USA).gbc").is_file())
    check("archive deleted after extract", not archive.exists())
    check("txt deleted after extract", not (OUT / "Vimm info.txt").exists())

    # state endpoint reflects everything
    state = client.get("/api/state").json()
    check("state has history + jobs + log",
          len(state["history"]) == 1 and len(state["jobs"]) >= 1 and len(state["log"]) > 0)

game_server.shutdown()
print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
