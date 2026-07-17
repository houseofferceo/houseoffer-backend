"""Regenerate hpi_data.py from the official UK House Price Index.

Source: HM Land Registry UKHPI (landregistry.data.gov.uk SPARQL), semi-detached
monthly index per region. Free, no key, no PropertyData credits.

Run from the repo root:  python3 tools/refresh_hpi.py
Writes hpi_data.py in place; review the git diff before committing.

Written 2026-07-17 after the Wemborough incident review found the bundled
table's recent shape disagreed with the official series (London semis
2024-01→2026-01: bundled +12.7% vs official +4.6%) as well as ending at
2026-01. The index base doesn't matter to the app (it uses ratios), so this
writes the official series as-is, monthly, without rebasing.
"""
import json
import urllib.parse
import urllib.request
from datetime import date

REGIONS = [
    "east-of-england", "london", "south-east", "south-west",
    "west-midlands-region", "east-midlands", "north-west",
    "yorkshire-and-the-humber", "north-east", "wales", "england",
]
ENDPOINT = "https://landregistry.data.gov.uk/landregistry/query"
FROM_MONTH = "2010-01"


def fetch_region(region):
    q = f"""PREFIX ukhpi: <http://landregistry.data.gov.uk/def/ukhpi/>
SELECT ?month ?idx WHERE {{
  ?obs ukhpi:refRegion <http://landregistry.data.gov.uk/id/region/{region}> ;
       ukhpi:refMonth ?month ;
       ukhpi:housePriceIndexSemiDetached ?idx .
  FILTER(str(?month) >= "{FROM_MONTH}")
}} ORDER BY ?month"""
    url = ENDPOINT + "?" + urllib.parse.urlencode({"query": q})
    req = urllib.request.Request(url, headers={"Accept": "application/sparql-results+json"})
    rows = json.load(urllib.request.urlopen(req, timeout=60))["results"]["bindings"]
    series = {b["month"]["value"]: float(b["idx"]["value"]) for b in rows}
    if not series:
        raise RuntimeError(f"no UKHPI rows for region {region!r}")
    return series


def main():
    data = {}
    for r in REGIONS:
        data[r] = fetch_region(r)
        print(f"{r}: {len(data[r])} months, {min(data[r])} → {max(data[r])}")

    lines = [
        "# UK House Price Index — semi-detached index, MONTHLY, official series as",
        "# published (no rebasing: the app only ever uses ratios between months).",
        "# Source: HM Land Registry UKHPI via landregistry.data.gov.uk SPARQL.",
        "# Regenerate with: python3 tools/refresh_hpi.py",
        f"# Last updated: {date.today().isoformat()}",
        "",
        "HPI_SEMI_DETACHED = {",
    ]
    for r in REGIONS:
        lines.append(f'    "{r}": {{')
        months = sorted(data[r])
        for i in range(0, len(months), 6):
            chunk = ", ".join(f'"{m}": {data[r][m]}' for m in months[i:i + 6])
            lines.append(f"        {chunk},")
        lines.append("    },")
    lines.append("}")
    lines.append("""

def get_hpi_index(region, year_month):
    \"\"\"
    Get HPI index for a region and month.
    Interpolates linearly between data points for missing months (the official
    series is monthly, so this only matters at the edges). A month after the
    table's end returns the latest value (clamped) rather than None — comps
    sold after the last published month were previously skipping HPI
    adjustment entirely.
    Falls back to 'england' if region not found.
    \"\"\"
    data = HPI_SEMI_DETACHED.get(region) or HPI_SEMI_DETACHED.get("england")
    if not data:
        return None

    if year_month in data:
        return data[year_month]

    all_months = sorted(data.keys())
    before = [m for m in all_months if m <= year_month]
    after = [m for m in all_months if m >= year_month]

    if not before:
        return None
    if not after:
        # Clamp forward: the newest published month stands in for later dates.
        return data[all_months[-1]]

    m1, m2 = before[-1], after[0]
    if m1 == m2:
        return data[m1]

    def months_since_2000(ym):
        y, m = ym.split("-")
        return int(y) * 12 + int(m)

    t1, t2, t = months_since_2000(m1), months_since_2000(m2), months_since_2000(year_month)
    fraction = (t - t1) / (t2 - t1)
    return round(data[m1] + (data[m2] - data[m1]) * fraction, 1)


def get_current_hpi(region):
    \"\"\"Get the most recent HPI index for a region.\"\"\"
    data = HPI_SEMI_DETACHED.get(region) or HPI_SEMI_DETACHED.get("england")
    if not data:
        return None, None
    latest_month = sorted(data.keys())[-1]
    return data[latest_month], latest_month
""")
    with open("hpi_data.py", "w") as f:
        f.write("\n".join(lines))
    print("hpi_data.py written")


if __name__ == "__main__":
    main()
