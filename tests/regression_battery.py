"""Regression battery — the full-product invariants check (P0 audit 2026-07-23).

One command:
    python3 tests/regression_battery.py --base https://houseoffer-backend.onrender.com

POSTs each listing class in tests/battery_urls.json to /api/report-data (JSON,
no emails sent) and asserts the invariants below on every response. Responses
are cached in tests/.battery_cache/ keyed by URL — a cached run costs ZERO
PropertyData credits; pass --fresh to force live rebuilds (~6-7 credits per
listing).

Invariants (from the 23-Jul dev brief):
  I1  exactly one verdict narrative — an auction lot must never carry a
      standard verdict/trio (trio_anchor=='auction' => no trio values);
  I2  walk-away >= target >= opening whenever a trio exists;
  I3  no recommendation above asking on normal listings (value case at
      MEDIUM+ may lift the walk-away, capped at asking x1.05);
  I4  comps count >= MIN_COMPARABLES or the response is explicitly flagged
      (broadened search / low comparable_confidence);
  I5  floor area, when present, within sanity bounds (20-500 m2);
  I6  valuation range spread flagged when > +/-25% around the midpoint
      (flag = confidence not 'high');
  I7  no unhandled 500s — rejection classes must fail with a clean error.

Per-slot 'expect'/'expect_any'/'expect_rejection' assertions come from the
battery file (scraper ground truth, hand-verified once — 2b).
"""
import argparse
import hashlib
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(__file__)
CACHE = os.path.join(HERE, ".battery_cache")
MIN_COMPARABLES = 10  # mirrors app.MIN_COMPARABLES without importing the app

PASS, FAIL, WARN = [], [], []


def note(bucket, slot, msg):
    bucket.append(f"slot {slot}: {msg}")
    tag = "PASS" if bucket is PASS else ("FAIL" if bucket is FAIL else "WARN")
    print(f"  {tag}  {msg}")


def fetch(base, url, fresh):
    os.makedirs(CACHE, exist_ok=True)
    key = os.path.join(CACHE, hashlib.sha1(url.encode()).hexdigest() + ".json")
    if not fresh and os.path.exists(key):
        with open(key) as f:
            return json.load(f)
    req = urllib.request.Request(
        base.rstrip("/") + "/api/report-data",
        data=json.dumps({"property_url": url}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=240) as r:
            body = {"status": r.status, "json": json.load(r)}
    except urllib.error.HTTPError as e:
        try:
            payload = json.load(e)
        except Exception:
            payload = {"raw": e.read()[:200].decode(errors="replace")}
        body = {"status": e.code, "json": payload}
    except Exception as e:
        body = {"status": None, "json": {"error": f"transport: {e}"}}
    with open(key, "w") as f:
        json.dump(body, f)
    return body


def check_slot(slot, cls, resp, expect, expect_any, expect_rejection, expect_graceful=False):
    status, data = resp["status"], resp["json"]

    if expect_graceful:
        # Dead/junk listings may still build (Rightmove keeps page data on
        # delisted pages) — the requirement is a clean rejection OR a flagged
        # LOW-confidence report, and NEVER a 500.
        if status == 500:
            note(FAIL, slot, f"{cls}: unhandled 500 (I7)")
        elif status != 200 or (isinstance(data, dict) and data.get("error")):
            note(PASS, slot, f"{cls}: rejected cleanly (HTTP {status})")
        else:
            r = data.get("report", data)
            if r.get("confidence_score") == "low":
                note(PASS, slot, f"{cls}: built but flagged LOW (graceful)")
            else:
                note(FAIL, slot, f"{cls}: junk built WITHOUT a low-confidence flag")
        return

    if expect_rejection:
        if status == 500:
            note(FAIL, slot, f"{cls}: unhandled 500 (I7)")
        elif status and status != 200 or (isinstance(data, dict) and data.get("error")):
            note(PASS, slot, f"{cls}: rejected cleanly (HTTP {status}, {str(data.get('error'))[:60]})")
        else:
            note(FAIL, slot, f"{cls}: expected rejection but got a report (I7)")
        return

    if status != 200 or not isinstance(data, dict) or data.get("error"):
        note(FAIL, slot, f"{cls}: HTTP {status} / error {str((data or {}).get('error'))[:80]} (I7)")
        return
    r = data.get("report", data)

    # I1 — mutually exclusive narratives
    if r.get("trio_anchor") == "auction" or r.get("sale_type") == "auction":
        if any(r.get(k) for k in ("open_offer", "target_price", "walk_away")):
            note(FAIL, slot, f"{cls}: auction lot carries a trio (I1)")
        else:
            note(PASS, slot, f"{cls}: auction handled, trio suppressed (I1)")
    else:
        o, t, w = r.get("open_offer"), r.get("target_price"), r.get("walk_away")
        ask = r.get("asking_price")
        if o and t and w:
            if not (o <= t <= w):
                note(FAIL, slot, f"{cls}: ordering broken o={o} t={t} w={w} (I2)")
            else:
                note(PASS, slot, f"{cls}: trio ordered £{o:,}/£{t:,}/£{w:,} (I2)")
            if ask:
                mid = r.get("weighted_midpoint")
                cap = ask * 1.05 if (mid and mid > ask
                                     and r.get("confidence_score") in ("high", "medium")) else ask
                if o > ask or t > ask or w > cap + 500:
                    note(FAIL, slot, f"{cls}: recommendation above asking (I3) o/t/w vs ask £{ask:,}")
                else:
                    note(PASS, slot, f"{cls}: nothing above asking (I3)")
        else:
            note(WARN, slot, f"{cls}: no trio on a non-auction listing — check always-a-play")

    n = r.get("comparables_count") or 0
    flagged = (r.get("search_broadened") or r.get("comparable_confidence") in ("low", "area_only")
               or r.get("confidence_score") == "low")
    if n >= MIN_COMPARABLES or flagged:
        note(PASS, slot, f"{cls}: comps n={n}, flagged={bool(flagged)} (I4)")
    else:
        note(FAIL, slot, f"{cls}: thin comps n={n} without any flag (I4)")

    fa = r.get("floor_area_sqm")
    if fa is not None and not (20 <= float(fa) <= 500):
        note(FAIL, slot, f"{cls}: floor area {fa} m² outside sanity bounds (I5)")

    lo, hi, mid = r.get("weighted_low"), r.get("weighted_high"), r.get("weighted_midpoint")
    if lo and hi and mid:
        spread = (hi - lo) / 2 / mid * 100
        if spread > 25 and r.get("confidence_score") == "high":
            note(FAIL, slot, f"{cls}: ±{spread:.0f}% spread yet HIGH confidence (I6)")
        elif spread > 25:
            note(PASS, slot, f"{cls}: wide spread ±{spread:.0f}% correctly not HIGH (I6)")

    # scraper ground-truth expectations (2b)
    for k, v in (expect or {}).items():
        if r.get(k) != v:
            note(FAIL, slot, f"{cls}: expected {k}={v!r}, got {r.get(k)!r}")
        else:
            note(PASS, slot, f"{cls}: {k}={v!r} as expected")
    for k, allowed in (expect_any or {}).items():
        if r.get(k) not in allowed:
            note(FAIL, slot, f"{cls}: expected {k} in {allowed}, got {r.get(k)!r}")
        else:
            note(PASS, slot, f"{cls}: {k}={r.get(k)!r} as expected")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://houseoffer-backend.onrender.com")
    ap.add_argument("--fresh", action="store_true",
                    help="force live rebuilds (spends PropertyData credits)")
    ap.add_argument("--only", type=int, help="run a single slot number")
    args = ap.parse_args()

    battery = json.load(open(os.path.join(HERE, "battery_urls.json")))["battery"]
    for item in battery:
        slot, cls, url = item["slot"], item["class"], item["url"]
        if args.only and slot != args.only:
            continue
        print(f"[slot {slot}] {cls}")
        if url == "TODO":
            note(WARN, slot, f"{cls}: no example URL yet — slot skipped")
            continue
        resp = fetch(args.base, url, args.fresh)
        check_slot(slot, cls, resp, item.get("expect"), item.get("expect_any"),
                   item.get("expect_rejection"), item.get("expect_graceful", False))

    print(f"\n{len(PASS)} pass, {len(FAIL)} fail, {len(WARN)} warn")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
