"""Buyer-estimate sanity bound (>3x asking = probable typo) — no network, no credits.

Born from a real incident (8 Sep 2026): a user typed GBP 5.85m against a
GBP 625k asking, which poisoned anchor_bias and seeded an absurd crowd vote.
These tests stub the builder and every outbound side-effect, then drive the
REAL build path (_run_free_build) and the REAL render path (/r/<id>) and assert:

  B1  estimate > 3x asking is flagged: buyer_estimate_implausible stored True,
      anchor_bias left null (not poisoned), NO owner-seed crowd vote, and the
      raw estimate still stored so the prompt can echo it;
  B2  estimate at exactly 3x is NOT flagged (boundary): behaves as before —
      anchor_bias computed, owner seed present;
  B3  a normal estimate is untouched: flag False, anchor_bias computed, seeded;
  B4  an unparseable estimate is not flagged and creates no seed (ValueError
      path) — the guard never raises;
  B5  a built report with no asking_price cannot flag (guard short-circuits);
  B6  the free report renders the "Check your number" prompt with the
      submitted figure ONLY when flagged, and never on a normal report.

Run:  python3 tests/test_estimate_bounds.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="houseoffer-estbound-test-")

import app as ho  # noqa: E402

# Crowd votes live at a module-level VOTES_DIR (hardcoded /tmp path, NOT under
# DATA_DIR). Point it at a fresh temp dir so runs are hermetic and a stale file
# from an earlier run of the same rid can never fake a pass or force a fail.
ho.VOTES_DIR = tempfile.mkdtemp(prefix="houseoffer-estbound-votes-")

PASS = FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    PASS += ok
    FAIL += not ok


ASKING = 625_000
# The two candidate anchor_bias baselines are deliberately DIFFERENT so a
# regression back to local_avg_sold cannot pass by coincidence (PR #32: the ONE
# baseline is the stored weighted midpoint, i.e. Our Valuation).
LOCAL_AVG = 500_000
MIDPOINT = 600_000

# A plausible built report. build_report_data is stubbed to return this, so
# the guard sees report["asking_price"] exactly as production would.
FAKE_REPORT = {
    "asking_price": ASKING, "asking_price_formatted": "£625,000",
    "local_avg_sold": LOCAL_AVG,
    "postcode": "NR1 3AY", "property_type": "semi-detached", "verdict": "overpriced",
    "weighted_low": 560_000, "weighted_high": 640_000, "weighted_midpoint": MIDPOINT,
    "weighted_midpoint_formatted": "£600,000",
    "open_offer": 575_000, "target_price": 600_000, "walk_away": 620_000,
    "confidence_score": "high", "comparables_count": 12, "comparables": [],
    "football_field": [], "country": "England", "days_on_market": None,
}


def stub_side_effects():
    """Stub the builder and every outbound call; keep storage/votes REAL (temp DATA_DIR)."""
    ho.build_report_data = lambda **kw: dict(FAKE_REPORT)
    ho.post_to_sheets = lambda payload: None
    ho.log_event = lambda rid, event_type, extra=None: None
    ho.send_report_email = lambda *a, **k: True
    ho.notify_owner = lambda *a, **k: None


def build(rid, estimate, report_override=None):
    if report_override is not None:
        ho.build_report_data = lambda **kw: dict(report_override)
    else:
        ho.build_report_data = lambda **kw: dict(FAKE_REPORT)
    ho._run_free_build(rid, {
        "property_url": "https://www.rightmove.co.uk/properties/12345678",
        "email": "buyer@example.com", "buyer_estimate": estimate,
        "report_url": f"https://houseoffer-backend.onrender.com/r/{rid}",
        "asking_price": ASKING, "postcode": "NR1 3AY",
        "bedrooms": "3", "property_type": "semi-detached",
    })
    return ho.load_report(rid) or {}


def owner_seed(rid):
    return [v for v in (ho._load_votes(rid) or []) if v.get("token") == f"owner-{rid}"]


stub_side_effects()

print("[B1] estimate > 3x asking (the real incident: £5.85m vs £625k)")
rid1 = "e5b0e5b00001"
s = build(rid1, "5,850,000")
check("build completed (status ready — no silent failure)", s.get("status") == "ready", str(s.get("status")))
check("buyer_estimate_implausible stored True", s.get("buyer_estimate_implausible") is True)
check("anchor_bias left null (not poisoned)", s.get("anchor_bias") is None, str(s.get("anchor_bias")))
check("no owner-seed crowd vote created", owner_seed(rid1) == [], str(owner_seed(rid1)))
check("raw estimate still stored for the prompt", s.get("buyer_estimate") == "5,850,000")

print("[B2] estimate at EXACTLY 3x asking is not flagged (boundary)")
rid2 = "e5b0e5b00002"
s = build(rid2, str(3 * ASKING))  # 1,875,000
expected_ab = round(((3 * ASKING - MIDPOINT) / MIDPOINT) * 100, 1)
check("not flagged", s.get("buyer_estimate_implausible") is False)
check(f"anchor_bias computed vs the stored weighted midpoint ({expected_ab})", s.get("anchor_bias") == expected_ab, str(s.get("anchor_bias")))
check("owner seed present with the estimate",
      len(owner_seed(rid2)) == 1 and owner_seed(rid2)[0].get("estimate") == 3 * ASKING, str(owner_seed(rid2)))

print("[B3] normal estimate is untouched")
rid3 = "e5b0e5b00003"
s = build(rid3, "600000")
check("not flagged", s.get("buyer_estimate_implausible") is False)
check("anchor_bias 0.0 vs the stored weighted midpoint (estimate == Our Valuation; vs local average it would be 20.0)",
      s.get("anchor_bias") == 0.0, str(s.get("anchor_bias")))
check("owner seed present", len(owner_seed(rid3)) == 1)

print("[B4] unparseable estimate never raises, is not flagged, creates no seed")
rid4 = "e5b0e5b00004"
s = build(rid4, "five hundred k")
check("build completed", s.get("status") == "ready", str(s.get("status")))
check("not flagged", s.get("buyer_estimate_implausible") is False)
check("anchor_bias null", s.get("anchor_bias") is None)
check("no owner seed", owner_seed(rid4) == [])

print("[B5] built report with no asking_price cannot flag (guard short-circuits)")
rid5 = "e5b0e5b00005"
no_asking = dict(FAKE_REPORT, asking_price=None)
s = build(rid5, "5,850,000", report_override=no_asking)
check("not flagged when asking_price is absent", s.get("buyer_estimate_implausible") is False)
ho.build_report_data = lambda **kw: dict(FAKE_REPORT)

print("[B6] free report renders the prompt only when flagged")
client = ho.app.test_client()
r_flag = client.get(f"/r/{rid1}")
b_flag = r_flag.get_data(as_text=True)
check("flagged report renders (200)", r_flag.status_code == 200, str(r_flag.status_code))
check("'Check your number' prompt shown", "Check your number" in b_flag)
check("prompt echoes the submitted figure", "£5,850,000" in b_flag)
r_norm = client.get(f"/r/{rid3}")
b_norm = r_norm.get_data(as_text=True)
check("normal report renders (200)", r_norm.status_code == 200, str(r_norm.status_code))
check("prompt absent on normal report", "Check your number" not in b_norm)

print(f"\n{PASS} pass, {FAIL} fail")
sys.exit(1 if FAIL else 0)
