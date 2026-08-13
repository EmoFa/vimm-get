"""Convert All: per-entry scoping, stage completion, self-heal, dedup, chaining.

Reproduces the reported bug first - three PS1 games sharing one psx/ folder,
where Convert All queued a compression job for every sheet against every entry.
Uses real chdman, as the other post-processing tests do.
"""
import os
import sys
import tempfile
import time
import uuid
import zipfile
from pathlib import Path

from pathlib import Path as _RepoPath
sys.path.insert(0, str(_RepoPath(__file__).resolve().parents[1]))

import vimm.server as srv

TESTDATA = Path(tempfile.mkdtemp(prefix="vimmca_data_"))
srv.DATA_DIR = TESTDATA

# chdman is a separate binary. Rather than pull a ~90 MB download during a
# test run, skip and say so; the app fetches it on demand in normal use.
from vimm.chd import find_chdman as _find_chdman

if _find_chdman() is None:
    print("SKIPPED: chdman not found - install it, or run the app's "
          "COMPRESS action once to fetch it")
    raise SystemExit(0)

failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    if not ok:
        failures.append(name)


def wait_for(predicate, timeout=180, poll=0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(poll)
    return False


def tiny_cue(folder: Path, stem: str):
    """A minimal but genuinely valid single-track disc image."""
    (folder / f"{stem}.bin").write_bytes(os.urandom(2352 * 150))
    (folder / f"{stem}.cue").write_text(
        f'FILE "{stem}.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n')
    return folder / f"{stem}.cue"


def make_entry(hub, system_dir: Path, title: str, discs: list[str],
               extracted=True, chd=False):
    """A history entry whose files already sit extracted in `system_dir`,
    exactly as the real pipeline leaves them."""
    on_disk = []
    for stem in discs:
        cue = tiny_cue(system_dir, stem)
        on_disk += [str(cue), str(cue.with_suffix(".bin"))]
    entry = {
        "key": uuid.uuid4().hex[:10],
        "vault_id": abs(hash(title)) % 100000,
        "title": title,
        "system_folder": system_dir.name,
        "dir": str(system_dir),
        "files": [{"filename": f"{s}.7z", "archive": str(system_dir / f"{s}.7z"),
                   "bytes": 1000, "disc": i + 1, "message": "CRC ok"}
                  for i, s in enumerate(discs)],
        "files_on_disk": on_disk,
        "stages": {"archive": True, "extracted": extracted,
                   "chd": chd, "m3u": False},
        "when": time.time(),
    }
    hub.history.insert(0, entry)
    return entry


# ============================================================ the reported bug
print("=== the reported bug: three games sharing one psx/ folder ===")
hub = srv.Hub()
hub.settings["auto_compress"] = False
hub.settings["auto_m3u"] = False
PSX = Path(tempfile.mkdtemp(prefix="vimmca_psx_")) / "psx"
PSX.mkdir(parents=True)

a = make_entry(hub, PSX, "Game A (PS1)", ["Game A (USA)"])
b = make_entry(hub, PSX, "Game B (PS1)", ["Game B (USA)"])
c = make_entry(hub, PSX, "Game C (PS1)", ["Game C (USA)"])

# Submit the compression stage for each entry, exactly as Convert All does.
for entry in (a, b, c):
    hub.submit_stage(entry, "chd")

chd_jobs = [j for j in hub.pipeline.jobs.values() if j.kind == "chd"]
targets = [Path(j.target).name for j in chd_jobs]
check("one chd job per sheet, not one per sheet per entry",
      len(chd_jobs) == 3, f"{len(chd_jobs)} jobs: {sorted(targets)}")
check("no duplicate targets", len(set(targets)) == len(targets), str(sorted(targets)))

owners = {Path(j.target).name: j.item_key for j in chd_jobs}
check("each job is owned by its own entry",
      owners.get("Game A (USA).cue") == a["key"]
      and owners.get("Game B (USA).cue") == b["key"]
      and owners.get("Game C (USA).cue") == c["key"], str(owners))

done = wait_for(lambda: all(j.status in ("done", "failed") for j in chd_jobs))
check("all compression jobs finished", done)
check("none failed", all(j.status == "done" for j in chd_jobs),
      str([(j.label, j.status, j.message) for j in chd_jobs if j.status != "done"]))

check("every entry flagged chd, not just the first",
      all(e["stages"]["chd"] for e in (a, b, c)),
      str([(e["title"], e["stages"]["chd"]) for e in (a, b, c)]))
check("no COMPRESS button remains on a converted game",
      not any(hub.can_chd(e) for e in (a, b, c)))
check("the .chd files exist", all((PSX / f"Game {n} (USA).chd").is_file()
                                  for n in "ABC"))

# ==================================================== multi-disc completion
print("=== a 3-disc game is only 'chd' once every disc is converted ===")
hub2 = srv.Hub()
hub2.settings["auto_compress"] = False
hub2.settings["auto_m3u"] = False
PSX2 = Path(tempfile.mkdtemp(prefix="vimmca_psx2_")) / "psx"
PSX2.mkdir(parents=True)
multi = make_entry(hub2, PSX2, "Three Disc Game (PS1)",
                   [f"Three Disc Game (USA) (Disc {n})" for n in (1, 2, 3)])

hub2.submit_stage(multi, "chd")
jobs2 = [j for j in hub2.pipeline.jobs.values() if j.kind == "chd"]
check("one job per disc", len(jobs2) == 3, f"{len(jobs2)}")

# Watch the flag while the jobs run: it must not flip early.
flipped_early = False
while not all(j.status in ("done", "failed") for j in jobs2):
    finished = sum(1 for j in jobs2 if j.status == "done")
    if multi["stages"]["chd"] and finished < 3:
        flipped_early = True
    time.sleep(0.05)
time.sleep(0.5)
check("not marked converted after only some discs", not flipped_early)
check("marked converted once all three are done", multi["stages"]["chd"])
check("m3u unlocks only now", hub2.can_m3u(multi))

# ============================================================== m3u scoping
print("=== building a playlist touches only that game's discs ===")
hub3 = srv.Hub()
hub3.settings["auto_m3u"] = False
PSX3 = Path(tempfile.mkdtemp(prefix="vimmca_psx3_")) / "psx"
PSX3.mkdir(parents=True)
# Two different multi-disc games in the same system folder, already CHDs.
for game in ("Alpha", "Beta"):
    for n in (1, 2):
        (PSX3 / f"{game} (USA) (Disc {n}).chd").write_bytes(b"chd")
alpha = {
    "key": "alpha", "vault_id": 1, "title": "Alpha (PS1)",
    "system_folder": "psx", "dir": str(PSX3),
    "files": [{"filename": f"Alpha (USA) (Disc {n}).7z",
               "archive": str(PSX3 / f"Alpha (USA) (Disc {n}).7z"),
               "bytes": 1, "disc": n, "message": ""} for n in (1, 2)],
    "files_on_disk": [str(PSX3 / f"Alpha (USA) (Disc {n}).chd") for n in (1, 2)],
    "stages": {"archive": True, "extracted": True, "chd": True, "m3u": False},
    "when": time.time(),
}
hub3.history.insert(0, alpha)
hub3.submit_stage(alpha, "m3u")
wait_for(lambda: all(j.status in ("done", "failed")
                     for j in hub3.pipeline.jobs.values()))
time.sleep(0.3)

check("Alpha got its playlist folder",
      (PSX3 / "Alpha (USA).m3u" / "Alpha (USA).m3u").is_file())
check("Beta was left completely alone",
      all((PSX3 / f"Beta (USA) (Disc {n}).chd").is_file() for n in (1, 2))
      and not (PSX3 / "Beta (USA).m3u").exists())

# ================================================================ self-heal
print("=== an already-converted entry repairs its own flag ===")
hub4 = srv.Hub()
PSX4 = Path(tempfile.mkdtemp(prefix="vimmca_psx4_")) / "psx"
PSX4.mkdir(parents=True)
(PSX4 / "Healed (USA).chd").write_bytes(b"chd")
healed = {
    "key": "healed", "vault_id": 2, "title": "Healed (PS1)",
    "system_folder": "psx", "dir": str(PSX4),
    "files": [{"filename": "Healed (USA).7z",
               "archive": str(PSX4 / "Healed (USA).7z"),
               "bytes": 1, "disc": 1, "message": ""}],
    "files_on_disk": [str(PSX4 / "Healed (USA).chd")],
    "stages": {"archive": True, "extracted": True, "chd": False, "m3u": False},
    "when": time.time(),
}
hub4.history.insert(0, healed)
check("starts wrongly flagged", hub4.can_chd(healed))
hub4.submit_stage(healed, "chd")
time.sleep(0.4)
check("pressing COMPRESS corrects the flag", healed["stages"]["chd"])
check("button disappears", not hub4.can_chd(healed))

# startup reconciliation does the same for history loaded from disk
hub4b = srv.Hub()
hub4b.history = [dict(healed, stages={"archive": True, "extracted": True,
                                      "chd": False, "m3u": False})]
hub4b.reconcile_history()
check("startup reconciliation also corrects it",
      hub4b.history[0]["stages"]["chd"])

# ===================================================================== dedup
print("=== a second Convert All click queues nothing extra ===")
hub5 = srv.Hub()
hub5.settings["auto_compress"] = False
PSX5 = Path(tempfile.mkdtemp(prefix="vimmca_psx5_")) / "psx"
PSX5.mkdir(parents=True)
dupe = make_entry(hub5, PSX5, "Dupe (PS1)", ["Dupe (USA)"])
hub5.submit_stage(dupe, "chd")
hub5.submit_stage(dupe, "chd")
pending = [j for j in hub5.pipeline.jobs.values()
           if j.kind == "chd" and j.status in ("queued", "running")]
check("the repeat submission was ignored",
      len([j for j in hub5.pipeline.jobs.values() if j.kind == "chd"]) == 1,
      f"{len(pending)} pending")
wait_for(lambda: all(j.status in ("done", "failed")
                     for j in hub5.pipeline.jobs.values()))

# ================================================================== chaining
print("=== one Convert All click carries a game all the way through ===")
hub6 = srv.Hub()
hub6.settings["auto_extract"] = False
hub6.settings["auto_compress"] = False   # chaining must not need these
hub6.settings["auto_m3u"] = False
PSX6 = Path(tempfile.mkdtemp(prefix="vimmca_psx6_")) / "psx"
PSX6.mkdir(parents=True)

# Two discs, still archived, as they are right after downloading.
archives = []
for n in (1, 2):
    stem = f"Chained (USA) (Disc {n})"
    staging = Path(tempfile.mkdtemp())
    cue = tiny_cue(staging, stem)
    archive = PSX6 / f"{stem}.7z"
    import py7zr
    with py7zr.SevenZipFile(archive, "w") as z:
        z.write(cue, f"{stem}/{stem}.cue")
        z.write(cue.with_suffix(".bin"), f"{stem}/{stem}.bin")
    archives.append(archive)

chained = {
    "key": "chained", "vault_id": 3, "title": "Chained (PS1)",
    "system_folder": "psx", "dir": str(PSX6),
    "files": [{"filename": a.name, "archive": str(a), "bytes": a.stat().st_size,
               "disc": i + 1, "message": "CRC ok"} for i, a in enumerate(archives)],
    "stages": {"archive": True, "extracted": False, "chd": False, "m3u": False},
    "when": time.time(),
}
hub6.history.insert(0, chained)
hub6.convert_all()

reached = wait_for(lambda: chained["stages"]["m3u"], timeout=240)
check("reached the m3u stage from one click", reached,
      str(chained["stages"]))
check("archives gone", not any(a.exists() for a in archives))
check("playlist written with both discs",
      (PSX6 / "Chained (USA).m3u" / "Chained (USA).m3u").is_file())
if (PSX6 / "Chained (USA).m3u" / "Chained (USA).m3u").is_file():
    lines = (PSX6 / "Chained (USA).m3u" / "Chained (USA).m3u").read_text().strip().splitlines()
    check("playlist lists the chds", all(l.endswith(".chd") for l in lines) and len(lines) == 2,
          str(lines))

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
