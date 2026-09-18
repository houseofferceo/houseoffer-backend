"""LOW-confidence gap gate (CEO 18 Sep) — no network, no credits.

A LOW-confidence estimate far from the asking price is never published as a
percentage. One stored flag drives every surface:
  tier "range" (gap > 20%): the indicative range, no percentage, "Why" bullets;
  tier "none"  (gap > 40%): no figure at all, "Why" bullets.

  G1  _low_gap_gate: thresholds, boundaries, LOW-only, above-asking gaps
  G2  _low_reasons / _evidence_gaps: priority order, listing reasons, never empty
  G3  free report, tier "range": range shown, no percentage anywhere, Why box,
      verdict and crowd lines use the range wording
  G4  free report, tier "none": no figure at all, Why box, crowd hook hidden
  G5  free report, LOW inside 20%: unchanged ("About 10% below the asking price")
  G6  legacy stored report without the new keys: tier derived at render time,
      bullets fall back to the stored confidence sentences
  G7  paid report: overpricing card and confidence card follow the same tiers
  G8  report email: verdict card follows the same tiers (the email is what gets
      forwarded and screenshotted)
  G9  crowd-vote JS never rewrites a LOW-gated line with a percentage

Run:  python3 tests/test_low_gap_gate.py
"""
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="houseoffer-lowgate-test-")

import app as ho  # noqa: E402

ho.VOTES_DIR = tempfile.mkdtemp(prefix="houseoffer-lowgate-votes-")
ho.app.testing = True

PASS = FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    PASS += ok
    FAIL += not ok


ASKING = 650_000
REASONS = ["only 4 usable comparable sales nearby",
           "the search had to be widened beyond the immediate postcode",
           "no reliable floor area was available to size-match against"]
BASE = {
    "asking_price": ASKING, "asking_price_formatted": "£650,000",
    "local_avg_sold": 500_000, "local_avg_sold_formatted": "£500,000", "sold_diff_pct": 30.0,
    "postcode": "RG1 5BX", "postcode_used": "RG1 5BX", "bedrooms": 3,
    "property_type": "terraced", "property_type_label": "terraced", "verdict": "overpriced",
    "open_offer": 575_000, "target_price": 600_000, "walk_away": 620_000,
    "comparables_count": 4, "comparables": [], "football_field": [], "search_broadened": True,
    "country": "England", "days_on_market": None, "address_resolution": "exact",
    "generated": "18 September 2026", "overpricing_flag": True, "asking_anomaly": False,
    "confidence_reasons": ["only 4 usable comparable sales nearby (stored sentence)",
                           "the search had to be widened beyond the immediate postcode (stored sentence)"],
    "confidence_caveat": "Confidence is low: only 4 usable comparable sales nearby.",
}


def report(mid, tier, confidence="low", **extra):
    low, high = int(mid * 0.92), int(mid * 1.08)
    r = dict(BASE, confidence_score=confidence, weighted_midpoint=mid, weighted_low=low, weighted_high=high,
             weighted_midpoint_formatted=f"£{mid:,}", weighted_low_formatted=f"£{low:,}",
             weighted_high_formatted=f"£{high:,}", low_gap_tier=tier,
             low_gap_pct=round(abs(ASKING - mid) / ASKING * 100, 1), low_reasons=list(REASONS))
    r.update(extra)
    return r


RANGE = report(500_000, "range")                      # 23.1% -> range £460,000 – £540,000
NONE = report(350_000, "none")                        # 46.2% -> range would be £322,000 – £378,000
LOW = report(600_000, None, sold_diff_pct=8.0)        # 7.7%  -> "About 10% below the asking price"

ho.post_to_sheets = lambda payload: None
ho.log_event = lambda rid, event_type, extra=None: None
ho.send_report_email = lambda *a, **k: True
ho.notify_owner = lambda *a, **k: None


def build(rid, rep):
    ho.build_report_data = lambda **kw: dict(rep)
    ho._run_free_build(rid, {
        "property_url": "https://www.rightmove.co.uk/properties/87654321",
        "email": "buyer@example.com", "buyer_estimate": "600000",
        "report_url": f"https://houseoffer-backend.onrender.com/r/{rid}",
        "asking_price": ASKING, "postcode": "RG1 5BX", "bedrooms": "3", "property_type": "terraced",
    })
    return ho.load_report(rid) or {}


def render(rid):
    try:
        r = ho.app.test_client().get(f"/r/{rid}")
        return r.status_code, r.get_data(as_text=True)
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def email(rep):
    with ho.app.app_context():
        return ho.render_template("report_email.html", report_url="https://houseoffer-backend.onrender.com/r/x", **rep)


PCT = re.compile(r"\d+(\.\d+)?%")

print("[G1] _low_gap_gate thresholds")
g = ho._low_gap_gate
check("HIGH is never gated", g("high", ASKING, 350_000) == (None, 46.2), str(g("high", ASKING, 350_000)))
check("LOW inside 20% is not gated (7.7%)", g("low", ASKING, 600_000) == (None, 7.7), str(g("low", ASKING, 600_000)))
check("exactly 20.0% is not gated (strictly above)", g("low", ASKING, 520_000) == (None, 20.0), str(g("low", ASKING, 520_000)))
check("23.1% -> range", g("low", ASKING, 500_000) == ("range", 23.1), str(g("low", ASKING, 500_000)))
check("exactly 40.0% -> range (strictly above for none)", g("low", ASKING, 390_000) == ("range", 40.0), str(g("low", ASKING, 390_000)))
check("41.5% -> none", g("low", ASKING, 380_000) == ("none", 41.5), str(g("low", ASKING, 380_000)))
check("above asking counts too (53.8% -> none)", g("low", ASKING, 1_000_000) == ("none", 53.8), str(g("low", ASKING, 1_000_000)))
check("no asking or no midpoint -> (None, None)", g("low", None, 600_000) == (None, None) and g("low", ASKING, None) == (None, None))
check("constants 20 / 40", ho.LOW_GAP_RANGE_PCT == 20 and ho.LOW_GAP_NONE_PCT == 40)

print("[G2] _low_reasons — the engine's own gaps, in priority order, never empty")
r = ho._low_reasons(4, True, False, None, 0)
check("thin comps, widened search, no floor area — in that order", r == REASONS, str(r))
r = ho._low_reasons(12, False, True, 80.0, 2)
check("type unknown + few size-matched", r == ["the property type could not be confirmed", "very few similar-sized sales to compare against"], str(r))
r = ho._low_reasons(12, False, False, 80.0, 6)
check("nothing specific -> the fallback reason", r == ["the local sold evidence diverges sharply from the asking price"], str(r))
r = ho._low_reasons(12, False, False, 80.0, 6, property_subtype="Bungalow", subtype_caveat_applied=True, sale_type="auction", asking_anomaly=True)
check("subtype, tenure and anomaly reasons appended", len(r) == 3 and r[0].startswith("listed as a bungalow") and "auction guide price" in r[1] and "non-standard listing" in r[2], str(r))
check("_evidence_gaps is the shared source", ho._evidence_gaps(4, True, False, None, 0) == REASONS)

print("[G3] free report — tier 'range'")
rid_r = "10a6a1e000000001"
s = build(rid_r, RANGE)
c, b = render(rid_r)
check("renders (200)", c == 200, str(c) if c else b[:300])
check("label kept", "Our estimate — LOW confidence" in b)
check("indicative range shown", "Indicative range: £460,000 – £540,000" in b)
check("approved straight-talk copy", "We can't put a reliable figure on this one. The sold evidence we found points well below the asking price" in b)
check("Why box, prominent title", 'class="low-why"' in b and "Why we're not giving a percentage" in b)
check("all three reasons as bullets", all(f"<li>{x}</li>" in b for x in REASONS))
check("lever line", "Confirming the exact address is the single biggest thing that sharpens this." in b)
check("no 'About N% below/above the asking price'", "% below the asking price" not in b and "% above the asking price" not in b)
check("verdict: 'sits above the range we can support', no percentage", "sits <strong>above the range we can support</strong>" in b and "% high" not in b and "% low" not in b)
check("crowd line uses the range", "our indicative range of <strong>£460,000 – £540,000</strong> (LOW confidence)" in b)
val_card = b[b.find('class="valuation-card"'):b.find("</div>", b.find('class="val-asking"'))]
check("no percentage inside the valuation card", not PCT.search(re.sub(r"<[^>]+>", " ", val_card)), (PCT.search(re.sub(r"<[^>]+>", " ", val_card)) or [None])[0] if PCT.search(re.sub(r"<[^>]+>", " ", val_card)) else "")
open(os.path.join(tempfile.gettempdir(), "houseoffer_lowgate_range_page.html"), "w", encoding="utf-8").write(b)

print("[G4] free report — tier 'none'")
rid_n = "10a6a1e000000002"
build(rid_n, NONE)
c, b = render(rid_n)
check("renders (200)", c == 200, str(c) if c else b[:300])
check("no-figure headline", "We couldn't value this property from the available evidence" in b)
check("Why box title", "Why we couldn't value it" in b)
check("all three reasons as bullets", all(f"<li>{x}</li>" in b for x in REASONS))
check("no range figures anywhere on the page", "£322,000" not in b and "£378,000" not in b)
check("no midpoint figure anywhere on the page", "£350,000" not in b)
check("no 'About N% …' and no 'Indicative range'", "% below the asking price" not in b and "Indicative range" not in b)
check("verdict: couldn't value, no number on the gap", "we couldn't value this property</strong>, so we're not putting a number on the gap" in b and "% high" not in b)
check("crowd-vs-data hook hidden", 'id="crowd-vs-data"' not in b)
check("lever line", "Confirming the exact address is the single biggest thing that sharpens this." in b)
open(os.path.join(tempfile.gettempdir(), "houseoffer_lowgate_none_page.html"), "w", encoding="utf-8").write(b)

print("[G5] free report — LOW inside 20% unchanged")
rid_l = "10a6a1e000000003"
build(rid_l, LOW)
c, b = render(rid_l)
check("renders (200)", c == 200, str(c) if c else b[:300])
check("still 'About 10% below the asking price'", "About 10% below the asking price" in b)
check("verdict still 'roughly 10% high'", "roughly 10% high" in b)
check("no Why box, no range headline", 'class="low-why"' not in b and "Indicative range" not in b)

print("[G6] legacy stored report (built before the gate) gets the tier at render time")
rid_g = "10a6a1e000000004"
legacy = dict(RANGE)
for k in ("low_gap_tier", "low_gap_pct", "low_reasons"):
    legacy.pop(k)
build(rid_g, legacy)
stored = ho.load_report(rid_g)
check("stored record really lacks the keys", "low_gap_tier" not in stored["report"])
c, b = render(rid_g)
check("renders (200)", c == 200, str(c) if c else b[:300])
check("range wording, no percentage", "Indicative range: £460,000 – £540,000" in b and "% below the asking price" not in b)
check("bullets fall back to the stored confidence sentences", "<li>only 4 usable comparable sales nearby (stored sentence)</li>" in b)

print("[G7] paid report — overpricing card and confidence card")
ho._offer_frontier = lambda report, profile: None
ho._personalise_offer = lambda report, profile, frontier: None
for label, rid, rep, expect_card, expect_title in (
        ("range", "10a6a1e000000005", RANGE, "indicative range <strong>£460,000 – £540,000</strong>, detail in the evidence below", "Why we're not giving a percentage"),
        ("none", "10a6a1e000000006", NONE, "we couldn't value this property from the available evidence — LOW confidence", "Why we couldn't value it")):
    build(rid, rep)
    st = ho.load_report(rid)
    st.update({"paid": True, "buyer_profile_skipped": True})
    ho.save_report(rid, st)
    c, b = render(rid)
    check(f"paid {label}: renders (200)", c == 200, str(c) if c else b[:300])
    check(f"paid {label}: overpricing card copy", expect_card in b)
    check(f"paid {label}: no 'N% lower'", "% lower</strong>" not in b)
    check(f"paid {label}: Why box in the confidence card", 'class="low-why"' in b and expect_title in b and all(f"<li>{x}</li>" in b for x in REASONS))
    if label == "none":
        check("paid none: no range figures in the overpricing card", "£322,000" not in b[b.find("overpricing-card"):b.find("overpricing-card") + 1500])
    open(os.path.join(tempfile.gettempdir(), f"houseoffer_lowgate_{label}_paid.html"), "w", encoding="utf-8").write(b)

print("[G8] report email — the same tiers")
e = email(RANGE)
check("range: eyebrow + range figure", "Indicative range — LOW confidence" in e and "£460,000 – £540,000" in e)
check("range: approved copy", "so we're showing the range the evidence supports rather than a percentage" in e)
check("range: Why box with the reasons", "Why we're not giving a percentage" in e and all(f"• {x}" in e for x in REASONS))
check("range: no 'Asking above market by' and no 30.0%", "Asking above market by" not in e and "30.0%" not in e)
check("range: lever line", "Confirming the exact address is the single biggest thing that sharpens this." in e)
open(os.path.join(tempfile.gettempdir(), "houseoffer_lowgate_range_email.html"), "w", encoding="utf-8").write(e)
e = email(NONE)
check("none: headline replaces the verdict", "We couldn't value this property from the available evidence" in e and "Asking above what the market supports" not in e)
check("none: LOW CONFIDENCE eyebrow, no figure", "LOW CONFIDENCE" in e and "£322,000" not in e and "£350,000" not in e and "Asking above market by" not in e)
check("none: Why box", "Why we couldn't value it" in e and all(f"• {x}" in e for x in REASONS))
check("none: preheader", "we couldn't value this property from the available evidence, and the report shows why" in e)
open(os.path.join(tempfile.gettempdir(), "houseoffer_lowgate_none_email.html"), "w", encoding="utf-8").write(e)
e = email(LOW)
check("inside 20%: unchanged stat (8.0%) and no Why box", "Asking above market by" in e and "8.0%" in e and "low-why" not in e and "Why we're not" not in e)

print("[G9] crowd-vote JS never rewrites a gated line with a percentage")
c, b = render(rid_r)
check("template exposes the tier to the JS", "HO.lowGap" in b)
js = b[b.find("HO.lowGap"):]
check("range tier keeps the range wording in the live rewrite", "indicative range" in js.lower())

print(f"\n{PASS} pass, {FAIL} fail")
sys.exit(1 if FAIL else 0)
