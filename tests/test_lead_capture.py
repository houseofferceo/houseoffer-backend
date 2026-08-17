"""Lead-capture flow invariants (17 Aug brief, item 1) — no network, no credits.

Asserts the email-first capture path end-to-end at the Flask layer:
  L1  /track?tier=29|99 renders the capture step (no more dead-end redirect);
  L2  £99 page carries the approved copy verbatim and the referral checkbox
      is present and UNTICKED;
  L3  POST /interest stores the lead durably (JSONL) with the referral state
      as its own field, before any external side effect;
  L4  £29 capture stores a pending intent and redirects onward to the form;
  L5  a bad email re-renders the form with an error, storing nothing;
  L6  /admin/sheets-probe requires the admin key.

Run:  python3 tests/test_lead_capture.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="houseoffer-lead-test-")

import app as ho  # noqa: E402

PASS = FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    PASS += ok
    FAIL += not ok


client = ho.app.test_client()

print("[1] /track renders the capture step (L1)")
r99 = client.get("/track?tier=99&postcode=NR1&verdict=value")
r29 = client.get("/track?tier=29&next=form")
check("tier=99 returns 200 page, not a redirect", r99.status_code == 200)
check("tier=29 returns 200 page, not a redirect", r29.status_code == 200)
check("legacy no-tier click still redirects",
      client.get("/track").status_code == 302)

print("[2] £99 copy + referral checkbox (L2)")
body = r99.get_data(as_text=True)
check("headline verbatim", "The £99 Playbook is still in build." in body)
check("subline verbatim",
      "Leave your email and we'll tell you the moment it's live." in body)
check("referral copy verbatim",
      "someone paid by the buyer, never the seller" in body)
check("no-sharing promise verbatim",
      "No data is shared with anyone until you say so." in body)
check("checkbox present and UNTICKED",
      'name="referral"' in body and "checked" not in body)
check("submit label verbatim", "Keep me posted" in body)

print("[3] £99 POST stores the lead with referral state (L3)")
r = client.post("/interest", data={"tier": "99", "email": "lead99@example.com",
                                   "source": "report", "referral": "1"})
check("returns confirmation page", r.status_code == 200
      and "You're on the list." in r.get_data(as_text=True))
with open(ho.LEADS_PATH) as f:
    leads = [json.loads(l) for l in f if l.strip()]
check("lead row written", len(leads) == 1)
check("referral captured as its own field",
      leads and leads[-1]["referral_interest"] is True)
check("tier recorded", leads and leads[-1]["tier"] == "99")

print("[4] £29 capture sets intent and continues to the form (L4)")
r = client.post("/interest", data={"tier": "29", "email": "buyer29@example.com",
                                   "source": "homepage"})
check("redirects onward to the report form", r.status_code == 302
      and "unlock=29" in r.headers.get("Location", ""))
intent = ho._get_intent("Buyer29@Example.com")
check("intent stored (case-insensitive)", bool(intent) and intent["tier"] == "29")
check("no intent for a £99-only lead", ho._get_intent("lead99@example.com") is None)

print("[5] Bad email stores nothing (L5)")
before = len(open(ho.LEADS_PATH).readlines())
r = client.post("/interest", data={"tier": "99", "email": "not-an-email"})
check("form re-rendered with error", r.status_code == 200
      and "please check it" in r.get_data(as_text=True))
check("no lead row added", len(open(ho.LEADS_PATH).readlines()) == before)
check("invalid tier rejected",
      client.post("/interest", data={"tier": "49", "email": "a@b.co"}).status_code == 400)

print("[6] Sheets probe is admin-gated (L6)")
check("no key → 401", client.get("/admin/sheets-probe").status_code == 401)

print(f"\n{PASS} pass, {FAIL} fail")
sys.exit(1 if FAIL else 0)
