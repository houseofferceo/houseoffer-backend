import os
import re
import requests
import smtplib
import threading
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from datetime import datetime

app = Flask(__name__)
CORS(app, origins=["https://houseoffer.netlify.app", "https://offerright.co.uk", "http://localhost:3000"])

PROPERTYDATA_API_KEY = os.environ.get("PROPERTYDATA_API_KEY")
EPC_API_KEY = os.environ.get("EPC_API_KEY")
EMAIL_ADDRESS = os.environ.get("EMAIL_ADDRESS")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
MIN_COMPARABLES = 3

# ── POSTCODE UTILITIES ─────────────────────────────────────────────────────────

def extract_postcode_from_url(url):
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
    raw = raw.strip().upper().replace(" ", "")
    return raw[:-3] + " " + raw[-3:]


def district_postcode(postcode):
    return postcode.strip().upper().replace(" ", "")[:-3]


def normalise_type_sold(property_type):
    """Property type strings as they appear in /sold-prices responses."""
    mapping = {
        "semi-detached": ["semi-detached_house", "semi_detached_house", "Semi-Detached"],
        "detached":      ["detached_house", "Detached"],
        "terraced":      ["terraced_house", "Terraced"],
        "flat":          ["flat", "Flat"],
    }
    return mapping.get(property_type.lower(), ["semi-detached_house", "semi_detached_house"])


def normalise_type_listings(property_type):
    """Property type strings as they appear in /prices-per-sqf responses."""
    mapping = {
        "semi-detached": ["semi-detached_house", "semi_detached_house"],
        "detached":      ["detached_house"],
        "terraced":      ["terraced_house"],
        "flat":          ["flat"],
    }
    return mapping.get(property_type.lower(), ["semi-detached_house", "semi_detached_house"])


def price_per_sqft_to_sqm(price_per_sqft):
    return price_per_sqft * 10.764


# ── EPC FLOOR AREA ─────────────────────────────────────────────────────────────

def get_floor_area_from_epc(postcode, address=None):
    """
    Look up floor area (m²) from the EPC register.
    Falls back gracefully if not found.
    """
    try:
        formatted = format_postcode(postcode)
        url = "https://epc.opendatacommunities.org/api/v1/domestic/search"
        params = {"postcode": formatted, "size": 10}
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {EPC_API_KEY}"
        }
        r = requests.get(url, params=params, headers=headers, timeout=10)
        if r.status_code != 200:
            return None
        rows = r.json().get("rows", [])
        if not rows:
            return None
        # If we have an address, try to match it
        if address:
            address_upper = address.upper()
            for row in rows:
                if any(part in row.get("address", "").upper() for part in address_upper.split()[:2]):
                    area = row.get("total-floor-area")
                    if area:
                        return float(area)
        # Otherwise return first result
        area = rows[0].get("total-floor-area")
        return float(area) if area else None
    except Exception:
        return None


# ── SOLD PRICE COMPARABLES ─────────────────────────────────────────────────────

def fetch_sold_prices(postcode):
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


def fetch_listings_psqf(postcode):
    """Fetch current listings with price per sqft — used only for £/sqm conversion."""
    try:
        r = requests.get(
            "https://api.propertydata.co.uk/prices-per-sqf",
            params={"key": PROPERTYDATA_API_KEY, "postcode": postcode},
            timeout=10
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def get_sold_comparables(postcode, property_type):
    """
    Get Land Registry sold price comparables for matching property type only.
    Auto-broadens from full postcode to district if fewer than MIN_COMPARABLES found.
    Never mixes property types.
    """
    type_keys = normalise_type_sold(property_type)
    formatted = format_postcode(postcode)

    data = fetch_sold_prices(formatted)
    comparables = _filter_sold(data, type_keys)

    broadened = False
    postcode_used = formatted
    if len(comparables) < MIN_COMPARABLES:
        district = district_postcode(postcode)
        data = fetch_sold_prices(district)
        comparables = _filter_sold(data, type_keys)
        broadened = True
        postcode_used = district

    return comparables, postcode_used, broadened


def _filter_sold(data, type_keys):
    if not data:
        return []
    try:
        transactions = data.get("data", {}).get("raw_data", [])
        matching = [
            t for t in transactions
            if t.get("type") in type_keys
            and t.get("price")
            and t.get("price") < 2_000_000
        ]
        return matching
    except Exception:
        return []


def avg_sold_price(comparables):
    """Use interquartile mean to reduce skew from outliers."""
    if not comparables:
        return None
    prices = sorted(c["price"] for c in comparables)
    n = len(prices)
    if n >= 5:
        q1 = n // 4
        q3 = n - q1
        trimmed = prices[q1:q3]
        return round(sum(trimmed) / len(trimmed)) if trimmed else round(sum(prices) / n)
    return round(sum(prices) / n)


# ── £/SQM FROM LISTINGS ────────────────────────────────────────────────────────

def get_local_avg_psqm(postcode, property_type):
    """
    Calculate local average £/sqm from current listings (prices-per-sqf endpoint).
    Filtered strictly by property type. Auto-broadens if needed.
    Used only as a conversion benchmark — not as the primary valuation.
    """
    type_keys = normalise_type_listings(property_type)
    formatted = format_postcode(postcode)

    data = fetch_listings_psqf(formatted)
    avg = _calc_avg_psqm(data, type_keys)

    if avg is None:
        district = district_postcode(postcode)
        data = fetch_listings_psqf(district)
        avg = _calc_avg_psqm(data, type_keys)

    return avg


def _calc_avg_psqm(data, type_keys):
    if not data:
        return None
    try:
        points = data.get("data", {}).get("raw_data", [])
        matching = [
            p for p in points
            if p.get("type") in type_keys and p.get("price_per_sqf")
        ]
        if not matching:
            return None
        avg_psqf = sum(p["price_per_sqf"] for p in matching) / len(matching)
        return round(price_per_sqft_to_sqm(avg_psqf))
    except Exception:
        return None


# ── REPORT BUILDER ─────────────────────────────────────────────────────────────

def build_report_data(property_url, asking_price, bedrooms, property_type,
                      postcode, floor_area_sqm=None, address=None):

    formatted = format_postcode(postcode)

    # 1. Land Registry sold price comparables (same property type only)
    comparables, postcode_used, broadened = get_sold_comparables(postcode, property_type)
    local_avg_sold = avg_sold_price(comparables)

    # Sold price comparison
    sold_diff_pct = None
    sold_verdict = None
    if local_avg_sold:
        sold_diff_pct = round(((asking_price - local_avg_sold) / local_avg_sold) * 100, 1)
        if sold_diff_pct > 8:
            sold_verdict = "overpriced"
        elif sold_diff_pct < -5:
            sold_verdict = "value"
        else:
            sold_verdict = "fair"

    # 2. Floor area — use provided value or look up from EPC
    if not floor_area_sqm and EPC_API_KEY:
        floor_area_sqm = get_floor_area_from_epc(postcode, address)

    # 3. £/sqm comparison
    asking_psqm = None
    local_avg_psqm = None
    psqm_diff_pct = None
    psqm_verdict = None

    if floor_area_sqm and floor_area_sqm > 0:
        asking_psqm = round(asking_price / floor_area_sqm)
        local_avg_psqm = get_local_avg_psqm(postcode, property_type)
        if local_avg_psqm:
            psqm_diff_pct = round(((asking_psqm - local_avg_psqm) / local_avg_psqm) * 100, 1)
            if psqm_diff_pct > 8:
                psqm_verdict = "overpriced"
            elif psqm_diff_pct < -5:
                psqm_verdict = "value"
            else:
                psqm_verdict = "fair"

    # Overall verdict — sold price comparison takes priority as it's Land Registry
    verdict = sold_verdict or psqm_verdict or "unknown"
    diff_pct = sold_diff_pct if sold_diff_pct is not None else psqm_diff_pct or 0

    return {
        "postcode": formatted,
        "postcode_used": postcode_used,
        "comparables_count": len(comparables),
        "search_broadened": broadened,
        "asking_price": asking_price,
        "asking_price_formatted": f"£{asking_price:,}",
        "bedrooms": bedrooms,
        "property_type": property_type,
        "floor_area_sqm": floor_area_sqm,
        # Sold price comparison
        "local_avg_sold": local_avg_sold,
        "local_avg_sold_formatted": f"£{local_avg_sold:,}" if local_avg_sold else None,
        "sold_diff_pct": sold_diff_pct,
        "sold_verdict": sold_verdict,
        # £/sqm comparison
        "asking_psqm": asking_psqm,
        "local_avg_psqm": local_avg_psqm,
        "psqm_diff_pct": psqm_diff_pct,
        "psqm_verdict": psqm_verdict,
        # Overall
        "verdict": verdict,
        "diff_pct": diff_pct,
        "days_on_market": None,
        "local_avg_dom": None,
        "dom_signal": None,
        "generated": datetime.now().strftime("%-d %B %Y"),
        "property_url": property_url,
    }



# ── EMAIL ──────────────────────────────────────────────────────────────────────

def send_report_email(to_email, report_html, postcode, verdict):
    """Send the free report as an HTML email via Resend."""
    try:
        plain = f"""Hi,

Here is your free HouseOffer property report for {postcode}.

Our verdict: {verdict.upper()}

To unlock your recommended offer price, walk-away price, and negotiation script, visit:
https://houseoffer.netlify.app/#pricing

The HouseOffer team
"""
        r = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "from": f"HouseOffer <{EMAIL_ADDRESS}>",
                "to": [to_email],
                "subject": f"Your free HouseOffer report — {postcode}",
                "html": report_html,
                "text": plain
            }
        )
        print(f"Resend response: {r.status_code} {r.text}")
        return r.status_code == 200
    except Exception as e:
        print(f"Email error: {e}")
        return False


# ── ROUTES ─────────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/test-epc")
def test_epc():
    """Test endpoint to check EPC API is working."""
    postcode = request.args.get("postcode", "DE1 1DR")
    try:
        formatted = format_postcode(postcode)
        url = "https://epc.opendatacommunities.org/api/v1/domestic/search"
        params = {"postcode": formatted, "size": 3}
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {EPC_API_KEY}"
        }
        r = requests.get(url, params=params, headers=headers, timeout=10)
        return jsonify({
            "status_code": r.status_code,
            "epc_key_set": bool(EPC_API_KEY),
            "response": r.json() if r.status_code == 200 else r.text
        })
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/report", methods=["POST"])
def generate_report():
    data = request.get_json(silent=True) or request.form
    property_url   = data.get("property_url", "")
    asking_price   = int(str(data.get("asking_price", 0)).replace(",", "").replace("£", ""))
    bedrooms       = data.get("bedrooms", "3")
    property_type  = data.get("property_type", "semi-detached")
    postcode       = data.get("postcode", "")
    address        = data.get("address", "")
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
        address=address,
    )
    return render_template("report_free.html", **report)


@app.route("/api/report-data", methods=["POST"])
def report_data_json():
    data = request.get_json(silent=True) or request.form
    property_url   = data.get("property_url", "")
    asking_price   = int(str(data.get("asking_price", 0)).replace(",", "").replace("£", ""))
    bedrooms       = data.get("bedrooms", "3")
    property_type  = data.get("property_type", "semi-detached")
    postcode       = data.get("postcode", "")
    address        = data.get("address", "")
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
        address=address,
    )
    return jsonify(report)




@app.route("/submit", methods=["POST"])
def submit():
    """
    Main endpoint — responds immediately, processes report in background thread.
    """
    data = request.get_json(silent=True) or request.form

    to_email       = data.get("email", "")
    property_url   = data.get("property-url", "") or data.get("property_url", "")
    asking_price   = int(str(data.get("asking_price", 0) or 0).replace(",", "").replace("£", "")) or 0
    bedrooms       = data.get("bedrooms", "3")
    property_type  = data.get("property_type", "semi-detached")
    postcode       = data.get("postcode", "")
    address        = data.get("address", "")
    floor_area_sqm = float(data.get("floor_area_sqm", 0) or 0) or None

    if not to_email:
        return jsonify({"error": "Email address required"}), 400

    # Respond immediately — process in background
    def process():
        pc = postcode
        if not pc and property_url:
            pc = extract_postcode_from_url(property_url)

        if not pc:
            send_holding_email(to_email, property_url)
            return

        try:
            report = build_report_data(
                property_url=property_url,
                asking_price=asking_price,
                bedrooms=bedrooms,
                property_type=property_type,
                postcode=pc,
                floor_area_sqm=floor_area_sqm,
                address=address,
            )
            with app.app_context():
                report_html = render_template("report_free.html", **report)
            send_report_email(to_email, report_html, report["postcode"], report["verdict"])
            notify_owner(to_email, property_url, report["postcode"], report["verdict"])
        except Exception as e:
            print(f"Background processing error: {e}")
            send_holding_email(to_email, property_url)

    thread = threading.Thread(target=process)
    thread.daemon = True
    thread.start()

    return jsonify({"status": "sent"})


def send_holding_email(to_email, property_url):
    """Send when we can't extract a postcode — asks user to reply with details."""
    try:
        requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "from": f"HouseOffer <{EMAIL_ADDRESS}>",
                "to": [to_email],
                "subject": "Your HouseOffer report — we need one more detail",
                "text": f"""Hi,

Thanks for submitting your property for analysis.

We weren't able to automatically extract the postcode from the link you provided:
{property_url}

Could you reply to this email with:
1. The property postcode
2. The asking price
3. Number of bedrooms
4. Property type (detached / semi-detached / terraced / flat)

We'll have your free report back to you within a few hours.

The HouseOffer team"""
            }
        )
    except Exception as e:
        print(f"Holding email error: {e}")


def notify_owner(to_email, property_url, postcode, verdict):
    """Notify the houseoffer inbox of each new submission via Resend."""
    try:
        requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "from": f"HouseOffer <{EMAIL_ADDRESS}>",
                "to": [EMAIL_ADDRESS],
                "subject": f"New submission: {postcode} — {verdict}",
                "text": f"New report request\n\nUser: {to_email}\nProperty: {property_url}\nPostcode: {postcode}\nVerdict: {verdict}"
            }
        )
    except Exception as e:
        print(f"Owner notify error: {e}")


if __name__ == "__main__":
    app.run(debug=True, port=5000)
