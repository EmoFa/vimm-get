"""Quiet by default; asks only when told to; takes the right file format.

Multi-format systems are the interesting part: the site publishes its own
`<select id="dl_format">` whose option values are the `alt` index, and for
Wii/Xbox/PS3 that select is the *only* place the formats are named - `Mirror[]`
reports one name for two genuinely different downloads.
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
from vimm.engine import choose_format, parse_formats, parse_vault_page

srv.DATA_DIR = Path(tempfile.mkdtemp(prefix="vimmpf_data_"))

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
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
    z.writestr("Game (USA).bin", os.urandom(60_000))
ZIP = buf.getvalue()

PAGE_HITS: list[int] = []
DOWNLOADS: list[tuple[int, int]] = []      # (media_id, alt)

# vault id -> (system label, format options, disc count, sizes per alt)
# Sizes mirror the real thing: a 0 means the site has no such file.
GAMES = {
    600: ("GameCube", [".ciso", ".nkit.iso", ".rvz"], 1, [900, 880, 870]),
    601: ("Wii", [".wbfs", ".rvz"], 1, [2000, 1400, 0]),
    602: ("Xbox", [".xiso.iso", ".iso"], 1, [700, 5500, 0]),
    603: ("PS3", ["JB Folder", ".dec.iso"], 1, [6600, 6800, 0]),
    # A GameCube game the site has no .rvz for: must fall back, not vanish.
    604: ("GameCube", [".ciso", ".nkit.iso", ".rvz"], 1, [900, 0, 0]),
    # Multi-disc PS1, single format.
    610: ("PS1", [], 3, [400, 0, 0]),
    611: ("PS1", [], 2, [400, 0, 0]),
}
# Games whose disc 1 has two revisions, for the version prompt.
TWO_VERSIONS = {620}


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
            system, formats, discs, sizes = GAMES.get(
                vault_id, ("PS1", [], 1, [400, 0, 0]))

            def entry(media_id, disc, version):
                return {
                    "ID": media_id, "SortOrder": disc, "Version": version,
                    "GoodTitle": base64.b64encode(
                        f"Game {vault_id} (USA) (Disc {disc}).bin".encode()).decode(),
                    "Serial": None,
                    "Zipped": str(sizes[0]), "AltZipped": str(sizes[1]),
                    "AltZipped2": str(sizes[2]),
                    "GoodHash": None, "GoodMd5": None, "GoodSha1": None,
                    "ZippedText": "a", "AltZippedText": "b", "AltZipped2Text": "c",
                    "Mirror": [system],
                }

            media = [entry(vault_id * 10 + n, n, "1.0")
                     for n in range(1, discs + 1)]
            if vault_id in TWO_VERSIONS:
                media.append(entry(vault_id * 10 + 9, 1, "1.1"))

            select = ""
            if formats:
                options = "".join(
                    f'<option value="{i}">{f}</option>'
                    for i, f in enumerate(formats))
                select = (f'<select id="dl_format" '
                          f'onchange="setFormat(\'dl_form\', this.value, media)">'
                          f'{options}</select>')
            host = f"http://127.0.0.1:{self.server.server_address[1]}/"
            html = (
                f"<html><head><title>The Vault: Game {vault_id} ({system})</title></head>"
                f'<body><form action="{host}" method="POST" id="dl_form">'
                f'<input name="mediaId" value="{vault_id}"></form>{select}'
                f"<script>let media={json.dumps(media)};</script></body></html>"
            ).encode()
            self._send(200, html, [("Content-Type", "text/html; charset=UTF-8")])
            return

        query = self.path.split("?", 1)[-1]
        raw = self.path.split("mediaId=")[-1].split("&")[0]
        if not raw.isdigit():
            self._send(200, b"<html><body>Vimm's Lair</body></html>",
                       [("Content-Type", "text/html; charset=UTF-8")])
            return
        alt = 0
        if "alt=" in query:
            alt = int(query.split("alt=")[-1].split("&")[0])
        DOWNLOADS.append((int(raw), alt))
        self._send(200, ZIP, [
            ("Content-Type", "application/zip"),
            ("Content-Disposition",
             f'attachment; filename="Game {raw} alt{alt}.zip"')])


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
SITE = f"http://127.0.0.1:{server.server_address[1]}"


def make_app(out_dir, **settings):
    app = srv.create_app()
    hub = app.state.hub
    hub.site_base_override = SITE
    hub.settings.update(out=str(out_dir), organize=False, delay=1.0,
                        sweeps=0, auto_extract=False, **settings)
    return app, hub


def reset():
    PAGE_HITS.clear()
    DOWNLOADS.clear()


# ============================================== the site's own format list
print("=== the page's format chooser is parsed, not guessed ===")
GC_HTML = ('<select id="dl_format" onchange="setFormat(\'dl_form\', this.value, media)">'
           '<option value="0" title=".ciso files">.ciso</option>'
           '<option value="1" title="nkit">.nkit.iso</option>'
           '<option value="2" title="rvz">.rvz</option></select>')
check("all three GameCube formats, in alt order",
      parse_formats(GC_HTML) == [".ciso", ".nkit.iso", ".rvz"],
      str(parse_formats(GC_HTML)))
check("a page without a chooser has no formats",
      parse_formats("<html><body>nothing here</body></html>") == [])
check("labels are unescaped and tidied",
      parse_formats('<select id="dl_format"><option value="0"> JB&nbsp;Folder '
                    "</option></select>")[0].endswith("Folder"))

# Mirror[] is not a substitute: Wii reports one name for two downloads.
from vimm.engine import Media
wii = Media(media_id=1, version="1.0", disc=1, filename="g.bin",
            sizes=[2000, 1400, 0], size_texts=["a", "b", "c"], formats=["Wii"],
            crc32=None, md5=None, sha1=None)
check("Mirror alone cannot find .rvz", choose_format(wii, ".rvz") == 0)
check("the page's labels can", choose_format(wii, ".rvz", [".wbfs", ".rvz"]) == 1)
check("an unknown format falls back to the site's default",
      choose_format(wii, ".nope", [".wbfs", ".rvz"]) == 0)
missing = Media(media_id=2, version="1.0", disc=1, filename="g.bin",
                sizes=[900, 0, 0], size_texts=["a", "b", "c"],
                formats=["GameCube"], crc32=None, md5=None, sha1=None)
check("a format the site has no file for falls back too",
      choose_format(missing, ".rvz", [".ciso", ".nkit.iso", ".rvz"]) == 0)

# ================================== the preference picks the right download
print("=== out of the box, every system takes Vimm's own default ===")
OUT = Path(tempfile.mkdtemp(prefix="vimmpf_fmt_"))
app, hub = make_app(OUT)
client = TestClient(app)

with client:
    reset()
    client.post("/api/queue", json={"text": "600\n601\n602\n603\n604"})
    client.post("/api/run/start")
    check("run finished", wait_for(lambda: hub.run_status.startswith("finished"), 180),
          hub.run_status)
    got = dict(DOWNLOADS)
    check("GameCube took .ciso (alt 0)", got.get(6001) == 0, str(got))
    check("Wii took .wbfs (alt 0)", got.get(6011) == 0, str(got))
    check("Xbox took .xiso.iso (alt 0)", got.get(6021) == 0, str(got))
    check("PS3 took JB Folder (alt 0)", got.get(6031) == 0, str(got))
    check("every default really is the site's first option",
          set(got.values()) == {0}, str(got))

print("=== changing the preference changes the download ===")
OUT2 = Path(tempfile.mkdtemp(prefix="vimmpf_fmt2_"))
app2, hub2 = make_app(OUT2, formats={"gc": ".rvz", "wii": ".rvz",
                                     "xbox": ".iso", "ps3": ".dec.iso"})
client2 = TestClient(app2)
with client2:
    reset()
    client2.post("/api/queue", json={"text": "600\n601\n602\n603\n604"})
    client2.post("/api/run/start")
    wait_for(lambda: hub2.run_status.startswith("finished"), 180)
    got = dict(DOWNLOADS)
    check("GameCube .rvz is alt 2", got.get(6001) == 2, str(got))
    check("Wii .rvz is alt 1", got.get(6011) == 1, str(got))
    check("Xbox .iso is alt 1", got.get(6021) == 1, str(got))
    check("PS3 .dec.iso is alt 1", got.get(6031) == 1, str(got))
    check("a GameCube game with no .rvz still downloaded, as alt 0",
          got.get(6041) == 0, str(got))

# ================================================= the default is silent
print("=== with the defaults, a multi-disc game asks nothing ===")
OUT3 = Path(tempfile.mkdtemp(prefix="vimmpf_quiet_"))
app3, hub3 = make_app(OUT3)
client3 = TestClient(app3)
with client3:
    reset()
    check("disc policy defaults to all", hub3.settings["disc_policy"] == "all")
    client3.post("/api/queue", json={"text": "610"})
    client3.post("/api/run/start")
    check("run finished", wait_for(lambda: hub3.run_status.startswith("finished"), 180),
          hub3.run_status)
    check("nothing was ever asked", hub3.prompt is None, str(hub3.prompt))
    check("all three discs downloaded",
          sorted(m for m, _ in DOWNLOADS) == [6101, 6102, 6103], str(DOWNLOADS))

# ============================================== "ask me" blocks and is heard
print("=== 'Discs - ask me' stops the run and takes the answer ===")
OUT4 = Path(tempfile.mkdtemp(prefix="vimmpf_ask_"))
app4, hub4 = make_app(OUT4, disc_policy="ask")
client4 = TestClient(app4)
with client4:
    reset()
    client4.post("/api/queue", json={"text": "610"})
    client4.post("/api/run/start")
    check("it asks", wait_for(lambda: hub4.prompt is not None, 30))
    prompt = hub4.prompt
    check("about the right game", prompt["vault_id"] == 610, str(prompt))
    check("offering every disc",
          [d["disc"] for d in prompt["discs"]] == [1, 2, 3], str(prompt["discs"]))

    time.sleep(0.8)
    check("and downloads nothing while it waits", DOWNLOADS == [], str(DOWNLOADS))

    r = client4.post(f"/api/prompt/{prompt['id']}", json={"answer": [1, 3]})
    check("the answer is accepted", r.status_code == 200, str(r.status_code))
    check("run finished", wait_for(lambda: hub4.run_status.startswith("finished"), 180),
          hub4.run_status)
    check("only the chosen discs downloaded",
          sorted(m for m, _ in DOWNLOADS) == [6101, 6103], str(DOWNLOADS))
    check("the question was withdrawn", hub4.prompt is None)

print("=== a single-disc game is never asked about ===")
OUT5 = Path(tempfile.mkdtemp(prefix="vimmpf_single_"))
app5, hub5 = make_app(OUT5, disc_policy="ask")
client5 = TestClient(app5)
with client5:
    reset()
    client5.post("/api/queue", json={"text": "602"})
    client5.post("/api/run/start")
    check("run finished", wait_for(lambda: hub5.run_status.startswith("finished"), 180),
          hub5.run_status)
    check("no prompt for one disc", hub5.prompt is None, str(hub5.prompt))

print("=== skipping a game at the prompt downloads none of it ===")
OUT6 = Path(tempfile.mkdtemp(prefix="vimmpf_skip_"))
app6, hub6 = make_app(OUT6, disc_policy="ask")
client6 = TestClient(app6)
with client6:
    reset()
    client6.post("/api/queue", json={"text": "611"})
    client6.post("/api/run/start")
    wait_for(lambda: hub6.prompt is not None, 30)
    client6.post(f"/api/prompt/{hub6.prompt['id']}", json={"answer": "skip"})
    check("run finished", wait_for(lambda: hub6.run_status.startswith("finished"), 180),
          hub6.run_status)
    check("nothing was downloaded", DOWNLOADS == [], str(DOWNLOADS))
    check("the game is marked skipped",
          hub6.queue_item(611)["status"] == "skipped",
          str(hub6.queue_item(611)["status"]))

# ================================================ a choice already made wins
print("=== a disc choice already ticked is not asked about again ===")
OUT7 = Path(tempfile.mkdtemp(prefix="vimmpf_chosen_"))
app7, hub7 = make_app(OUT7, disc_policy="ask")
client7 = TestClient(app7)
with client7:
    reset()
    client7.post("/api/queue", json={"text": "610"})
    client7.post("/api/queue/610/resolve")
    client7.post("/api/queue/610/discs", json={"discs": [2]})
    client7.post("/api/run/start")
    check("run finished", wait_for(lambda: hub7.run_status.startswith("finished"), 180),
          hub7.run_status)
    check("no prompt - the choice was already made", hub7.prompt is None,
          str(hub7.prompt))
    check("and it was honoured",
          sorted(m for m, _ in DOWNLOADS) == [6102], str(DOWNLOADS))

# ====================================== "Revision - ask me" uses `pick`
print("=== 'Revision - ask me' asks which revision ===")
OUT8 = Path(tempfile.mkdtemp(prefix="vimmpf_rev_"))
app8, hub8 = make_app(OUT8, version_policy="ask")
client8 = TestClient(app8)
with client8:
    reset()
    check("the run flag is set", hub8.build_options().pick is True)
    client8.post("/api/queue", json={"text": "620"})
    client8.post("/api/run/start")
    check("it asks", wait_for(lambda: hub8.prompt is not None, 30))
    prompt = hub8.prompt
    check("about revisions", prompt["kind"] == "versions", str(prompt["kind"]))
    versions = sorted(v["version"] for v in prompt["versions"])
    check("listing both", versions == ["1.0", "1.1"], str(versions))

    older = next(v["media_id"] for v in prompt["versions"] if v["version"] == "1.0")
    client8.post(f"/api/prompt/{prompt['id']}", json={"answer": [older]})
    check("run finished", wait_for(lambda: hub8.run_status.startswith("finished"), 180),
          hub8.run_status)
    check("the chosen revision downloaded",
          [m for m, _ in DOWNLOADS] == [older], str(DOWNLOADS))

# ==================================================== Stop is never trapped
print("=== Stop works while a question is on screen ===")
OUT9 = Path(tempfile.mkdtemp(prefix="vimmpf_stop_"))
app9, hub9 = make_app(OUT9, disc_policy="ask")
client9 = TestClient(app9)
with client9:
    reset()
    client9.post("/api/queue", json={"text": "610"})
    client9.post("/api/run/start")
    check("it asks", wait_for(lambda: hub9.prompt is not None, 30))
    t0 = time.time()
    client9.post("/api/run/stop")
    stopped = wait_for(lambda: not hub9.run_status.endswith("ing"), 30)
    check("Stop is not held up by the modal", stopped, hub9.run_status)
    check("and it was prompt about it", time.time() - t0 < 10,
          f"{time.time() - t0:.1f}s")
    check("the question was withdrawn", hub9.prompt is None)
    check("nothing downloaded", DOWNLOADS == [], str(DOWNLOADS))

print("=== a stale answer is refused ===")
with client9:
    r = client9.post("/api/prompt/deadbeef", json={"answer": [1]})
    check("409 for a question that is no longer open", r.status_code == 409,
          str(r.status_code))

server.shutdown()
print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
