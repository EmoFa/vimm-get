#!/usr/bin/env python3
"""Run every test suite and report a summary.

    python tests/run_all.py            # everything that runs offline
    python tests/run_all.py -v         # stream each suite's own output
    python tests/run_all.py test_search test_sweeps    # just these

Each suite is a standalone script that exits non-zero on failure, so it can
also be run on its own:

    python tests/test_download_resilience.py

Most suites are fully offline - they spin up a local HTTP server told to
misbehave in the specific ways Vimm's Lair does (cutting a transfer mid-
stream, answering 429 with the busy page, going silent), rather than
touching the real site.

Two things are opt-in, because a first run should be neither slow nor
surprising:

  * suites needing `chdman` skip themselves if it is not installed
  * the live search checks need VIMMGET_LIVE_TESTS=1
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO = TESTS_DIR.parent


def suites(selected: list[str]) -> list[Path]:
    found = sorted(p for p in TESTS_DIR.glob("test_*.py"))
    if not selected:
        return found
    wanted = {s.removesuffix(".py") for s in selected}
    picked = [p for p in found if p.stem in wanted]
    missing = wanted - {p.stem for p in picked}
    if missing:
        raise SystemExit(f"no such suite: {', '.join(sorted(missing))}")
    return picked


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("suites", nargs="*", help="run only these (bare names ok)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="stream each suite's output instead of summarising")
    args = parser.parse_args()

    chosen = suites(args.suites)
    print(f"running {len(chosen)} suite(s) from {TESTS_DIR}\n")

    passed, failed, skipped = [], [], []
    started = time.monotonic()

    for path in chosen:
        label = path.stem
        print(f"  {label:<34} ", end="", flush=True)
        began = time.monotonic()
        result = subprocess.run(
            [sys.executable, "-u", str(path)],
            cwd=REPO,
            capture_output=not args.verbose,
            text=True,
        )
        took = time.monotonic() - began
        output = "" if args.verbose else (result.stdout or "") + (result.stderr or "")

        if result.returncode == 0 and "SKIPPED:" in output:
            skipped.append((label, output))
            reason = next((l for l in output.splitlines() if "SKIPPED:" in l), "")
            print(f"skip  {took:5.1f}s  {reason.split('SKIPPED:')[-1].strip()[:44]}")
        elif result.returncode == 0:
            passed.append(label)
            print(f"ok    {took:5.1f}s")
        else:
            failed.append((label, output))
            print(f"FAIL  {took:5.1f}s")

    print()
    for label, output in failed:
        print("=" * 70)
        print(f"{label} failed:")
        # The suites print their own PASS/FAIL lines; surface the useful ones.
        lines = [l for l in output.splitlines()
                 if "FAIL" in l or "Error" in l or "Traceback" in l]
        print("\n".join(lines[-25:]) or output[-2000:])
        print()

    total = time.monotonic() - started
    summary = f"{len(passed)} passed"
    if skipped:
        summary += f", {len(skipped)} skipped"
    if failed:
        summary += f", {len(failed)} FAILED"
    print(f"{summary}  in {total:.0f}s")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
