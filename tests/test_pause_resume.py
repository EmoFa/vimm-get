"""Reproduce: pause then Start again restarts from scratch.

Hypothesis: the .part file is named from the server's Content-Disposition,
but a fresh download() call looks for a .part named from the media title
(stem + ".zip"). When the server's extension differs (.7z for the big disc
systems), the second run finds nothing to resume and starts over.
"""
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

PAYLOAD = os.urandom(3_000_000)
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
    z.writestr("Game (USA) (Disc 1).iso", PAYLOAD)
BLOB = buf.getvalue()

# The site serves the big disc systems as .7z, while media.filename has no
# extension at all - so the planned name is "... (Disc 1).zip".
SERVER_NAME = "Game (USA) (Disc 1).7z"
MEDIA_NAME = "Game (USA) (Disc 1)"

offsets = []


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        start = 0
        rng = self.headers.get("Range")
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
        offsets.append(start)
        body = BLOB[start:]
        self.send_response(206 if start else 200)
        if start:
            self.send_header("Content-Range", f"bytes {start}-{len(BLOB)-1}/{len(BLOB)}")
        self.send_header("Content-Type", "application/x-7z-compressed")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition", f'attachment; filename="{SERVER_NAME}"')
        self.end_headers()
        # Drip so the test can cancel mid-stream.
        for i in range(0, len(body), 65536):
            self.wfile.write(body[i:i + 65536])
            self.wfile.flush()
            time.sleep(0.02)


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
site = f"http://127.0.0.1:{server.server_address[1]}/"

OUT = Path(tempfile.mkdtemp(prefix="vimmpause_"))


def opts():
    o = dict(vd.DEFAULTS)
    o.update(dict(list=False, quiet=True, delay=0, jitter=0, backoff=0.05))
    return argparse.Namespace(**o)


def media():
    return vd.Media(media_id=77, version="1.0", disc=1, filename=MEDIA_NAME,
                    sizes=[len(BLOB), 0, 0], size_texts=["3 MB", "0", "0"],
                    formats=["PS1"], crc32=None, md5=None, sha1=None)


page = vd.VaultPage(vault_id=1, title="Game (PS1)", download_host=site)

failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    if not ok:
        failures.append(name)


# --- run 1: start, then "pause" (cancel) part-way -----------------------------
cancel = threading.Event()
client = vd.VimmClient(opts(), cancel_event=cancel)
threading.Timer(0.7, cancel.set).start()
try:
    client.download(page, media(), 0, OUT)
    print("  (unexpected: finished before the pause)")
except vd.Cancelled:
    pass

parts = sorted(p.name for p in OUT.iterdir() if p.name.endswith(".part"))
kept = sum(p.stat().st_size for p in OUT.iterdir() if p.name.endswith(".part"))
print(f"  after pause: {parts}  ({kept:,} bytes)")
check("a .part survives the pause", kept > 0)

# --- run 2: press Start again -------------------------------------------------
offsets.clear()
client2 = vd.VimmClient(opts())
statuses = []
client2.listener.status = statuses.append
result = client2.download(page, media(), 0, OUT)

print(f"  second run range offsets: {offsets}")
print(f"  status lines: {statuses}")
check("second run resumes instead of restarting", offsets and offsets[0] > 0,
      f"first request asked for byte {offsets[0] if offsets else '?'}")
check("engine logged a resume", any("resuming at" in s for s in statuses))
check("file complete and correct",
      (OUT / SERVER_NAME).is_file() and (OUT / SERVER_NAME).read_bytes() == BLOB)

server.shutdown()
print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
