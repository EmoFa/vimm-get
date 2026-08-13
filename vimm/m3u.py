"""
m3u playlist creation for multi-disc games.

Only multi-disc games get playlists, and only on systems whose emulators
read m3u (RetroArch's disc-swap convention). Layout, as chosen: the discs
move into a folder whose name ends in .m3u, with the playlist of the same
name inside it - front-ends that treat a ".m3u" directory as one entry then
show a single game rather than three discs.

    psx/
      Final Fantasy VII (USA).m3u/
        Final Fantasy VII (USA).m3u
        Final Fantasy VII (USA) (Disc 1).chd
        Final Fantasy VII (USA) (Disc 2).chd
        Final Fantasy VII (USA) (Disc 3).chd
"""

from __future__ import annotations

import re
from pathlib import Path

from .engine import VimmError

# Folder names (SYSTEM_FOLDERS values) whose common emulators read m3u.
# Editable in the web app's settings.
DEFAULT_M3U_SYSTEMS = ["psx", "saturn", "segacd", "tgcd", "dreamcast", "cdimono1"]

# Disc image types worth listing in a playlist, in preference order: a .chd
# stands alone; .cue/.gdi reference their .bin tracks (which must move with
# them but never appear in the m3u).
_PLAYLIST_TYPES = (".chd", ".cue", ".gdi")
_COMPANION_TYPES = (".bin", ".raw", ".iso", ".img", ".sub", ".wav")

_DISC_TAG = re.compile(r"\s*\((?:Disc|Disk|CD)\s*(\d+)\)", re.I)


def disc_number(name: str) -> int | None:
    match = _DISC_TAG.search(name)
    return int(match.group(1)) if match else None


def group_key(name: str) -> str:
    """Filename stem minus the disc tag: the game's identity."""
    return _DISC_TAG.sub("", Path(name).stem).strip()


def find_disc_sets(folder: Path) -> dict[str, list[Path]]:
    """Multi-disc sets in `folder`: game name -> playlist-worthy disc files.

    Only groups with 2+ discs count - single-disc games get no playlist.
    Discs already filed into a `.m3u` folder are skipped, so re-running is
    a no-op rather than nesting folders.
    """
    # game -> disc number -> best file for that disc
    groups: dict[str, dict[int, Path]] = {}
    for path in sorted(folder.iterdir()):
        if not path.is_file() or path.suffix.lower() not in _PLAYLIST_TYPES:
            continue
        disc = disc_number(path.name)
        if disc is None:
            continue
        discs = groups.setdefault(group_key(path.name), {})
        current = discs.get(disc)
        # One entry per disc: a .chd supersedes the .cue/.gdi it was made
        # from, so a half-converted folder never lists a disc twice.
        if current is None or _rank(path) < _rank(current):
            discs[disc] = path

    return {
        name: [discs[d] for d in sorted(discs)]
        for name, discs in groups.items()
        if len(discs) >= 2
    }


def _rank(path: Path) -> int:
    """Lower wins. Playlists prefer the compressed image."""
    return _PLAYLIST_TYPES.index(path.suffix.lower())


def _companions_of(disc: Path) -> list[Path]:
    """Files referenced by a cue/gdi sheet (bin tracks etc.) that must move
    with it. Matched by shared stem prefix in the same folder."""
    if disc.suffix.lower() not in (".cue", ".gdi"):
        return []
    stem = disc.stem
    out = []
    for sibling in disc.parent.iterdir():
        if sibling == disc or not sibling.is_file():
            continue
        if sibling.suffix.lower() in _COMPANION_TYPES and sibling.stem.startswith(stem):
            out.append(sibling)
    return out


def build_m3u(folder: Path, game_name: str, discs: list[Path]) -> Path:
    """Move `discs` (plus their track files) into `folder/game_name.m3u/` and
    write `game_name.m3u` inside it. Returns the m3u path. Idempotent."""
    if len(discs) < 2:
        raise VimmError("m3u playlists are only for multi-disc games")

    # The folder itself carries the .m3u extension, matching the playlist
    # file inside it.
    game_dir = folder / f"{game_name}.m3u"
    game_dir.mkdir(exist_ok=True)

    entries: list[str] = []
    for disc in discs:
        to_move = [disc] + _companions_of(disc)
        for path in to_move:
            target = game_dir / path.name
            if path.resolve() != target.resolve():
                if target.exists():
                    raise VimmError(f"refusing to overwrite {target}")
                path.rename(target)
        entries.append(disc.name)

    m3u_path = game_dir / f"{game_name}.m3u"
    m3u_path.write_text("\n".join(entries) + "\n", encoding="utf-8")
    return m3u_path


def make_playlists(folder: Path, system_folder_name: str,
                   allowed_systems: list[str] | None = None) -> list[Path]:
    """Create playlists for every multi-disc set in `folder`, if the system
    qualifies. Returns the m3u paths written (empty when nothing applies)."""
    allowed = allowed_systems if allowed_systems is not None else DEFAULT_M3U_SYSTEMS
    if system_folder_name.lower() not in [s.lower() for s in allowed]:
        return []
    written = []
    for game_name, discs in find_disc_sets(folder).items():
        written.append(build_m3u(folder, game_name, discs))
    return written
