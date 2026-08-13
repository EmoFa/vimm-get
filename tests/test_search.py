"""Search parser: honeypot rejection on a fixture, plus one live spot-check
of each page shape."""
import os
import sys
import time

from pathlib import Path as _RepoPath
sys.path.insert(0, str(_RepoPath(__file__).resolve().parents[1]))
from vimm import search as vs

# Fixture built from the real page structure captured earlier, including the
# honeypot's deliberately irregular whitespace.
FIXTURE = """
<table><caption><table><tr><th>System</th><th>Title</th><th>Region</th>
<th>Version</th><th>Languages</th></tr></table></caption>
<tr><td style="width:80px; text-align:center">DS</td>
<td style="width:auto"><a href="/vault/999999" style="display:  none">9</a><a href= "/vault/30152" onmouseover="buildTooltip(this, 30152, 256, 384)">Chrono Trigger</a></td>
<td><div><img src="/images/flags/europe.png" class="flag" title="Europe"></div></td>
<td>1.0</td><td>en fr</td></tr>
<tr><td style="text-align:center">SNES</td>
<td><a style="DISPLAY:NONE" href="/vault/999999">9</a><a href="/vault/2140">Chrono Trigger &nbsp;</a></td>
<td><img title="USA" src="/images/flags/us.png"></td>
<td>1.0</td><td>-</td></tr>
</table>
"""

hits = vs.parse_results(FIXTURE)
print("fixture hits:")
for h in hits:
    print(f"  {h.vault_id}  {h.label}  v{h.version}  {h.languages}")
assert len(hits) == 2, f"expected 2 hits, got {len(hits)}"
assert [h.vault_id for h in hits] == [30152, 2140], "wrong IDs - honeypot leaked?"
assert all(h.vault_id != vs.HONEYPOT_ID for h in hits)
assert hits[0].system == "DS" and hits[0].regions == ["Europe"]
assert hits[1].title == "Chrono Trigger"
print("fixture PASS\n")

# --- live spot checks ---------------------------------------------------------
# Opt-in: these contact vimm.net. Enable with VIMMGET_LIVE_TESTS=1.
if os.environ.get("VIMMGET_LIVE_TESTS") != "1":
    print("live checks skipped (set VIMMGET_LIVE_TESTS=1 to run them)")
    print("\nAll search cases passed.")
    raise SystemExit(0)

s = vs.make_session()

live = vs.search(s, "chrono trigger")
print(f"live search 'chrono trigger': {len(live)} hits")
for h in live[:8]:
    print(f"  {h.vault_id:6d}  {h.label}")
assert live, "no live results"
assert all(h.vault_id != vs.HONEYPOT_ID for h in live), "honeypot in live results"
systems = {h.system for h in live}
assert {"SNES", "PS1", "DS"} & systems, f"expected SNES/PS1/DS, got {systems}"
time.sleep(2)

listing = vs.browse(s, "SNES", "C")
print(f"\nlive browse SNES/C: {len(listing)} entries")
for h in listing[:5]:
    print(f"  {h.vault_id:6d}  {h.label}")
assert len(listing) > 30, "SNES section C should have many entries"
assert all(h.vault_id != vs.HONEYPOT_ID for h in listing)
assert any("Chrono Trigger" in h.title for h in listing), "Chrono Trigger missing from SNES/C"
assert all(h.system == "SNES" for h in listing)

# guard rails
try:
    vs.search(s, "ab")
    raise AssertionError("short query should raise")
except vs.VimmError:
    pass
try:
    vs.browse(s, "Amiga", "A")
    raise AssertionError("unknown system should raise")
except vs.VimmError:
    pass

print("\nAll search cases passed.")
