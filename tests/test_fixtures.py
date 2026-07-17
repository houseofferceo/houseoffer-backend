"""Regression fixtures for the valuation engine — zero PropertyData credits.

Two named fixtures, each born from a real mispricing incident:

  9 Fenton Street, LS29 7EX   (14 Jul 2026) — weighted-mean bedroom-blind
      mispricing; fixed by the robust-median combiner + context-only weak
      methods. Expected midpoint ~£323-326k on the stored payload maths.
      The property's own sale completed 15 Jan 2026 at £330,000 — 1.4% from
      our midpoint — which is also this engine's best accuracy datapoint.

  121/95 Wemborough Road, HA7 2ED (16 Jul 2026) — dead Land Registry endpoint
      + size-blind comps on a 275m² subject; published £680k vs £1.25M asking
      with a LOW-confidence pocket-claim. Fixed by the /landregistry/query
      endpoint correction, the size-mismatch guardrail and the free-report
      LOW-confidence gate.

What this file tests WITHOUT spending credits (Land Registry SPARQL is free):
  - the LR direct endpoint answers and carries the ground-truth sales;
  - find_last_sale / get_last_sale_candidates wiring on the LR-only path
    (PropertyData stubbed to empty);
  - the weighted-median combiner reproduces the LS29 range from its stored
    payload (tests/fixtures/ls29_new.json).

Full-build expectations (midpoint bands, guardrail fires, confidence tiers)
are asserted in tests/fixtures/expected.json and validated by live rebuilds —
those DO cost credits and are run deliberately, not in CI.

Run:  python3 tests/test_fixtures.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATA_DIR", "/tmp/houseoffer-test-data")

import app as ho  # noqa: E402

FIXDIR = os.path.join(os.path.dirname(__file__), "fixtures")
PASS = FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    PASS += ok
    FAIL += not ok


print("[1] Land Registry direct endpoint (the Wemborough root cause)")
rows = ho._fetch_land_registry_direct("HA7 2ED")
check("HA7 2ED returns sales", len(rows) > 0, f"{len(rows)} rows")
sales = {(r.get("address"), r.get("date"), r.get("price")) for r in rows}
check("121's Sep-2025 £875k sale present",
      ("121 WEMBOROUGH ROAD HA7 2ED", "2025-09-30", 875000) in sales)
check("121's Apr-2025 £750k sale present",
      ("121 WEMBOROUGH ROAD HA7 2ED", "2025-04-17", 750000) in sales)

print("[2] Last-sale + address-picker wiring (LR-only path, PropertyData stubbed)")
ho.get_all_sold_at_postcode = lambda pc, **kw: ([], {})
ls = ho.find_last_sale("HA7 2ED", "121 Wemborough Road")
check("find_last_sale(121 Wemborough) = £875k Sep-2025",
      bool(ls) and ls.get("price") == 875000 and ls.get("date") == "2025-09-30", str(ls))
cands = ho.get_last_sale_candidates("HA7 2ED")
cand_list = cands[0] if isinstance(cands, tuple) else cands
addrs = {(c.get("address") or "") for c in (cand_list or [])}
check("picker contains no. 95", any(a.startswith("95 ") for a in addrs),
      f"{len(addrs)} candidates")
ls29 = ho.find_last_sale("LS29 7EX", "9 Fenton Street")
check("find_last_sale(9 Fenton St) = £330k Jan-2026",
      bool(ls29) and ls29.get("price") == 330000 and str(ls29.get("date", "")).startswith("2026-01"),
      str(ls29))

print("[3] Weighted-median combiner reproduces the stored LS29 range")
payload = json.load(open(os.path.join(FIXDIR, "ls29_new.json")))
voters = [m for m in payload["football_field"] if m["available"] and m["weight"] > 0]
lo = ho._weighted_median([(m["low"], m["weight"]) for m in voters])
hi = ho._weighted_median([(m["high"], m["weight"]) for m in voters])
check("weighted_low matches stored", lo == payload["weighted_low"], f"{lo} vs {payload['weighted_low']}")
check("weighted_high matches stored", hi == payload["weighted_high"], f"{hi} vs {payload['weighted_high']}")
mid = round((lo + hi) / 2)
exp = json.load(open(os.path.join(FIXDIR, "expected.json")))
band = exp["ls29_7ex"]["midpoint_band"]
check(f"midpoint in fixture band {band}", band[0] <= mid <= band[1], f"{mid:,}")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
