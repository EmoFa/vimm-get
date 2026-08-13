"""
Search and browse scraping for Vimm's Lair.

Result pages (`/vault/?p=list&q=...` and `/vault/?p=list&system=...&section=...`)
share one table shape: System | Title | Region flags | Version | Languages.

Every row also carries a hidden honeypot anchor (`/vault/999999`, styled
`display:none` with deliberately irregular whitespace) placed before the real
link, to trip naive scrapers. The parser here takes only visible anchors and
rejects the decoy ID outright.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

import requests

from .engine import BASE, BROWSER_HEADERS, REFERER, SYSTEM_FOLDERS, VimmError

LIST_URL = BASE + "/vault/"

# The decoy vault ID used by the site's hidden trap links.
HONEYPOT_ID = 999999

# Sections accepted by the per-system listing pages.
SECTIONS = ["number"] + [chr(c) for c in range(ord("A"), ord("Z") + 1)]

# Every browsable system, in the site's own order (verified live).
SYSTEMS = [
    "Atari2600", "Atari5200", "Atari7800", "Jaguar", "JaguarCD", "Lynx",
    "NES", "SNES", "N64", "GameCube", "Wii", "WiiWare", "WiiU",
    "GB", "GBC", "GBA", "DS", "3DS", "VB",
    "SMS", "Genesis", "SegaCD", "32X", "Saturn", "Dreamcast", "GG",
    "PS1", "PS2", "PS3", "PSP",
    "Xbox", "Xbox360", "X360-D",
    "TG16", "TGCD", "CDi",
]

_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)
_ANCHOR = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.S | re.I)
_HREF_ID = re.compile(r"/vault/(\d+)")
_STYLE = re.compile(r'style\s*=\s*["\']([^"\']*)["\']', re.I)
_HIDDEN = re.compile(r"display\s*:\s*none", re.I)
_FLAG = re.compile(r'<img[^>]*\btitle\s*=\s*["\']([^"\']+)["\']', re.I)

# Vimm marks entries with a badge next to the title, e.g.
#   <b class="redBorder" title="Demo">D</b>
# The title attribute is the real tag name; the element text is a short code.
# Keying off this is exact - far safer than matching words in the title,
# where "Demo" would also catch "Demon Sword" and "Demolition Man".
_BADGE = re.compile(
    r'<(?:b|span)[^>]*class="[^"]*redBorder[^"]*"[^>]*title="([^"]+)"[^>]*>',
    re.I)

# The vocabulary observed on the live site, most common first.
KNOWN_TAGS = [
    "Demo",
    "Xbox Live Indie Games",
    "Unlicensed",
    "Xbox Live Arcade",
    "Prototype",
    "Download unavailable",
    "Bonus disc",
]

# Hidden by default: the two Nolan asked for, plus entries the site says have
# no file behind them (queueing one could only fail).
DEFAULT_HIDDEN_TAGS = ["Demo", "Prototype", "Download unavailable"]

# The tag that means "there is nothing to download here".
UNAVAILABLE_TAG = "Download unavailable"


def normalise_tag(tag: str) -> str:
    """Comparable form of a tag name.

    The unavailable tag reads 'Download unavailable - Please upload it! ⚠' on
    the site, so compare on the part before any dash and ignore case.
    """
    return html.unescape(tag).split(" - ")[0].strip().lower()


@dataclass
class SearchHit:
    vault_id: int
    title: str
    system: str
    regions: list[str] = field(default_factory=list)
    version: str = ""
    languages: str = ""
    tags: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        region = "/".join(self.regions) if self.regions else "?"
        return f"{self.title} ({self.system}) [{region}]"

    @property
    def downloadable(self) -> bool:
        """False when the site marks the entry as having no file yet."""
        return not any(normalise_tag(t) == normalise_tag(UNAVAILABLE_TAG)
                       for t in self.tags)

    def as_dict(self) -> dict:
        return {
            "vault_id": self.vault_id,
            "title": self.title,
            "system": self.system,
            "regions": self.regions,
            "version": self.version,
            "languages": self.languages,
            "tags": self.tags,
            "downloadable": self.downloadable,
        }


def _clean(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    return html.unescape(re.sub(r"\s+", " ", text)).strip()


def _real_anchor(cell_html: str) -> tuple[int, str] | None:
    """The visible vault link in a title cell, skipping the honeypot."""
    for attrs, inner in _ANCHOR.findall(cell_html):
        match = _HREF_ID.search(attrs)
        if not match:
            continue
        vault_id = int(match.group(1))
        if vault_id == HONEYPOT_ID:
            continue
        style = _STYLE.search(attrs)
        if style and _HIDDEN.search(style.group(1)):
            continue
        return vault_id, _clean(inner)
    return None


def parse_results(page_html: str, default_system: str = "") -> list[SearchHit]:
    hits: list[SearchHit] = []
    for row_html in _ROW.findall(page_html):
        cells = _CELL.findall(row_html)
        if len(cells) < 2:
            continue

        # The title cell is the one holding the vault link. With a system
        # column (cross-system search) it is cell 1; without, cell 0.
        anchor = _real_anchor(cells[1])
        title_index = 1
        if anchor is None:
            anchor = _real_anchor(cells[0])
            title_index = 0
        if anchor is None:
            continue
        vault_id, title = anchor

        # Badges live in the same cell as the title link.
        tags = [html.unescape(t).strip()
                for t in _BADGE.findall(cells[title_index])]

        system = _clean(cells[0]) if title_index == 1 else default_system
        regions: list[str] = []
        version = ""
        languages = ""
        rest = cells[title_index + 1 :]
        if rest:
            regions = [html.unescape(t) for t in _FLAG.findall(rest[0])]
            if not regions and _clean(rest[0]):
                regions = [_clean(rest[0])]
        if len(rest) > 1:
            version = _clean(rest[1])
        if len(rest) > 2:
            languages = _clean(rest[2])

        hits.append(SearchHit(vault_id, title, system or default_system,
                              regions, version, languages, tags))
    return hits


def filter_by_tags(hits: list[SearchHit],
                   hidden: list[str]) -> tuple[list[SearchHit], list[SearchHit]]:
    """Split hits into (kept, hidden) by tag. Case- and suffix-tolerant."""
    wanted = {normalise_tag(t) for t in hidden}
    if not wanted:
        return list(hits), []
    kept, dropped = [], []
    for hit in hits:
        if any(normalise_tag(t) in wanted for t in hit.tags):
            dropped.append(hit)
        else:
            kept.append(hit)
    return kept, dropped


def tag_vocabulary(hits: list[SearchHit]) -> list[str]:
    """Known tags plus anything new the site has started using."""
    seen = list(KNOWN_TAGS)
    known = {normalise_tag(t) for t in seen}
    for hit in hits:
        for tag in hit.tags:
            if normalise_tag(tag) not in known:
                known.add(normalise_tag(tag))
                seen.append(tag)
    return seen


def region_rank(hit: SearchHit, prefer: list[str]) -> int:
    """Lower is better; used to sort search results by region preference."""
    if not prefer:
        return 0
    haystack = " ".join(hit.regions).lower()
    for index, token in enumerate(prefer):
        if token.lower() in haystack:
            return index
    return len(prefer)


def sort_by_preference(hits: list[SearchHit], prefer: list[str]) -> list[SearchHit]:
    """Stable sort: preferred regions first, original site order otherwise."""
    return sorted(hits, key=lambda h: region_rank(h, prefer))


def _fetch(session: requests.Session, params: dict, timeout: int) -> str:
    response = session.get(
        LIST_URL, params=params, headers={"Referer": REFERER}, timeout=timeout
    )
    if response.status_code == 429:
        raise VimmError("the site is asking us to slow down - try again shortly")
    response.raise_for_status()
    return response.text


def search(session: requests.Session, query: str, timeout: int = 30) -> list[SearchHit]:
    """Cross-system title search. The site requires 3+ characters."""
    query = query.strip()
    if len(query) < 3:
        raise VimmError("search needs at least 3 characters")
    page_html = _fetch(session, {"p": "list", "q": query}, timeout)
    return parse_results(page_html)


def browse(
    session: requests.Session, system: str, section: str, timeout: int = 30
) -> list[SearchHit]:
    """One letter (or 'number') of a system's alphabetical listing."""
    if system not in SYSTEMS and system.lower() not in SYSTEM_FOLDERS:
        raise VimmError(f"unknown system {system!r}")
    if section not in SECTIONS:
        raise VimmError(f"section must be one of {', '.join(SECTIONS)}")
    page_html = _fetch(
        session, {"p": "list", "system": system, "section": section}, timeout
    )
    return parse_results(page_html, default_system=system)


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)
    return session
