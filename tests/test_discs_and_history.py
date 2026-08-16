"""Disc choice survives lazy lookup.

A game at the bottom of a long queue must have its disc checkboxes ready
before the run reaches it, and a box ticked while an earlier game downloads
must still be honoured. Plus: finished games leave the queue, everything else
stays. All against a local server that counts what it is asked for.
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

srv.DATA_DIR = Path(tempfile.mkdtemp(prefix="vimmdh_data_"))
# The look-ahead paces itself against real downloads; here it paces itself
# against a local server, so it needs to be proportionally quicker.
srv.LOOKAHEAD_GAP = 0.05
srv.LOOKAHEAD_BUSY_GAP = 0.3

from fastapi.testclient import TestClient

failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    if not ok:
        failures.append(name)


def wait_for(predicate, timeout=60, poll=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(poll)
    return False


# ------------------------------------------------------------ test server
PAYLOAD = os.urandom(400_000)
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
    z.writestr("Game (USA).bin", PAYLOAD)
ZIP = buf.getvalue()

PAGE_HITS: list[int] = []      # vault ids requested
MEDIA_HITS: list[int] = []     # media ids actually downloaded
MODE = {"vault": "ok", "drip": False}

# Vault ids in this range are multi-disc; the media id of disc N of game G is
# G * 10 + N, so what was downloaded says exactly which discs were taken.
# 851 is here so every disc of it can be unticked, which is the only way to
# make the engine skip a game outright.
# One game per section. history.json and vault_cache.json live in a DATA_DIR
# shared by every hub in this file, so a reused id inherits the previous
# section's history entry and arrives already cached.
MULTI = {880: 3, 881: 3, 851: 2, 882: 3, 883: 3, 884: 3}


def media_entry(media_id, title, disc):
    return {
        "ID": media_id, "GoodTitle": base64.b64encode(title.encode()).decode(),
        "Serial": None, "SortOrder": disc, "Version": "1.0",
        "Zipped": str(len(ZIP) // 1024), "AltZipped": "0", "AltZipped2": "0",
        "GoodHash": None, "GoodMd5": None, "GoodSha1": None,
        "ZippedText": "0.4 MB", "AltZippedText": "0", "AltZipped2Text": "0",
        "Mirror": ["PS1"],
    }


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
            vault_id = int(self.path.rsplit("/", 1)[1])
            PAGE_HITS.append(vault_id)
            if MODE["vault"] == "busy":
                self._send(429, b"<html><body>You're currently downloading "
                                b"something. Please wait for your download to "
                                b"finish.</body></html>",
                           [("Content-Type", "text/html; charset=UTF-8")])
                return
            discs = MULTI.get(vault_id, 1)
            media = [media_entry(vault_id * 10 + n,
                                 f"Game {vault_id} (USA) (Disc {n}).bin", n)
                     for n in range(1, discs + 1)]
            host = f"http://127.0.0.1:{self.server.server_address[1]}/"
            html = (
                f"<html><head><title>The Vault: Game {vault_id} (PS1)</title></head>"
                f'<body><form action="{host}" method="POST" id="dl_form">'
                f'<input name="mediaId" value="{vault_id}"></form>'
                f"<script>let media={json.dumps(media)};</script></body></html>"
            ).encode()
            self._send(200, html, [("Content-Type", "text/html; charset=UTF-8")])
            return

        raw = self.path.split("mediaId=")[-1].split("&")[0]
        if not raw.isdigit():
            # The startup site check asks for "/" - answer it so the hub sees
            # a reachable site instead of a stack trace.
            self._send(200, b"<html><body>Vimm's Lair</body></html>",
                       [("Content-Type", "text/html; charset=UTF-8")])
            return
        media_id = int(raw)
        MEDIA_HITS.append(media_id)
        start = 0
        if self.headers.get("Range"):
            start = int(self.headers["Range"].split("=")[1].split("-")[0])
        body = ZIP[start:]
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
            for i in range(0, len(body), 16384):
                self.wfile.write(body[i:i + 16384])
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


def reset():
    PAGE_HITS.clear()
    MEDIA_HITS.clear()


# ================= the run looks each game up as it reaches it, once each
print("=== the run resolves as it goes, one page view per game ===")
OUT = Path(tempfile.mkdtemp(prefix="vimmdh_lazy_"))
app, hub = make_app(OUT)
client = TestClient(app)

with client:
    reset()
    ids = "\n".join(str(820 + n) for n in range(8))
    client.post("/api/queue", json={"text": ids})
    check("nothing resolved on add",
          not any(q["resolved"] for q in hub.queue))
    check("and nothing fetched on add", PAGE_HITS == [], str(PAGE_HITS))

    client.post("/api/run/start")
    check("run finished", wait_for(lambda: hub.run_status.startswith("finished"), 180),
          hub.run_status)
    check("every page fetched exactly once",
          sorted(PAGE_HITS) == [820 + n for n in range(8)], str(sorted(PAGE_HITS)))

# ======================= a disc unticked before Start stays unticked
print("=== unticking a disc before pressing Start ===")
# The run reports every page it reaches, cached or not, and rebuilding the
# disc list from it used to re-tick everything - undoing the choice on
# screen, and handing every disc back to the next `build_options()`.
OUT1B = Path(tempfile.mkdtemp(prefix="vimmdh_before_"))
app1b, hub1b = make_app(OUT1B)
client1b = TestClient(app1b)

with client1b:
    reset()
    client1b.post("/api/queue", json={"text": "882"})     # three discs
    client1b.post("/api/queue/882/resolve")
    client1b.post("/api/queue/882/discs", json={"discs": [1, 3]})
    check("disc 2 is unticked before the run starts",
          [d["selected"] for d in hub1b.queue_item(882)["discs"]] == [True, False, True],
          str(hub1b.queue_item(882)["discs"]))

    client1b.post("/api/run/start")
    check("run finished",
          wait_for(lambda: hub1b.run_status.startswith("finished"), 180),
          hub1b.run_status)

    # The game leaves the queue when it finishes, so the surviving evidence
    # is what was actually fetched.
    taken = sorted(m for m in MEDIA_HITS if m // 10 == 882)
    check("only the ticked discs were downloaded", taken == [8821, 8823], str(taken))

print("=== and the choice survives the page being applied again ===")
# This is the half that really downloaded the wrong files: a second Start,
# after a Pause or Stop, rebuilds the options from the live queue item.
OUT1C = Path(tempfile.mkdtemp(prefix="vimmdh_again_"))
app1c, hub1c = make_app(OUT1C)
client1c = TestClient(app1c)

with client1c:
    reset()
    client1c.post("/api/queue", json={"text": "883"})
    client1c.post("/api/queue/883/resolve")
    client1c.post("/api/queue/883/discs", json={"discs": [1, 3]})
    # Exactly what the run does when it reaches the game.
    hub1c.apply_page(883, hub1c._pages[883])
    check("disc 2 is still unticked afterwards",
          [d["selected"] for d in hub1c.queue_item(883)["discs"]] == [True, False, True],
          str(hub1c.queue_item(883)["discs"]))
    check("so a second Start would still skip it",
          hub1c.disc_overrides().get(883) == [1, 3], str(hub1c.disc_overrides()))
    check("and the run options rebuild the same way",
          hub1c.build_options().disc_overrides.get(883) == [1, 3])

print("=== a game nobody touched still defaults to every disc ===")
with client1c:
    client1c.post("/api/queue", json={"text": "884"})
    client1c.post("/api/queue/884/resolve")
    hub1c.apply_page(884, hub1c._pages[884])
    check("all three discs selected",
          all(d["selected"] for d in hub1c.queue_item(884)["discs"]),
          str(hub1c.queue_item(884)["discs"]))
    check("and it contributes no override",
          884 not in hub1c.disc_overrides(), str(hub1c.disc_overrides()))

# ==================================== a disc ticked mid-run is still honoured
print("=== unticking a disc while an earlier game downloads ===")
OUT2 = Path(tempfile.mkdtemp(prefix="vimmdh_tick_"))
app2, hub2 = make_app(OUT2)
client2 = TestClient(app2)
MODE["drip"] = True

with client2:
    reset()
    # Two slow single-disc games first, then the three-disc one at the back.
    client2.post("/api/queue", json={"text": "830\n831\n880"})
    # Opening the row is how you find out about the discs before its turn.
    client2.post("/api/queue/880/resolve")
    client2.post("/api/run/start")

    ready = (hub2.queue_item(880) or {}).get("resolved")
    check("the multi-disc game was resolved on demand, before its turn", ready)
    item = hub2.queue_item(880)
    check("it shows three discs", len(item.get("discs") or []) == 3,
          str(item.get("discs")))

    # Drop disc 2 while the run is still on an earlier game.
    r = client2.post("/api/queue/880/discs", json={"discs": [1, 3]})
    check("the change is accepted mid-run", r.status_code == 200, str(r.status_code))
    check("the live run options were updated",
          hub2._opts.disc_overrides.get(880) == [1, 3],
          str(hub2._opts.disc_overrides))

    check("run finished", wait_for(lambda: hub2.run_status.startswith("finished"), 180),
          hub2.run_status)
    taken = sorted(m for m in MEDIA_HITS if m // 10 == 880)
    check("only the discs left ticked were downloaded",
          taken == [8801, 8803], str(taken))

MODE["drip"] = False

# ================================== a game already under way cannot be changed
print("=== a disc choice cannot be changed once its game has started ===")
OUT3 = Path(tempfile.mkdtemp(prefix="vimmdh_late_"))
app3, hub3 = make_app(OUT3)
client3 = TestClient(app3)
MODE["drip"] = True

with client3:
    reset()
    client3.post("/api/queue", json={"text": "881"})
    client3.post("/api/run/start")
    check("download under way",
          wait_for(lambda: (hub3.queue_item(881) or {}).get("status") == "downloading", 30))

    r = client3.post("/api/queue/881/discs", json={"discs": [1]})
    check("the server refuses", r.status_code == 409, str(r.status_code))
    check("and says why", "too late" in r.json().get("error", ""),
          str(r.json()))
    check("the selection is untouched",
          all(d["selected"] for d in hub3.queue_item(881)["discs"]))

    client3.post("/api/run/stop")
    wait_for(lambda: not hub3.run_status.endswith("ing"), 30)

MODE["drip"] = False

# ================================== being throttled on lookup is harmless
print("=== a busy site during an on-demand lookup does not spoil the run ===")
OUT4 = Path(tempfile.mkdtemp(prefix="vimmdh_busy_"))
app4, hub4 = make_app(OUT4)
client4 = TestClient(app4)

with client4:
    reset()
    client4.post("/api/queue", json={"text": "840\n841"})
    MODE["vault"] = "busy"
    client4.post("/api/queue/840/resolve")     # meets the refusal
    MODE["vault"] = "ok"
    check("the refused game is still queued, not failed",
          hub4.queue_item(840)["status"] == "queued",
          hub4.queue_item(840)["status"])
    client4.post("/api/run/start")
    check("run finished anyway",
          wait_for(lambda: hub4.run_status.startswith("finished"), 180),
          hub4.run_status)
    check("nothing was marked failed by the lookup",
          not any(q["status"] == "failed" for q in hub4.queue),
          str([(q["vault_id"], q["status"]) for q in hub4.queue]))
    check("both games downloaded", "2 downloaded" in hub4.run_status,
          hub4.run_status)

# ======================== finished games leave the queue, others stay put
print("=== only completed games leave the queue ===")
OUT5 = Path(tempfile.mkdtemp(prefix="vimmdh_leave_"))
app5, hub5 = make_app(OUT5)
client5 = TestClient(app5)

with client5:
    reset()
    client5.post("/api/queue", json={"text": "850\n851"})
    # Deselect every disc of 851 so the engine skips it - a "skipped" game
    # is recorded nowhere else, so it has to stay visible in the queue.
    # Through the API, because only a deliberate choice counts as an override.
    client5.post("/api/queue/851/resolve")
    client5.post("/api/queue/851/discs", json={"discs": []})
    client5.post("/api/run/start")
    check("run finished", wait_for(lambda: hub5.run_status.startswith("finished"), 180),
          hub5.run_status)

    left = {q["vault_id"]: q["status"] for q in hub5.queue}
    check("the completed game is gone from the queue", 850 not in left, str(left))
    check("it is in the history",
          any(h["vault_id"] == 850 for h in hub5.history),
          str([h["vault_id"] for h in hub5.history]))
    check("the skipped game stays in the queue, with a reason",
          left.get(851) == "skipped", str(left))
    check("the skipped game is not in the history",
          not any(h["vault_id"] == 851 for h in hub5.history))

# ============ a multi-disc game stays put until every disc has arrived
print("=== a multi-disc game does not leave the queue after disc 1 ===")
OUT7 = Path(tempfile.mkdtemp(prefix="vimmdh_hold_"))
app7, hub7 = make_app(OUT7)
client7 = TestClient(app7)
MODE["drip"] = True

with client7:
    reset()
    client7.post("/api/queue", json={"text": "881"})     # three discs
    client7.post("/api/run/start")

    # Watch the queue for the whole run: the row must never vanish while
    # discs are still to come. Downloading in the background with no card is
    # exactly the bug this guards.
    seen_after_first = []
    check("disc 1 finished",
          wait_for(lambda: len([m for m in MEDIA_HITS if m // 10 == 881]) >= 2, 60),
          str(MEDIA_HITS))
    while not hub7.run_status.startswith("finished"):
        seen_after_first.append(hub7.queue_item(881) is not None)
        time.sleep(0.1)

    check("the row survived every disc after the first",
          all(seen_after_first), f"{seen_after_first.count(False)} sightings missing")
    check("all three discs downloaded",
          sorted(m for m in MEDIA_HITS if m // 10 == 881) == [8811, 8812, 8813],
          str(sorted(MEDIA_HITS)))
    check("only then does it leave the queue",
          hub7.queue_item(881) is None, str(hub7.queue))
    check("and it is in the history, with all three files",
          len(next(h for h in hub7.history if h["vault_id"] == 881)["files"]) == 3)

MODE["drip"] = False

# ======================= a known game costs nothing the second time around
print("=== the disk cache spares a repeat lookup ===")
OUT6 = Path(tempfile.mkdtemp(prefix="vimmdh_cache_"))
app6, hub6 = make_app(OUT6)          # a fresh Hub over the same DATA_DIR
client6 = TestClient(app6)

with client6:
    reset()
    # 880 was looked up earlier in this file, so its discs are already known.
    client6.post("/api/queue", json={"text": "880"})
    item = hub6.queue_item(880)
    check("its discs are there the moment it is queued",
          len(item.get("discs") or []) == 3, str(item.get("discs")))
    check("and it cost no page view", PAGE_HITS == [], str(PAGE_HITS))
    check("but it is not treated as a choice the user made",
          not item.get("chosen"), str(item.get("chosen")))

server.shutdown()
print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
