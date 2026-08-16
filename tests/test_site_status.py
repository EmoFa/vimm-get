"""Site reachability check: every branch, the false-positive guard, and the
server wiring. Points at a local server - never the live site."""
import json
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from pathlib import Path as _RepoPath
sys.path.insert(0, str(_RepoPath(__file__).resolve().parents[1]))

import vimm.server as srv

srv.DATA_DIR = Path(tempfile.mkdtemp(prefix="vimmsite_"))

from fastapi.testclient import TestClient

from vimm.engine import check_site

failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    if not ok:
        failures.append(name)


def wait_for(predicate, timeout=20, poll=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(poll)
    return False


def first_message(ws, timeout=10):
    """The socket's first message, or None if it never speaks.

    Read on a side thread: a plain receive blocks for ever when nothing is
    sent, and a regression here should fail the suite rather than hang it.
    """
    box = {}

    def read():
        try:
            box["text"] = ws.receive_text()
        except Exception as exc:  # noqa: BLE001
            box["error"] = exc

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    reader.join(timeout)
    return json.loads(box["text"]) if "text" in box else None


MODE = {"kind": "ok"}

HEALTHY = b"<html><body><h1>Vimm's Lair</h1><p>Welcome to the Vault.</p></body></html>"
MAINTENANCE_200 = (b"<html><body><h1>Vimm's Lair</h1>"
                   b"<p>We are down for maintenance, back soon.</p></body></html>")
# The vault's real tag text - contains "unavailable" but is NOT maintenance.
VAULT_TAG = (b'<html><body><span class="redBorder" '
             b'title="Download unavailable - Please upload it!">!</span>'
             b"</body></html>")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        kind = MODE["kind"]
        if kind == "hang":
            time.sleep(30)
            return
        body, status = {
            "ok": (HEALTHY, 200),
            "maintenance_200": (MAINTENANCE_200, 200),
            "maintenance_503": (b"<html>Service Unavailable</html>", 503),
            "vault_tag": (VAULT_TAG, 200),
            "server_error": (b"<html>oops</html>", 500),
            "not_found": (b"<html>nope</html>", 404),
        }[kind]
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=UTF-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
SITE = f"http://127.0.0.1:{server.server_address[1]}"

# ---------------------------------------------------------------- branches
print("=== classification ===")
CASES = [
    ("ok",              "up",          "healthy 200"),
    ("maintenance_503", "maintenance", "HTTP 503"),
    ("maintenance_200", "maintenance", "200 with a maintenance notice"),
    ("vault_tag",       "up",          "'Download unavailable' tag is NOT maintenance"),
    ("server_error",    "down",        "HTTP 500"),
    ("not_found",       "down",        "HTTP 404"),
]
for kind, want, label in CASES:
    MODE["kind"] = kind
    got = check_site(base=SITE, timeout=5)
    check(f"{label} -> {want}", got.state == want, f"got {got.state}: {got.detail}")

# unreachable host
gone = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
dead_port = gone.server_address[1]
gone.server_close()
got = check_site(base=f"http://127.0.0.1:{dead_port}", timeout=5)
check("nothing listening -> down", got.state == "down", f"{got.state}: {got.detail}")

# timeout
MODE["kind"] = "hang"
started = time.monotonic()
got = check_site(base=SITE, timeout=2)
elapsed = time.monotonic() - started
check("a hanging site -> down", got.state == "down", f"{got.state}: {got.detail}")
check("timeout is respected", elapsed < 6, f"{elapsed:.1f}s")
MODE["kind"] = "ok"

check("a fresh status starts unknown",
      srv.SiteStatus().state == "unknown")

# ------------------------------------------------------------ server wiring
print("=== server wiring ===")
app = srv.create_app()
hub = app.state.hub
hub.site_base_override = SITE
client = TestClient(app)

with client:
    # The startup hook fires the check; give the thread a moment.
    deadline = time.time() + 15
    while time.time() < deadline and hub.site.state in ("unknown", "checking"):
        time.sleep(0.1)
    check("checked automatically at startup", hub.site.state == "up",
          f"{hub.site.state}: {hub.site.detail}")

    state = client.get("/api/state").json()
    check("/api/state carries the site status",
          state.get("site", {}).get("state") == "up", str(state.get("site")))

    # Manual re-check, and the event that drives the indicator.
    with client.websocket_connect("/ws") as ws:
        MODE["kind"] = "maintenance_503"
        r = client.post("/api/site/check")
        check("re-check endpoint answers", "site" in r.json())
        seen = []
        deadline = time.time() + 15
        while time.time() < deadline:
            event = json.loads(ws.receive_text())
            if event["type"] == "site":
                seen.append(event["site"]["state"])
                if event["site"]["state"] not in ("checking",):
                    break
        check("a site event reaches the client", "maintenance" in seen, str(seen))
        check("the checking state is announced first", seen and seen[0] == "checking",
              str(seen))
    check("hub reflects the new reading", hub.site.state == "maintenance")

    # ------------------------------------------------ frontend freshness
    # A cached index.html is why the indicator appeared to be missing: the
    # button is plain HTML, so a stale shell shows nothing at all.
    MODE["kind"] = "ok"
    page = client.get("/")
    check("/ forbids caching",
          "no-store" in page.headers.get("cache-control", ""),
          page.headers.get("cache-control", "<none>"))
    check("the served page really contains the indicator",
          'id="site-status"' in page.text)
    css = client.get("/static/style.css")
    check("/static forbids caching",
          "no-store" in css.headers.get("cache-control", ""),
          css.headers.get("cache-control", "<none>"))
    check("the served stylesheet really styles the indicator",
          ".sitestatus.up .sitedot" in css.text)
    api = client.get("/api/state")
    check("api responses are left alone",
          "no-store" not in api.headers.get("cache-control", ""))

    # A site that never answers must not stop the app responding.
    MODE["kind"] = "hang"
    client.post("/api/site/check")
    started = time.monotonic()
    r = client.get("/api/state")
    elapsed = time.monotonic() - started
    check("state endpoint stays responsive during a hanging check",
          r.status_code == 200 and elapsed < 2, f"{elapsed:.2f}s")
    check("indicator shows 'checking' meanwhile", hub.site.state == "checking")

# ============ the reported bug: the answer arrives before anyone is listening
print("=== a client connecting late is still told the result ===")
# The startup check finishes about a second in, often between the page
# fetching /api/state and its socket connecting. Events go only to whoever is
# listening at the time, so that answer used to be thrown away and the
# indicator sat on "checking..." until clicked. Forced here rather than raced:
# the check is allowed to finish with nothing connected at all.
MODE["kind"] = "ok"
app2 = srv.create_app()
hub2 = app2.state.hub
hub2.site_base_override = SITE
client2 = TestClient(app2)

with client2:
    hub2.check_site()
    check("the check settles with no client attached",
          wait_for(lambda: hub2.site.state != "checking", 20), hub2.site.state)

    # Only now does a browser turn up.
    with client2.websocket_connect("/ws") as ws:
        first = first_message(ws) or {}
        check("the socket is greeted with a state snapshot",
              first.get("type") == "state",
              "nothing was sent at all" if not first else str(first.get("type")))
        site = first.get("state", {}).get("site", {})
        check("carrying a settled site state, not 'checking'",
              site.get("state") in ("up", "down", "maintenance"),
              str(site))
        check("and a reason to show", bool(site.get("detail")), str(site))

print("=== the snapshot carries the rest of the picture too ===")
with client2:
    hub2.run_status = "running"
    hub2.prompt = {"id": "abc", "kind": "discs", "title": "Something"}
    with client2.websocket_connect("/ws") as ws:
        snap = (first_message(ws) or {}).get("state", {})
        check("run status is included", snap.get("run") == "running",
              str(snap.get("run")))
        check("an outstanding question is included",
              (snap.get("prompt") or {}).get("id") == "abc",
              str(snap.get("prompt")))
    hub2.prompt = None
    hub2.run_status = "idle"

print("=== both routes serve the same thing ===")
with client2:
    over_http = set(client2.get("/api/state").json())
    with client2.websocket_connect("/ws") as ws:
        over_ws = set((first_message(ws) or {}).get("state", {}))
    check("/api/state and the socket snapshot agree on their keys",
          over_http == over_ws, str(over_http ^ over_ws))

print("=== a check that blows up still settles ===")
app3 = srv.create_app()
hub3 = app3.state.hub
_real_check = srv.check_site
srv.check_site = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
try:
    with TestClient(app3):
        hub3.check_site()
        check("it ends up down rather than stuck on checking",
              wait_for(lambda: hub3.site.state == "down", 20), hub3.site.state)
        check("and says what went wrong", "boom" in hub3.site.detail,
              hub3.site.detail)
finally:
    srv.check_site = _real_check

server.shutdown()
print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
