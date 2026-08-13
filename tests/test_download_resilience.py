"""Offline harness: reproduce interrupted / busy / stalled downloads locally.

A real HTTP server with Range support, told to misbehave in the specific ways
a contended Vimm IP misbehaves. This avoids deliberately tripping the real
site's concurrency limiter.
"""
import argparse
import io
import tempfile
import socket
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

# ---------------------------------------------------------------- payload ---
import os
# 4 MB: comfortably many 64 KB chunks, so an interruption mid-file
# leaves real bytes on disk - as it would with a 460 MB disc image.
PAYLOAD = os.urandom(4_000_000)
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
    z.writestr("Test Game (USA).gbc", PAYLOAD)
ZIP_BYTES = buf.getvalue()
INNER_CRC = f"{zlib.crc32(PAYLOAD) & 0xFFFFFFFF:08X}"
FILENAME = "Test Game (USA).zip"

BUSY_PAGE = (
    b"<html><body><h1>Download limit</h1><p>You already have a download "
    b"in progress. Only one download at a time is allowed per IP address. "
    b"Please wait and try again.</p></body></html>"
)

# behaviour knobs, mutated per test
STATE = {"mode": "ok", "kill_after": 0, "fail_times": 0, "count": 0, "log": []}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        STATE["count"] += 1
        n = STATE["count"]
        rng = self.headers.get("Range")
        start = 0
        if rng and rng.startswith("bytes="):
            start = int(rng.split("=")[1].split("-")[0])
        STATE["log"].append((n, start))

        mode = STATE["mode"]

        if mode == "busy" and n <= STATE["fail_times"]:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=UTF-8")
            self.send_header("Content-Length", str(len(BUSY_PAGE)))
            self.end_headers()
            self.wfile.write(BUSY_PAGE)
            return

        if mode == "stall" and n <= STATE["fail_times"]:
            # Headers, then silence: the half-open socket case.
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(ZIP_BYTES)))
            self.end_headers()
            self.wfile.flush()
            time.sleep(8)
            return

        body = ZIP_BYTES[start:]
        total = len(ZIP_BYTES)
        if start:
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{total - 1}/{total}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition", f'attachment; filename="{FILENAME}"')
        self.end_headers()

        if mode in ("kill", "reset") and n <= STATE["fail_times"]:
            # Declare the full length, send only part of it, then drop the
            # connection: what a transfer looks like when the slot is taken.
            cut = int(len(body) * STATE["kill_after"])
            self.wfile.write(body[:cut])
            self.wfile.flush()
            sock = self.connection
            if mode == "reset":
                # Abrupt RST. In-flight data is discarded by the OS, so the
                # client may salvage nothing - the harsher case.
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                b"\x01\x00\x00\x00\x00\x00\x00\x00")
            else:
                # Graceful FIN after the data has drained, so the bytes
                # already sent do reach the client.
                time.sleep(0.3)
                try:
                    sock.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
            sock.close()
            self.close_connection = True
            return

        self.wfile.write(body)


def make_opts(**over):
    o = dict(vd.DEFAULTS)
    o.update(dict(list=False, quiet=True, backoff=0.05, busy_wait=0.05,
                  busy_wait_max=0.2, delay=0, jitter=0, stall_timeout=2))
    o.update(over)
    return argparse.Namespace(**o)


def make_media():
    return vd.Media(media_id=999, version="1.0", disc=1,
                    filename="Test Game (USA).gbc",
                    sizes=[len(ZIP_BYTES), 0, 0], size_texts=["3.8 MB", "0", "0"],
                    formats=["GBC"], crc32=INNER_CRC, md5=None, sha1=None)


def run_case(name, mode, fail_times, kill_after=0.0, opts_over=None, out=None):
    STATE.update(mode=mode, fail_times=fail_times, kill_after=kill_after,
                 count=0, log=[])
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    opts = make_opts(**(opts_over or {}))
    page = vd.VaultPage(vault_id=1, title="Test Game (GBC)",
                        download_host=f"http://127.0.0.1:{port}/")
    client = vd.VimmClient(opts)
    dest = Path(out) if out else Path(tempfile.mkdtemp(prefix="vimmres_"))

    error = None
    result = None
    t0 = time.monotonic()
    try:
        result = client.download(page, make_media(), 0, dest)
    except vd.VimmError as exc:
        error = exc
    server.shutdown()

    final = dest / FILENAME
    ok = final.exists() and final.read_bytes() == ZIP_BYTES
    offsets = [s for _, s in STATE["log"]]
    print(f"\n--- {name} ---")
    print(f"  requests      : {STATE['count']}  range offsets: {offsets}")
    print(f"  elapsed       : {time.monotonic() - t0:.1f}s")
    print(f"  result        : {result.status if result else 'EXC'}"
          f"  {result.message if result else error}")
    print(f"  bytes exact   : {ok}")
    return ok, result, error, offsets


if __name__ == "__main__":
    failures = []

    # 1. Killed mid-stream twice. The whole point: must resume, not restart.
    ok, res, err, offs = run_case("kill-midstream", "kill", 2, kill_after=0.4)
    resumed = len(offs) >= 3 and offs[1] > 0 and offs[2] > offs[1]
    print(f"  resumed fwd   : {resumed}  (offsets should climb, not return to 0)")
    if not (ok and res and res.status == "ok" and resumed):
        failures.append("kill-midstream")
    if res and "CRC ok" not in res.message:
        failures.append("kill-midstream CRC")

    # 1b. Abrupt RST, where in-flight bytes may be lost entirely.
    ok, res, err, offs = run_case("reset-midstream", "reset", 2, kill_after=0.4)
    if not (ok and res and res.status == "ok"):
        failures.append("reset-midstream")

    # 2. Busy page twice, then the file.
    ok, res, err, offs = run_case("busy-page", "busy", 2)
    if not (ok and res and res.status == "ok"):
        failures.append("busy-page")

    # 3. Silent connection: must time out and resume, not hang.
    ok, res, err, offs = run_case("stall", "stall", 1)
    if not (ok and res and res.status == "ok"):
        failures.append("stall")

    # 4. Genuinely stuck: must terminate, not spin forever.
    ok, res, err, offs = run_case("give-up", "kill", 99, kill_after=0.0,
                                  opts_over={"retries": 3, "max_attempts": 20})
    print(f"  terminated    : {err is not None}")
    if err is None:
        failures.append("give-up should have raised")
    if STATE["count"] > 21:
        failures.append("give-up exceeded ceiling")

    # 5. Complete .part already on disk -> promoted, not re-downloaded.
    d = Path(tempfile.mkdtemp(prefix="vimm416_"))
    (d / (FILENAME + ".part")).write_bytes(ZIP_BYTES)
    ok, res, err, offs = run_case("complete-part", "ok", 0, out=str(d))
    print(f"  server hits   : {STATE['count']} (0-1 means it reused the .part)")
    if not (ok and res and res.status == "ok"):
        failures.append("complete-part")

    print("\n" + "=" * 60)
    if failures:
        print("FAILURES: " + ", ".join(failures))
        sys.exit(1)
    print("All resilience cases passed.")
