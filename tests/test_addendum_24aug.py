"""24 Aug addendum invariants (A5–A9) — zero credits, no network.

Covers the pure logic the addendum changed:
  S1  cascade tier 1: type + beds exact + size ±20% when n ≥ 5;
  S2  string bedrooms match (the latent "3" != 3 bug the old inline code had);
  S3  falls to size-only (1b) when exact-bed count is thin;
  S4  falls to beds ±1 (tier 2) when no size data;
  S5  NEVER collapses below the minimum — thin tiers keep the full type set
      (the ced22b6 regression protection, in both directions);
  S6  broadened searches carry the _broadened suffix (the brief's tier 4);
  S7  unknown property type → LOW with the explicit type-filter caveat and
      no contradictory "matched on property type" claim (A8);
  S8  a beds±1 set cannot claim HIGH confidence.

Run:  python3 tests/test_addendum_24aug.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="houseoffer-a24-test-"))

import app as ho  # noqa: E402

PASS = FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    PASS += ok
    FAIL += not ok


def comp(addr, price, beds=None):
    return {"address": addr, "price": price, "bedrooms": beds, "date": "2026-01"}


def lookup(*addrs_sqm):
    out = {}
    for addr, sqm in addrs_sqm:
        import re
        out[re.sub(r"[^A-Z0-9]", "", addr.upper())] = {"sqm": sqm, "psqm": 3000}
    return out


SUBJ_SQM = 90.0  # ±20% band (on sqft) ≈ 72–108 m²

print("[1] Tier 1 fires: beds exact + size ±20%, n>=5 (S1/S2)")
comps = [comp(f"{i} A ST", 300000 + i * 1000, beds="3") for i in range(5)] \
      + [comp(f"{i} B ST", 500000, beds="5") for i in range(5)]
lk = lookup(*[(f"{i} A ST", 92.0) for i in range(5)],
            *[(f"{i} B ST", 160.0) for i in range(5)])
sel, label, tier, size_n = ho._select_headline_comps(comps, 3, SUBJ_SQM, lk, False)
check("tier 1_beds_size selected", tier == "1_beds_size" and label == "bedroom_matched")
check("only the like-for-like five survive", len(sel) == 5
      and all(c["price"] < 400000 for c in sel))
check("string bedrooms matched (S2)", all(c["bedrooms"] == "3" for c in sel))

print("[2] Falls to size-only when exact beds are thin (S3)")
comps2 = [comp(f"{i} A ST", 300000, beds="3" if i < 3 else None) for i in range(6)]
lk2 = lookup(*[(f"{i} A ST", 88.0) for i in range(6)])
sel, label, tier, _ = ho._select_headline_comps(comps2, 3, SUBJ_SQM, lk2, False)
check("tier 1b_size selected", tier == "1b_size" and label == "size_matched", tier)
check("all six size-matched kept", len(sel) == 6)

print("[3] Falls to beds ±1 with no size data (S4)")
comps3 = [comp(f"{i} C ST", 250000, beds=b) for i, b in
          enumerate(["2", "3", "3", "4", "2", "6", "6", "6"])]
sel, label, tier, _ = ho._select_headline_comps(comps3, 3, None, {}, False)
check("tier 2_beds_band selected", tier == "2_beds_band" and label == "bedroom_band")
check("6-bed outliers excluded, ±1 kept", len(sel) == 5
      and all(c["bedrooms"] in ("2", "3", "4") for c in sel))

print("[4] Never collapses below the minimum (S5)")
comps4 = [comp(f"{i} D ST", 250000, beds="3" if i < 2 else None) for i in range(8)]
sel, label, tier, _ = ho._select_headline_comps(comps4, 3, None, {}, False)
check("thin tiers keep the FULL type set", tier == "3_type" and label is None
      and len(sel) == 8)
sel, label, tier, _ = ho._select_headline_comps(comps4, None, SUBJ_SQM, {}, False)
check("no bedrooms known → type set unchanged", tier == "3_type" and len(sel) == 8)

print("[5] Broadened suffix (S6)")
_, _, tier, _ = ho._select_headline_comps(comps4, 3, None, {}, True)
check("suffix records the widened search", tier == "3_type_broadened")

print("[6] Unknown type forces LOW with honest wording (S7/A8)")
score, reasons, caveat = ho._resolve_confidence(
    comparable_tier="postcode", comparable_confidence="area_only",
    type_unknown=True, comp_count=15, sale_type=None, is_new_build=False,
    has_value=True, weighted_midpoint=300000, asking_price=310000)
joined = " ".join(reasons)
check("score LOW", score == "low")
check("names the type-filter mechanism", "could not be filtered by type" in joined)
check("no contradictory type-match claim", "matched on property type" not in joined)

print("[7] beds±1 set cannot claim HIGH (S8)")
score, _, _ = ho._resolve_confidence(
    comparable_tier="postcode", comparable_confidence="bedroom_band",
    type_unknown=False, comp_count=12, sale_type=None, is_new_build=False,
    has_value=True, weighted_midpoint=300000, asking_price=310000)
check("bedroom_band caps at MEDIUM", score == "medium", score)

print(f"\n{PASS} pass, {FAIL} fail")
sys.exit(1 if FAIL else 0)
