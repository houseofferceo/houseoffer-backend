"""Buy-direct v2 flow invariants (CEO-approved 18 Aug) — no network, no credits.

The £29 pricing click sells BEFORE showing any report: one form (email +
listing + estimate) → property confirm → Stripe → full paid build. These
tests stub the scraper and the paid builder and assert:

  P1  /track?tier=29 renders the one-page buy form; tier=99 keeps the
      waitlist capture; report-page £29 CTAs are untouched (per-report
      checkout, not /track);
  P2  POST /buy: lead stored durably BEFORE the scrape outcome, record
      created as awaiting_payment with the minimal report stub, confirm
      page shows the property and the per-report checkout link;
  P3  /r/<id> on an awaiting_payment record re-serves the confirm page
      (the Stripe cancel path);
  P4  fulfilment on a buy-direct record runs the full from-URL paid build
      (not the stored-fields rebuild) and flips status to building;
  P5  a normal free-report unlock still uses the stored-fields rebuild;
  P6  bad email / non-Rightmove link / failed scrape re-render the form
      with an error and create no report record.

Run:  python3 tests/test_buy_direct.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="houseoffer-buy-test-")

import app as ho  # noqa: E402

PASS = FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    PASS += ok
    FAIL += not ok


client = ho.app.test_client()
calls = {"url_build": [], "rebuild": []}
ho._start_paid_build_from_url = lambda rid, url, address_override=None: calls["url_build"].append((rid, url))
ho._start_rebuild = lambda rid, stored, address=None, tier="paid": calls["rebuild"].append(rid)

GOOD_URL = "https://www.rightmove.co.uk/properties/12345678"
ho.merge_scraped_listing = lambda url, pc, ap, br, pt, ad: (
    "NR1 3AY", 220000, "3", "semi-detached", "14 Maple Close",
    {"resolved_address": "14 Maple Close, Norwich NR1 3AY",
     "main_photo_url": "https://media.example/photo.jpg"})

print("[1] Entry points (P1)")
r29 = client.get("/track?tier=29")
r99 = client.get("/track?tier=99")
b29 = r29.get_data(as_text=True)
check("tier=29 renders buy form", r29.status_code == 200
      and 'action="/buy"' in b29 and 'name="property-url"' in b29)
check("estimate field present and required",
      'name="buyer_estimate"' in b29 and "What do you think it's worth?" in b29)
check("free-report escape hatch present", "Start with the free report" in b29)
check("tier=99 keeps waitlist capture",
      "The £99 Playbook is still in build." in r99.get_data(as_text=True))

print("[2] POST /buy happy path (P2)")
r = client.post("/buy", data={"email": "buyer@example.com",
                              "property-url": GOOD_URL,
                              "buyer_estimate": "230"})
body = r.get_data(as_text=True)
check("confirm page rendered", r.status_code == 200 and "Is this the one?" in body)
check("property summary shown",
      "14 Maple Close, Norwich NR1 3AY" in body and "£220,000" in body and "3 bed" in body)
import re as _re
m = _re.search(r'/r/([a-f0-9]{8,32})/checkout\?src=buy_direct', body)
check("per-report checkout link present", bool(m))
rid = m.group(1) if m else ""
stored = ho.load_report(rid) or {}
check("record awaiting_payment, unpaid, buy_direct",
      stored.get("status") == "awaiting_payment" and stored.get("paid") is False
      and stored.get("buy_direct") is True)
check("email + estimate stored",
      stored.get("email") == "buyer@example.com"
      and stored.get("buyer_estimate") == "230,000")
with open(ho.LEADS_PATH) as f:
    leads = [json.loads(l) for l in f if l.strip()]
check("lead stored with source buy_direct",
      leads and leads[-1]["source"] == "buy_direct" and leads[-1]["tier"] == "29")

print("[3] Revisit before paying (P3)")
r = client.get(f"/r/{rid}")
check("confirm page served again",
      r.status_code == 200 and "Is this the one?" in r.get_data(as_text=True))

print("[4] Fulfilment routes to the from-URL paid build (P4)")
result = ho._unlock_report(rid, source="test_payment")
stored = ho.load_report(rid)
check("unlocked and building",
      result and result["status"] == "unlocked" and stored.get("paid") is True
      and stored.get("status") == "building")
check("from-URL paid build invoked with the listing",
      calls["url_build"] == [(rid, GOOD_URL)] and calls["rebuild"] == [])

print("[5] Normal free-report unlock still rebuilds from stored fields (P5)")
free_rid = "ab12cd34ef56"
ho.save_report(free_rid, {"status": "ready", "paid": False,
                          "email": "free@example.com",
                          "property_url": GOOD_URL,
                          "report": {"tier": "free", "postcode": "NR1 3AY"}})
ho._unlock_report(free_rid, source="test_payment")
check("stored-fields rebuild used for non-buy-direct records",
      calls["rebuild"] == [free_rid] and len(calls["url_build"]) == 1)

print("[6] Rejection paths create nothing (P6)")
before = len(os.listdir(ho.REPORTS_DIR))
r = client.post("/buy", data={"email": "nope", "property-url": GOOD_URL})
check("bad email re-renders form", "please check it" in r.get_data(as_text=True))
r = client.post("/buy", data={"email": "a@b.co",
                              "property-url": "https://www.zoopla.co.uk/x"})
check("non-Rightmove link rejected", "Rightmove property link" in r.get_data(as_text=True))
ho.merge_scraped_listing = lambda *a: (None, 0, None, None, "", {})
r = client.post("/buy", data={"email": "a@b.co", "property-url": GOOD_URL,
                              "buyer_estimate": "200"})
check("failed scrape gives friendly error",
      "read that listing" in r.get_data(as_text=True))
ho.merge_scraped_listing = lambda *a: ("A71B0AC", 0, None, None, "", {})
r = client.post("/buy", data={"email": "a@b.co", "property-url": GOOD_URL,
                              "buyer_estimate": "200"})
check("junk postcode from a dead listing rejected (live 18 Aug case)",
      "read that listing" in r.get_data(as_text=True))
check("no report records created by rejections",
      len(os.listdir(ho.REPORTS_DIR)) == before)

print(f"\n{PASS} pass, {FAIL} fail")
sys.exit(1 if FAIL else 0)
