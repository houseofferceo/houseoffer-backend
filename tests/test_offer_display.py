"""Render-time offer display invariants — zero PropertyData credits, no network.

Born from a live incident (10 Aug 2026, report d42dfb9efe2b, NR1 3AY):
a value-verdict home (evidence midpoint ~16% above the £220k asking) with an
answered buyer profile displayed "Open with £231,000" — £11k ABOVE asking —
because the render-time Frontier floored its bands at weighted_low (the
analysis), collapsed them onto the value-case walk-away (asking × 1.05), and
_personalise_offer clamped the displayed open into that band. The build-time
open-below-asking rule (2026-06-10) never applied to the DISPLAYED number.

These tests pin the asking-anchor philosophy (CEO-approved 2026-07-17) at the
display layer, where the regression battery (stored-trio invariants over HTTP)
cannot see:

  D1  no Frontier position ever displays a price at or above asking;
  D2  value case (weighted_low above the display ceiling) collapses every
      position to asking − £1k, labelled 'asking', and flags value_case;
  D3  neutral buyer answers never move the displayed opening offer;
  D4  the personalised open is hard-capped at asking − £1k even when the
      band handed to it sits above asking;
  D5  the normal (non-value) case keeps distinct bands and the walk-away
      ceiling semantics.

Run:  python3 tests/test_offer_display.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATA_DIR", "/tmp/houseoffer-test-data")

import app as ho  # noqa: E402

PASS = FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    PASS += ok
    FAIL += not ok


# The NR1 3AY shape: value verdict, walk-away lifted to asking × 1.05 (CEO §8),
# evidence floor well above asking.
VALUE_REPORT = {
    "asking_price": 220000,
    "walk_away": 231000,
    "weighted_low": 245000,
    "open_offer": 213000,
    "local_sold_discount_pct": 3.0,
    "days_on_market": 30,
    "local_avg_dom": 60,
    "price_reduced": False,
}

NEUTRAL_PROFILE = {"attachment": "this_one", "position": "first_time",
                   "timeline": "one_three"}
# +1 (several) −1 (fast) = balanced, but WITH drivers — answers genuinely engaged.
BALANCED_DRIVEN_PROFILE = {"attachment": "several", "timeline": "fast"}

print("[1] Value case frontier (D1/D2)")
f = ho._offer_frontier(VALUE_REPORT)
check("frontier built", f is not None)
check("value_case flagged", bool(f and f.get("value_case")))
if f:
    ask = VALUE_REPORT["asking_price"]
    worst = max(p["price_hi"] for p in f["positions"])
    check("no position at/above asking (D1)", worst <= ask - 1000,
          f"max £{worst:,} vs asking £{ask:,}")
    check("all positions collapse to asking − £1k (D2)",
          all(p["price_lo"] == p["price_hi"] == ask - 1000 for p in f["positions"]))
    check("all positions labelled 'asking' (D2)",
          all(p["collapsed"] == "asking" for p in f["positions"]))

print("[2] Neutral answers never move the open (D3)")
f = ho._offer_frontier(VALUE_REPORT, NEUTRAL_PROFILE)
p13n = ho._personalise_offer(VALUE_REPORT, NEUTRAL_PROFILE, f)
check("personalisation payload built", p13n is not None)
if p13n:
    check("open unmoved on neutral answers", not p13n["moved"],
          f"personal £{p13n['personal_open']:,} vs base £{p13n['base_open']:,}")
    check("displayed open below asking",
          p13n["personal_open"] <= VALUE_REPORT["asking_price"] - 1000)

print("[3] Engaged answers stay capped below asking (D1/D4)")
f = ho._offer_frontier(VALUE_REPORT, BALANCED_DRIVEN_PROFILE)
p13n = ho._personalise_offer(VALUE_REPORT, BALANCED_DRIVEN_PROFILE, f)
if p13n:
    check("personal open ≤ asking − £1k",
          p13n["personal_open"] <= VALUE_REPORT["asking_price"] - 1000,
          f"£{p13n['personal_open']:,}")
else:
    check("personalisation payload built", False)

print("[4] Cap holds even against a hand-built above-asking band (D4)")
rogue_frontier = {
    "positions": [
        {"key": "secure", "name": "Secure", "price_lo": 231000,
         "price_hi": 231000, "price_label": "£231,000"},
        {"key": "balanced", "name": "Balanced", "price_lo": 231000,
         "price_hi": 231000, "price_label": "£231,000"},
        {"key": "aggressive", "name": "Aggressive", "price_lo": 231000,
         "price_hi": 231000, "price_label": "£231,000"},
    ],
    "walk_away_formatted": "£231,000",
}
p13n = ho._personalise_offer(VALUE_REPORT, BALANCED_DRIVEN_PROFILE, rogue_frontier)
if p13n:
    check("rogue band clamped to asking − £1k",
          p13n["personal_open"] == VALUE_REPORT["asking_price"] - 1000,
          f"£{p13n['personal_open']:,}")
else:
    check("personalisation payload built", False)

print("[5] Normal case unchanged (D5)")
NORMAL_REPORT = {
    "asking_price": 420000,
    "walk_away": 400000,
    "weighted_low": 350000,
    "open_offer": 380000,
    "local_sold_discount_pct": 4.0,
    "days_on_market": 90,
    "local_avg_dom": 55,
    "price_reduced": True,
    "reduction_pct": 6.0,
}
f = ho._offer_frontier(NORMAL_REPORT)
check("frontier built", f is not None)
if f:
    check("value_case not flagged", not f["value_case"])
    check("no position above walk-away",
          all(p["price_hi"] <= NORMAL_REPORT["walk_away"] for p in f["positions"]))
    check("no position at/above asking (D1)",
          all(p["price_hi"] <= NORMAL_REPORT["asking_price"] - 1000
              for p in f["positions"]))
    check("bands remain distinct (not a blanket collapse)",
          len({p["price_lo"] for p in f["positions"]}) > 1,
          " / ".join(p["price_label"] for p in f["positions"]))
    check("no 'asking' collapse label outside the value case",
          all(p["collapsed"] != "asking" for p in f["positions"]))

print(f"\n{PASS} pass, {FAIL} fail")
sys.exit(1 if FAIL else 0)
