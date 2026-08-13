"""Cooperative cancellation: a cancel mid-stream stops promptly, keeps the
.part, and a fresh run resumes from it."""
import argparse
import io
import os
import sys
import tempfile
import threading
import time
import zipfile
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from pathlib import Path as _RepoPath
sys.path.insert(0, str(_RepoPath(__file__).resolve().parents[1]))
import vimm.engine as vd

PAYLOAD = os.urandom(4_000_000)
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
    z.writestr("Test (USA).gbc", PAYLOAD)
ZIP_BYTES = buf.getvalue()
INNER_CRC = f"{zlib.crc32(PAYLOAD) & 0xFFFFFFFF:08X}"
FILENAME = "Test (USA).zip"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        rng = self.headers.get("Range")
        start = int(rng.split("=")[1].split("-")[0]) if rng else 0
        body = ZIP_BYTES[start:]
        total = len(ZIP_BYTES)
        if start:
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{total-1}/{total}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition", f'attachment; filename="{FILENAME}"')
        self.end_headers()
        # Drip the body so there is time to cancel mid-stream.
        for i in range(0, len(body), 65536):
            self.wfile.write(body[i:i + 65536])
            self.wfile.flush()
            time.sleep(0.02)


def opts(**over):
    o = dict(vd.DEFAULTS)
    o.update(dict(list=False, quiet=True, delay=0, jitter=0, backoff=0.05))
    o.update(over)
    return argparse.Namespace(**o)


def media():
    return vd.Media(999, "1.0", 1, "Test (USA).gbc", [len(ZIP_BYTES), 0, 0],
                    ["3.8 MB", "0", "0"], ["GBC"], INNER_CRC, None, None)


srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
page = vd.VaultPage(1, "T (GBC)", f"http://127.0.0.1:{srv.server_address[1]}/")
dest = Path(tempfile.mkdtemp(prefix="vimmcancel_"))

# --- run 1: cancel ~0.6s in --------------------------------------------------
cancel = threading.Event()
client = vd.VimmClient(opts(), cancel_event=cancel)
timer = threading.Timer(0.6, cancel.set)
timer.start()
t0 = time.monotonic()
try:
    client.download(page, media(), 0, dest)
    print("FAIL: download completed, cancel never fired")
    sys.exit(1)
except vd.Cancelled:
    latency = time.monotonic() - t0 - 0.6
part = dest / (FILENAME + ".part")
kept = part.stat().st_size if part.exists() else 0
print(f"cancelled: reaction {latency:.2f}s after the event, kept {kept:,} bytes")
assert part.exists() and 0 < kept < len(ZIP_BYTES), ".part should be a real partial file"
assert latency < 1.0, "cancel should take effect within ~a chunk"

# --- run 2: fresh client resumes the .part -----------------------------------
client2 = vd.VimmClient(opts())
messages = []
client2.listener.status = messages.append
result = client2.download(page, media(), 0, dest)
print(f"resumed run: {result.status}  {result.message}")
print(f"  status lines: {messages}")
assert result.status == "ok" and "CRC ok" in result.message
assert any("resuming at" in m for m in messages), "second run should resume, not restart"
final = dest / FILENAME
assert final.read_bytes() == ZIP_BYTES

# --- cancel during a wait ----------------------------------------------------
cancel3 = threading.Event()
client3 = vd.VimmClient(opts(), cancel_event=cancel3)
threading.Timer(0.3, cancel3.set).start()
t0 = time.monotonic()
try:
    client3._sleep(30, "test wait")
    print("FAIL: wait was not interrupted")
    sys.exit(1)
except vd.Cancelled:
    took = time.monotonic() - t0
print(f"wait interrupted after {took:.2f}s (not 30s)")
assert took < 2

srv.shutdown()
print("\nAll cancellation cases passed.")
