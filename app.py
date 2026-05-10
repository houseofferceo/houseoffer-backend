import os
import re
import requests
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from datetime import datetime

app = Flask(__name__)
CORS(app)

PROPERTYDATA_API_KEY = os.environ.get("PROPERTYDATA_API_KEY")

# ── UTILITIES ──────────────────────────────────────────────────────────────────

def extract_postcode_from_url(url):
    """
    Try to pull a postcode from a Rightmove or Zoopla URL or page.
    Rightmove property IDs look like: rightmove.co.uk/properties/12345678
    We scrape just enough to get the postcode.
    """
    # First try: postcode directly in URL (rare but possible)
    pc_pattern = r'([A-Z]{1,2}\d{1,2}[A-Z]?\s?\d[A-Z]{2})'
    match = re.search(pc_pattern, url.upper())
    if match:
        return match.group(1).replace(" ", "").upper()

    # Second try: fetch the page and extract postcode from HTML
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; HouseOffer/1.0)"}
        resp = requests.get(url, headers=headers, timeout=10)
        html = resp.text
        match = re.search(pc_pattern, html.upper())
        if match:
            return match.group(1).replace(" ", "").upper()
    except Exception:
        pass

    return None


def format_postcode(raw):
    """Ensure postcode has a space in the right place for API calls."""
    raw = raw.strip().upper().replace(" ", "")
    return raw[:-3] + " " + raw[-3:]


def get_property_data(postcode):
    """
    Call PropertyData API and return the data we need for the free report.
    Endpoints used:
      - /prices-per-sqf  → local £/sqft averages by property type
      - /sold-prices     → recent sales on the street
      - /prices          → days on market / listing signals
    """
    formatted = format_postcode(postcode)
    base = "https://api.propertydata.co.uk"
    headers = {}  # API key goes in query params for PropertyData

    results = {}

    # 1. Price per sq ft by property type in this postcode
    r1 = requests.get(
        f"{base}/prices-per-sqf",
        params={"key": PROPERTYDATA_API_KEY, "postcode": formatted},
        timeout=10
    )
    if r1.status_code == 200:
        results["prices_per_sqf"] = r1.json()

    # 2. Recent sold prices
    r2 = requests.get(
        f"{base}/sold-prices",
        params={"key": PROPERTYDATA_API_KEY, "postcode": formatted},
        timeout=10
    )
    if r2.status_code == 200:
        results["sold_prices"] = r2.json()

    # 3. Current listing data (days on market signals)
    r3 = requests.get(
        f"{base}/prices",
        params={"key": PROPERTYDATA_API_KEY, "postcode": formatted},
        timeout=10
    )
    if r3.status_code == 200:
        results["listing_prices"] = r3.json()

    return results


def calculate_verdict(asking_psqm, local_avg_psqm):
    """Return verdict string and percentage difference."""
    if local_avg_psqm == 0:
        return "unknown", 0
    diff_pct = ((asking_psqm - local_avg_psqm) / local_avg_psqm) * 100
    if diff_pct > 8:
        verdict = "overpriced"
    elif diff_pct < -5:
        verdict = "value"
    else:
        verdict = "fair"
    return verdict, round(diff_pct, 1)


def sqft_to_sqm(sqft):
    return round(sqft * 0.0929, 1)


def build_report_data(property_url, asking_price, bedrooms, property_type, postcode, floor_area_sqm=None):
    """
    Pull data and build the dict that populates the report template.
    asking_price: int (e.g. 285000)
    floor_area_sqm: float or None — if None we can't compute £/sqm for this property
    """
    raw_data = get_property_data(postcode)

    # Extract local avg price per sqft → convert to sqm
    local_psqf = None
    try:
        psqf_data = raw_data.get("prices_per_sqf", {})
        # PropertyData returns data by property type: detached, semi_detached, terraced, flat
        type_key = {
            "semi-detached": "semi_detached",
            "detached": "detached",
            "terraced": "terraced",
            "flat": "flat",
        }.get(property_type.lower(), "semi_detached")
        local_psqf = psqf_data.get("data", {}).get(type_key, {}).get("avg")
    except Exception:
        pass

    local_avg_psqm = sqft_to_sqm(local_psqf) if local_psqf else None

    # Asking price per sqm (only if we have floor area)
    asking_psqm = None
    if floor_area_sqm and floor_area_sqm > 0:
        asking_psqm = round(asking_price / floor_area_sqm)

    verdict = "unknown"
    diff_pct = 0
    if asking_psqm and local_avg_psqm:
        verdict, diff_pct = calculate_verdict(asking_psqm, local_avg_psqm)

    # Days on market
    dom = None
    local_avg_dom = None
    try:
        listing_data = raw_data.get("listing_prices", {}).get("data", {})
        dom = listing_data.get("days_on_market")
        local_avg_dom = listing_data.get("avg_days_on_market")
    except Exception:
        pass

    dom_signal = None
    if dom and local_avg_dom:
        if dom > local_avg_dom * 1.5:
            dom_signal = "high"   # strong negotiation signal
        elif dom > local_avg_dom:
            dom_signal = "medium"
        else:
            dom_signal = "low"

    return {
        "postcode": format_postcode(postcode),
        "asking_price": asking_price,
        "asking_price_formatted": f"£{asking_price:,}",
        "bedrooms": bedrooms,
        "property_type": property_type,
        "asking_psqm": asking_psqm,
        "local_avg_psqm": round(local_avg_psqm) if local_avg_psqm else None,
        "verdict": verdict,
        "diff_pct": diff_pct,
        "days_on_market": dom,
        "local_avg_dom": local_avg_dom,
        "dom_signal": dom_signal,
        "generated": datetime.now().strftime("%-d %B %Y"),
        "property_url": property_url,
    }


# ── ROUTES ─────────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/report", methods=["POST"])
def generate_report():
    """
    Netlify form POSTs to here (via webhook) or we call directly.
    Expects JSON: { property_url, asking_price, bedrooms, property_type, postcode, floor_area_sqm }
    Returns: rendered HTML report
    """
    data = request.get_json(silent=True) or request.form

    property_url   = data.get("property_url", "")
    asking_price   = int(str(data.get("asking_price", 0)).replace(",", "").replace("£", ""))
    bedrooms       = data.get("bedrooms", "3")
    property_type  = data.get("property_type", "semi-detached")
    postcode       = data.get("postcode", "")
    floor_area_sqm = float(data.get("floor_area_sqm", 0) or 0) or None

    # If no postcode supplied, try extracting from URL
    if not postcode and property_url:
        postcode = extract_postcode_from_url(property_url)

    if not postcode:
        return jsonify({"error": "Could not determine postcode from URL. Please supply it manually."}), 400

    report = build_report_data(
        property_url=property_url,
        asking_price=asking_price,
        bedrooms=bedrooms,
        property_type=property_type,
        postcode=postcode,
        floor_area_sqm=floor_area_sqm,
    )

    return render_template("report_free.html", **report)


@app.route("/api/report-data", methods=["POST"])
def report_data_json():
    """Same as above but returns raw JSON — useful for testing."""
    data = request.get_json(silent=True) or request.form
    property_url   = data.get("property_url", "")
    asking_price   = int(str(data.get("asking_price", 0)).replace(",", "").replace("£", ""))
    bedrooms       = data.get("bedrooms", "3")
    property_type  = data.get("property_type", "semi-detached")
    postcode       = data.get("postcode", "")
    floor_area_sqm = float(data.get("floor_area_sqm", 0) or 0) or None

    if not postcode and property_url:
        postcode = extract_postcode_from_url(property_url)

    if not postcode:
        return jsonify({"error": "Could not determine postcode"}), 400

    report = build_report_data(
        property_url=property_url,
        asking_price=asking_price,
        bedrooms=bedrooms,
        property_type=property_type,
        postcode=postcode,
        floor_area_sqm=floor_area_sqm,
    )
    return jsonify(report)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
