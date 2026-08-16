"""Holding downloads back while post-processing runs, for slow drives.

Downloading and extracting at once is free on a normal disk and finishes
sooner, so it stays the default. On a drive that can only manage one stream
it is ruinous - measured on a USB flash drive, a second writer cost 74% of
the download's throughput and produced stalls over three seconds long. Hence
an opt-in setting.
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
from vimm.pipeline import Job, PipelineWorker

srv.DATA_DIR = Path(tempfile.mkdtemp(prefix="vimmwait_data_"))

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


# ================================================== idle() is exact, not approximate
print("=== the pipeline reports idle only when it really is ===")
events = []
gate = threading.Event()
worker = PipelineWorker(on_event=lambda job: events.append((job.kind, job.status)))
check("idle while nothing has been submitted",
      wait_for(worker.idle, 5), str(worker.idle()))

# A job whose work blocks until we let go, so "running" is observable.
def slow_execute(job):
    gate.wait(10)


worker._execute = slow_execute
worker.submit(Job(kind="extract", label="slow", target=Path("x"), item_key="k"))
check("not idle while a job runs",
      wait_for(lambda: not worker.idle(), 5), str(worker.idle()))
gate.set()
check("idle again once the worker parks", wait_for(worker.idle, 5))

# The window the parked flag exists for: a job is marked done before the
# event that queues the next stage is delivered, so job statuses alone would
# read as idle mid-chain.
print("=== idle() stays false through the gap between chained stages ===")
seen_idle_midchain = []
chained = PipelineWorker()
chained._execute = lambda job: None


def chain(job):
    if job.status == "done" and job.kind == "extract":
        # Exactly the moment _on_job_event queues the next stage.
        seen_idle_midchain.append(chained.idle())
        chained.submit(Job(kind="chd", label="next", target=Path("y"), item_key="k"))


chained.on_event = chain
chained.submit(Job(kind="extract", label="first", target=Path("x"), item_key="k"))
check("the chain completed", wait_for(chained.idle, 5))
check("and it never looked idle while chaining",
      seen_idle_midchain == [False], str(seen_idle_midchain))

# ============================================ the gate itself holds and releases
print("=== the gate holds a download back, and lets it go ===")
hub = srv.Hub()
hub.settings["wait_for_processing"] = True
listener = srv.WebListener(hub)

held = threading.Event()
hub.pipeline._execute = lambda job: held.wait(20)
hub.pipeline.submit(Job(kind="chd", label="busy", target=Path("z"), item_key="k"))
wait_for(lambda: not hub.pipeline.idle(), 5)

returned = threading.Event()
threading.Thread(target=lambda: (listener.before_download(), returned.set()),
                 daemon=True).start()
check("it is still waiting while the job runs", not returned.wait(1.0))
held.set()
check("and returns once the pipeline is quiet", returned.wait(10))

print("=== with the setting off it never waits ===")
hub2 = srv.Hub()
hub2.settings["wait_for_processing"] = False
listener2 = srv.WebListener(hub2)
busy = threading.Event()
hub2.pipeline._execute = lambda job: busy.wait(20)
hub2.pipeline.submit(Job(kind="chd", label="busy", target=Path("z"), item_key="k"))
wait_for(lambda: not hub2.pipeline.idle(), 5)
t0 = time.time()
listener2.before_download()
check("returned immediately despite a running job", time.time() - t0 < 0.5,
      f"{time.time() - t0:.2f}s")
busy.set()

print("=== Stop is never trapped behind the gate ===")
hub3 = srv.Hub()
hub3.settings["wait_for_processing"] = True
listener3 = srv.WebListener(hub3)
stuck = threading.Event()
hub3.pipeline._execute = lambda job: stuck.wait(30)
hub3.pipeline.submit(Job(kind="chd", label="long", target=Path("z"), item_key="k"))
wait_for(lambda: not hub3.pipeline.idle(), 5)
released = threading.Event()
threading.Thread(target=lambda: (listener3.before_download(), released.set()),
                 daemon=True).start()
check("waiting as expected while the conversion runs", not released.wait(0.6))
t0 = time.time()
hub3._cancel.set()
check("cancelling releases it promptly", released.wait(5),
      f"{time.time() - t0:.2f}s")
stuck.set()

# ============================================= end to end: downloads are serialised
print("=== with the setting on, disc 2 waits for disc 1 to be processed ===")

buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
    z.writestr("Game (USA).bin", os.urandom(80_000))
ZIP = buf.getvalue()

VAULT = 5150
REQUESTS: list[tuple[int, float]] = []       # (disc, when)


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
                "GoodTitle": base64.b64encode(
                    f"Game (USA) (Disc {disc}).bin".encode()).decode(),
                "Serial": None, "Zipped": "1", "AltZipped": "0",
                "AltZipped2": "0", "GoodHash": None, "GoodMd5": None,
                "GoodSha1": None, "ZippedText": "1 MB", "AltZippedText": "0",
                "AltZipped2Text": "0", "Mirror": ["GBC"],
            } for disc in (1, 2)]
            host = f"http://127.0.0.1:{self.server.server_address[1]}/"
            html = (
                f"<html><head><title>The Vault: Game (GBC)</title></head>"
                f'<body><form action="{host}" method="POST" id="dl_form">'
                f'<input name="mediaId" value="{VAULT}"></form>'
                f"<script>let media={json.dumps(media)};</script></body></html>"
            ).encode()
            self._send(200, html, [("Content-Type", "text/html; charset=UTF-8")])
            return
        raw = self.path.split("mediaId=")[-1].split("&")[0]
        if not raw.isdigit():
            self._send(200, b"ok", [("Content-Type", "text/html")])
            return
        REQUESTS.append((int(raw) % 10, time.time()))
        self._send(200, ZIP, [
            ("Content-Type", "application/zip"),
            ("Content-Disposition",
             f'attachment; filename="Game (USA) (Disc {int(raw) % 10}).zip"')])


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
SITE = f"http://127.0.0.1:{server.server_address[1]}"

OUT = Path(tempfile.mkdtemp(prefix="vimmwait_out_"))
app = srv.create_app()
hub4 = app.state.hub
hub4.site_base_override = SITE
hub4.settings.update(out=str(OUT), organize=False, delay=1.0, sweeps=0,
                     auto_extract=True, auto_compress=False, auto_m3u=False,
                     wait_for_processing=True)

# Extraction has to outlast the pause the engine already takes between
# downloads - `delay` plus up to `jitter`, so up to 4s here. Otherwise disc 2
# would arrive late anyway and the assertion below would pass whether the
# gate worked or not.
real_submit = hub4.pipeline._execute
extract_finished: list[float] = []
SLOW_EXTRACT = 6.0


def slow_extract(job):
    if job.kind == "extract":
        time.sleep(SLOW_EXTRACT)
    real_submit(job)
    if job.kind == "extract":
        extract_finished.append(time.time())


hub4.pipeline._execute = slow_extract
client = TestClient(app)

with client:
    client.post("/api/queue", json={"text": str(VAULT)})
    client.post("/api/run/start")
    check("the run finished",
          wait_for(lambda: hub4.run_status.startswith("finished"), 120),
          hub4.run_status)
    check("both discs were fetched", len(REQUESTS) == 2, str(REQUESTS))
    # The run can outpace the pipeline when the gate is off, so wait for the
    # extraction before comparing - otherwise the interesting assertion below
    # would be skipped in exactly the case it is meant to catch.
    check("disc 1's extraction finished",
          wait_for(lambda: extract_finished, 60), str(extract_finished))

    if len(REQUESTS) == 2 and extract_finished:
        disc2_started = next(when for disc, when in REQUESTS if disc == 2)
        check("disc 2 did not start until disc 1 was processed",
              disc2_started > extract_finished[0],
              f"disc2 at {disc2_started:.2f}, extract done {extract_finished[0]:.2f}")

server.shutdown()
print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
