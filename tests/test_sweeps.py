"""Sweep behaviour: unfinished items get retried, and sweeping stops when
a whole pass stops helping."""
import argparse
import sys
import tempfile
from pathlib import Path

from pathlib import Path as _RepoPath
sys.path.insert(0, str(_RepoPath(__file__).resolve().parents[1]))
import vimm.engine as vd


def opts(**over):
    o = dict(vd.DEFAULTS)
    o.update(dict(list=False, quiet=True, delay=0, jitter=0, out=tempfile.mkdtemp(prefix="vimmsweep_"),
                  all_versions=False, pick=False, browser=False))
    o.update(over)
    return argparse.Namespace(**o)


def make_page(vault_id):
    m = vd.Media(vault_id, "1.0", 1, f"Game {vault_id}.rom", [1000, 0, 0],
                 ["1 KB", "0", "0"], ["NES"], None, None, None)
    return vd.VaultPage(vault_id, f"Game {vault_id} (NES)", "http://x/", [m])


class FakeClient(vd.VimmClient):
    """Fails vault 2 for the first `fail_until` attempts, then succeeds."""

    def __init__(self, o, fail_until, grow=True):
        super().__init__(o)
        self.fail_until = fail_until
        self.grow = grow
        self.attempts = {}

    def fetch_vault(self, vault_id):
        return make_page(vault_id)

    def download(self, page, media, alt, dest_dir):
        vid = page.vault_id
        n = self.attempts.get(vid, 0) + 1
        self.attempts[vid] = n
        if vid == 2 and n <= self.fail_until:
            raise vd.VimmError(f"simulated failure {n}")
        return vd.Result(vid, media.media_id, media.filename, "ok", 1000, "downloaded")


def summary(results):
    out = {}
    for r in results:
        out[r.status] = out.get(r.status, 0) + 1
    return out


print("=== case 1: vault 2 fails once, sweep 1 recovers it ===")
o = opts()
c = FakeClient(o, fail_until=1)
res = vd.run_http.__wrapped__ if hasattr(vd.run_http, "__wrapped__") else None
# drive the sweep loop directly with our fake client
import types
orig = vd.VimmClient
vd.VimmClient = lambda o, **kw: c
results = vd.run_http([1, 2, 3], o)
vd.VimmClient = orig
print("  attempts per vault:", c.attempts)
print("  summary:", summary(results))
assert summary(results).get("ok") == 3, "sweep should have recovered vault 2"
assert summary(results).get("failed") is None, "no failures should remain"
print("  PASS")

print("\n=== case 2: vault 2 always fails, sweeps must stop ===")
o = opts(sweeps=10)
c = FakeClient(o, fail_until=999)
vd.VimmClient = lambda o, **kw: c
results = vd.run_http([1, 2], o)
vd.VimmClient = orig
print("  attempts per vault:", c.attempts)
print("  summary:", summary(results))
assert summary(results).get("failed") == 1
# vault 2: 1 initial + sweeps that add nothing -> must stop early, not run all 10
assert c.attempts[2] <= 3, f"expected early stop, got {c.attempts[2]} attempts"
print("  PASS (stopped after", c.attempts[2], "attempts, not 11)")

print("\n=== case 3: sweeps disabled ===")
o = opts(sweeps=0)
c = FakeClient(o, fail_until=1)
vd.VimmClient = lambda o, **kw: c
results = vd.run_http([1, 2], o)
vd.VimmClient = orig
print("  attempts per vault:", c.attempts)
assert summary(results).get("failed") == 1, "no sweep means the failure stands"
assert c.attempts[2] == 1
print("  PASS")

print("\nAll sweep cases passed.")
