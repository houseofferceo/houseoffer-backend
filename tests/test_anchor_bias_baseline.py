"""anchor_bias: ONE baseline everywhere — the stored weighted midpoint (Our Valuation).

No network, no credits. Stubs the builder and every outbound side-effect, then
drives the REAL build path (_run_free_build), the REAL render path (/r/<id>) and
the REAL admin read path (/admin/recent), capturing what each consumer receives.

The fixture DELIBERATELY decouples the two candidate baselines
(local_avg_sold = £500k, weighted_midpoint = £600k) so the old formula and the
new one give different numbers for every estimate — an equal-baseline fixture
cannot tell them apart.

  A1  stored report:        anchor_bias vs weighted_midpoint (10.0, not 32.0)
  A2  Submissions sheet row: same value, and self-consistent with the row's own
                            our_valuation column (the WS5 4BL discrepancy)
  A3  submission_created:   same value in the event payload
  A4  owner alert email:    same value passed to notify_owner
  A5  owner-seed vote row:  our_valuation is the same baseline
  A6  /admin/recent:        same value on the read path
  A7  sign: an estimate BELOW our valuation but above the raw comp average is
      negative (the old baseline reported it as "above market")
  A8  no weighted_midpoint -> anchor_bias None even when local_avg_sold exists
      (no silent fallback to the old baseline)
  A9  >3x asking guard (PR #39) still excludes it under the new baseline
  A10 exactly 3x (boundary) is computed vs the midpoint
  A11 rebased trust-fix template renders: LOW + unverified-address banner, LOW
      figure as a discount to asking (the locked-strip teaser-glyph assertions
      travel with that fix, held for the paywall trust-patch PR)

Run:  python3 tests/test_anchor_bias_baseline.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="houseoffer-anchor-test-")
os.environ["ADMIN_KEY"] = "anchor-baseline-test-key-not-real"

import app as ho  # noqa: E402

# Crowd votes live at a hardcoded /tmp VOTES_DIR (not under DATA_DIR) — point it
# at a fresh temp dir so the run is hermetic.
ho.VOTES_DIR = tempfile.mkdtemp(prefix="houseoffer-anchor-votes-")
ho.app.testing = True  # propagate template errors into the checks below

PASS = FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    PASS += ok
    FAIL += not ok


ASKING = 650_000
LOCAL_AVG = 500_000   # raw comparable average — the OLD baseline
MIDPOINT = 600_000    # stored Our Valuation (weighted_midpoint) — the ONE baseline


def pct(est, base):
    return round(((est - base) / base) * 100, 1)


# A plausible built report. build_report_data is stubbed to return this, so the
# build path sees report["weighted_midpoint"] / ["local_avg_sold"] exactly as
# production would. sold_diff_pct/local_avg_sold_formatted are what
# report_email.html needs so the REAL email render succeeds and notify_owner runs.
FAKE_REPORT = {
    "asking_price": ASKING, "asking_price_formatted": "£650,000",
    "local_avg_sold": LOCAL_AVG, "local_avg_sold_formatted": "£500,000",
    "sold_diff_pct": 30.0,
    "postcode": "RG1 5BX", "postcode_used": "RG1 5BX", "bedrooms": 3,
    "property_type": "terraced", "property_type_label": "terraced", "verdict": "overpriced",
    "weighted_low": 560_000, "weighted_high": 640_000, "weighted_midpoint": MIDPOINT,
    "weighted_low_formatted": "£560,000", "weighted_high_formatted": "£640,000",
    "weighted_midpoint_formatted": "£600,000",
    "open_offer": 575_000, "target_price": 600_000, "walk_away": 620_000,
    "confidence_score": "high", "comparables_count": 12, "comparables": [],
    "football_field": [], "country": "England", "days_on_market": None,
    "address_resolution": "exact", "generated": "16 September 2026",
}

SHEET, EVENTS, OWNER = [], [], []
ho.post_to_sheets = lambda payload: SHEET.append(dict(payload))
ho.log_event = lambda rid, event_type, extra=None: EVENTS.append((rid, event_type, dict(extra or {})))
ho.send_report_email = lambda *a, **k: True
ho.notify_owner = lambda *a, **k: OWNER.append((a, k))


def build(rid, estimate, report_override=None):
    """Run the real background build with captured side-effects; return the stored record."""
    del SHEET[:], EVENTS[:], OWNER[:]
    src = report_override if report_override is not None else FAKE_REPORT
    ho.build_report_data = lambda **kw: dict(src)
    ho._run_free_build(rid, {
        "property_url": "https://www.rightmove.co.uk/properties/87654321",
        "email": "buyer@example.com", "buyer_estimate": estimate,
        "report_url": f"https://houseoffer-backend.onrender.com/r/{rid}",
        "asking_price": ASKING, "postcode": "RG1 5BX",
        "bedrooms": "3", "property_type": "terraced",
    })
    return ho.load_report(rid) or {}


def sheet_rows(rid, kind):
    return [p for p in SHEET if p.get("uuid") == rid and p.get("type") == kind]


def event_extras(rid, kind):
    return [e for r, t, e in EVENTS if r == rid and t == kind]


def get(path):
    try:
        r = ho.app.test_client().get(path)
        return r.status_code, r.get_data(as_text=True)
    except Exception as exc:  # app.testing=True: template errors surface here
        return None, f"{type(exc).__name__}: {exc}"


EST = 660_000
NEW, OLD = pct(EST, MIDPOINT), pct(EST, LOCAL_AVG)  # 10.0 vs 32.0

print(f"[A1] stored report: anchor_bias vs weighted_midpoint ({NEW}), not local_avg_sold ({OLD})")
rid1 = "ab1a5e00000001"
s = build(rid1, "660,000")
check("build completed (status ready — no silent failure)", s.get("status") == "ready", str(s.get("status")))
check(f"stored anchor_bias == {NEW}", s.get("anchor_bias") == NEW, str(s.get("anchor_bias")))
check(f"stored anchor_bias != {OLD} (old baseline)", s.get("anchor_bias") != OLD)

print("[A2] Submissions sheet row carries the same value and is self-consistent")
rows = sheet_rows(rid1, "submission")
row = rows[0] if rows else {}
check("exactly one Submissions row posted", len(rows) == 1, str(len(rows)))
check(f"row anchor_bias == {NEW}", row.get("anchor_bias") == NEW, str(row.get("anchor_bias")))
check("row our_valuation column is the same baseline (weighted_midpoint)",
      row.get("our_valuation") == MIDPOINT, str(row.get("our_valuation")))
check("row is self-consistent: (estimate − our_valuation) / our_valuation == anchor_bias",
      bool(row.get("our_valuation")) and pct(EST, row["our_valuation"]) == row.get("anchor_bias"),
      f"{row.get('anchor_bias')} vs {pct(EST, row['our_valuation']) if row.get('our_valuation') else 'n/a'}")

print("[A3] submission_created event carries the same value")
ev = event_extras(rid1, "submission_created")
check("exactly one submission_created event", len(ev) == 1, str(len(ev)))
check(f"event anchor_bias == {NEW}", bool(ev) and ev[0].get("anchor_bias") == NEW, str(ev[0].get("anchor_bias") if ev else None))

print("[A4] owner alert email receives the same value")
check("notify_owner called (report_email.html rendered against the fixture)", len(OWNER) == 1, str(len(OWNER)))
ab_arg = None
if OWNER:
    a, k = OWNER[0]
    ab_arg = a[5] if len(a) > 5 else k.get("anchor_bias")
check(f"notify_owner anchor_bias == {NEW}", ab_arg == NEW, str(ab_arg))

print("[A5] owner-seed vote row uses the same baseline as our_valuation")
votes = sheet_rows(rid1, "vote")
check("one owner-seed vote row posted", len(votes) == 1 and votes[0].get("source") == "owner_seed", str(len(votes)))
check("vote row our_valuation == weighted_midpoint", bool(votes) and votes[0].get("our_valuation") == MIDPOINT,
      str(votes[0].get("our_valuation") if votes else None))

print("[A6] /admin/recent (read path) shows the same value")
code, body = get(f"/admin/recent?key={os.environ['ADMIN_KEY']}")
recent = {}
try:
    recent = {r["report_id"]: r for r in json.loads(body).get("recent", [])}
except Exception:
    pass
check("/admin/recent returns 200", code == 200, str(code))
check(f"/admin/recent anchor_bias == {NEW} for the report", recent.get(rid1, {}).get("anchor_bias") == NEW,
      str(recent.get(rid1, {}).get("anchor_bias")))

print("[A7] sign: estimate below Our Valuation but above the raw comp average is NEGATIVE")
rid2 = "ab1a5e00000002"
s = build(rid2, "540000")
check(f"anchor_bias == {pct(540_000, MIDPOINT)} (old baseline would have said +{pct(540_000, LOCAL_AVG)})",
      s.get("anchor_bias") == pct(540_000, MIDPOINT), str(s.get("anchor_bias")))

print("[A8] no weighted_midpoint -> anchor_bias None (no silent fallback to local_avg_sold)")
rid3 = "ab1a5e00000003"
no_mid = dict(FAKE_REPORT, weighted_midpoint=None, weighted_midpoint_formatted=None, confidence_score="low")
s = build(rid3, "660,000", report_override=no_mid)
check("build completed (status ready)", s.get("status") == "ready", str(s.get("status")))
check("stored anchor_bias is None", s.get("anchor_bias") is None, str(s.get("anchor_bias")))
rows3 = sheet_rows(rid3, "submission")
check("Submissions row anchor_bias is None too", bool(rows3) and rows3[0].get("anchor_bias") is None,
      str(rows3[0].get("anchor_bias") if rows3 else "no row"))

print("[A9] >3x asking guard (PR #39) still excludes anchor_bias under the new baseline")
rid4 = "ab1a5e00000004"
s = build(rid4, str(3 * ASKING + 1))
check("flagged buyer_estimate_implausible", s.get("buyer_estimate_implausible") is True, str(s.get("buyer_estimate_implausible")))
check("anchor_bias None", s.get("anchor_bias") is None, str(s.get("anchor_bias")))
rows4 = sheet_rows(rid4, "submission")
check("Submissions row anchor_bias None", bool(rows4) and rows4[0].get("anchor_bias") is None)

print("[A10] exactly 3x asking (boundary) is computed vs the midpoint")
rid5 = "ab1a5e00000005"
s = build(rid5, str(3 * ASKING))
check("not flagged", s.get("buyer_estimate_implausible") is False)
check(f"anchor_bias == {pct(3 * ASKING, MIDPOINT)}", s.get("anchor_bias") == pct(3 * ASKING, MIDPOINT), str(s.get("anchor_bias")))

print("[A11] rebased trust-fix template renders (LOW + unverified address / HIGH + resolved)")
rid6 = "ab1a5e00000006"
low_unverified = dict(FAKE_REPORT, confidence_score="low", address_resolution=None)
s = build(rid6, "600000", report_override=low_unverified)
code, body = get(f"/r/{rid6}")
check("LOW + unverified-address free report renders (200)", code == 200, str(code) if code else body[:400])
check("'Address unverified' banner shown", "Address unverified" in body)
check("LOW estimate labelled as LOW confidence", "Our estimate — LOW confidence" in body)
check("LOW figure stated as a discount to asking ('About 10% below the asking price')",
      "About 10% below the asking price" in body)
code2, body2 = get(f"/r/{rid1}")
check("HIGH + resolved-address report renders (200)", code2 == 200, str(code2) if code2 else body2[:400])
check("no 'Address unverified' banner when the address resolved", "Address unverified" not in body2)
check("HIGH report shows the precise estimate, not the LOW discount line",
      "£600,000" in body2 and "Our estimate — LOW confidence" not in body2)

print(f"\n{PASS} pass, {FAIL} fail")
sys.exit(1 if FAIL else 0)
