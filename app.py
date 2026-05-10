import os
import re
import requests
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from datetime import datetime

app = Flask(__name__)
CORS(app)

PROPERTYDATA_API_KEY = os.environ.get("PROPERTYDATA_API_KEY")
MIN_COMPARABLES = 3  # minimum sold price matches before we broaden the search

# ── UTILITIES ──────────────────────────────────────────────────────────────────

def extract_postcode_from_url(url):
    """Try to pull a postcode from a Rightmove or Zoopla URL or page."""
    pc_pattern = r'([A-Z]{1,2}\d{1,2}[A-Z]?\s?\d[A-Z]{2})'
    match = re.search(pc_pattern, url.upper())
    if match:
        return match.group(1).replace(" ", "").upper()
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; HouseOffer/1.0)"}
        resp = requests.get(url, headers=headers, timeout=10)
        match = re.search(pc_pattern, resp.text.upper())
        if match:
            return match.group(1).replace(" ", "").upper()
    except Exception:
        pass
    return None


def format_postcode(raw):
    """Ensure postcode has a space: DE11DR -> DE1 1DR."""
    raw = raw.strip().upper().replace(" ", "")
    return raw[:-3] + " " + raw[-3:]


def district_postcode(full_postcode):
    """Extract district from full postcode: DE1 1DR -> DE1."""
    return full_postcode.strip().upper().replace(" ", "")[:-3]


def normalise_type(property_type):
    """Map user-facing property type to PropertyData sold-prices type strings."""
    mapping = {
        "semi-detached": ["semi_detached", "Semi-Detached"],
        "detached":      ["detached", "Detached"],
        "terraced":      ["terraced", "Terraced"],
        "flat":          ["flat", "Flat"],
    }
    return mapping.get(property_type.lower(), ["semi_detached", "Semi-Detached"])


def price_per_sqft_to_sqm(price_per_sqft):
    """Convert £/sqft to £/sqm."""
    return price_per_sqft * 10.764


# ── DATA FETCHING ──────────────────────────────────────────────────────────────

def fetch_sold_prices(postcode):
    """Fetch Land Registry sold prices from PropertyData."""
    try:
        r = requests.get(
            "https://api.propertydata.co.uk/sold-prices",
            params={"key": PROPERTYDATA_API_KEY, "postcode": postcode},
            timeout=10
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def get_comparables(postcode, property_type):
    """
    Get sold price comparables for the given property type.
    Strategy:
      1. Try full postcode (e.g. DE1 1DR) — most precise
      2. If fewer than MIN_COMPARABLES matching sales, broaden to district (e.g. DE1)
      3. Never mix property types — flats never compared against houses
    Returns (comparables list, postcode used, broadened bool)
    """
    type_keys = normalise_type(property_type)
    formatted = format_postcode(postcode)

    # Step 1: full postcode
    data = fetch_sold_prices(formatted)
    comparables = extract_matching_sales(data, type_keys)

    if len(comparables) >= MIN_COMPARABLES:
        return comparables, formatted, False

    # Step 2: broaden to district postcode
    district = district_postcode(postcode)
    data = fetch_sold_prices(district)
    comparables = extract_matching_sales(data, type_keys)

    return comparables, district, True


def extract_matching_sales(data, type_keys):
    """
    Pull matching sold transactions from a PropertyData /sold-prices response.
    Only include records that have a valid price_per_sqf so we can do £/sqm maths.
    """
    if not data:
        return []
    try:
        transactions = data.get("data", {}).get("transactions", [])
        matching = [
            t for t in transactions
            if t.get("property_type") in type_keys
            and t.get("price_per_sqf") and float(t["price_per_sqf"]) > 0
        ]
        return matching
    except Exception:
        return []


def calculate_local_avg_psqm(comparables):
    """Average £/sqm from a list of comparable sold transactions."""
    if not comparables:
        return None
    avg_psqf = sum(float(c["price_per_sqf"]) for c in comparables) / len(comparables)
    return round(price_per_sqft_to_sqm(avg_psqf))


def calculate_verdict(asking_psqm, local_avg_psqm):
    """Return verdict and % difference vs local average."""
    if not local_avg_psqm or local_avg_psqm == 0:
        return "unknown", 0
    diff_pct = ((asking_psqm - local_avg_psqm) / local_avg_psqm) * 100
    if diff_pct > 8:
        verdict = "overpriced"
    elif diff_pct < -5:
        verdict = "value"
    else:
        verdict = "fair"
    return verdict, round(diff_pct, 1)


# ── REPORT BUILDER ─────────────────────────────────────────────────────────────

def build_report_data(property_url, asking_price, bedrooms, property_type, postcode, floor_area_sqm=None):
    """
    Pull sold price comparables and build the report data dict.
    Uses Land Registry sold prices only — never listing/asking prices.
    Automatically broadens from full postcode to district if not enough comparables.
    Never mixes property types.
    """
    comparables, postcode_used, broadened = get_comparables(postcode, property_type)
    local_avg_psqm = calculate_local_avg_psqm(comparables)

    # Asking price per sqm (requires floor area)
    asking_psqm = None
    if floor_area_sqm and floor_area_sqm > 0:
        asking_psqm = round(asking_price / floor_area_sqm)

    verdict = "unknown"
    diff_pct = 0
    if asking_psqm and local_avg_psqm:
        verdict, diff_pct = calculate_verdict(asking_psqm, local_avg_psqm)

    return {
        "postcode": format_postcode(postcode),
        "postcode_used": postcode_used,
        "comparables_count": len(comparables),
        "search_broadened": broadened,
        "asking_price": asking_price,
        "asking_price_formatted": f"£{asking_price:,}",
        "bedrooms": bedrooms,
        "property_type": property_type,
        "asking_psqm": asking_psqm,
        "local_avg_psqm": local_avg_psqm,
        "verdict": verdict,
        "diff_pct": diff_pct,
        "days_on_market": None,   # future: wire up listing data
        "local_avg_dom": None,
        "dom_signal": None,
        "generated": datetime.now().strftime("%-d %B %Y"),
        "property_url": property_url,
    }


# ── ROUTES ─────────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/report", methods=["POST"])
def generate_report():
    """Returns rendered HTML report."""
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
        return jsonify({"error": "Could not determine postcode."}), 400

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
    """Returns raw JSON — for testing."""
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
