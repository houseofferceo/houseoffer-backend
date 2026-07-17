"""Sold-price back-test harness — scores the valuation engine against ACTUAL
sold prices at ZERO PropertyData cost.

Provenance: the 17-Jul-2026 task brief specified this harness as commit 2 of
wemborough-fix.patch, but that commit never reached the repository (checked
chat, all remote branches). This implementation was written from the brief's
spec and is flagged for management diff-review. Where possible it IMPORTS the
production functions rather than mirroring them (_weighted_median,
_median_trim, postcode_to_region, hpi_data) so the combiner cannot drift.

Method: for each of 12 stratified post towns (the HA7 and LS29 fixture areas
included), pull every standard Land Registry sale since 2023 (free SPARQL),
hold out ~17 sales completed in 2026 as ground truth, and value each one using
only evidence available BEFORE its completion date:

  - comparable sales: same town + property type, up to 36 months before the
    target sale, the target property excluded; production _median_trim applied;
    IQR bounds + interquartile mean, exactly like production methods 1a/1b
    (weights 1 and 2), HPI-adjusted to the target's month;
  - last sale: the target property's own most recent prior transaction,
    HPI-adjusted, weight 3/2/1 by age — production method 2 / A5;
  - price per m²: EPC floor areas joined to comp sales give a town £/m²
    (median, HPI-adjusted); × the target's EPC floor area, ±5%, weight 1 —
    production method 3;
  - combiner: production _weighted_median over method lows/highs.

Guardrail mirrors (same triggers as build_report_data, documented deltas):
  - size-mismatch: floor area known + size-matched comp subset thin + £/m²
    >25% from the comp average -> comparable methods' weight set to 0,
    confidence capped at medium;
  - thin set: fewer than MIN_COMPARABLES type-matched comps -> confidence low.

Known divergences from production (by data availability, per the brief's
"fix the mirror, don't touch production" rule):
  - Land Registry data has no bedroom counts, so the bedroom-matched context
    methods and bedroom guards are out of scope;
  - no asking price exists for a completed sale, so the asking-anchored
    methods, B2/B3 guards and verdicts are out of scope — this scores the
    sold-evidence engine only;
  - "size-matched" = EPC-joined comps within ±20% of the target's floor area;
  - comps are town-level (Land Registry towns), production narrows by
    postcode first — the harness is closer to production's "area" tier.

Usage (repo root, EPC_API_KEY set):
    python3 tools/backtest.py --out backtest_results
Every HTTP response is cached in .backtest_cache/ — re-runs are free and
offline. Emits <out>.csv and <out>.html (review table, worst-first).
"""
import argparse
import csv
import hashlib
import json
import os
import re
import statistics
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATA_DIR", "/tmp/houseoffer-backtest-data")

from app import _weighted_median, _median_trim, postcode_to_region, MIN_COMPARABLES  # noqa: E402
from hpi_data import get_hpi_index  # noqa: E402

CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", ".backtest_cache")
SPARQL = "https://landregistry.data.gov.uk/landregistry/query"
EPC_BASE = "https://api.get-energy-performance-data.communities.gov.uk"
EPC_KEY = os.environ.get("EPC_API_KEY", "")

# 12 stratified post towns: both incident-fixture areas, then a spread of
# regions and price bands (London/SE expensive -> NE/Wales cheap).
TOWNS = ["STANMORE", "ILKLEY", "READING", "LUTON", "NORWICH", "SOLIHULL",
         "LEICESTER", "STOCKPORT", "YORK", "DARLINGTON", "CARDIFF", "PLYMOUTH"]
TARGETS_PER_TOWN = 17
PTYPE_OK = {"detached", "semi-detached", "terraced", "flat-maisonette"}


def _cached(key, fetch):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, hashlib.sha1(key.encode()).hexdigest() + ".json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    data = fetch()
    with open(path, "w") as f:
        json.dump(data, f)
    time.sleep(0.15)  # politeness between real calls
    return data


def _http_json(url, headers):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.load(r)


def town_sales(town):
    """All standard-category sales for a post town since 2023 (cached)."""
    q = f"""PREFIX lrppi: <http://landregistry.data.gov.uk/def/ppi/>
PREFIX lrcommon: <http://landregistry.data.gov.uk/def/common/>
SELECT ?pc ?paon ?saon ?street ?amount ?date ?ptype ?newb WHERE {{
  ?addr lrcommon:town "{town}" .
  ?transx lrppi:propertyAddress ?addr ;
          lrppi:pricePaid ?amount ;
          lrppi:transactionDate ?date ;
          lrppi:transactionCategory <http://landregistry.data.gov.uk/def/ppi/standardPricePaidTransaction> .
  OPTIONAL {{ ?transx lrppi:propertyType ?ptype }}
  OPTIONAL {{ ?transx lrppi:newBuild ?newb }}
  ?addr lrcommon:postcode ?pc .
  FILTER(?date >= "2023-01-01"^^<http://www.w3.org/2001/XMLSchema#date>)
  OPTIONAL {{ ?addr lrcommon:paon ?paon }}
  OPTIONAL {{ ?addr lrcommon:saon ?saon }}
  OPTIONAL {{ ?addr lrcommon:street ?street }}
}} ORDER BY DESC(?date) LIMIT 8000"""

    def fetch():
        url = SPARQL + "?" + urllib.parse.urlencode({"query": q})
        return _http_json(url, {"Accept": "application/sparql-results+json"})

    rows = _cached(f"town:{town}", fetch)["results"]["bindings"]
    out = []
    for b in rows:
        v = lambda k: b.get(k, {}).get("value")  # noqa: E731
        price = int(float(v("amount"))) if v("amount") else None
        if not price or price < 40_000 or price >= 2_000_000:
            continue
        out.append({
            "pc": v("pc"), "paon": (v("paon") or "").upper(),
            "saon": (v("saon") or "").upper(),
            "street": (v("street") or "").upper(),
            "price": price, "date": (v("date") or "")[:10],
            "ptype": (v("ptype") or "").split("/")[-1],
            "new_build": v("newb") == "true",
        })
    return out


def addr_key(s):
    return f"{s['saon']}|{s['paon']}|{s['street']}|{s['pc']}"


def epc_postcode(pc):
    def fetch():
        url = f"{EPC_BASE}/api/domestic/search?" + urllib.parse.urlencode(
            {"postcode": pc, "page_size": 100})
        try:
            return _http_json(url, {"Accept": "application/json",
                                    "Authorization": f"Bearer {EPC_KEY}"})
        except Exception:
            return {"data": []}
    d = _cached(f"epc:{pc}", fetch).get("data")
    return d if isinstance(d, list) else []


def epc_certificate(number):
    def fetch():
        url = f"{EPC_BASE}/api/certificate?" + urllib.parse.urlencode(
            {"certificate_number": number})
        try:
            return _http_json(url, {"Accept": "application/json",
                                    "Authorization": f"Bearer {EPC_KEY}"})
        except Exception:
            return {}
    return _cached(f"cert:{number}", fetch)


def floor_area_for(sale):
    """EPC floor area for a sale's address: search its postcode, match
    addressLine1 on house number + street, fetch the certificate."""
    if not sale["pc"] or not sale["paon"]:
        return None
    want = f"{sale['saon']} {sale['paon']} {sale['street']}".strip()
    want_num = sale["paon"]
    for row in epc_postcode(sale["pc"]):
        line = (row.get("addressLine1") or "").upper()
        line_num = (re.match(r"(\d+[A-Z]?)\b", line) or [None]) and \
            (re.match(r"(\d+[A-Z]?)\b", line).group(1) if re.match(r"(\d+[A-Z]?)\b", line) else None)
        if line == want or (line_num and line_num == want_num and sale["street"] and sale["street"] in line):
            cert = epc_certificate(row.get("certificateNumber")) or {}
            body = cert.get("data", cert)
            for field in ("totalFloorArea", "total_floor_area", "total-floor-area"):
                try:
                    area = float(body.get(field) or 0)
                    if area > 15:
                        return area
                except (TypeError, ValueError):
                    continue
    return None


def postcode_history(pc):
    """Full sale history for one postcode (all years) — mirrors production
    _fetch_land_registry_direct, which find_last_sale uses. Cached."""
    q = f"""PREFIX lrppi: <http://landregistry.data.gov.uk/def/ppi/>
PREFIX lrcommon: <http://landregistry.data.gov.uk/def/common/>
SELECT ?paon ?saon ?street ?amount ?date WHERE {{
  ?addr lrcommon:postcode "{pc}" .
  ?transx lrppi:propertyAddress ?addr ;
          lrppi:pricePaid ?amount ;
          lrppi:transactionDate ?date .
  OPTIONAL {{ ?addr lrcommon:paon ?paon }}
  OPTIONAL {{ ?addr lrcommon:saon ?saon }}
  OPTIONAL {{ ?addr lrcommon:street ?street }}
}} ORDER BY DESC(?date) LIMIT 50"""

    def fetch():
        url = SPARQL + "?" + urllib.parse.urlencode({"query": q})
        return _http_json(url, {"Accept": "application/sparql-results+json"})

    rows = _cached(f"pchist:{pc}", fetch)["results"]["bindings"]
    out = []
    for b in rows:
        v = lambda k: b.get(k, {}).get("value")  # noqa: E731
        if not v("amount"):
            continue
        out.append({"pc": pc, "paon": (v("paon") or "").upper(),
                    "saon": (v("saon") or "").upper(),
                    "street": (v("street") or "").upper(),
                    "price": int(float(v("amount"))), "date": (v("date") or "")[:10]})
    return out


def hpi_ratio(region, from_month, to_month):
    a, b = get_hpi_index(region, from_month), get_hpi_index(region, to_month)
    return (b / a) if a and b else None


def iq_bounds_mean(prices):
    """Production methods 1a/1b: IQR bounds + interquartile mean."""
    prices = sorted(prices)
    n = len(prices)
    q1, q3 = max(0, n // 4), min(n - 1, n - n // 4)
    mid = round(sum(prices[q1:n - q1]) / len(prices[q1:n - q1])) if n >= 5 else round(sum(prices) / n)
    return prices[q1], prices[q3], mid


def value_target(target, sales, psqm_points):
    """Value one held-out sale with pre-sale evidence only. Returns a row dict."""
    region = postcode_to_region(target["pc"])
    t_month = target["date"][:7]
    t_key = addr_key(target)

    pool = [s for s in sales
            if s["ptype"] == target["ptype"] and addr_key(s) != t_key
            and s["date"] < target["date"]
            and (int(target["date"][:4]) * 12 + int(target["date"][5:7])) -
                (int(s["date"][:4]) * 12 + int(s["date"][5:7])) <= 36]
    # Production narrows geographically before it values (postcode, then
    # broadened area). Mirror that ladder: postcode sector ("HA7 2"), then
    # district ("HA7"), then the whole post town — first rung with enough
    # comparables wins, and town-level breadth caps confidence below.
    sector = (target["pc"] or "").rsplit(" ", 1)[0] + " " + (target["pc"] or " ")[-3]
    district = (target["pc"] or "").split(" ")[0]
    comp_tier = "town"
    for tier, pred in (("sector", lambda s: s["pc"] and s["pc"].startswith(sector)),
                       ("district", lambda s: s["pc"] and s["pc"].startswith(district + " ")),
                       ("town", lambda s: True)):
        tier_comps = _median_trim([s for s in pool if pred(s)])
        if len(tier_comps) >= MIN_COMPARABLES or tier == "town":
            comps, comp_tier = tier_comps, tier
            break

    methods = []  # (low, high, weight, name)
    adj = []
    for c in comps:
        r = hpi_ratio(region, c["date"][:7], t_month)
        adj.append(round(c["price"] * r) if r else c["price"])
    if comps:
        lo, hi, mid = iq_bounds_mean([c["price"] for c in comps])
        methods.append([lo, hi, 1, "comps_raw"])
        lo, hi, mid = iq_bounds_mean(adj)
        methods.append([lo, hi, 2, "comps_hpi"])
    comp_mid = iq_bounds_mean(adj)[2] if adj else None

    # last sale: the property's own most recent prior transaction, from the
    # postcode's FULL history (production find_last_sale looks back decades,
    # not just the harness's 2023+ comp window)
    prior = sorted((s for s in postcode_history(target["pc"])
                    if addr_key(s) == t_key and s["date"] < target["date"]),
                   key=lambda s: s["date"])
    last_sale = prior[-1] if prior else None
    if last_sale:
        r = hpi_ratio(region, last_sale["date"][:7], t_month)
        if r:
            v = last_sale["price"] * r
            age = int(target["date"][:4]) - int(last_sale["date"][:4])
            w = 3 if age < 5 else (2 if age < 10 else 1)
            methods.append([round(v * 0.95), round(v * 1.05), w, "last_sale"])

    # £/m²: town median (HPI-adjusted, same type where possible) × target area
    area = floor_area_for(target)
    psqm_mid = None
    size_matched_n = 0
    pts = [p for p in psqm_points if p["ptype"] == target["ptype"]] or psqm_points
    if area and len(pts) >= 5:
        vals = []
        for p in pts:
            r = hpi_ratio(region, p["month"], t_month)
            if r:
                vals.append(p["psqm"] * r)
        if len(vals) >= 5:
            psqm_mid = round(statistics.median(vals) * area)
            methods.append([round(psqm_mid * 0.95), round(psqm_mid * 1.05), 1, "psqm"])
        size_matched_n = sum(1 for p in pts if abs(p["area"] - area) / area <= 0.20)

    # ── guardrail mirrors ─────────────────────────────────────────────────────
    guards = []
    if (area and psqm_mid and comp_mid and size_matched_n < 3
            and abs(psqm_mid - comp_mid) / comp_mid > 0.25):
        guards.append("size_mismatch")
        for m in methods:
            if m[3].startswith("comps"):
                m[2] = 0
    if len(comps) < MIN_COMPARABLES:
        guards.append("thin_set")

    voters = [m for m in methods if m[2] > 0]
    if not voters:
        return None
    lo = _weighted_median([(m[0], m[2]) for m in voters])
    hi = _weighted_median([(m[1], m[2]) for m in voters])
    est = round((lo + hi) / 2)

    # confidence mirror (documented approximation of the production model)
    conf = "medium"
    if len(comps) >= 15 and size_matched_n >= 5 and comp_tier != "town":
        conf = "high"
    if psqm_mid and comp_mid and abs(psqm_mid - comp_mid) / comp_mid > 0.25:
        conf = "medium" if conf == "high" else conf
    if "size_mismatch" in guards and conf == "high":
        conf = "medium"
    if "thin_set" in guards:
        conf = "low"

    err = (est - target["price"]) / target["price"] * 100
    # worst-row auto-classification per the brief
    note = ""
    if abs(err) > 15:
        if last_sale and (int(target["date"][:4]) * 12 + int(target["date"][5:7])) - \
                (int(last_sale["date"][:4]) * 12 + int(last_sale["date"][5:7])) <= 24 \
                and target["price"] > last_sale["price"] * 1.25:
            note = "renovation flip (resold >25% up within 24mo)"
        elif "thin_set" in guards:
            note = "thin data"
        elif "size_mismatch" in guards or (area is None and abs(err) > 20):
            note = "size-blind comps"
        elif comp_mid and target["price"] < 0.45 * comp_mid and est > target["price"] * 2:
            note = ("non-standard transaction? actual far below the local floor — "
                    "check lease/tenure/share (production's asking-anomaly gate "
                    "would flag this listing)")
        else:
            note = "genuine anomaly — review"

    return {
        "town": target["pc"].split(" ")[0] and target["pc"], "postcode": target["pc"],
        "address": f"{target['saon']} {target['paon']} {target['street']}".strip(),
        "date": target["date"], "ptype": target["ptype"],
        "actual": target["price"], "estimate": est,
        "err_pct": round(err, 1), "abs_err_pct": round(abs(err), 1),
        "confidence": conf, "n_comps": len(comps), "comp_tier": comp_tier,
        "size_matched_n": size_matched_n,
        "floor_area": area, "guards": "+".join(guards),
        "last_sale": f"{last_sale['price']}@{last_sale['date']}" if last_sale else "",
        "voters": ",".join(m[3] for m in voters), "note": note,
    }


def run(out_prefix):
    if not EPC_KEY:
        print("WARNING: EPC_API_KEY not set — £/m² method and size guard disabled")
    all_rows = []
    for town in TOWNS:
        sales = town_sales(town)
        targets = [s for s in sales if s["date"] >= "2026-01-01"
                   and s["ptype"] in PTYPE_OK and not s["new_build"] and s["paon"]]
        targets.sort(key=lambda s: s["price"])
        step = max(1, len(targets) // TARGETS_PER_TOWN)
        targets = targets[::step][:TARGETS_PER_TOWN]

        # shared town £/m² base: EPC-join the 40 most recent pre-2026 sales
        psqm_points = []
        for s in [x for x in sales if x["date"] < "2026-01-01" and x["ptype"] in PTYPE_OK][:40]:
            a = floor_area_for(s)
            if a:
                psqm_points.append({"psqm": s["price"] / a, "area": a,
                                    "month": s["date"][:7], "ptype": s["ptype"]})

        done = 0
        for t in targets:
            row = value_target(t, sales, psqm_points)
            if row:
                row["town"] = town
                all_rows.append(row)
                done += 1
        print(f"{town}: {len(sales)} sales, {done} targets valued, "
              f"{len(psqm_points)} £/m² points")

    all_rows.sort(key=lambda r: -r["abs_err_pct"])
    write_csv(all_rows, out_prefix + ".csv")
    write_html(all_rows, out_prefix + ".html")
    summarise(all_rows)


def summarise(rows):
    errs = [r["abs_err_pct"] for r in rows]
    print(f"\nn={len(rows)}  median abs err {statistics.median(errs):.1f}%  "
          f"within 10%: {sum(e <= 10 for e in errs) / len(errs) * 100:.0f}%  "
          f"within 20%: {sum(e <= 20 for e in errs) / len(errs) * 100:.0f}%")
    for tier in ("high", "medium", "low"):
        sub = [r["abs_err_pct"] for r in rows if r["confidence"] == tier]
        if sub:
            print(f"  {tier:6} n={len(sub):3}  median {statistics.median(sub):.1f}%  "
                  f"within10 {sum(e <= 10 for e in sub) / len(sub) * 100:.0f}%")
    for g in ("size_mismatch", "thin_set"):
        n = sum(1 for r in rows if g in r["guards"])
        print(f"  guard {g}: fired {n}/{len(rows)} ({n / len(rows) * 100:.0f}%)")


def write_csv(rows, path):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print("wrote", path)


def write_html(rows, path):
    errs = [r["abs_err_pct"] for r in rows]
    tiers = {t: [r["abs_err_pct"] for r in rows if r["confidence"] == t]
             for t in ("high", "medium", "low")}
    pct = lambda xs, cap: (sum(e <= cap for e in xs) / len(xs) * 100) if xs else 0  # noqa: E731

    def tile(label, value, sub=""):
        return (f'<div class="tile"><div class="t-label">{label}</div>'
                f'<div class="t-value">{value}</div><div class="t-sub">{sub}</div></div>')

    tier_rows = "".join(
        f"<tr><td>{t.upper()}</td><td>{len(v)}</td>"
        f"<td>{statistics.median(v):.1f}%</td><td>{pct(v,10):.0f}%</td><td>{pct(v,20):.0f}%</td></tr>"
        for t, v in tiers.items() if v)
    guard_rows = "".join(
        f"<tr><td>{g}</td><td>{sum(1 for r in rows if g in r['guards'])}</td>"
        f"<td>{sum(1 for r in rows if g in r['guards']) / len(rows) * 100:.0f}%</td></tr>"
        for g in ("size_mismatch", "thin_set"))
    body_rows = "".join(
        f"<tr><td>{r['town']}</td><td>{r['address']}<br><span class='muted'>{r['postcode']} · "
        f"{r['date']} · {r['ptype']}</span></td>"
        f"<td class='num'>£{r['actual']:,}</td><td class='num'>£{r['estimate']:,}</td>"
        f"<td class='num'>{r['err_pct']:+.1f}%</td><td>{r['confidence']}</td>"
        f"<td class='num'>{r['n_comps']}</td><td>{r['guards'] or '—'}</td>"
        f"<td>{r['note'] or ''}</td></tr>" for r in rows)

    html = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sold-price back-test — {len(rows)} held-out 2026 sales</title>
<style>
:root {{ --ink:#1e1c18; --mid:#5c5849; --muted:#9b9488; --line:#e0d9ce;
        --teal:#1a6b5a; --surface:#fff; --bg:#f7f3ed; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --ink:#ece8e1; --mid:#b3ada2; --muted:#7c766b; --line:#3a3831;
          --teal:#4fa78f; --surface:#232119; --bg:#1a1814; }} }}
body {{ font: 14px/1.5 system-ui, sans-serif; color: var(--ink);
       background: var(--bg); margin: 0; padding: 24px; }}
h1 {{ font-size: 19px; }} .sub {{ color: var(--mid); margin-bottom: 18px; }}
.tiles {{ display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 20px; }}
.tile {{ background: var(--surface); border: 1px solid var(--line);
        border-radius: 10px; padding: 12px 18px; min-width: 130px; }}
.t-label {{ font-size: 11px; text-transform: uppercase; letter-spacing: .07em;
           color: var(--muted); }}
.t-value {{ font-size: 24px; font-weight: 700; }}
.t-sub {{ font-size: 12px; color: var(--mid); }}
table {{ border-collapse: collapse; width: 100%; background: var(--surface);
        border: 1px solid var(--line); border-radius: 10px; margin-bottom: 22px; }}
th, td {{ text-align: left; padding: 7px 10px; border-top: 1px solid var(--line);
         vertical-align: top; }}
th {{ font-size: 11px; text-transform: uppercase; letter-spacing: .06em;
     color: var(--muted); border-top: 0; }}
.num {{ font-variant-numeric: tabular-nums; text-align: right; }}
.muted {{ color: var(--muted); font-size: 12px; }}
.wrap {{ overflow-x: auto; }}
</style></head><body>
<h1>Sold-price back-test — held-out 2026 completions</h1>
<div class="sub">Engine valued each sale using only evidence dated before its
completion; scored against the actual price paid. Zero PropertyData credits
(Land Registry + EPC + bundled HPI). Sorted worst-first.</div>
<div class="tiles">
{tile("Held-out sales", len(rows))}
{tile("Median abs error", f"{statistics.median(errs):.1f}%")}
{tile("Within 10%", f"{pct(errs,10):.0f}%")}
{tile("Within 20%", f"{pct(errs,20):.0f}%")}
</div>
<h2 style="font-size:15px">Accuracy by confidence tier</h2>
<div class="wrap"><table><tr><th>Tier</th><th>n</th><th>Median abs err</th>
<th>Within 10%</th><th>Within 20%</th></tr>{tier_rows}</table></div>
<h2 style="font-size:15px">Guardrail fire rates</h2>
<div class="wrap"><table><tr><th>Guard</th><th>Fired</th><th>Rate</th></tr>{guard_rows}</table></div>
<h2 style="font-size:15px">All rows (worst first)</h2>
<div class="wrap"><table><tr><th>Town</th><th>Property</th><th>Actual</th>
<th>Estimate</th><th>Error</th><th>Conf</th><th>Comps</th><th>Guards</th>
<th>Note</th></tr>{body_rows}</table></div>
</body></html>"""
    with open(path, "w") as f:
        f.write(html)
    print("wrote", path)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="backtest_results")
    ap.add_argument("--towns", help="comma list to override the 12 defaults")
    args = ap.parse_args()
    if args.towns:
        TOWNS = [t.strip().upper() for t in args.towns.split(",")]
    run(args.out)
