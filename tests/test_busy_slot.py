"""Cancel-the-blocking-download behaviour, reproduced offline.

The server mimics the real 429 page: it refuses while a slot is held, and
serves the file only once /cancel.php has been hit.
"""
import argparse
import io
import tempfile
import sys
import threading
import time
import zipfile
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from pathlib import Path as _RepoPath
sys.path.insert(0, str(_RepoPath(__file__).resolve().parents[1]))
import vimm.engine as vd

PAYLOAD = b"ROMDATA!" * 20000
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
    z.writestr("Test (USA).gbc", PAYLOAD)
ZIP_BYTES = buf.getvalue()
INNER_CRC = f"{zlib.crc32(PAYLOAD) & 0xFFFFFFFF:08X}"
FILENAME = "Test (USA).zip"

# Mirrors the real page, including the relative cancel link.
BUSY_429 = (
    "<!DOCTYPE HTML><html><head><title>Vimm's Lair: Error 429</title></head><body>"
    "<p>You're currently downloading <b>Final Fantasy VII</b> for the <b>PS1</b>.</p>"
    "<p>Please wait for your download to finish before starting another.</p>"
    "<p>If you're not downloading anymore you may "
    '<a href="/cancel.php">cancel your download</a>.</p>'
    "</body></html>"
).encode()

STATE = {"slot_held": True, "hits": [], "busy_forever": False}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        STATE["hits"].append(path)

        if path == "/cancel.php":
            if not STATE["busy_forever"]:
                STATE["slot_held"] = False
            body = b"<html><body>Your download has been cancelled.</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if STATE["slot_held"]:
            self.send_response(429)
            self.send_header("Content-Type", "text/html; charset=UTF-8")
            self.send_header("Content-Length", str(len(BUSY_429)))
            self.end_headers()
            self.wfile.write(BUSY_429)
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(ZIP_BYTES)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition", f'attachment; filename="{FILENAME}"')
        self.end_headers()
        self.wfile.write(ZIP_BYTES)


def opts(**over):
    o = dict(vd.DEFAULTS)
    o.update(dict(list=False, quiet=True, delay=0, jitter=0, backoff=0.05,
                  busy_wait=0.05, busy_wait_max=0.2, cancel_wait=0.05))
    o.update(over)
    return argparse.Namespace(**o)


def media():
    return vd.Media(999, "1.0", 1, "Test (USA).gbc", [len(ZIP_BYTES), 0, 0],
                    ["156 KB", "0", "0"], ["GBC"], INNER_CRC, None, None)


def run(name, o, busy_forever=False, out=None):
    STATE.update(slot_held=True, hits=[], busy_forever=busy_forever)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    page = vd.VaultPage(1, "T (GBC)", f"http://127.0.0.1:{srv.server_address[1]}/")
    c = vd.VimmClient(o)
    d = Path(out) if out else Path(tempfile.mkdtemp(prefix="vimmbusy_"))
    res = err = None
    t0 = time.monotonic()
    try:
        res = c.download(page, media(), 0, d)
    except vd.VimmError as e:
        err = e
    srv.shutdown()
    ok = (d / FILENAME).exists() and (d / FILENAME).read_bytes() == ZIP_BYTES
    print(f"\n--- {name} ---")
    print(f"  server hits : {STATE['hits']}")
    print(f"  cancels     : {c._cancels}   elapsed: {time.monotonic() - t0:.1f}s")
    print(f"  result      : {res.status if res else 'EXC'}  {res.message if res else err}")
    print(f"  bytes exact : {ok}")
    return ok, res, err, list(STATE["hits"]), c


fails = []

# 1. Default: hit the cancel link, then download immediately.
ok, res, err, hits, c = run("cancel-then-download", opts())
if not (ok and res and res.status == "ok"):
    fails.append("cancel-then-download")
if "/cancel.php" not in hits:
    fails.append("never called cancel.php")
if c._cancels != 1:
    fails.append(f"expected 1 cancel, got {c._cancels}")

# 2. --no-cancel-busy: must NOT touch cancel.php, and must give up waiting.
ok, res, err, hits, c = run("no-cancel-waits", opts(cancel_busy=False, busy_retries=2),
                            busy_forever=True)
print(f"  touched cancel.php: {'/cancel.php' in hits}")
if "/cancel.php" in hits:
    fails.append("--no-cancel-busy still cancelled")
if err is None:
    fails.append("--no-cancel-busy should eventually give up")

# 3. Cancel keeps failing to free the slot -> capped, then falls back to waiting.
ok, res, err, hits, c = run("cancel-capped", opts(max_cancels=3, busy_retries=2),
                            busy_forever=True)
n_cancels = hits.count("/cancel.php")
print(f"  cancel.php calls: {n_cancels} (capped at 3)")
if n_cancels > 3:
    fails.append(f"exceeded max_cancels: {n_cancels}")
if err is None:
    fails.append("should give up once capped and still busy")

print("\n" + "=" * 60)
if fails:
    print("FAILURES: " + "; ".join(fails))
    sys.exit(1)
print("All cancel cases passed.")
