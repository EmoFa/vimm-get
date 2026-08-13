"""
Post-processing job runner: extract -> compress -> playlist, one job at a
time on a dedicated worker thread (so a conversion can run while the next
download proceeds - but conversions never run in parallel with each other).
"""

from __future__ import annotations

import queue
import threading
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import chd as chd_mod
from . import extract as extract_mod
from . import m3u as m3u_mod
from .engine import VimmError


@dataclass
class Job:
    kind: str                      # extract | chd | m3u | chdman-setup
    label: str                     # human-readable, e.g. the filename
    target: Path                   # what it operates on
    item_key: str | None = None    # owning history item, if any
    extra: dict = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    status: str = "queued"         # queued | running | done | failed
    progress: float = 0.0          # 0..1
    message: str = ""

    def as_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "label": self.label,
            "item_key": self.item_key, "status": self.status,
            "progress": round(self.progress, 4), "message": self.message,
        }


class PipelineWorker:
    """Single background thread draining a job queue. `on_event(job)` fires
    on every meaningful change, from the worker thread."""

    def __init__(self, on_event=None):
        self.on_event = on_event or (lambda job: None)
        self.jobs: dict[str, Job] = {}
        self._queue: queue.Queue[Job] = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="pipeline")
        self._thread.start()

    def submit(self, job: Job) -> Job | None:
        """Queue a job, unless the same work is already pending.

        Returns None when the job was a duplicate. A second click on Convert
        All, or two entries that happen to name the same file, must not queue
        the same conversion twice - the second run would only fail on output
        that already exists.
        """
        if self._pending_key(job) in {self._pending_key(existing)
                                      for existing in self.jobs.values()
                                      if existing.status in ("queued", "running")}:
            return None
        self.jobs[job.id] = job
        self._queue.put(job)
        self.on_event(job)
        return job

    @staticmethod
    def _pending_key(job: Job) -> tuple[str, str]:
        try:
            target = str(Path(job.target).resolve())
        except OSError:
            target = str(job.target)
        return job.kind, target

    def snapshot(self) -> list[dict]:
        return [job.as_dict() for job in self.jobs.values()]

    def _emit(self, job: Job, *, progress: float | None = None,
              message: str | None = None) -> None:
        if progress is not None:
            job.progress = progress
        if message is not None:
            job.message = message
        self.on_event(job)

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            job.status = "running"
            self._emit(job)
            try:
                self._execute(job)
                job.status = "done"
                job.progress = 1.0
                self._emit(job)
            except VimmError as exc:
                job.status = "failed"
                self._emit(job, message=str(exc))
            except Exception as exc:  # noqa: BLE001 - a job must never kill the worker
                job.status = "failed"
                self._emit(job, message=f"unexpected: {exc}")
                traceback.print_exc()

    def _execute(self, job: Job) -> None:
        def progress(done, total):
            if total:
                self._emit(job, progress=min(done / total, 1.0))

        def status(text):
            self._emit(job, message=text)

        if job.kind == "extract":
            kept = extract_mod.extract_archive(job.target, progress=progress)
            job.extra["extracted"] = [str(p) for p in kept]
            job.message = f"extracted {len(kept)} file(s), archive deleted"
        elif job.kind == "chd":
            out = chd_mod.compress_to_chd(
                job.target,
                delete_sources=job.extra.get("delete_sources", True),
                progress=progress, status=status,
            )
            job.extra["chd"] = str(out)
            job.message = f"{out.name} ({out.stat().st_size / 1e6:.0f} MB)"
        elif job.kind == "m3u":
            # Scoped to this game's own discs; the system folder holds every
            # other game too, and a folder-wide sweep would build playlists
            # for them as well.
            files = job.extra.get("files")
            if files:
                written = m3u_mod.make_playlist_for(
                    job.target, files, job.extra.get("system_folder", ""),
                    job.extra.get("allowed_systems"),
                )
            else:
                written = m3u_mod.make_playlists(
                    job.target, job.extra.get("system_folder", ""),
                    job.extra.get("allowed_systems"),
                )
            job.extra["playlists"] = [str(p) for p in written]
            job.message = (f"{len(written)} playlist(s) written" if written
                           else "no multi-disc sets found (or system not m3u-capable)")
        else:
            raise VimmError(f"unknown job kind {job.kind!r}")
