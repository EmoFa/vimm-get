# VimmGet

A downloader for [Vimm's Lair](https://vimm.net) with a local web interface: search the
vault, queue games, watch downloads with live progress and ETA, then extract archives,
compress disc images to CHD, and build m3u playlists — all from your browser, all
running locally in Python.

## Quick start

Python 3.11+ on Windows, macOS, or Linux:

```bash
pip install -r requirements.txt
python vimmget.py
```

Your browser opens `http://127.0.0.1:8317`. That page is served by the Python process
on your machine — nothing runs anywhere else, and only the Python side talks to
vimm.net.

## Using it

**Add games** — paste vault IDs or URLs into the top box (one per line), or type a
title and press Enter to **search**. Results are ordered by your preferred regions and
add to the queue in one click.

**Search filtering** — Vimm tags entries (Demo, Prototype, Unlicensed, Xbox Live
Arcade, …). Demos, prototypes, and entries the site marks as having no file are hidden
by default; a `N hidden — SHOW` toggle reveals them, and Settings lets you choose
exactly which tags to hide. Tags are read from the site's own markup, so a game called
_Demon's Crest_ is never mistaken for a demo.

**Multi-disc games** — a game with more than one disc shows a checkbox per disc, all
ticked by default. Untick any you don't want before starting.

**Start / Pause / Stop** — one download at a time, in queue order, with live speed and
ETA. Pause and Stop are always safe: partial files are kept and **resume from the exact
byte** when you press Start again.

**History** — one card per game (a two-disc game is a single entry listing both discs),
with its processing stages:

The three stages run in order, and each button only appears once the previous stage is
done and the system actually needs it:

- **EXTRACT** — unpacks `.zip`/`.7z`, then deletes the archive and the site's info
  `.txt`. Vimm wraps each disc in its own folder; extraction lifts the contents into
  the system folder and removes the wrapper, so a multi-disc game's discs end up side
  by side.
- **COMPRESS** — only for systems whose games arrive as split disc images
  (`psx, saturn, segacd, tgcd, dreamcast, cdimono1`), turning `bin + cue/gdi` into a
  single `.chd`. You can add systems to the whitelist in the settings.
- **M3U** — appears only after compression, and only for multi-disc games on
  m3u-capable systems. It creates a folder whose name ends in `.m3u`, moves the discs'
  `.chd` files into it, and writes a playlist listing each one on its own line:

  ```
  psx/
    Final Fantasy VII (USA).m3u/
      Final Fantasy VII (USA).m3u
      Final Fantasy VII (USA) (Disc 1).chd
      Final Fantasy VII (USA) (Disc 2).chd
      Final Fantasy VII (USA) (Disc 3).chd
  ```

- **CONVERT ALL** runs the outstanding stage for everything in history.

**Everything is manual by default.** Settings has toggles to automate extract →
compress → m3u after each download.

### CHD compression and chdman

CHD conversion uses **chdman**, the MAME project's tool. The first time you compress on
Windows, VimmGet downloads the **official MAME release** from the MAME project's
GitHub, verifies its published SHA-256, extracts _only_ `chdman.exe` into `tools/`, and
discards the rest (one-time ~90 MB). On macOS/Linux install it with
`brew install rom-tools` or `apt/dnf install mame-tools`; it is found on PATH.

## How it behaves toward the site

- **It scrapes because it must**: the vault ID is not the download ID
  (`vault/1234` serves `mediaId=1208`), the download host changes, and downloads
  require a `Referer: https://vimm.net/` header — without it the server answers 400.
- **One download at a time**, a 5–8 s pause between items, `Retry-After` honoured,
  exponential backoff. No CAPTCHA solving, no proxy rotation, no connection-splitting.
- **Interruptions cost one 64 KB chunk, not the file.** Every retry resumes from the
  byte offset on disk, and the retry budget only counts attempts that gained nothing.
- **The per-IP download slot**: Vimm allows one download per IP. On a shared IP (VPN),
  someone else can take the slot; the site then offers a cancel link, and VimmGet
  follows it and retries within seconds rather than waiting out a stranger's multi-GB
  download. The site's own warning applies: that stranger can cancel yours right back.
  Turn this off in Settings to wait politely instead.
- **Verification**: each finished zip's internal CRC32 is checked against the checksum
  the vault page publishes (`CRC ok` in the log).
- **Sweeps**: after the main pass, unfinished items are retried until a pass stops
  helping.

## Folder names

With per-system sorting on, files land in folders matching EmulationStation/Batocera
conventions — `snes`, `psx`, `gc`, `mastersystem`, `atarijaguarcd`, … — verified
against every system the site lists. WiiWare lands in `wii` and Xbox 360 digital in
`xbox360` (same hardware).

## Signing in (optional)

Some files need an account. VimmGet never sees your password — export your browser
session with any "Get cookies.txt" extension while signed in to vimm.net and point
Settings at the file. Treat that file like a password (it is gitignored for exactly
that reason).

## Layout

```
vimm/engine.py     the download engine - all site knowledge lives here
vimm/search.py     vault search, tag parsing, honeypot filtering
vimm/extract.py    archive extraction + cleanup
vimm/chd.py        chdman locate / verify-download / run
vimm/m3u.py        multi-disc playlist layout
vimm/pipeline.py   post-processing job runner
vimm/server.py     FastAPI: REST + WebSocket + static frontend
web/               the interface - plain HTML/CSS/JS, no build step
vimmget.py             start the web app
```

No Node, no bundler, no build: the frontend is served as-is, so editing `web/` and
refreshing the browser is the whole development loop.

## Note

This tool automates downloads you could perform by hand. Whether you're entitled to a
given file, and how much you pull down, is on you — keep the load light, and consider
supporting the site if you use it a lot.

## License

[MIT](LICENSE).
