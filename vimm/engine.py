"""
Vimm's Lair download engine.

Everything the downloader knows about the site lives here: resolving a vault
ID to its real download target, the Referer gate, resume, the per-IP slot,
CRC verification, version/disc selection. No terminal I/O - front-ends
observe a run through a `Listener` and stop it through a `threading.Event`.

Front-end: `vimm.server`, the local web app.

This is deliberately a *polite* client:
  * one download at a time, never parallel
  * a configurable delay between downloads
  * honours Retry-After on 429/503 and backs off on transient errors
  * sends the headers a normal browser sends (the site requires a Referer)

It does not attempt to defeat CAPTCHAs, rotate IPs, or otherwise evade the
site's protections. If the site says no, the run reports it and moves on.
"""

from __future__ import annotations

import argparse
import base64
import csv
import html
import http.cookiejar
import json
import os
import random
import re
import threading
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests


BASE = "https://vimm.net"
VAULT_URL = BASE + "/vault/{vault_id}"
REFERER = BASE + "/"

# A current, ordinary desktop Chrome UA. The site rejects requests that do not
# look like a browser; this is the same identification a real visit sends.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
}

# Windows-illegal characters, plus control chars.
_BAD_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_REGION_TAG = re.compile(r"\(([^()]+)\)")
_SYSTEM_TAG = re.compile(r"\(([^()]+)\)\s*$")

# Folder name per system when `organize` is on, keyed by the tag Vimm puts
# at the end of a vault page title ("Phantasy Star (SMS)" -> SMS). Verified
# against every system the site lists; the `folders` option overrides
# individual entries.
SYSTEM_FOLDERS = {
    "atari2600": "atari2600",
    "atari5200": "atari5200",
    "atari7800": "atari7800",
    "jaguar": "atarijaguar",
    "jaguarcd": "atarijaguarcd",
    "lynx": "atarilynx",
    "cdi": "cdimono1",
    "dreamcast": "dreamcast",
    "gg": "gamegear",
    "gb": "gb",
    "gba": "gba",
    "gbc": "gbc",
    "gamecube": "gc",
    "genesis": "genesis",
    "sms": "mastersystem",
    "3ds": "n3ds",
    "n64": "n64",
    "ds": "nds",
    "nes": "nes",
    "ps1": "psx",
    "ps2": "ps2",
    "ps3": "ps3",
    "psp": "psp",
    "saturn": "saturn",
    "32x": "sega32x",
    "segacd": "segacd",
    "snes": "snes",
    "tg16": "tg16",
    "tgcd": "tgcd",
    "vb": "virtualboy",
    "wii": "wii",
    "wiiu": "wiiu",
    "xbox": "xbox",
    "xbox360": "xbox360",
    # Two systems Vimm carries that had no folder of their own; these run on
    # the same hardware as their parent, so they share its folder.
    "wiiware": "wii",
    "x360-d": "xbox360",
}

_unknown_systems: set[str] = set()


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass
class Media:
    """One downloadable entry from a vault page's `media` array."""

    media_id: int
    version: str  # revision, e.g. "1.0", "1.1"
    disc: int  # SortOrder: disc/cart number for multi-disc sets
    filename: str  # decoded GoodTitle, e.g. "Chrono Trigger (USA).sfc"
    sizes: list[int]  # bytes, indexed by `alt` (0 = primary format)
    size_texts: list[str]
    formats: list[str]  # Mirror[], indexed by `alt`
    crc32: str | None
    md5: str | None
    sha1: str | None

    def size(self, alt: int) -> int:
        return self.sizes[alt] if alt < len(self.sizes) else 0

    def size_text(self, alt: int) -> str:
        return self.size_texts[alt] if alt < len(self.size_texts) else "?"

    def format_name(self, alt: int) -> str:
        if alt < len(self.formats):
            return self.formats[alt]
        return f"alt{alt}"

    @property
    def regions(self) -> list[str]:
        """Parenthesised tags in the title, e.g. ['USA', 'En,Fr']."""
        return [t.strip() for t in _REGION_TAG.findall(self.filename)]

    def label(self, alt: int = 0) -> str:
        return (
            f"[{self.media_id}] {self.filename}  "
            f"v{self.version} disc {self.disc}  "
            f"{self.format_name(alt)} {self.size_text(alt)}"
        )


@dataclass
class VaultPage:
    vault_id: int
    title: str
    download_host: str  # e.g. "https://dl3.vimm.net/"
    media: list[Media] = field(default_factory=list)


@dataclass
class Result:
    vault_id: int
    media_id: int | str
    filename: str
    status: str  # ok | skipped | failed
    bytes: int
    message: str
    partial: int = 0  # bytes sitting in a .part file, for sweep accounting


class VimmError(Exception):
    """A non-retryable problem with a specific vault ID."""


class RetryableError(Exception):
    """A transient problem worth retrying."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class BusyError(RetryableError):
    """The server, or our IP, is already occupied with another download.

    Vimm's Lair allows one download at a time per IP address, and answers a
    second request with HTTP 429 and a page naming the file that holds the
    slot. Sharing an exit node (a VPN) means someone else can take that slot.
    Not a failure in itself, so this carries its own wait schedule and budget.
    """


class Cancelled(Exception):
    """The front-end asked the run to stop. Partial files stay on disk and
    resume next run, so cancelling is always safe."""


# Wait reason used between discs of one multi-disc game (as opposed to
# between two different games). Front-ends match on this to explain the
# pause, which would otherwise look like a stall.
NEXT_DISC_WAIT = "waiting for the next disc"


class Listener:
    """Observer for a run. Every method is optional; the default is silent.

    The web server turns these into WebSocket events. Engine code must never
    print - it reports here instead.
    Calls arrive on the engine's worker thread.
    """

    def item_started(self, position: int, total: int, vault_id: int,
                     sweep_label: str = "") -> None: ...

    def page_resolved(self, page: VaultPage) -> None: ...

    def status(self, text: str) -> None:
        """An informational line about the current item."""

    def warn(self, text: str) -> None:
        """A problem being handled (retry, unmapped system, bad input line)."""

    def waiting(self, seconds: float, reason: str) -> None: ...

    def progress_begin(self, vault_id: int, filename: str, total: int,
                       done: int) -> None: ...

    def progress(self, vault_id: int, filename: str, done: int, total: int) -> None:
        """Throttled to ~10 Hz by the engine."""

    def progress_done(self, text: str) -> None:
        """The transfer attempt ended; `text` is the closing summary line."""

    def would_get(self, media: Media, alt: int, dest: Path) -> None:
        """Dry-run (`list`) line."""

    def item_done(self, result: Result) -> None: ...

    def sweep_started(self, sweep: int, pending: int) -> None: ...

    def sweep_finished(self, sweep: int, recovered: int, still_failed: int,
                       stopped: bool) -> None: ...

    def choose_versions(self, page: VaultPage, candidates: list[Media],
                        alt_of) -> list[Media]:
        """Called when `pick` is set and a disc has several versions."""
        return candidates[:1]


class _Meter:
    """Feeds listener.progress at a sane rate while the chunk loop runs."""

    def __init__(self, listener: Listener, vault_id: int, filename: str,
                 total: int, done: int):
        self.listener = listener
        self.vault_id = vault_id
        self.filename = filename
        self.total = total
        self.done = done
        self.start = time.monotonic()
        self._last = 0.0
        listener.progress_begin(vault_id, filename, total, done)

    def advance(self, n: int) -> None:
        self.done += n
        now = time.monotonic()
        if now - self._last >= 0.1:
            self._last = now
            self.listener.progress(self.vault_id, self.filename, self.done, self.total)


# Phrases the site uses when it is turning us away rather than erroring.
BUSY_PHRASES = (
    "already",
    "in progress",
    "only one",
    "one download",
    "another download",
    "simultaneous",
    "concurrent",
    "limit",
    "queue",
    "too many",
    "please wait",
    "try again",
    "slow down",
)


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------


def parse_id_lines(lines, source: str = "input", warn=None) -> list[int]:
    """Extract vault IDs from lines of text (a file, or pasted input).

    Bare IDs are the expected format. Blank lines, `#` comments, trailing
    comments and full vault URLs are tolerated so hand-maintained lists do
    not break the run.
    """
    ids: list[int] = []
    seen: set[int] = set()
    for lineno, raw in enumerate(lines, 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        match = re.search(r"/vault/(\d+)", line) or re.fullmatch(r"(\d+)", line)
        if not match:
            if warn:
                warn(f"{source}:{lineno}: cannot read an ID from {raw.strip()!r}")
            continue
        vault_id = int(match.group(1))
        if vault_id in seen:
            continue
        seen.add(vault_id)
        ids.append(vault_id)
    return ids


def _extract_json_array(text: str, start: int) -> str:
    """Return the JSON array beginning at `start`, respecting string literals."""
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise VimmError("malformed media array on the vault page")


def _b64_title(value: str | None) -> str:
    if not value:
        return ""
    try:
        return base64.b64decode(value).decode("utf-8", "replace")
    except Exception:
        return ""


def _int_kb(value) -> int:
    """The page reports sizes in kibibytes as strings."""
    try:
        return int(str(value).strip() or 0) * 1024
    except (TypeError, ValueError):
        return 0


def parse_vault_page(vault_id: int, page_html: str) -> VaultPage:
    title_match = re.search(r"<title>(.*?)</title>", page_html, re.S | re.I)
    title = html.unescape(title_match.group(1).strip()) if title_match else f"vault/{vault_id}"
    title = re.sub(r"^The Vault:\s*", "", title)

    if "The Vault: Error" in title or "Error 404" in page_html:
        raise VimmError("vault page not found")

    form = re.search(r"<form\b[^>]*\bid=\"dl_form\"[^>]*>", page_html)
    if not form:
        form = re.search(r"<form\b[^>]*\baction=\"([^\"]*dl[^\"]*vimm\.net[^\"]*)\"[^>]*>", page_html)
    if not form:
        raise VimmError("no download form on the page (game may not be downloadable)")

    action_match = re.search(r'action="([^"]+)"', form.group(0))
    if not action_match:
        raise VimmError("download form has no action URL")
    action = html.unescape(action_match.group(1))
    if action.startswith("//"):
        action = "https:" + action
    elif action.startswith("/"):
        action = BASE + action

    marker = re.search(r"\blet\s+media\s*=\s*\[", page_html)
    if not marker:
        raise VimmError("no media list on the page")
    raw_array = _extract_json_array(page_html, marker.end() - 1)
    try:
        entries = json.loads(raw_array)
    except json.JSONDecodeError as exc:
        raise VimmError(f"could not parse media list: {exc}") from exc

    media: list[Media] = []
    for entry in entries:
        sizes = [
            _int_kb(entry.get("Zipped")),
            _int_kb(entry.get("AltZipped")),
            _int_kb(entry.get("AltZipped2")),
        ]
        size_texts = [
            str(entry.get("ZippedText") or "?"),
            str(entry.get("AltZippedText") or "?"),
            str(entry.get("AltZipped2Text") or "?"),
        ]
        mirrors = entry.get("Mirror") or []
        media.append(
            Media(
                media_id=int(entry["ID"]),
                version=str(entry.get("Version") or "1.0"),
                disc=int(entry.get("SortOrder") or 1),
                filename=_b64_title(entry.get("GoodTitle")) or f"media-{entry['ID']}",
                sizes=sizes,
                size_texts=size_texts,
                formats=[str(m) for m in mirrors],
                crc32=(entry.get("GoodHash") or None),
                md5=(entry.get("GoodMd5") or None),
                sha1=(entry.get("GoodSha1") or None),
            )
        )

    if not media:
        raise VimmError("vault page lists no media")

    return VaultPage(vault_id=vault_id, title=title, download_host=action, media=media)


def filename_from_disposition(header: str | None) -> str | None:
    """Pull a filename out of a Content-Disposition header."""
    if not header:
        return None
    star = re.search(r"filename\*\s*=\s*([^']*)'[^']*'([^;]+)", header, re.I)
    if star:
        return unquote(star.group(2).strip())
    quoted = re.search(r'filename\s*=\s*"([^"]+)"', header, re.I)
    if quoted:
        return quoted.group(1)
    bare = re.search(r"filename\s*=\s*([^;]+)", header, re.I)
    if bare:
        return bare.group(1).strip()
    return None


def safe_filename(name: str, fallback: str) -> str:
    name = _BAD_FILENAME_CHARS.sub("_", name).strip(" .")
    return name or fallback


# Extensions the site actually serves archives as.
ARCHIVE_SUFFIXES = (".zip", ".7z", ".rar")


def find_download(dest_dir: Path, stem: str, partial: bool) -> Path | None:
    """An archive already on disk for `stem`, whatever extension it has.

    The download filename comes from the server, not from us, so matching on
    the stem is what lets a later run recognise its own partial or finished
    file. Matching requires the separating dot, so "Game (Disc 1)" never
    picks up "Game (Disc 10)".
    """
    prefix = stem + "."
    try:
        entries = sorted(dest_dir.iterdir())
    except OSError:
        return None
    for path in entries:
        if not path.name.startswith(prefix) or not path.is_file():
            continue
        if partial:
            if path.name.endswith(".part"):
                return path
        elif (not path.name.endswith(".part")
              and path.suffix.lower() in ARCHIVE_SUFFIXES):
            return path
    return None


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def human_duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    if seconds >= 3600:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    if seconds >= 60:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds}s"


# --------------------------------------------------------------------------
# Cookies
# --------------------------------------------------------------------------


def load_cookies(session: requests.Session, path: Path) -> str:
    """Attach cookies from a Netscape cookies.txt or a raw `k=v; k=v` file."""
    text = path.read_text(encoding="utf-8-sig").strip()
    if not text:
        return "empty cookie file, continuing anonymously"

    if text.lstrip().startswith("# Netscape HTTP Cookie File") or "\t" in text:
        jar = http.cookiejar.MozillaCookieJar()
        try:
            jar.load(str(path), ignore_discard=True, ignore_expires=True)
        except http.cookiejar.LoadError as exc:
            raise VimmError(f"could not read {path}: {exc}") from exc
        for cookie in jar:
            session.cookies.set_cookie(cookie)
        return f"loaded {len(jar)} cookie(s) from {path.name}"

    # Raw header form: "session=abc; other=def", optionally `cookie = "..."`.
    raw = "; ".join(
        line.split("#", 1)[0].strip()
        for line in text.splitlines()
        if line.split("#", 1)[0].strip()
    )
    raw = re.sub(r'^\s*cookie\s*[:=]\s*', "", raw, flags=re.I).strip().strip("\"'")

    count = 0
    for pair in raw.split(";"):
        if "=" not in pair:
            continue
        name, _, value = pair.partition("=")
        name, value = name.strip(), value.strip()
        if not name:
            continue
        # Set on the apex domain so it is also sent to dl*.vimm.net.
        session.cookies.set(name, value, domain=".vimm.net", path="/")
        count += 1
    return f"loaded {count} cookie(s) from {path.name}"


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


class VimmClient:
    def __init__(
        self,
        opts: argparse.Namespace,
        listener: Listener | None = None,
        cancel_event: threading.Event | None = None,
    ):
        self.opts = opts
        self.listener = listener or Listener()
        self.cancel_event = cancel_event
        self.session = requests.Session()
        self.session.headers.update(BROWSER_HEADERS)
        self._cancels = 0

    # -- network primitives -------------------------------------------------

    def _check_cancelled(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise Cancelled()

    def _sleep(self, seconds: float, reason: str) -> None:
        if seconds <= 0:
            self._check_cancelled()
            return
        self.listener.waiting(seconds, reason)
        if self.cancel_event is not None:
            # Interruptible: a cancel during a long busy-wait takes effect
            # immediately instead of after the wait runs out.
            if self.cancel_event.wait(seconds):
                raise Cancelled()
        else:
            time.sleep(seconds)

    def _with_retries(self, what: str, fn, progress_probe=None):
        """Run `fn`, retrying transient failures.

        The retry budget counts *stalls*, not attempts. If `progress_probe`
        reports that an attempt moved bytes onto disk before it died, that
        attempt cost nothing: the stall counter and the backoff both reset.
        A download interrupted twenty times still finishes, so long as it is
        getting somewhere each time.

        Only attempts that achieve nothing at all count against `retries`,
        with `max_attempts` as a final backstop against spinning forever.
        """
        stall_limit = max(self.opts.retries, 0)
        busy_limit = max(self.opts.busy_retries, 0)
        hard_limit = max(self.opts.max_attempts, 1)

        stalls = 0  # consecutive attempts that gained nothing
        busy_waits = 0  # consecutive "come back later" responses
        attempt = 0

        while True:
            attempt += 1
            self._check_cancelled()
            before = progress_probe() if progress_probe else 0
            try:
                return fn()
            except (RetryableError, requests.RequestException) as exc:
                after = progress_probe() if progress_probe else 0
                gained = after - before
                busy = isinstance(exc, BusyError)

                if gained > 0:
                    # Real progress. Forgive the interruption entirely.
                    stalls = 0
                    busy_waits = 0
                else:
                    if busy:
                        busy_waits += 1
                    else:
                        stalls += 1

                if stalls > stall_limit:
                    raise VimmError(
                        f"{what}: {exc} (no progress after {stalls} attempts)"
                    ) from exc
                if busy_waits > busy_limit:
                    raise VimmError(
                        f"{what}: {exc} (still busy after {busy_waits} waits)"
                    ) from exc
                if attempt >= hard_limit:
                    raise VimmError(
                        f"{what}: {exc} (hit the {hard_limit}-attempt ceiling)"
                    ) from exc

                delay = getattr(exc, "retry_after", None)
                if delay is None:
                    if busy:
                        # Someone else holds the slot. Wait long enough for a
                        # real download to plausibly finish.
                        delay = min(
                            self.opts.busy_wait * (1.5 ** max(busy_waits - 1, 0)),
                            self.opts.busy_wait_max,
                        )
                    else:
                        delay = min(self.opts.backoff * (2 ** max(stalls - 1, 0)), 300)
                    delay += random.uniform(0, 1.5)

                if gained > 0:
                    self.listener.warn(f"{what}: {exc} (kept {human_bytes(gained)})")
                else:
                    self.listener.warn(f"{what}: {exc}")

                reason = "IP busy, waiting for the slot" if busy else f"retry {attempt + 1}"
                self._sleep(delay, reason)

    @staticmethod
    def _check_transient(response: requests.Response) -> None:
        code = response.status_code
        if code not in (429, 503, 502, 504, 408):
            return

        retry_after = None
        header = response.headers.get("Retry-After")
        if header:
            try:
                retry_after = float(header)
            except ValueError:
                retry_after = None

        # 429/503 mean "at capacity", which is the per-IP limit's likely shape.
        error = BusyError if code in (429, 503) else RetryableError
        raise error(f"HTTP {code} from server", retry_after)

    def _handle_busy(self, response: requests.Response) -> RetryableError:
        """React to the "you're already downloading" page.

        The page offers a link to release the slot. Taking it is far faster
        than waiting out a transfer that may be several gigabytes, so by
        default we do exactly what the page invites us to do and retry at
        once. Turning `cancel_busy` off waits instead.
        """
        text = response.text
        stripped = re.sub(r"<[^>]+>", " ", text[:4000])
        message = html.unescape(re.sub(r"\s+", " ", stripped)).strip()[:200] or "IP busy"

        link = re.search(r'href\s*=\s*["\']([^"\']*cancel[^"\']*)["\']', text, re.I)
        cancel_url = urljoin(response.url, link.group(1)) if link else None

        if not self.opts.cancel_busy:
            return BusyError(f"IP busy: {message}")
        if cancel_url is None:
            return BusyError(f"IP busy, no cancel link offered: {message}")
        if self._cancels >= self.opts.max_cancels:
            return BusyError(
                f"IP busy and already cancelled {self._cancels} times, backing off"
            )

        try:
            cancelled = self.session.get(
                cancel_url, headers={"Referer": REFERER}, timeout=self.opts.timeout
            )
            cancelled.close()
        except requests.RequestException as exc:
            return BusyError(f"IP busy, cancel request failed ({exc})")

        if cancelled.status_code >= 400:
            return BusyError(f"IP busy, cancel returned HTTP {cancelled.status_code}")

        self._cancels += 1
        self.listener.status(f"released the download slot ({message[:90]})")
        return RetryableError(
            "cancelled the download holding the slot", retry_after=self.opts.cancel_wait
        )

    @staticmethod
    def _classify_html(text: str) -> RetryableError:
        """Turn a message page served in place of a file into the right error.

        The site answers a refused download with HTML rather than a status
        code, so this decides whether we should wait or simply retry. Neither
        outcome is fatal - a page here never means the file is unavailable.
        """
        snippet = re.sub(r"<[^>]+>", " ", text[:4000])
        snippet = html.unescape(re.sub(r"\s+", " ", snippet)).strip()
        lowered = snippet.lower()
        message = snippet[:200] or "no message"
        if any(phrase in lowered for phrase in BUSY_PHRASES):
            return BusyError(f"server says wait: {message}")
        return RetryableError(f"server returned a page instead of a file: {message}")

    # -- vault page ---------------------------------------------------------

    def fetch_vault(self, vault_id: int) -> VaultPage:
        # site_base lets tests aim at a local server instead of the live site.
        origin = (getattr(self.opts, "site_base", None) or BASE).rstrip("/")
        url = f"{origin}/vault/{vault_id}"

        def do_request() -> str:
            response = self.session.get(url, timeout=self.opts.timeout, headers={"Referer": REFERER})
            self._check_transient(response)
            if response.status_code == 404:
                raise VimmError(f"vault ID {vault_id} does not exist (HTTP 404)")
            if response.status_code == 403:
                raise VimmError("HTTP 403 on the vault page - the site refused the request")
            response.raise_for_status()
            return response.text

        page_html = self._with_retries(f"vault/{vault_id}", do_request)
        return parse_vault_page(vault_id, page_html)

    # -- download -----------------------------------------------------------

    def download(self, page: VaultPage, media: Media, alt: int, dest_dir: Path) -> Result:
        params = {"mediaId": str(media.media_id)}
        if alt:
            params["alt"] = str(alt)

        expected = media.size(alt)
        stem = safe_filename(Path(media.filename).stem, f"media-{media.media_id}")
        planned_name = stem + ".zip"

        # What is already on disk wins over the planned name. The server
        # names the file itself (the big disc systems arrive as .7z, not the
        # .zip assumed here), so a run that starts fresh must look for the
        # real name or it would ignore a partial file and download it all
        # again - which is what pausing and restarting used to do.
        existing_final = find_download(dest_dir, stem, partial=False)
        final_path = existing_final or (dest_dir / planned_name)

        if final_path.exists() and not self.opts.overwrite:
            size = final_path.stat().st_size
            return Result(
                page.vault_id, media.media_id, final_path.name, "skipped", size,
                "already present (enable overwrite in Settings to replace)",
            )

        existing_part = find_download(dest_dir, stem, partial=True)
        part_path = existing_part or final_path.with_suffix(
            final_path.suffix + ".part")
        if existing_part is not None:
            # Keep final_path consistent with the partial file's real name.
            final_path = part_path.with_suffix("")
        resumes = 0
        self._cancels = 0  # cancels are budgeted per file

        def part_size() -> int:
            """Bytes safely on disk. This is what makes a retry worth having."""
            try:
                return part_path.stat().st_size
            except OSError:
                return 0

        def finalize(meter: _Meter | None, written: int) -> Result:
            os.replace(part_path, final_path)
            note = self._verify(final_path, media) if self.opts.verify else ""
            tail = f" after {resumes} resume{'s' if resumes != 1 else ''}" if resumes else ""
            summary = f"done  {final_path.name}  {human_bytes(written)}"
            if meter is not None:
                summary += f" in {human_duration(time.monotonic() - meter.start)}"
            summary += f"{note}{tail}"
            self.listener.progress_done(summary)
            return Result(
                page.vault_id, media.media_id, final_path.name, "ok", written,
                (note.strip(" |") or "downloaded") + tail,
            )

        def do_request() -> Result:
            nonlocal final_path, part_path, resumes

            # Recomputed every attempt, from the bytes actually on disk. An
            # interrupted transfer picks up where it stopped instead of
            # starting over - the whole point of retrying at all.
            resume_from = part_size() if self.opts.resume else 0
            path_before = part_path

            headers = {"Referer": REFERER, "Accept": "*/*"}
            if resume_from:
                headers["Range"] = f"bytes={resume_from}-"

            with self.session.get(
                page.download_host,
                params=params,
                headers=headers,
                stream=True,
                # Separate read timeout: a connection that goes silent (a
                # half-open socket after losing the slot) is dropped and
                # resumed rather than hanging until the run is abandoned.
                timeout=(15, self.opts.stall_timeout),
            ) as response:
                # The per-IP limit arrives as 429 with a page offering to
                # release the slot, so it is handled before the generic
                # transient check gets a chance to turn it into a plain wait.
                if response.status_code == 429:
                    raise self._handle_busy(response)
                self._check_transient(response)

                if response.status_code == 416:
                    # Range past the end. Usually means the .part is already
                    # the complete file, so keep it instead of starting over.
                    have = part_size()
                    if have and (not expected or have >= expected * 0.98):
                        return finalize(None, have)
                    part_path.unlink(missing_ok=True)
                    raise RetryableError("partial file was unusable, restarting", retry_after=0)

                if response.status_code in (401, 403):
                    raise VimmError(
                        f"HTTP {response.status_code} - this file needs a signed-in "
                        "session (add a cookies file in Settings)"
                    )
                if response.status_code == 400:
                    raise VimmError("HTTP 400 - the server rejected the download request")
                response.raise_for_status()

                if "text/html" in response.headers.get("Content-Type", ""):
                    problem = self._classify_html(response.text)
                    # A busy notice served as a plain 200 gets the same
                    # treatment as the 429 form of it.
                    raise self._handle_busy(response) if isinstance(problem, BusyError) else problem

                server_name = filename_from_disposition(response.headers.get("Content-Disposition"))
                if server_name:
                    new_name = safe_filename(server_name, planned_name)
                    if new_name != final_path.name:
                        final_path = dest_dir / new_name
                        part_path = final_path.with_suffix(final_path.suffix + ".part")
                        if final_path.exists() and not self.opts.overwrite:
                            return Result(
                                page.vault_id, media.media_id, final_path.name, "skipped",
                                final_path.stat().st_size,
                                "already present (enable overwrite in Settings to replace)",
                            )

                if part_path != path_before and resume_from:
                    # We asked to resume one file and the server named another.
                    # Redo the attempt against the right path so no bytes land
                    # at the wrong offset.
                    raise RetryableError("filename changed, restarting attempt", retry_after=0)

                appending = resume_from > 0 and response.status_code == 206
                if resume_from and not appending:
                    # Server ignored our Range header; start over.
                    resume_from = 0

                length_header = response.headers.get("Content-Length")
                body_bytes = int(length_header) if length_header and length_header.isdigit() else 0
                total = (resume_from + body_bytes) if body_bytes else expected

                dest_dir.mkdir(parents=True, exist_ok=True)
                meter = _Meter(self.listener, page.vault_id, final_path.name,
                               total, resume_from)

                mode = "ab" if appending else "wb"
                if appending:
                    resumes += 1
                    self.listener.status(f"resuming at {human_bytes(resume_from)}")

                # The `with` closes and flushes on the way out even when the
                # connection dies mid-iteration, so whatever arrived is on
                # disk before the error is re-raised for the retry loop.
                try:
                    with open(part_path, mode) as handle:
                        # 64 KB rather than something larger: a chunk still in
                        # flight when the connection dies is lost, so this
                        # caps what an interruption can cost us.
                        for chunk in response.iter_content(chunk_size=64 * 1024):
                            if self.cancel_event is not None and self.cancel_event.is_set():
                                # Keep the .part - a later run resumes from it.
                                self.listener.progress_done(
                                    f"{final_path.name}: cancelled at "
                                    f"{human_bytes(part_size())}"
                                )
                                raise Cancelled()
                            if chunk:
                                handle.write(chunk)
                                meter.advance(len(chunk))
                except (requests.RequestException, OSError, EOFError) as exc:
                    kept = part_size()
                    self.listener.progress_done(
                        f"{final_path.name}: interrupted at {human_bytes(kept)}"
                    )
                    raise RetryableError(f"connection lost: {exc}") from exc

                written = part_size()
                if total and written < total:
                    self.listener.progress_done(
                        f"{final_path.name}: truncated at {human_bytes(written)}"
                    )
                    raise RetryableError(
                        f"incomplete transfer ({human_bytes(written)} of {human_bytes(total)})"
                    )

                return finalize(meter, written)

        return self._with_retries(
            f"download {media.media_id}", do_request, progress_probe=part_size
        )

    def _verify(self, path: Path, media: Media) -> str:
        """Compare the CRC32 recorded in the zip index against the vault's hash.

        Reads only the zip central directory, so it costs nothing meaningful
        and never extracts anything.
        """
        if not media.crc32 or not zipfile.is_zipfile(path):
            return ""
        try:
            with zipfile.ZipFile(path) as archive:
                entries = [i for i in archive.infolist() if not i.is_dir()]
                if not entries:
                    return "  | empty archive"
                actual = {f"{i.CRC:08X}" for i in entries}
        except zipfile.BadZipFile:
            return "  | WARNING: corrupt zip"
        if media.crc32.upper().zfill(8) in actual:
            return "  | CRC ok"
        return f"  | WARNING: CRC mismatch (expected {media.crc32.upper()})"


# --------------------------------------------------------------------------
# Version / disc selection
# --------------------------------------------------------------------------


def _version_key(version: str) -> tuple:
    parts = re.findall(r"\d+", version)
    return tuple(int(p) for p in parts) if parts else (0,)


def _preference_rank(media: Media, prefer: list[str]) -> int:
    """Lower is better. Matches `prefer` tokens against region tags/filename."""
    if not prefer:
        return 0
    haystack = media.filename.lower()
    for index, token in enumerate(prefer):
        if token.lower() in haystack:
            return index
    return len(prefer)


def choose_format(media: Media, requested: str | None) -> int:
    """Resolve the `format` option to an `alt` index for this entry."""
    if requested is None:
        return 0
    if requested.isdigit():
        alt = int(requested)
        return alt if media.size(alt) > 0 else 0
    for alt, name in enumerate(media.formats):
        if name.lower() == requested.lower() and media.size(alt) > 0:
            return alt
    for alt, name in enumerate(media.formats):
        if requested.lower() in name.lower() and media.size(alt) > 0:
            return alt
    return 0


def select_media(
    page: VaultPage,
    opts: argparse.Namespace,
    listener: Listener | None = None,
) -> list[tuple[Media, int]]:
    """Pick which entries of a vault page to download."""
    listener = listener or Listener()

    # A per-game override (the web app's disc picker) wins over the global
    # `discs` option; an empty list there means "this game was deselected".
    wanted_discs: set[int] | None = None
    overrides = getattr(opts, "disc_overrides", None) or {}
    override = overrides.get(page.vault_id, overrides.get(str(page.vault_id)))
    if override is not None:
        wanted_discs = {int(d) for d in override}
    elif opts.discs and opts.discs.lower() != "all":
        wanted_discs = {int(d) for d in re.findall(r"\d+", opts.discs)}

    by_disc: dict[int, list[Media]] = {}
    for media in page.media:
        by_disc.setdefault(media.disc, []).append(media)

    alt_of = lambda m: choose_format(m, opts.format)  # noqa: E731
    selected: list[tuple[Media, int]] = []

    for disc in sorted(by_disc):
        if wanted_discs is not None and disc not in wanted_discs:
            continue
        candidates = [m for m in by_disc[disc] if m.size(alt_of(m)) > 0]
        if not candidates:
            continue

        if opts.all_versions:
            chosen = candidates
        elif len(candidates) == 1:
            chosen = candidates
        elif opts.pick:
            chosen = listener.choose_versions(page, candidates, alt_of)
        else:
            reverse = opts.version_policy == "latest"
            chosen = [
                min(
                    candidates,
                    key=lambda m: (
                        _preference_rank(m, opts.prefer),
                        [-v for v in _version_key(m.version)] if reverse else list(_version_key(m.version)),
                    ),
                )
            ]

        for media in chosen:
            selected.append((media, alt_of(media)))

    return selected


# --------------------------------------------------------------------------
# Options
# --------------------------------------------------------------------------


DEFAULTS = {
    "out": "downloads",
    "delay": 5.0,
    "jitter": 3.0,
    "retries": 6,
    "backoff": 5.0,
    "timeout": 60,
    "stall_timeout": 120,
    "max_attempts": 100,
    "busy_wait": 45.0,
    "busy_wait_max": 300.0,
    "busy_retries": 20,
    "cancel_busy": True,
    "cancel_wait": 3.0,
    "max_cancels": 10,
    "sweeps": 10,
    "prefer": [],
    "format": None,
    "discs": "all",
    "version_policy": "latest",
    "cookies": None,
    "organize": False,
    "overwrite": False,
    "resume": True,
    "verify": True,
    "log": "download_log.csv",
    "folders": {},
    "site_base": None,  # test override: aim at a local server, never live
    # vault_id -> [disc numbers]. Set per game by the web app's disc picker;
    # takes precedence over the global `discs` option for those games.
    "disc_overrides": {},  # vault_id -> [disc numbers], from the disc picker
}


# Run-shape flags that are not download settings but are read by run code.
_RUN_FLAGS = {"all_versions": False, "pick": False, "list": False}


def make_options(**overrides) -> argparse.Namespace:
    """A complete options namespace from DEFAULTS plus overrides.

    The web server and the tests both build their options here.
    """
    settings = dict(DEFAULTS)
    settings.update(_RUN_FLAGS)
    unknown = set(overrides) - set(settings)
    if unknown:
        raise ValueError(f"unknown option(s): {', '.join(sorted(unknown))}")
    settings.update(overrides)
    if isinstance(settings["prefer"], str):
        settings["prefer"] = [t.strip() for t in settings["prefer"].split(",") if t.strip()]
    if settings["format"] is not None:
        settings["format"] = str(settings["format"])
    settings["discs"] = str(settings["discs"])
    return argparse.Namespace(**settings)


# --------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------


def write_log(path: Path, results: list[Result]) -> None:
    if not results:
        return
    new_file = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if new_file:
            writer.writerow(["timestamp", "vault_id", "media_id", "filename",
                             "status", "bytes", "message"])
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for r in results:
            writer.writerow([stamp, r.vault_id, r.media_id, r.filename,
                             r.status, r.bytes, r.message])


def system_folder(title: str, overrides: dict | None = None, warn=None) -> str:
    """Folder name for a vault page title, e.g. "Phantasy Star (SMS)" -> mastersystem.

    An unrecognised system is not fatal: it gets a lowercased folder of its
    own and a one-time note, so a system Vimm adds later still lands
    somewhere sensible.
    """
    tag = _SYSTEM_TAG.search(title)
    if not tag:
        return "misc"
    code = tag.group(1).strip()

    table = dict(SYSTEM_FOLDERS)
    if overrides:
        table.update({str(k).strip().lower(): str(v) for k, v in overrides.items()})

    folder = table.get(code.lower())
    if folder is None:
        if code not in _unknown_systems and warn:
            _unknown_systems.add(code)
            warn(f"unmapped system {code!r} - using folder {code.lower()!r}")
        folder = code.lower().replace(" ", "")
    return safe_filename(folder, "misc")


def destination_for(
    page: VaultPage, opts: argparse.Namespace, listener: Listener | None = None
) -> Path:
    root = Path(opts.out)
    if not opts.organize:
        return root
    warn = listener.warn if listener else None
    return root / system_folder(page.title, getattr(opts, "folders", None), warn=warn)


def run_pass(
    vault_ids: list[int],
    opts: argparse.Namespace,
    client: VimmClient,
    label: str = "",
) -> list[Result]:
    """One pass over the ID list. Safe to repeat: finished files are skipped
    and partial ones resume, so a second pass only does outstanding work."""
    listener = client.listener
    results: list[Result] = []
    downloads_done = 0

    for index, vault_id in enumerate(vault_ids, 1):
        client._check_cancelled()
        listener.item_started(index, len(vault_ids), vault_id, label)
        try:
            page = client.fetch_vault(vault_id)
        except VimmError as exc:
            listener.status(f"FAIL  {exc}")
            result = Result(vault_id, "?", "", "failed", 0, str(exc))
            results.append(result)
            listener.item_done(result)
            continue

        listener.page_resolved(page)
        selections = select_media(page, opts, listener)
        if not selections:
            listener.status("skip  nothing matched the current selection options")
            result = Result(vault_id, "?", "", "skipped", 0, "no matching version")
            results.append(result)
            listener.item_done(result)
            continue

        dest_dir = destination_for(page, opts, listener)
        for disc_index, (media, alt) in enumerate(selections):
            if opts.list:
                listener.would_get(media, alt, dest_dir)
                result = Result(vault_id, media.media_id, media.filename,
                                "listed", media.size(alt), "dry run")
                results.append(result)
                listener.item_done(result)
                continue

            if downloads_done:
                delay = opts.delay + random.uniform(0, opts.jitter)
                # Distinguish the pause between discs of one game from the
                # pause between games, so a front-end can say which is
                # happening instead of looking stalled.
                reason = (NEXT_DISC_WAIT if disc_index > 0
                          else "being polite between downloads")
                client._sleep(delay, reason)

            try:
                result = client.download(page, media, alt, dest_dir)
            except VimmError as exc:
                listener.status(f"FAIL  {exc}")
                stem = safe_filename(Path(media.filename).stem, "x")
                part = find_download(dest_dir, stem, partial=True)
                kept = part.stat().st_size if part else 0
                if kept:
                    listener.status(f"      {human_bytes(kept)} kept for the next attempt")
                result = Result(vault_id, media.media_id, media.filename, "failed",
                                0, str(exc), partial=kept)
            results.append(result)
            listener.item_done(result)
            downloads_done += 1

    return results


def run_http(
    vault_ids: list[int],
    opts: argparse.Namespace,
    listener: Listener | None = None,
    cancel_event: threading.Event | None = None,
) -> list[Result]:
    listener = listener or Listener()
    client = VimmClient(opts, listener=listener, cancel_event=cancel_event)

    if opts.cookies:
        cookie_path = Path(opts.cookies)
        if not cookie_path.is_file():
            raise VimmError(f"Cookie file not found: {cookie_path}")
        listener.status(f"cookies: {load_cookies(client.session, cookie_path)}")

    results = run_pass(vault_ids, opts, client)
    if opts.list or opts.sweeps <= 0:
        return results

    # Sweep: retry whatever is unfinished. By now the other users sharing our
    # IP have likely finished, and resume means a sweep continues mid-file
    # rather than starting over. Stop as soon as a whole pass adds nothing.
    final: dict[int, list[Result]] = {}
    for r in results:
        final.setdefault(r.vault_id, []).append(r)

    for sweep in range(1, opts.sweeps + 1):
        pending = sorted(
            vault_id for vault_id, rs in final.items()
            if any(r.status == "failed" for r in rs)
        )
        if not pending:
            break

        before = sum(r.partial for rs in final.values() for r in rs)
        listener.sweep_started(sweep, len(pending))

        swept = run_pass(pending, opts, client, label=f"sweep {sweep} ")
        for r in swept:
            final[r.vault_id] = [x for x in final[r.vault_id] if x.status != "failed"]
            final[r.vault_id].append(r)

        after = sum(r.partial for rs in final.values() for r in rs)
        recovered = sum(1 for r in swept if r.status == "ok")
        still_failed = sum(1 for r in swept if r.status == "failed")

        stopped = not recovered and after <= before
        listener.sweep_finished(sweep, recovered, still_failed, stopped)
        if stopped:
            break

    return [r for rs in final.values() for r in rs]
