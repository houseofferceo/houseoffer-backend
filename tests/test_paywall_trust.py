"""Paywall trust patch at the £29 locks — presentation-only, no network, no credits.

Stubs the builder and every outbound side-effect, builds a report through the
REAL background build (_run_free_build), renders the REAL free report (/r/<id>)
and asserts the four trust elements at each £29 lock:

  sample-report link · 7-day refund guarantee · Stripe mark · HIGH-confidence line

  P1  lock 1 (locked football-field overlay): all four on a HIGH report
  P2  lock 2 (leverage strip, days_on_market set): sample link + Stripe mark
  P3  £29 upgrade card: all four
  P4  mobile sticky bar: Stripe mark, above the £29 button
  P5  poll: four one-tap options; hoPoll() logs paywall_poll through the
      existing /log beacon (no backend change)
  P6  gating: LOW confidence, or an asking anomaly, drops the HIGH-confidence
      line only — the other three elements stay
  P7  locked-strip teasers are placeholder glyphs; the fake £305k/£312k/£330k
      and "was £310,000/£325,000" numbers never render (held over from PR #32)
  P8  the sample-report link is at every lock (lock 1, lock 2, upgrade card)
  P9  send_report_email sets reply_to = ceo@houseoffer.uk, so "reply to your
      report email" in the guarantee reaches a monitored inbox
  P10 the £29 CTAs still route to /r/<id>/checkout with their src tags
      (no pricing or unlock change)
  P11 the above-fold £29 card (shown to visitors who arrived through the
      £29 capture step, upgrade_intent set): Stripe mark, guarantee and
      sample-report link beside the CTA; absent without upgrade_intent

Writes the rendered lock-1 block to <tmp>/houseoffer_paywall_lock1.html for
the PR description.  Run:  python3 tests/test_paywall_trust.py
"""
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="houseoffer-paywall-test-")

import app as ho  # noqa: E402

ho.VOTES_DIR = tempfile.mkdtemp(prefix="houseoffer-paywall-votes-")
ho.app.testing = True  # propagate template errors into the checks below

PASS = FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    PASS += ok
    FAIL += not ok


ASKING = 650_000
FAKE_REPORT = {
    "asking_price": ASKING, "asking_price_formatted": "£650,000",
    "local_avg_sold": 500_000, "local_avg_sold_formatted": "£500,000", "sold_diff_pct": 30.0,
    "postcode": "RG1 5BX", "postcode_used": "RG1 5BX", "bedrooms": 3,
    "property_type": "terraced", "property_type_label": "terraced", "verdict": "overpriced",
    "weighted_low": 560_000, "weighted_high": 640_000, "weighted_midpoint": 600_000,
    "weighted_low_formatted": "£560,000", "weighted_high_formatted": "£640,000",
    "weighted_midpoint_formatted": "£600,000",
    "open_offer": 575_000, "target_price": 600_000, "walk_away": 620_000,
    "confidence_score": "high", "asking_anomaly": False,
    "comparables_count": 12, "comparables": [], "football_field": [],
    "country": "England", "days_on_market": 45, "local_avg_dom": 30,
    "address_resolution": "exact", "generated": "16 September 2026",
}

_REAL_SEND_REPORT_EMAIL = ho.send_report_email  # P9 exercises the real function
ho.post_to_sheets = lambda payload: None
ho.log_event = lambda rid, event_type, extra=None: None
ho.send_report_email = lambda *a, **k: True
ho.notify_owner = lambda *a, **k: None

SAMPLE = 'href="https://houseoffer.uk/sample-report/"'
GUARANTEE = "Not useful? Reply to your report email within 7 days for a full refund."
STRIPE = "Secure payment via Stripe"
HIGH_LINE = "You're viewing a HIGH-confidence result"
FAKE_TEASERS = ("£305,000", "£312,000", "£330,000", "£310,000", "£325,000")


def build(rid, report):
    ho.build_report_data = lambda **kw: dict(report)
    ho._run_free_build(rid, {
        "property_url": "https://www.rightmove.co.uk/properties/87654321",
        "email": "buyer@example.com", "buyer_estimate": "600000",
        "report_url": f"https://houseoffer-backend.onrender.com/r/{rid}",
        "asking_price": ASKING, "postcode": "RG1 5BX",
        "bedrooms": "3", "property_type": "terraced",
    })
    return ho.load_report(rid) or {}


def render(rid):
    try:
        r = ho.app.test_client().get(f"/r/{rid}")
        return r.status_code, r.get_data(as_text=True)
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def block(html, marker):
    """The element opened at `marker` up to the first `</a>` that closes it."""
    i = html.find(marker)
    if i < 0:
        return ""
    m = re.compile(r"</a>\s*</div>", re.S).search(html, i)
    return html[i:m.end()] if m else html[i:i + 2000]


print("[P1] lock 1 — locked football-field overlay, HIGH report")
rid = "9a11a11000000001"
s = build(rid, FAKE_REPORT)
code, body = render(rid)
check("report renders (200)", code == 200, str(code) if code else body[:300])
lock1 = block(body, 'class="locked-ff-overlay-inline"')
check("lock 1 block found", bool(lock1))
check("HIGH-confidence line", HIGH_LINE in lock1)
check("Stripe mark", STRIPE in lock1)
check("7-day guarantee, verbatim", GUARANTEE in lock1)
check("sample-report link opens in a new tab", SAMPLE in lock1 and 'target="_blank"' in lock1)
snippet_path = os.path.join(tempfile.gettempdir(), "houseoffer_paywall_lock1.html")
with open(snippet_path, "w", encoding="utf-8") as fh:
    fh.write(lock1)
print(f"        lock-1 block written to {snippet_path}")

print("[P2] lock 2 — leverage strip (days_on_market set)")
lock2 = block(body, 'class="leverage-note"')
check("leverage strip rendered", 'class="leverage-strip"' in body and bool(lock2))
check("sample-report link", SAMPLE in lock2)
check("Stripe mark", STRIPE in lock2)

print("[P3] £29 upgrade card")
card = block(body, 'class="upgrade-card u-report"')
check("upgrade card found", bool(card))
check("HIGH-confidence line", HIGH_LINE in card)
check("Stripe mark", STRIPE in card)
check("7-day guarantee, verbatim", GUARANTEE in card)
check("sample-report link", SAMPLE in card)

print("[P4] mobile sticky bar")
check("Stripe mark in the mobile bar", '<div class="mobile-secure">🔒 Secure payment via Stripe</div>' in body)
check("mark sits above the £29 button", 0 <= body.find("mobile-secure") < body.find("src=mobile_bar"))

print("[P5] one-tap poll")
check("poll section present", 'id="paywall-poll"' in body)
for reason in ("too_expensive", "not_sure_accurate", "not_ready", "just_browsing"):
    check(f"option {reason}", f"hoPoll('{reason}')" in body)
check("hoPoll logs paywall_poll via the /log beacon", "window.hoPoll = function(reason)" in body
      and 'log("paywall_poll", {reason: reason})' in body)

print("[P6] gating of the HIGH-confidence line")
rid_low = "9a11a11000000002"
build(rid_low, dict(FAKE_REPORT, confidence_score="low"))
c_low, b_low = render(rid_low)
l1_low = block(b_low, 'class="locked-ff-overlay-inline"')
check("LOW report renders (200)", c_low == 200, str(c_low) if c_low else b_low[:300])
check("LOW: no HIGH-confidence line anywhere", HIGH_LINE not in b_low)
check("LOW: guarantee, Stripe and sample link still at lock 1",
      GUARANTEE in l1_low and STRIPE in l1_low and SAMPLE in l1_low)
rid_anom = "9a11a11000000003"
build(rid_anom, dict(FAKE_REPORT, asking_anomaly=True))
c_an, b_an = render(rid_anom)
check("asking anomaly renders (200)", c_an == 200, str(c_an) if c_an else b_an[:300])
check("asking anomaly: no HIGH-confidence line even at HIGH", HIGH_LINE not in b_an)

print("[P7] locked-strip teasers are glyphs, never the fake numbers")
check("no fake teaser numbers on the HIGH page", not any(t in body for t in FAKE_TEASERS),
      ", ".join(t for t in FAKE_TEASERS if t in body))
check("no fake teaser numbers on the LOW page", not any(t in b_low for t in FAKE_TEASERS))
check("placeholder glyphs in both locked strips (3 + 4)", body.count("£•••,•••") >= 7, str(body.count("£•••,•••")))

print("[P8] sample-report link at every lock")
check("at least three sample-report links (lock 1, lock 2, upgrade card)", body.count(SAMPLE) >= 3, str(body.count(SAMPLE)))

print("[P9] report email carries reply_to = ceo@houseoffer.uk")
captured = {}
_real_post = ho.requests.post
ho.requests.post = lambda *a, **k: captured.update(k.get("json") or {}) or type("R", (), {"status_code": 200, "text": "ok"})()
try:
    _REAL_SEND_REPORT_EMAIL("buyer@example.com", "<p>report</p>", "RG1 5BX", "overpriced",
                            report_url="https://houseoffer-backend.onrender.com/r/x")
finally:
    ho.requests.post = _real_post
check("reply_to set", captured.get("reply_to") == "ceo@houseoffer.uk", str(captured.get("reply_to")))
check("to is still the buyer", captured.get("to") == ["buyer@example.com"], str(captured.get("to")))

print("[P10] £29 CTAs unchanged: checkout routes and src tags")
for src in ("locked_section", "leverage_strip", "upgrade_card", "mobile_bar"):
    check(f"checkout?src={src}", f"/r/{rid}/checkout?src={src}" in body)

print("[P11] above-fold £29 card — visitors who came through the £29 capture step")
check("card absent without upgrade_intent", "You came for the full report" not in body)
rid_int = "9a11a11000000004"
stored_int = build(rid_int, FAKE_REPORT)
ho.save_report(rid_int, dict(stored_int, upgrade_intent="29"))
c_int, b_int = render(rid_int)
check("intent report renders (200)", c_int == 200, str(c_int) if c_int else b_int[:300])
card_af = ""
i_af = b_int.find("You came for the full report")
if i_af >= 0:
    start_af = b_int.rfind("<div style=", 0, i_af)
    m_af = re.compile(r"</a>\s*</div>", re.S).search(b_int, i_af)
    card_af = b_int[start_af:m_af.end()] if m_af else ""
check("above-fold card present with upgrade_intent", bool(card_af))
check("above-fold: £29 CTA still routes to checkout src=intent_above_fold",
      f"/r/{rid_int}/checkout?src=intent_above_fold" in card_af)
check("above-fold: Stripe mark", STRIPE in card_af)
check("above-fold: 7-day guarantee, verbatim", GUARANTEE in card_af)
check("above-fold: sample-report link, new tab", SAMPLE in card_af and 'target="_blank"' in card_af)
check("above-fold: no HIGH-confidence line (not requested there)", HIGH_LINE not in card_af)
check("sample-report link at four places on an intent visit", b_int.count(SAMPLE) >= 4, str(b_int.count(SAMPLE)))
af_path = os.path.join(tempfile.gettempdir(), "houseoffer_paywall_abovefold.html")
with open(af_path, "w", encoding="utf-8") as fh:
    fh.write(card_af)
print(f"        above-fold block written to {af_path}")

print(f"\n{PASS} pass, {FAIL} fail")
sys.exit(1 if FAIL else 0)
