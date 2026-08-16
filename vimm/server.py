"""
Local web server: the browser talks to this on localhost, and this talks to
Vimm's Lair. REST for actions and state, a WebSocket for live events, static
files for the frontend. One engine download at a time (the engine's rule),
plus one post-processing job at a time on its own worker.

State lives in data/: settings.json and history.json.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import chd as chd_mod
from . import search as search_mod
from .engine import (
    NEXT_DISC_WAIT,
    Cancelled,
    Listener,
    Result,
    SiteStatus,
    VimmError,
    check_site,
    destination_for,
    find_download,
    human_bytes,
    human_duration,
    make_options,
    safe_filename,
    parse_id_lines,
    run_http,
    system_folder,
    write_log,
)
from .m3u import DEFAULT_M3U_SYSTEMS
from .pipeline import Job, PipelineWorker

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
WEB_DIR = ROOT / "web"

# Systems worth converting to CHD, and the systems offered in the settings.
# Two reasons a system belongs: its games arrive as split disc images that CHD
# collapses into one file (the bin+cue and gdi systems), or its images are
# simply large and its emulator reads CHD - which is the case for PS2, where
# PCSX2 opens .chd directly. Both Jaguars stay out because BigPEmu cannot read
# CHD, and GameCube stays out because Dolphin cannot either; .rvz is the
# equivalent there and is offered under FORMATS instead.
CHD_SYSTEM_OPTIONS = [
    ("psx", "PS1"), ("saturn", "Saturn"), ("segacd", "Sega CD"),
    ("tgcd", "TurboGrafx-CD"), ("dreamcast", "Dreamcast"),
    ("cdimono1", "Philips CD-i"), ("ps2", "PS2"),
]
DEFAULT_CHD_SYSTEMS = [folder for folder, _ in CHD_SYSTEM_OPTIONS]

# Systems whose emulators read .m3u for disc swapping. GameCube is here but
# not in the CHD list above: Dolphin handles multi-disc games through a
# playlist perfectly well, it just will not open a .chd.
M3U_SYSTEM_OPTIONS = [
    ("psx", "PS1"), ("saturn", "Saturn"), ("segacd", "Sega CD"),
    ("tgcd", "TurboGrafx-CD"), ("dreamcast", "Dreamcast"),
    ("cdimono1", "Philips CD-i"), ("gc", "GameCube"),
]

DEFAULT_SETTINGS = {
    "out": str(Path.home() / "Downloads" / "Vimm"),
    "organize": True,
    "prefer": "USA, Europe",
    # latest | first | ask - "ask" stops the run to let you choose a revision.
    "version_policy": "latest",
    # all | ask - "ask" stops the run at each multi-disc game. Off by default:
    # a multi-disc game is one game, and taking every disc is nearly always
    # what is wanted.
    "disc_policy": "all",
    # Which download the site should give us, per system. These match the
    # site's own default - the first option in its chooser - so out of the box
    # you get exactly what vimm.net would hand you. Verified against the live
    # vault pages: GameCube offers .ciso/.nkit.iso/.rvz, Wii .wbfs/.rvz, Xbox
    # .xiso.iso/.iso, PS3 JB Folder/.dec.iso.
    "formats": {"gc": ".ciso", "wii": ".wbfs",
                "xbox": ".xiso.iso", "ps3": "JB Folder"},
    "delay": 5.0,
    "sweeps": 10,
    "cancel_busy": True,
    "cookies": "",
    "auto_extract": False,
    "auto_compress": False,
    "auto_m3u": False,
    # Hold the next download until extraction and conversion have finished.
    # Off by default: on a normal disk the overlap is free and finishes
    # sooner. It is for drives that can only manage one stream at a time -
    # measured on a USB flash drive, a second writer cost 74% of the
    # download's throughput and produced stalls of over three seconds.
    "wait_for_processing": False,
    "m3u_systems": DEFAULT_M3U_SYSTEMS,
    "chd_systems": DEFAULT_CHD_SYSTEMS,
    "hidden_tags": search_mod.DEFAULT_HIDDEN_TAGS,
    "delete_chd_sources": True,
}


def _load_json(path: Path, fallback):
    try:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return fallback


def _save_json(path: Path, data) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _migrate_history(raw: list) -> list[dict]:
    """Bring older per-file history entries into the grouped shape.

    Early versions wrote one entry per downloaded file, so a two-disc game
    appeared twice. Entries are now keyed by vault_id with a `files` list.
    """
    grouped: dict[int, dict] = {}
    order: list[int] = []
    for entry in raw:
        if not isinstance(entry, dict) or "vault_id" not in entry:
            continue
        vault_id = entry["vault_id"]
        if "files" in entry:  # already current
            grouped[vault_id] = entry
            if vault_id not in order:
                order.append(vault_id)
            continue
        archive = entry.get("archive", "")
        parent = grouped.get(vault_id)
        if parent is None:
            parent = {
                "key": entry.get("key", uuid.uuid4().hex[:10]),
                "vault_id": vault_id,
                "title": entry.get("title", f"vault/{vault_id}"),
                "system_folder": entry.get("system_folder", ""),
                "dir": entry.get("dir", ""),
                "files": [],
                "stages": entry.get("stages", {}),
                "when": entry.get("when", time.time()),
            }
            grouped[vault_id] = parent
            order.append(vault_id)
        parent["files"].append({
            "filename": Path(archive).name,
            "archive": archive,
            "bytes": entry.get("bytes", 0),
            "disc": len(parent["files"]) + 1,
            "message": entry.get("message", ""),
        })
    return [grouped[v] for v in order]


class Hub:
    """Everything the app knows, plus the event fan-out to WebSockets."""

    # Test hook: aims the engine at a local server instead of the real site.
    site_base_override: str | None = None

    def __init__(self):
        self.settings = {**DEFAULT_SETTINGS,
                         **_load_json(DATA_DIR / "settings.json", {})}
        self.history: list[dict] = _migrate_history(
            _load_json(DATA_DIR / "history.json", []))
        # A game's disc structure never changes, so a vault page is worth
        # looking up once ever. Keyed by vault id (as a string, since that is
        # what JSON gives back): title, system, size and the disc list.
        self.vault_cache: dict[str, dict] = _load_json(
            DATA_DIR / "vault_cache.json", {})
        self.queue: list[dict] = []
        self.run_status = "idle"
        self.log_lines: list[str] = []
        self.tag_vocabulary: list[str] = list(search_mod.KNOWN_TAGS)
        # Whether vimm.net itself is reachable. Checked once at startup and
        # again whenever the user clicks the indicator.
        self.site = SiteStatus()

        self.loop: asyncio.AbstractEventLoop | None = None
        self._sockets: set[WebSocket] = set()
        self._lock = threading.Lock()

        self._run_thread: threading.Thread | None = None
        # The question currently on screen, if the user asked to be asked.
        self.prompt: dict | None = None
        self._prompt_event = threading.Event()
        self._prompt_answer = None
        self._cancel = threading.Event()
        # Set by Stop (as opposed to Pause) so the cancelled run knows to
        # throw the partial downloads away rather than keep them.
        self._discard_on_stop = False
        self._pages: dict[int, object] = {}   # vault_id -> VaultPage
        # vault_id -> how many files the current run means to fetch for it.
        # The playlist stage waits on this; nothing else may.
        self._expected_discs: dict[int, int] = {}
        self._opts = None

        # Entry keys currently being driven through every stage by Convert
        # All. Kept off the entry itself so it never reaches history.json.
        self._chaining: set[str] = set()

        self.pipeline = PipelineWorker(on_event=self._on_job_event)
        self.search_session = search_mod.make_session()
        self.reconcile_history()

    # ------------------------------------------------------------ event bus

    def emit(self, event: dict) -> None:
        """Thread-safe broadcast to every connected WebSocket."""
        if self.loop is None:
            return
        payload = json.dumps(event)
        for socket in list(self._sockets):
            self.loop.call_soon_threadsafe(
                lambda s=socket: asyncio.ensure_future(self._send(s, payload)))

    async def _send(self, socket: WebSocket, payload: str) -> None:
        try:
            await socket.send_text(payload)
        except Exception:
            self._sockets.discard(socket)

    def snapshot(self) -> dict:
        """Everything a client needs to draw the whole interface.

        Served by `/api/state` and pushed to a WebSocket the moment it
        connects, so both routes cannot drift apart.
        """
        return {
            "queue": self.queue,
            "history": [self.decorate(h) for h in self.history],
            "jobs": self.pipeline.snapshot(),
            "settings": self.settings,
            "run": self.run_status,
            "log": self.log_lines[-200:],
            "tag_vocabulary": self.tag_vocabulary,
            # Folder names stay defined in one place; the settings drawer
            # renders its checkboxes from these rather than repeating them.
            "system_options": {"chd": CHD_SYSTEM_OPTIONS,
                               "m3u": M3U_SYSTEM_OPTIONS},
            "site": self.site.as_dict(),
            "prompt": self.prompt,
        }

    # -------------------------------------------------------- site status

    def check_site(self) -> None:
        """Re-check whether vimm.net is reachable, off the event loop.

        Runs on a throwaway thread so a slow or hanging request never delays
        startup or blocks the server while it waits.
        """
        self.site = SiteStatus("checking", "contacting vimm.net...", time.time())
        self.emit({"type": "site", "site": self.site.as_dict()})

        def run() -> None:
            try:
                result = check_site(base=self.site_base_override)
            except Exception as exc:  # noqa: BLE001 - must always settle
                # `check_site` handles the request failures it expects, but
                # anything it does not would kill this thread and strand the
                # indicator on "checking..." for good. That word should only
                # ever mean a check is actually in flight.
                result = SiteStatus("down", f"check failed: {exc}"[:120],
                                    time.time())
            self.site = result
            self.emit({"type": "site", "site": result.as_dict()})
            if result.state != "up":
                self.log(f"! vimm.net: {result.detail}")

        threading.Thread(target=run, daemon=True, name="site-check").start()

    def log(self, text: str) -> None:
        with self._lock:
            self.log_lines.append(text)
            del self.log_lines[:-500]
        self.emit({"type": "log", "text": text})

    # -------------------------------------------------------------- queue

    def add_ids(self, pairs: list[tuple[int, str, str]]) -> int:
        """pairs: (vault_id, title, system_label). Returns how many were new."""
        added: list[int] = []
        with self._lock:
            existing = {q["vault_id"] for q in self.queue}
            for vault_id, title, system in pairs:
                if vault_id in existing:
                    continue
                existing.add(vault_id)
                size_text = ""
                discs: list[dict] = []
                # `scanned` means we know how many discs this game has. It is
                # not `resolved`, which means we hold the actual page.
                scanned = False

                meta = self.vault_cache.get(str(vault_id))
                if meta:
                    # Seen in some earlier session. Disc structure does not
                    # change, so the checkboxes can appear right now for free.
                    title = meta.get("title") or title
                    system = meta.get("system") or system
                    size_text = meta.get("size_text", "")
                    discs = [{**d, "selected": True}
                             for d in meta.get("discs", [])]
                    scanned = True

                self.queue.append({
                    "vault_id": vault_id,
                    "title": title or f"vault/{vault_id}",
                    "system": system,
                    "size_text": size_text,
                    "status": "queued",
                    "progress": 0.0,
                    "message": "",
                    "speed": 0.0,
                    "eta": 0.0,
                    "resolved": False,
                    "scanned": scanned,
                    # True once the user has actually picked discs for this
                    # game, as opposed to us merely knowing what they are.
                    "chosen": False,
                    "discs": discs,
                })
                added.append(vault_id)
        if added:
            # No page lookups here beyond what the cache already knows. The
            # rest fill in as the run reaches each game.
            self.log(f"added {len(added)} item(s) to the queue")
            self.emit({"type": "queue", "queue": self.queue})
        return len(added)

    def queue_item(self, vault_id: int) -> dict | None:
        return next((q for q in self.queue if q["vault_id"] == vault_id), None)

    def patch_item(self, vault_id: int, **fields) -> None:
        item = self.queue_item(vault_id)
        if item is None:
            return
        item.update(fields)
        self.emit({"type": "item", "item": item})

    def drop_item(self, vault_id: int) -> None:
        """Take a finished game out of the queue - history has it now."""
        with self._lock:
            for index, item in enumerate(self.queue):
                if item["vault_id"] == vault_id:
                    del self.queue[index]
                    break
            else:
                return
        self.emit({"type": "queue", "queue": self.queue})

    # ---------------------------------------------------------- resolution

    def apply_page(self, vault_id: int, page) -> None:
        """Fill in an item's real title, system and disc list from its page."""
        self._pages[vault_id] = page
        opts = self._opts or self.build_options()
        # Whatever is already ticked stays ticked. This runs again for every
        # game as the run reaches it - `run_pass` reports the page even when
        # it came from the cache - so asserting True here would quietly undo
        # a disc the user unticked before pressing Start.
        item = self.queue_item(vault_id) or {}
        picked = {d["disc"]: d.get("selected", True)
                  for d in (item.get("discs") or [])}
        discs: dict[int, dict] = {}
        for media in page.media:
            # One row per disc; which version is downloaded is decided later
            # by the engine's preference rules. A disc we have not seen
            # before defaults to selected, so "all" still means all.
            discs.setdefault(media.disc, {
                "disc": media.disc,
                "size_text": media.size_text(0),
                "selected": picked.get(media.disc, True),
            })
        disc_list = [discs[d] for d in sorted(discs)]
        title = page.title
        system = system_folder(page.title, getattr(opts, "folders", None))
        size_text = page.media[0].size_text(0) if page.media else ""
        self.patch_item(
            vault_id,
            title=title,
            system=system,
            size_text=size_text,
            discs=disc_list,
            resolved=True,
            scanned=True,
        )
        # Remember it, so this game never costs a page view again. Which
        # discs are *ticked* is per-queue-item and deliberately not stored.
        self.vault_cache[str(vault_id)] = {
            "title": title,
            "system": system,
            "size_text": size_text,
            "discs": [{"disc": d["disc"], "size_text": d["size_text"]}
                      for d in disc_list],
        }
        _save_json(DATA_DIR / "vault_cache.json", self.vault_cache)

    def resolve_item(self, vault_id: int, force: bool = False) -> dict:
        """Look one game up now, on request.

        Used when a row is opened before the run has reached it. Anything
        already known - held page, or disc counts recovered from the cache -
        answers without touching the network unless `force` says otherwise.
        """
        item = self.queue_item(vault_id)
        if item is None:
            return {"error": "unknown item"}
        if not force:
            if vault_id in self._pages and not item.get("resolved"):
                self.apply_page(vault_id, self._pages[vault_id])
            if item.get("resolved") or item.get("scanned"):
                return {"item": self.queue_item(vault_id)}

        from .engine import BusyError, VimmClient

        try:
            # A name lookup is optional and someone is waiting on it, so it
            # gives up at the first refusal. The engine's patient retry
            # schedule - up to 20 busy waits growing to five minutes each -
            # belongs to downloads, not to this.
            opts = self.build_options()
            opts.retries = 0
            opts.busy_retries = 0
            opts.max_attempts = 1
            opts.timeout = min(opts.timeout, 20)
            client = VimmClient(opts)
            self.apply_page(vault_id, client.fetch_vault(vault_id))
        except BusyError as exc:
            # Throttling is not a property of this game. Leave it queued and
            # unresolved so it can be looked up again, or simply downloaded.
            self.patch_item(vault_id, message="the site is busy - try again")
            self.log(f"! vault/{vault_id}: {exc}")
        except VimmError as exc:
            message = str(exc)
            if "429" in message or "busy" in message.lower():
                self.patch_item(vault_id, message="the site is busy - try again")
            else:
                self.patch_item(vault_id, status="failed", message=message,
                                resolved=True)
            self.log(f"! vault/{vault_id}: {exc}")
        except Exception as exc:  # noqa: BLE001 - a lookup must never crash the app
            self.log(f"! vault/{vault_id}: {exc}")
        return {"item": self.queue_item(vault_id)}

    # -------------------------------------------------------------- prompts

    def ask(self, kind: str, payload: dict, default):
        """Put a question to the user and block the run until it is answered.

        Only ever reached when the user has opted into asking, so it waits
        indefinitely rather than guessing after a timeout. Pause and Stop are
        never trapped behind it: the wait watches `self._cancel` too, and
        answers with the default if the run is being torn down.
        """
        prompt_id = uuid.uuid4().hex[:10]
        self._prompt_answer = None
        self._prompt_event = threading.Event()
        self.prompt = {"id": prompt_id, "kind": kind, **payload}
        self.emit({"type": "prompt", "prompt": self.prompt})
        try:
            while not self._prompt_event.wait(0.25):
                if self._cancel.is_set():
                    return default
            answer = self._prompt_answer
            return default if answer is None else answer
        finally:
            self.prompt = None
            self.emit({"type": "prompt", "prompt": None})

    def answer_prompt(self, prompt_id: str, answer) -> dict:
        prompt = self.prompt
        if prompt is None or prompt["id"] != prompt_id:
            # Already withdrawn - the run was stopped, or this is a stale tab.
            return {"error": "that question is no longer open"}
        self._prompt_answer = answer
        self._prompt_event.set()
        return {"ok": True}

    # ---------------------------------------------------------------- runs

    def set_discs(self, vault_id: int, wanted: set[int]) -> dict:
        """Tick or untick the discs of a multi-disc game.

        A run keeps one options object for its whole life, and `select_media`
        reads `disc_overrides` from it at the moment it reaches each game. So
        updating it here applies to everything the run has not started yet -
        which is what lets a choice be made during a run, not only before it.
        """
        item = self.queue_item(vault_id)
        if item is None:
            return {"error": "unknown item"}
        if item["status"] not in ("queued", "paused"):
            return {"error": f"already {item['status']} - too late to change discs"}

        # Unticking a disc that is already on disk, whole or partly, would
        # read as "undo that download" and do nothing of the sort. Ticking is
        # always allowed - including a disc the run has gone past, which Start
        # picks up on its next pass.
        self.refresh_disc_states(vault_id)
        for disc in item.get("discs") or []:
            if disc["disc"] in wanted or not disc.get("selected", True):
                continue
            if disc.get("done"):
                return {"error": f"disc {disc['disc']} has already been "
                                 f"downloaded"}
            if disc.get("active"):
                return {"error": f"disc {disc['disc']} is part-downloaded - "
                                 f"it resumes on Start"}

        for disc in item.get("discs") or []:
            disc["selected"] = disc["disc"] in wanted
        item["chosen"] = True
        if self._opts is not None and len(item.get("discs") or []) > 1:
            self._opts.disc_overrides[vault_id] = [
                d["disc"] for d in item["discs"] if d["selected"]]
        self.emit({"type": "item", "item": item})
        return {"item": item}

    def disc_overrides(self) -> dict[int, list[int]]:
        """Per-game disc choices the user actually made.

        Only games ticked through deliberately. A game whose discs are merely
        *known* - filled in from the cache, say - must not appear here, or it
        would look like a settled choice and suppress the "ask me" prompt.
        """
        overrides: dict[int, list[int]] = {}
        for item in self.queue:
            discs = item.get("discs") or []
            if item.get("chosen") and len(discs) > 1:
                overrides[item["vault_id"]] = [
                    d["disc"] for d in discs if d.get("selected", True)]
        return overrides

    def build_options(self):
        s = self.settings
        out_dir = s["out"].strip() or DEFAULT_SETTINGS["out"]
        return make_options(
            site_base=self.site_base_override,
            out=out_dir,
            organize=bool(s["organize"]),
            prefer=s["prefer"],
            version_policy=s["version_policy"],
            # "ask" is not a sort order - it routes to the engine's existing
            # `pick` hook instead of a policy.
            pick=s["version_policy"] == "ask",
            ask_discs=s.get("disc_policy") == "ask",
            formats=dict(s.get("formats") or {}),
            delay=float(s["delay"]),
            sweeps=int(s["sweeps"]),
            cancel_busy=bool(s["cancel_busy"]),
            cookies=s["cookies"].strip() or None,
            log=str(Path(out_dir) / "download_log.csv"),
            disc_overrides=self.disc_overrides(),
        )

    def start_run(self) -> str:
        if self._run_thread is not None and self._run_thread.is_alive():
            return "already running"
        pending = [q for q in self.queue
                   if q["status"] not in ("done", "skipped")]
        if not pending:
            return "nothing to do"
        for item in pending:
            item["status"] = "queued"
            item["progress"] = 0.0
        self.emit({"type": "queue", "queue": self.queue})

        self._cancel = threading.Event()
        self._opts = self.build_options()
        ids = [q["vault_id"] for q in pending]
        self._run_thread = threading.Thread(
            target=self._run, args=(ids,), daemon=True, name="engine")
        self._run_thread.start()
        self.run_status = "running"
        self.emit({"type": "run", "status": self.run_status})
        return "started"

    def item_partials(self, vault_ids: list[int] | None = None) -> list[Path]:
        """Partial downloads belonging to the given queue items.

        Only `.part` files, found under whatever name the server gave them
        (the large disc systems arrive as .7z, not the .zip we plan for), so
        a finished download can never be caught up in this.
        """
        found: list[Path] = []
        opts = self._opts or self.build_options()
        wanted = set(vault_ids) if vault_ids is not None else None
        for item in self.queue:
            if wanted is not None and item["vault_id"] not in wanted:
                continue
            page = self._pages.get(item["vault_id"])
            if page is None:
                continue
            dest = Path(destination_for(page, opts))
            for media in page.media:
                stem = safe_filename(Path(media.filename).stem,
                                     f"media-{media.media_id}")
                part = find_download(dest, stem, partial=True)
                if part is not None and part not in found:
                    found.append(part)
        return found

    def refresh_disc_states(self, vault_id: int | None = None) -> None:
        """Mark each disc as already downloaded, part-downloaded, or neither.

        "Done" comes from history as well as from disk: `find_download`
        only recognises an archive, and extraction deletes that, so a
        finished disc would otherwise stop reading as done the moment it was
        unpacked - and quietly become unlockable again. History records every
        successful download with its disc number and outlives the archive.

        Only called when the answer can change - a disc finishing, a run
        ending - never on the progress tick, which fires far too often to be
        listing directories.
        """
        opts = self._opts or self.build_options()
        changed: dict[int, dict] = {}
        for item in self.queue:
            if vault_id is not None and item["vault_id"] != vault_id:
                continue
            discs = item.get("discs") or []
            page = self._pages.get(item["vault_id"])
            if not discs or page is None:
                continue
            entry = next((h for h in self.history
                          if h["vault_id"] == item["vault_id"]), None)
            done: set[int] = {f.get("disc") for f in (entry or {}).get("files", [])}
            active: set[int] = set()
            dest = Path(destination_for(page, opts))
            for media in page.media:
                stem = safe_filename(Path(media.filename).stem,
                                     f"media-{media.media_id}")
                if find_download(dest, stem, partial=False) is not None:
                    done.add(media.disc)
                elif find_download(dest, stem, partial=True) is not None:
                    active.add(media.disc)
            for disc in discs:
                was = (disc.get("done"), disc.get("active"))
                disc["done"] = disc["disc"] in done
                # A disc that finished is no longer "in progress", however
                # many partial files an earlier attempt left lying about.
                disc["active"] = disc["disc"] in active and not disc["done"]
                if was != (disc["done"], disc["active"]):
                    changed[item["vault_id"]] = item
        for item in changed.values():
            self.emit({"type": "item", "item": item})

    def partials_summary(self) -> dict:
        parts = self.item_partials()
        total = 0
        for path in parts:
            try:
                total += path.stat().st_size
            except OSError:
                pass
        return {"count": len(parts), "bytes": total,
                "human": human_bytes(total) if total else ""}

    def stop_run(self, pause: bool) -> str:
        """Pause keeps the partial files; Stop throws them away.

        Until now these differed only in the status text, which made the two
        buttons indistinguishable in use.
        """
        running = self._run_thread is not None and self._run_thread.is_alive()
        self._discard_on_stop = not pause
        if not running:
            # Stop still has meaning when idle: clear out partial downloads
            # left behind by an earlier run.
            if not pause:
                self._discard_partials()
            return "not running"
        self._cancel.set()
        self.run_status = "pausing" if pause else "stopping"
        self.emit({"type": "run", "status": self.run_status})
        return self.run_status

    def _discard_partials(self) -> int:
        """Delete partial downloads and reset their items to queued."""
        removed = 0
        freed = 0
        for path in self.item_partials():
            try:
                freed += path.stat().st_size
                path.unlink()
                removed += 1
            except OSError as exc:
                self.log(f"! could not remove {path.name}: {exc}")
        with self._lock:
            for item in self.queue:
                if item["status"] in ("paused", "downloading", "working",
                                      "waiting", "failed"):
                    item.update(status="queued", progress=0.0, message="",
                                speed=0.0, eta=0.0, waiting_until=0)
        if removed:
            self.log(f"discarded {removed} partial download(s), "
                     f"freeing {human_bytes(freed)}")
        self.emit({"type": "queue", "queue": self.queue})
        return removed

    def _pending_ids(self, attempted: set[int]) -> list[int]:
        """Queued games this run has not taken a turn at yet."""
        return [q["vault_id"] for q in self.queue
                if q["status"] == "queued" and q["vault_id"] not in attempted]

    def _run(self, ids: list[int]) -> None:
        listener = WebListener(self)
        results = []
        try:
            # Adding to a queue that is already running should just work, so
            # the batch is re-read from the queue rather than frozen at Start.
            # Every id a pass touches leaves "queued" - item_done moves it on,
            # and a finished game is dropped from the queue outright - so this
            # ends on its own; `attempted` makes that a property of the code
            # instead of a chain of reasoning somewhere else.
            attempted: set[int] = set()
            batch = list(ids)
            while batch:
                attempted.update(batch)
                # Share the page cache: anything already looked up for the
                # disc picker is reused, and pages fetched here serve later
                # sweeps.
                results += run_http(batch, self._opts, listener=listener,
                                    cancel_event=self._cancel, pages=self._pages)
                batch = self._pending_ids(attempted)
                if batch:
                    self.log(f"{len(batch)} more added since starting - "
                             f"carrying on")

            done = sum(1 for r in results if r.status == "ok")
            failed = sum(1 for r in results if r.status == "failed")
            self.run_status = (f"finished - {done} downloaded"
                               + (f", {failed} failed" if failed else ""))
            if results and self._opts.log:
                try:
                    write_log(Path(self._opts.log), results)
                except OSError:
                    pass
        except Cancelled:
            if getattr(self, "_discard_on_stop", False):
                # Stop: nothing is kept, so the next run starts clean.
                self._discard_partials()
                self.run_status = "stopped - partial downloads discarded"
            else:
                with self._lock:
                    for item in self.queue:
                        if item["status"] in ("working", "downloading",
                                              "waiting", "queued"):
                            item["status"] = "paused"
                self.run_status = "paused - partial files resume on Start"
                self.emit({"type": "queue", "queue": self.queue})
        except VimmError as exc:
            self.run_status = f"error: {exc}"
            self.log(f"! {exc}")
        except Exception as exc:  # noqa: BLE001 - surface, never die silently
            self.run_status = f"error: {exc}"
            self.log(f"! unexpected: {exc}")
        finally:
            # However the run ended, the picker needs to know which discs are
            # now on disk before anyone can edit them again.
            self.refresh_disc_states()
            self.emit({"type": "run", "status": self.run_status})

    # -------------------------------------------------------------- history

    def is_chd_system(self, entry: dict) -> bool:
        """Whether this game's system is one we compress at all."""
        systems = [s.lower() for s in self.settings.get("chd_systems", [])]
        return entry.get("system_folder", "").lower() in systems

    def can_chd(self, entry: dict) -> bool:
        """Whether the COMPRESS action is offerable right now.

        Includes "not already done", so the flag alone decides the button and
        callers cannot disagree about what it means.
        """
        stages = entry.get("stages", {})
        return (self.is_chd_system(entry)
                and bool(stages.get("extracted"))
                and not stages.get("chd"))

    def compression_outstanding(self, entry: dict) -> bool:
        """Whether this game is still meant to be compressed.

        False when the system does not use CHD, or compression is switched
        off - which is what lets a playlist be built over bin+cue files
        instead of waiting forever for a conversion that is never coming.
        """
        if not self.is_chd_system(entry) or not self.settings.get("auto_compress"):
            return False
        if entry.get("stages", {}).get("chd"):
            return False
        files = self.entry_files(entry)
        # With nothing on disk to judge by, trust the stage flag above: the
        # conversion has not happened. With files, ask them - a game that
        # arrived with no cue or gdi has nothing to wait for.
        return not files or bool(chd_mod.compressible_among(files))

    def all_discs_present(self, entry: dict) -> bool:
        """Every disc this run means to fetch has arrived.

        Unknown - a restart, or an entry predating the run - means there is
        nothing to wait for.
        """
        expected = entry.get("discs_expected") or 0
        return len(entry.get("files", [])) >= expected

    def can_m3u(self, entry: dict) -> bool:
        """Playlists come last, once the whole game is settled.

        Every disc downloaded, every archive unpacked, and nothing left to
        compress that we mean to compress - so the playlist lists .chd files
        rather than cue sheets about to be replaced, and a half-downloaded
        game is never folded away.
        """
        systems = [s.lower() for s in self.settings.get("m3u_systems", [])]
        stages = entry.get("stages", {})
        return (entry.get("system_folder", "").lower() in systems
                and len(entry.get("files", [])) > 1
                and bool(stages.get("extracted"))
                and self.all_discs_present(entry)
                and not self.compression_outstanding(entry)
                and not stages.get("m3u"))

    def decorate(self, entry: dict) -> dict:
        """Entry plus the flags the UI needs to decide which buttons exist."""
        return {**entry, "can_chd": self.can_chd(entry),
                "can_m3u": self.can_m3u(entry)}

    def add_history(self, result: Result) -> dict | None:
        """Record a finished file, grouped under its game."""
        page = self._pages.get(result.vault_id)
        if page is None or result.status != "ok":
            return None
        dest = destination_for(page, self._opts)
        folder = system_folder(page.title, getattr(self._opts, "folders", None))
        archive = str(Path(dest) / result.filename)

        # The real disc number, from the page we already hold. Counting rows
        # gets this wrong whenever a disc is re-recorded or only a subset was
        # chosen - picking discs 1 and 3 would file them as 1 and 2.
        disc_no = next((m.disc for m in page.media
                        if m.media_id == result.media_id), 0)

        with self._lock:
            entry = next((h for h in self.history
                          if h["vault_id"] == result.vault_id), None)
            if entry is None:
                entry = {
                    "key": uuid.uuid4().hex[:10],
                    "vault_id": result.vault_id,
                    "title": page.title,
                    "system_folder": folder if self._opts.organize else "",
                    "dir": str(dest),
                    "files": [],
                    "stages": {},
                    "when": time.time(),
                }
                self.history.insert(0, entry)
            entry["dir"] = str(dest)
            # A newly downloaded archive means there is work to do again, so
            # the post-processing flags re-open. Without this a re-download
            # inherits "already extracted, already compressed, already
            # playlisted" from the previous run and the whole chain declines
            # to start. It is equally right mid-run: disc 2 arriving after
            # disc 1 has been compressed does leave compression outstanding.
            entry["stages"] = {"archive": True, "extracted": False,
                               "chd": False, "m3u": False}
            if self._expected_discs.get(result.vault_id):
                entry["discs_expected"] = self._expected_discs[result.vault_id]
            # Re-downloading the same disc replaces its row rather than
            # adding a duplicate.
            entry["files"] = [f for f in entry["files"]
                              if f["archive"] != archive]
            entry["files"].append({
                "filename": result.filename,
                "archive": archive,
                "bytes": result.bytes,
                "disc": disc_no or len(entry["files"]) + 1,
                "message": result.message,
            })
            entry["files"].sort(key=lambda f: f.get("disc", 0))
            _save_json(DATA_DIR / "history.json", self.history)

        self.emit({"type": "history_item", "item": self.decorate(entry)})
        return entry

    def history_item(self, key: str) -> dict | None:
        return next((h for h in self.history if h["key"] == key), None)

    def save_history(self) -> None:
        with self._lock:
            _save_json(DATA_DIR / "history.json", self.history)

    # ------------------------------------------------------ pipeline chain

    # ------------------------------------------------- which files are ours

    def entry_files(self, entry: dict) -> list[Path]:
        """The files on disk that belong to this game.

        `entry["dir"]` is the *system* folder, shared with every other game on
        that system, so per-game work must never scan it. Extraction records
        what it produced; that list is the authoritative answer and survives
        the flatten step renaming files.
        """
        tracked = entry.get("files_on_disk")
        if tracked:
            return [Path(p) for p in tracked if Path(p).is_file()]
        return self._guess_entry_files(entry)

    @staticmethod
    def _guess_entry_files(entry: dict) -> list[Path]:
        """Best effort for entries recorded before file tracking existed:
        match on the archive's stem. Returns nothing rather than guessing
        wildly, so a caller refuses instead of touching the whole folder."""
        folder = Path(entry.get("dir", ""))
        stems = [Path(f["archive"]).stem for f in entry.get("files", [])
                 if f.get("archive")]
        if not folder.is_dir() or not stems:
            return []
        return [path for path in sorted(folder.iterdir())
                if path.is_file() and any(path.stem.startswith(s) for s in stems)]

    def track_files(self, entry: dict, paths) -> None:
        """Record files as belonging to this game, dropping any now gone."""
        tracked = list(entry.get("files_on_disk") or [])
        for path in paths:
            text = str(path)
            if text not in tracked:
                tracked.append(text)
        entry["files_on_disk"] = [p for p in tracked if Path(p).is_file()]

    def _already_compressed(self, entry: dict) -> bool:
        files = self.entry_files(entry)
        return (bool(files) and any(p.suffix.lower() == ".chd" for p in files)
                and not chd_mod.compressible_among(files))

    # ------------------------------------------------------------- stages

    def submit_stage(self, entry: dict, kind: str, only=None) -> Job | None:
        """`only` names specific files to work on.

        Used to compress one disc the moment it is extracted, without waiting
        for its siblings - so it skips the game-level "everything is
        extracted" precondition, which is exactly what is not yet true then.
        """
        item_dir = Path(entry["dir"])
        if kind == "extract":
            archives = [Path(f["archive"]) for f in entry.get("files", [])
                        if Path(f["archive"]).is_file()]
            if not archives:
                self.log(f"! nothing to extract for {entry['title']}")
                return None
            submitted = None
            for archive in archives:
                submitted = self.pipeline.submit(Job(
                    kind="extract", label=archive.name, target=archive,
                    item_key=entry["key"])) or submitted
            return submitted
        if kind == "chd":
            if only is not None:
                sheets = chd_mod.compressible_among([Path(p) for p in only])
                if not sheets:
                    return None
                return self._submit_chd(entry, sheets)
            if not self.can_chd(entry):
                self.log(f"! CHD is not applicable to {entry['title']}")
                return None
            sheets = chd_mod.compressible_among(self.entry_files(entry))
            if not sheets:
                # Nothing left to do. If the discs are already .chd the flag
                # was simply stale, so correct it rather than leaving a button
                # that can never accomplish anything.
                if self._already_compressed(entry):
                    self._mark_stage(entry, "chd")
                    self.log(f"{entry['title']} was already compressed")
                else:
                    self.log(f"! no .cue/.gdi to compress for {entry['title']}")
                return None
            return self._submit_chd(entry, sheets)
        if kind == "m3u":
            if not self.can_m3u(entry):
                self.log(f"! m3u is not applicable to {entry['title']}")
                return None
            folder_name = entry["system_folder"] or item_dir.name
            return self.pipeline.submit(Job(
                kind="m3u", label=entry["title"], target=item_dir,
                item_key=entry["key"],
                extra={"system_folder": folder_name,
                       "allowed_systems": self.settings["m3u_systems"],
                       # Only this game's discs - the folder holds others.
                       "files": [str(p) for p in self.entry_files(entry)]}))
        return None

    def _submit_chd(self, entry: dict, sheets) -> Job | None:
        submitted = None
        for sheet in sheets:
            submitted = self.pipeline.submit(Job(
                kind="chd", label=sheet.name, target=sheet,
                item_key=entry["key"],
                extra={"delete_sources": bool(self.settings["delete_chd_sources"])}
            )) or submitted
        return submitted

    def _mark_stage(self, entry: dict, stage: str) -> None:
        entry.setdefault("stages", {})[stage] = True
        self.save_history()
        self.emit({"type": "history_item", "item": self.decorate(entry)})

    def next_stage(self, entry: dict, forced: bool) -> str | None:
        """The stage this entry needs next, if any.

        `forced` is Convert All, which runs a game all the way through;
        otherwise the auto_* settings decide whether to continue.
        """
        stages = entry.get("stages", {})
        # A stage that is applicable but switched off falls through to the
        # next one rather than ending the chain. Returning None here is what
        # stopped a playlist ever being built with compression turned off:
        # CHD was applicable, unwanted, and the m3u step below was never
        # reached.
        if not stages.get("extracted"):
            if forced or self.settings["auto_extract"]:
                return "extract"
        if self.can_chd(entry) and not stages.get("chd"):
            if forced or self.settings["auto_compress"]:
                return "chd"
        if self.can_m3u(entry) and not stages.get("m3u"):
            if forced or self.settings["auto_m3u"]:
                return "m3u"
        return None

    def convert_all(self) -> int:
        """Take every eligible game to its finished state, one click."""
        submitted = 0
        for entry in list(self.history):
            stage = self.next_stage(entry, forced=True)
            if stage is None:
                continue
            self._chaining.add(entry["key"])
            if self.submit_stage(entry, stage):
                submitted += 1
            else:
                self._chaining.discard(entry["key"])
        return submitted

    def reconcile_history(self) -> int:
        """Bring stage flags in line with what is actually on disk.

        Only ever upgrades a flag. Repairs history written before the stages
        were tracked per game - notably entries that were compressed but left
        unflagged, which would otherwise keep offering a COMPRESS button.
        """
        changed = 0
        for entry in self.history:
            stages = entry.setdefault("stages", {})
            if entry.get("files_on_disk"):
                self.track_files(entry, [])  # prune vanished files
            if not stages.get("extracted"):
                archives = [f for f in entry.get("files", [])
                            if Path(f.get("archive", "")).is_file()]
                if not archives and self.entry_files(entry):
                    stages["extracted"] = True
                    changed += 1
            if (stages.get("extracted") and not stages.get("chd")
                    and self._already_compressed(entry)):
                stages["chd"] = True
                changed += 1
        if changed:
            self.save_history()
        return changed

    def _on_job_event(self, job: Job) -> None:
        self.emit({"type": "job", "job": job.as_dict()})
        if job.status not in ("done", "failed"):
            return
        if job.status == "failed":
            self.log(f"! {job.kind} failed: {job.label}: {job.message}")
        entry = self.history_item(job.item_key) if job.item_key else None
        if entry is None:
            return
        stage_completed = False
        if job.status == "done":
            if job.kind == "extract":
                # Everything this archive produced belongs to this game.
                produced = job.extra.get("extracted", [])
                self.track_files(entry, produced)
                # Compress this disc now, while the next one downloads,
                # rather than waiting for the whole game to be unpacked.
                # Scoped to what this job produced, so the sheets of discs
                # that have not arrived yet are simply not there to submit.
                # Deliberately not conditioned on the "chd" flag: `produced`
                # and `compressible_among` already narrow this to sheets that
                # genuinely need converting, and a flag that wrongly reads
                # "done" must never be able to skip a disc.
                if self.settings.get("auto_compress") and self.is_chd_system(entry):
                    self.submit_stage(entry, "chd", only=produced)
                # Extracted once no archive remains *and* every disc has
                # arrived - "nothing left to unpack" is trivially true after
                # disc 1 of 2 and means nothing.
                remaining = [f for f in entry.get("files", [])
                             if Path(f["archive"]).is_file()]
                if not remaining and self.all_discs_present(entry):
                    entry["stages"]["extracted"] = True
                    stage_completed = True
            elif job.kind == "chd":
                # The .chd replaces the sheet and its tracks; re-tracking
                # prunes what chdman deleted.
                produced = job.extra.get("chd")
                self.track_files(entry, [produced] if produced else [])
                # "Nothing compressible left" is only meaningful once every
                # disc is downloaded *and* unpacked. Disc 1's conversion can
                # finish while disc 2 is still queued behind it for
                # extraction, and at that instant there is indeed nothing
                # compressible - because disc 2 is still a .7z. Marking the
                # game converted there is what left disc 2 as a cue sheet.
                if (not chd_mod.compressible_among(self.entry_files(entry))
                        and self.all_discs_present(entry)
                        and entry["stages"].get("extracted")):
                    entry["stages"]["chd"] = True
                    stage_completed = True
            elif job.kind == "m3u":
                # The discs move into the playlist folder, so the paths we
                # were tracking no longer exist and would simply be pruned,
                # leaving the entry with no record of its own game. That
                # folder holds this game and nothing else, so re-track it.
                playlists = job.extra.get("playlists", [])
                moved = [str(sibling)
                         for playlist in playlists
                         for sibling in Path(playlist).parent.iterdir()
                         if sibling.is_file()]
                self.track_files(entry, playlists + moved)
                entry["stages"]["m3u"] = True
                stage_completed = True

        chained = entry["key"] in self._chaining
        if stage_completed:
            following = self.next_stage(entry, forced=chained)
            if following:
                self.submit_stage(entry, following)
            elif chained:
                self._chaining.discard(entry["key"])
        elif job.status == "failed" and chained:
            # Don't keep driving a game whose stage just failed.
            self._chaining.discard(entry["key"])

        self.save_history()
        self.emit({"type": "history_item", "item": self.decorate(entry)})


class WebListener(Listener):
    """Engine events -> hub state + WebSocket broadcast."""

    def __init__(self, hub: Hub):
        self.hub = hub
        self._current: int | None = None
        self._start = 0.0
        self._start_bytes = 0
        self._speed = 0.0      # smoothed bytes/sec
        self._last_at = 0.0
        self._last_done = 0
        # vault_id -> how many files this game still owes, and how they went.
        # `item_done` fires per disc, so this is what tells disc 1 of 3
        # finishing apart from the whole game finishing.
        self._plan: dict[int, dict] = {}

    def item_started(self, position, total, vault_id, sweep_label=""):
        self._current = vault_id
        self.hub.patch_item(vault_id, status="working", message="")
        self.hub.log(f"{sweep_label}[{position}/{total}] vault/{vault_id}")

    def page_resolved(self, page):
        # The run has the page in hand, so fill the row in properly - this is
        # how names appear without any lookup of their own.
        self.hub.apply_page(page.vault_id, page)
        self.hub.log(f"  {page.title}")

    def status(self, text):
        self.hub.log(f"  {text}")

    def warn(self, text):
        self.hub.log(f"  ! {text}")

    def waiting(self, seconds, reason):
        self.hub.log(f"  ... waiting {human_duration(seconds)} ({reason})")
        if self._current is None:
            return
        # Between discs of one game the bar would otherwise sit idle and look
        # stalled, so say what is happening and until when.
        next_disc = reason == NEXT_DISC_WAIT
        self.hub.patch_item(
            self._current,
            status="waiting",
            waiting_until=time.time() + seconds,
            waiting_kind="disc" if next_disc else "polite",
            message=("waiting for next disc" if next_disc
                     else "pausing between downloads"),
        )

    def progress_begin(self, vault_id, filename, total, done):
        self._start = time.monotonic()
        self._start_bytes = done
        self._last_at = self._start
        self._last_done = done
        self._speed = 0.0
        self.progress(vault_id, filename, done, total)

    def progress(self, vault_id, filename, done, total):
        item = self.hub.queue_item(vault_id)
        if item is None:
            return
        now = time.monotonic()
        gap = now - self._last_at
        if gap >= 0.25:
            instant = max(done - self._last_done, 0) / gap
            # Exponential smoothing: a steady figure the eye can read,
            # without lagging a real speed change for long.
            self._speed = instant if self._speed == 0 else (
                0.7 * self._speed + 0.3 * instant)
            self._last_at = now
            self._last_done = done

        eta = ((total - done) / self._speed) if (self._speed > 0 and total) else 0.0
        item.update({
            "status": "downloading",
            "progress": min(done / total, 1.0) if total else 0.0,
            "speed": self._speed,
            "eta": eta,
            "message": (f"{human_bytes(done)} / {human_bytes(total)}"
                        if total else human_bytes(done)),
            "waiting_until": 0,
        })
        self.hub.emit({"type": "item", "item": item})

    def progress_done(self, text):
        self.hub.log(f"  {text}")

    def before_download(self):
        """Hold the transfer back until the pipeline is quiet, if asked to.

        Only for drives that cannot do two things at once. Watches the run's
        cancel event as well, so Pause and Stop are never stuck behind a
        conversion that has minutes left to run.
        """
        if not self.hub.settings.get("wait_for_processing"):
            return
        if self.hub.pipeline.idle():
            return
        self.hub.log("  waiting for extraction/conversion to finish "
                     "before the next download")
        while not self.hub._cancel.is_set():
            if self.hub.pipeline.idle():
                return
            self.hub._cancel.wait(0.25)

    def item_plan(self, vault_id: int, downloads: int):
        self._plan[vault_id] = {"left": downloads, "failed": 0}
        # The playlist stage waits for all of them, and only it does.
        self.hub._expected_discs[vault_id] = downloads

    def item_done(self, result: Result):
        plan = self._plan.get(result.vault_id)
        if plan is not None:
            plan["left"] = max(plan["left"] - 1, 0)
            if result.status == "failed":
                plan["failed"] += 1

        entry = self.hub.add_history(result)
        # Unpack this disc straight away, so it is extracted and compressed
        # while the next one downloads. The pipeline runs on its own thread
        # and de-duplicates by target, so re-submitting per disc costs
        # nothing and queues nothing twice. Only the playlist waits for the
        # whole game.
        if entry is not None and self.hub.settings["auto_extract"]:
            self.hub.submit_stage(entry, "extract")

        # A disc that just landed can no longer be unticked, and extraction
        # may have taken its archive away, so re-read what is on disk.
        self.hub.refresh_disc_states(result.vault_id)

        if plan is not None and plan["left"] > 0 and result.status == "ok":
            # One disc of several. The game is not finished, so its row stays
            # exactly where it is - moving it to history now would take the
            # card away while the remaining discs are still downloading.
            remaining = plan["left"]
            self.hub.patch_item(
                result.vault_id, progress=0.0, speed=0.0, eta=0.0,
                waiting_until=0,
                message=f"disc done, {remaining} more to go")
            return

        status_map = {"ok": "done", "skipped": "skipped", "failed": "failed",
                      "listed": "listed"}
        self.hub.patch_item(result.vault_id,
                            status=status_map.get(result.status, result.status),
                            progress=1.0 if result.status == "ok" else 0.0,
                            speed=0.0, eta=0.0, waiting_until=0,
                            message=result.message)
        # Finished games leave the queue so whatever is downloading stays at
        # the top; they are in the history now. Only a game that is wholly
        # "ok" goes: `add_history` records nothing else, so dropping a skipped
        # or failed one would erase it without trace, the retry sweeps need
        # the failed ones, and a game whose disc 2 failed is not finished
        # however well disc 3 went.
        if result.status == "ok" and (plan is None or plan["failed"] == 0):
            self.hub.drop_item(result.vault_id)

    def choose_discs(self, page, discs):
        """Only reached with "Discs - ask me" on. Blocks the run."""
        item = self.hub.queue_item(page.vault_id) or {}
        known = {d["disc"]: d for d in (item.get("discs") or [])}
        self.hub.log(f"  waiting for you to choose discs for {page.title}")
        answer = self.hub.ask("discs", {
            "vault_id": page.vault_id,
            "title": page.title,
            "discs": [{"disc": d,
                       "size_text": (known.get(d) or {}).get("size_text", ""),
                       "selected": True} for d in discs],
        }, default=list(discs))
        if answer == "skip":
            return []
        chosen = [int(d) for d in answer]
        self.hub.patch_item(page.vault_id, chosen=True)
        return chosen

    def choose_versions(self, page, candidates, alt_of):
        """Only reached with "Revision - ask me" on. Blocks the run."""
        self.hub.log(f"  waiting for you to choose a revision for {page.title}")
        answer = self.hub.ask("versions", {
            "vault_id": page.vault_id,
            "title": page.title,
            "disc": candidates[0].disc,
            "versions": [{"media_id": m.media_id, "version": m.version,
                          "filename": m.filename,
                          "size_text": m.size_text(alt_of(m))}
                         for m in candidates],
        }, default=[candidates[0].media_id])
        if answer == "skip":
            return []
        wanted = {int(m) for m in answer}
        picked = [m for m in candidates if m.media_id in wanted]
        return picked or candidates[:1]

    def sweep_started(self, sweep, pending):
        self.hub.log(f"=== sweep {sweep}: retrying {pending} unfinished ===")


# --------------------------------------------------------------------------
# FastAPI wiring
# --------------------------------------------------------------------------


def create_app() -> FastAPI:
    app = FastAPI(title="VimmGet")
    hub = Hub()
    app.state.hub = hub

    @app.on_event("startup")
    async def _startup():
        hub.loop = asyncio.get_running_loop()
        hub.check_site()

    @app.middleware("http")
    async def no_cache_frontend(request, call_next):
        """Serve the interface fresh, always.

        Without an explicit directive browsers fall back to heuristic caching
        and can reuse index.html or style.css without revalidating, so an
        update to web/ appears not to have happened. The app serves a few KB
        from local disk, where caching buys nothing and costs exactly that
        confusion.
        """
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.startswith("/static"):
            response.headers["Cache-Control"] = "no-store, must-revalidate"
        return response

    # ------------------------------------------------------------- state

    @app.get("/api/state")
    def state():
        return hub.snapshot()

    @app.post("/api/site/check")
    def recheck_site():
        hub.check_site()
        return {"site": hub.site.as_dict()}

    # ------------------------------------------------------------- queue

    @app.post("/api/queue")
    def add_queue(body: dict):
        if "hits" in body:
            pairs = [(int(h["vault_id"]), h.get("title", ""), h.get("system", ""))
                     for h in body["hits"]]
        else:
            warnings: list[str] = []
            ids = parse_id_lines(str(body.get("text", "")).splitlines(),
                                 "input", warnings.append)
            for w in warnings:
                hub.log(f"! {w}")
            pairs = [(vault_id, "", "") for vault_id in ids]
        added = hub.add_ids(pairs)
        return {"added": added, "queue": hub.queue}

    @app.post("/api/queue/{vault_id}/resolve")
    def resolve_one(vault_id: int, force: bool = False):
        """Look a single game up, when its row is opened."""
        result = hub.resolve_item(vault_id, force=force)
        if "error" in result:
            return JSONResponse(result, status_code=404)
        return result

    @app.post("/api/queue/{vault_id}/discs")
    def set_discs(vault_id: int, body: dict):
        """Tick/untick which discs of a multi-disc game to download.

        Accepted right up until the run starts that game, so the choice can
        be made while earlier games download.
        """
        result = hub.set_discs(vault_id, {int(d) for d in body.get("discs", [])})
        if "error" in result:
            code = 404 if result["error"] == "unknown item" else 409
            return JSONResponse(result, status_code=code)
        return result

    @app.delete("/api/queue/{vault_id}")
    def remove_queue(vault_id: int):
        with hub._lock:
            hub.queue = [q for q in hub.queue if q["vault_id"] != vault_id]
        hub.emit({"type": "queue", "queue": hub.queue})
        return {"queue": hub.queue}

    @app.post("/api/queue/clear")
    def clear_queue():
        with hub._lock:
            hub.queue = [q for q in hub.queue
                         if q["status"] in ("working", "downloading", "waiting")]
        hub.emit({"type": "queue", "queue": hub.queue})
        return {"queue": hub.queue}

    @app.post("/api/queue/reorder")
    def reorder(body: dict):
        order = [int(v) for v in body.get("order", [])]
        index = {vault_id: i for i, vault_id in enumerate(order)}
        with hub._lock:
            hub.queue.sort(key=lambda q: index.get(q["vault_id"], 10**9))
        hub.emit({"type": "queue", "queue": hub.queue})
        return {"queue": hub.queue}

    # --------------------------------------------------------------- run

    @app.post("/api/prompt/{prompt_id}")
    def answer_prompt(prompt_id: str, body: dict):
        """Answer whatever the run is currently waiting on.

        `answer` is a list of disc numbers or media ids, or the string "skip".
        """
        result = hub.answer_prompt(prompt_id, body.get("answer"))
        if "error" in result:
            return JSONResponse(result, status_code=409)
        return result

    @app.post("/api/run/start")
    def start():
        return {"status": hub.start_run(), "run": hub.run_status}

    @app.post("/api/run/pause")
    def pause():
        return {"status": hub.stop_run(pause=True), "run": hub.run_status}

    @app.post("/api/run/stop")
    def stop():
        return {"status": hub.stop_run(pause=False), "run": hub.run_status}

    @app.get("/api/run/partials")
    def partials():
        """What Stop would throw away, so the prompt can be truthful."""
        return hub.partials_summary()

    # ------------------------------------------------------------- search

    @app.get("/api/search")
    def do_search(q: str):
        prefer = [t.strip() for t in str(hub.settings["prefer"]).split(",")
                  if t.strip()]
        try:
            hits = search_mod.search(hub.search_session, q)
        except VimmError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": str(exc)}, status_code=502)

        hub.tag_vocabulary = search_mod.tag_vocabulary(hits)
        hits = search_mod.sort_by_preference(hits, prefer)
        kept, hidden = search_mod.filter_by_tags(hits,
                                                 hub.settings["hidden_tags"])
        return {
            "hits": [h.as_dict() for h in kept],
            "hidden": [h.as_dict() for h in hidden],
            "tag_vocabulary": hub.tag_vocabulary,
        }

    # ----------------------------------------------------------- pipeline

    @app.post("/api/items/{key}/{stage}")
    def run_stage(key: str, stage: str):
        if stage not in ("extract", "chd", "m3u"):
            return JSONResponse({"error": f"unknown stage {stage}"}, status_code=400)
        entry = hub.history_item(key)
        if entry is None:
            return JSONResponse({"error": "unknown item"}, status_code=404)
        job = hub.submit_stage(entry, stage)
        return {"job": job.as_dict() if job else None}

    @app.post("/api/convert-all")
    def convert_all():
        return {"submitted": hub.convert_all()}

    @app.delete("/api/history/{key}")
    def remove_history(key: str):
        with hub._lock:
            hub.history = [h for h in hub.history if h["key"] != key]
        hub.save_history()
        return {"history": [hub.decorate(h) for h in hub.history]}

    # ----------------------------------------------------------- settings

    @app.get("/api/settings")
    def get_settings():
        return hub.settings

    @app.put("/api/settings")
    def put_settings(body: dict):
        for key in DEFAULT_SETTINGS:
            if key in body:
                hub.settings[key] = body[key]
        _save_json(DATA_DIR / "settings.json", hub.settings)
        hub.emit({"type": "settings", "settings": hub.settings})
        # Gating depends on settings, so the UI needs fresh flags.
        hub.emit({"type": "history", "history": [hub.decorate(h)
                                                 for h in hub.history]})
        return hub.settings

    # ---------------------------------------------------------- websocket

    @app.websocket("/ws")
    async def websocket(socket: WebSocket):
        await socket.accept()
        hub._sockets.add(socket)
        # Catch the client up before anything else. Events are fire-and-forget
        # to whoever is listening at the time, so anything that happened
        # before this socket existed was dropped - the startup site check
        # being the one that bit: it finishes about a second in, often in the
        # gap between the page fetching /api/state and getting here, leaving
        # the indicator on "checking..." with the answer already thrown away.
        try:
            await socket.send_text(json.dumps({"type": "state",
                                               "state": hub.snapshot()}))
        except Exception:  # noqa: BLE001 - a client that vanished is not news
            hub._sockets.discard(socket)
            return
        try:
            while True:
                await socket.receive_text()  # keep-alive; content ignored
        except WebSocketDisconnect:
            hub._sockets.discard(socket)

    # ------------------------------------------------------------- static

    @app.get("/")
    def index():
        return FileResponse(WEB_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
    return app
