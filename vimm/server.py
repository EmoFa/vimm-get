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
import queue
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
    VimmError,
    destination_for,
    human_bytes,
    human_duration,
    make_options,
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

# Systems where a game arrives as split disc images (bin+cue, gdi) that CHD
# usefully collapses into one file. Deliberately excludes ps2/psp (single
# ISOs - nothing to collapse) and both Jaguars (BigPEmu cannot read CHD).
DEFAULT_CHD_SYSTEMS = ["psx", "saturn", "segacd", "tgcd", "dreamcast", "cdimono1"]

DEFAULT_SETTINGS = {
    "out": str(Path.home() / "Downloads" / "Vimm"),
    "organize": True,
    "prefer": "USA, Europe",
    "version_policy": "latest",
    "delay": 5.0,
    "sweeps": 10,
    "cancel_busy": True,
    "cookies": "",
    "auto_extract": False,
    "auto_compress": False,
    "auto_m3u": False,
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
        self.queue: list[dict] = []
        self.run_status = "idle"
        self.log_lines: list[str] = []
        self.tag_vocabulary: list[str] = list(search_mod.KNOWN_TAGS)

        self.loop: asyncio.AbstractEventLoop | None = None
        self._sockets: set[WebSocket] = set()
        self._lock = threading.Lock()

        self._run_thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._pages: dict[int, object] = {}   # vault_id -> VaultPage
        self._opts = None

        # Entry keys currently being driven through every stage by Convert
        # All. Kept off the entry itself so it never reaches history.json.
        self._chaining: set[str] = set()

        self.pipeline = PipelineWorker(on_event=self._on_job_event)
        self.search_session = search_mod.make_session()
        self.reconcile_history()

        # Background resolver: fills in title/system/discs for queued items
        # so the disc picker and the per-system buttons work before any
        # download starts.
        self._resolve_queue: queue.Queue[int] = queue.Queue()
        threading.Thread(target=self._resolve_loop, daemon=True,
                         name="resolver").start()

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
                self.queue.append({
                    "vault_id": vault_id,
                    "title": title or f"vault/{vault_id}",
                    "system": system,
                    "size_text": "",
                    "status": "queued",
                    "progress": 0.0,
                    "message": "",
                    "speed": 0.0,
                    "eta": 0.0,
                    "resolved": False,
                    "discs": [],
                })
                added.append(vault_id)
        if added:
            self.log(f"added {len(added)} item(s) to the queue")
            self.emit({"type": "queue", "queue": self.queue})
            for vault_id in added:
                self._resolve_queue.put(vault_id)
        return len(added)

    def queue_item(self, vault_id: int) -> dict | None:
        return next((q for q in self.queue if q["vault_id"] == vault_id), None)

    def patch_item(self, vault_id: int, **fields) -> None:
        item = self.queue_item(vault_id)
        if item is None:
            return
        item.update(fields)
        self.emit({"type": "item", "item": item})

    # ---------------------------------------------------------- resolution

    def _resolve_loop(self) -> None:
        """Fetch each queued game's vault page once, for title/size/discs."""
        from .engine import VimmClient

        while True:
            vault_id = self._resolve_queue.get()
            item = self.queue_item(vault_id)
            if item is None or item.get("resolved"):
                continue
            try:
                opts = self.build_options()
                client = VimmClient(opts)
                page = client.fetch_vault(vault_id)
                self._pages[vault_id] = page

                discs: dict[int, dict] = {}
                for media in page.media:
                    # One row per disc; the version actually downloaded is
                    # chosen later by the engine's preference rules.
                    discs.setdefault(media.disc, {
                        "disc": media.disc,
                        "size_text": media.size_text(0),
                        "selected": True,
                    })
                folder = system_folder(page.title,
                                       getattr(opts, "folders", None))
                self.patch_item(
                    vault_id,
                    title=page.title,
                    system=folder,
                    size_text=page.media[0].size_text(0) if page.media else "",
                    discs=[discs[d] for d in sorted(discs)],
                    resolved=True,
                )
            except VimmError as exc:
                self.patch_item(vault_id, status="failed", message=str(exc),
                                resolved=True)
                self.log(f"! vault/{vault_id}: {exc}")
            except Exception as exc:  # noqa: BLE001 - never kill the resolver
                self.patch_item(vault_id, status="failed", message=str(exc),
                                resolved=True)
            # Courtesy gap between page fetches.
            time.sleep(1.0)

    # ---------------------------------------------------------------- runs

    def disc_overrides(self) -> dict[int, list[int]]:
        """Per-game disc choices from the queue's checkboxes."""
        overrides: dict[int, list[int]] = {}
        for item in self.queue:
            discs = item.get("discs") or []
            if len(discs) > 1:
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

    def stop_run(self, pause: bool) -> str:
        if self._run_thread is None or not self._run_thread.is_alive():
            return "not running"
        self._cancel.set()
        self.run_status = "pausing" if pause else "stopping"
        self.emit({"type": "run", "status": self.run_status})
        return self.run_status

    def _run(self, ids: list[int]) -> None:
        listener = WebListener(self)
        try:
            results = run_http(ids, self._opts, listener=listener,
                               cancel_event=self._cancel)
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
            self.run_status = "paused - partial files resume on Start"
            with self._lock:
                for item in self.queue:
                    if item["status"] in ("working", "downloading", "waiting",
                                          "queued"):
                        item["status"] = "paused"
            self.emit({"type": "queue", "queue": self.queue})
        except VimmError as exc:
            self.run_status = f"error: {exc}"
            self.log(f"! {exc}")
        except Exception as exc:  # noqa: BLE001 - surface, never die silently
            self.run_status = f"error: {exc}"
            self.log(f"! unexpected: {exc}")
        finally:
            self.emit({"type": "run", "status": self.run_status})

    # -------------------------------------------------------------- history

    def can_chd(self, entry: dict) -> bool:
        """Whether the COMPRESS action is offerable right now.

        Includes "not already done", so the flag alone decides the button and
        callers cannot disagree about what it means.
        """
        systems = [s.lower() for s in self.settings.get("chd_systems", [])]
        stages = entry.get("stages", {})
        return (entry.get("system_folder", "").lower() in systems
                and bool(stages.get("extracted"))
                and not stages.get("chd"))

    def can_m3u(self, entry: dict) -> bool:
        """Playlists come last: the discs have to be compressed first, so the
        playlist lists .chd files rather than cue sheets that would be
        replaced moments later."""
        systems = [s.lower() for s in self.settings.get("m3u_systems", [])]
        stages = entry.get("stages", {})
        return (entry.get("system_folder", "").lower() in systems
                and len(entry.get("files", [])) > 1
                and bool(stages.get("chd"))
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
                    "stages": {"archive": True, "extracted": False,
                               "chd": False, "m3u": False},
                    "when": time.time(),
                }
                self.history.insert(0, entry)
            # Re-downloading the same disc replaces its row rather than
            # adding a duplicate.
            entry["files"] = [f for f in entry["files"]
                              if f["archive"] != archive]
            entry["files"].append({
                "filename": result.filename,
                "archive": archive,
                "bytes": result.bytes,
                "disc": len(entry["files"]) + 1,
                "message": result.message,
            })
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

    def submit_stage(self, entry: dict, kind: str) -> Job | None:
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
            submitted = None
            for sheet in sheets:
                submitted = self.pipeline.submit(Job(
                    kind="chd", label=sheet.name, target=sheet,
                    item_key=entry["key"],
                    extra={"delete_sources": bool(self.settings["delete_chd_sources"])}
                )) or submitted
            return submitted
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
        if not stages.get("extracted"):
            return "extract" if (forced or self.settings["auto_extract"]) else None
        if self.can_chd(entry) and not stages.get("chd"):
            return "chd" if (forced or self.settings["auto_compress"]) else None
        if self.can_m3u(entry) and not stages.get("m3u"):
            return "m3u" if (forced or self.settings["auto_m3u"]) else None
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
                self.track_files(entry, job.extra.get("extracted", []))
                # Only mark the game extracted once no archive remains.
                remaining = [f for f in entry.get("files", [])
                             if Path(f["archive"]).is_file()]
                if not remaining:
                    entry["stages"]["extracted"] = True
                    stage_completed = True
            elif job.kind == "chd":
                # The .chd replaces the sheet and its tracks; re-tracking
                # prunes what chdman deleted.
                produced = job.extra.get("chd")
                self.track_files(entry, [produced] if produced else [])
                # A multi-disc game is not converted until every disc is, so
                # this waits for the last sheet rather than the first.
                if not chd_mod.compressible_among(self.entry_files(entry)):
                    entry["stages"]["chd"] = True
                    stage_completed = True
            elif job.kind == "m3u":
                self.track_files(entry, job.extra.get("playlists", []))
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

    def item_started(self, position, total, vault_id, sweep_label=""):
        self._current = vault_id
        self.hub.patch_item(vault_id, status="working", message="")
        self.hub.log(f"{sweep_label}[{position}/{total}] vault/{vault_id}")

    def page_resolved(self, page):
        self.hub._pages[page.vault_id] = page
        self.hub.patch_item(page.vault_id, title=page.title)
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

    def item_done(self, result: Result):
        status_map = {"ok": "done", "skipped": "skipped", "failed": "failed",
                      "listed": "listed"}
        self.hub.patch_item(result.vault_id,
                            status=status_map.get(result.status, result.status),
                            progress=1.0 if result.status == "ok" else 0.0,
                            speed=0.0, eta=0.0, waiting_until=0,
                            message=result.message)
        entry = self.hub.add_history(result)
        if entry is not None and self.hub.settings["auto_extract"]:
            self.hub.submit_stage(entry, "extract")

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

    # ------------------------------------------------------------- state

    @app.get("/api/state")
    def state():
        return {
            "queue": hub.queue,
            "history": [hub.decorate(h) for h in hub.history],
            "jobs": hub.pipeline.snapshot(),
            "settings": hub.settings,
            "run": hub.run_status,
            "log": hub.log_lines[-200:],
            "tag_vocabulary": hub.tag_vocabulary,
        }

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

    @app.post("/api/queue/{vault_id}/discs")
    def set_discs(vault_id: int, body: dict):
        """Tick/untick which discs of a multi-disc game to download."""
        item = hub.queue_item(vault_id)
        if item is None:
            return JSONResponse({"error": "unknown item"}, status_code=404)
        wanted = {int(d) for d in body.get("discs", [])}
        for disc in item.get("discs", []):
            disc["selected"] = disc["disc"] in wanted
        hub.emit({"type": "item", "item": item})
        return {"item": item}

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

    @app.post("/api/run/start")
    def start():
        return {"status": hub.start_run(), "run": hub.run_status}

    @app.post("/api/run/pause")
    def pause():
        return {"status": hub.stop_run(pause=True), "run": hub.run_status}

    @app.post("/api/run/stop")
    def stop():
        return {"status": hub.stop_run(pause=False), "run": hub.run_status}

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
