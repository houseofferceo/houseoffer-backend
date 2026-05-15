import os
import re
import json
import requests
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from datetime import datetime

app = Flask(__name__)
CORS(app, origins=["https://houseoffer.netlify.app", "https://offerright.co.uk", "http://localhost:3000"])

PROPERTYDATA_API_KEY = os.environ.get("PROPERTYDATA_API_KEY")
EPC_API_KEY = os.environ.get("EPC_API_KEY")
EMAIL_ADDRESS = os.environ.get("EMAIL_ADDRESS")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
MIN_COMPARABLES = 10

def format_postcode(raw):
    raw = raw.strip().upper().replace(" ", "")
    return raw[:-3] + " " + raw[-3:]

def district_postcode(postcode):
    return postcode.strip().upper().replace(" ", "")[:-3]

def normalise_type_sold(property_type):
    mapping = {
        "semi-detached": ["semi-detached_house", "semi_detached_house", "Semi-Detached"],
        "detached":      ["detached_house", "Detached"],
        "terraced":      ["terraced_house", "Terraced"],
        "flat":          ["flat", "Flat"],
    }
    return mapping.get(property_type.lower(), ["semi-detached_house", "semi_detached_house"])

def normalise_type_listings(property_type):
    mapping = {
        "semi-detached": ["semi-detached_house", "semi_detached_house"],
        "detached":      ["detached_house"],
        "terraced":      ["terraced_house"],
        "flat":          ["flat"],
    }
    return mapping.get(property_type.lower(), ["semi-detached_house", "semi_detached_house"])

def price_per_sqft_to_sqm(p):
    return p * 10.764

def scrape_rightmove(url):
    result = {"postcode": None, "asking_price": 0, "bedrooms": 3, "property_type": "semi-detached"}
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        resp = requests.get(url, headers=headers, timeout=15)
        html = resp.text
        json_match = re.search(r'window\.PAGE_MODEL\s*=\s*(\{.*?\});', html, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(1))
                prop = data.get("propertyData", {})
                price = prop.get("prices", {}).get("primaryPrice", "")
                if price:
                    result["asking_price"] = int(re.sub(r"[^0-9]", "", str(price)))
                beds = prop.get("bedrooms")
                if beds:
                    result["bedrooms"] = int(beds)
                ptype = (prop.get("propertySubType", "") or prop.get("propertyType", "")).lower()
                if "semi" in ptype:
                    result["property_type"] = "semi-detached"
                elif "detached" in ptype:
                    result["property_type"] = "detached"
                elif "terraced" in ptype:
                    result["property_type"] = "terraced"
                elif "flat" in ptype or "apartment" in ptype:
                    result["property_type"] = "flat"
                addr = prop.get("address", {})
                pc = addr.get("outcode", "") + addr.get("incode", "")
                if pc:
                    result["postcode"] = pc
            except Exception as e:
                print(f"JSON parse error: {e}")
        pc_pattern = r'([A-Z]{1,2}\d{1,2}[A-Z]?\s?\d[A-Z]{2})'
        pc_match = re.search(pc_pattern, html.upper())
        if pc_match and not result["postcode"]:
            result["postcode"] = pc_match.group(1).replace(" ", "").upper()
    except Exception as e:
        print(f"Scrape error: {e}")
    return result

def extract_postcode_from_url(url):
    pc_pattern = r'([A-Z]{1,2}\d{1,2}[A-Z]?\s?\d[A-Z]{2})'
    match = re.search(pc_pattern, url.upper())
    if match:
        return match.group(1).replace(" ", "").upper()
    return None

def get_floor_area_from_epc(postcode, address=None):
    try:
        formatted = format_postcode(postcode)
        r = requests.get(
            "https://epc.opendatacommunities.org/api/v1/domestic/search",
            params={"postcode": formatted, "size": 10},
            headers={"Accept": "application/json", "Authorization": f"Bearer {EPC_API_KEY}"},
            timeout=10
        )
        if r.status_code != 200:
            return None
        rows = r.json().get("rows", [])
        if not rows:
            return None
        if address:
            for row in rows:
                if any(part in row.get("address", "").upper() for part in address.upper().split()[:2]):
                    area = row.get("total-floor-area")
                    if area:
                        return float(area)
        area = rows[0].get("total-floor-area")
        return float(area) if area else None
    except Exception:
        return None

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
        return [t for t in transactions if t.get("type") in type_keys and t.get("price") and t.get("price") < 2_000_000]
    except Exception:
        return []

def avg_sold_price(comparables):
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

def get_local_avg_psqm(postcode, property_type):
    type_keys = normalise_type_listings(property_type)
    formatted = format_postcode(postcode)
    data = fetch_listings_psqf(formatted)
    avg = _calc_avg_psqm(data, type_keys)
    if avg is None:
        data = fetch_listings_psqf(district_postcode(postcode))
        avg = _calc_avg_psqm(data, type_keys)
    return avg

def _calc_avg_psqm(data, type_keys):
    if not data:
        return None
    try:
        points = data.get("data", {}).get("raw_data", [])
        matching = [p for p in points if p.get("type") in type_keys and p.get("price_per_sqf")]
        if not matching:
            return None
        avg_psqf = sum(p["price_per_sqf"] for p in matching) / len(matching)
        return round(price_per_sqft_to_sqm(avg_psqf))
    except Exception:
        return None

def build_report_data(property_url, asking_price, bedrooms, property_type,
                      postcode, floor_area_sqm=None, address=None):
    formatted = format_postcode(postcode)
    comparables, postcode_used, broadened = get_sold_comparables(postcode, property_type)
    local_avg_sold = avg_sold_price(comparables)

    sold_diff_pct = None
    sold_verdict = None
    if local_avg_sold:
        sold_diff_pct = round(((asking_price - local_avg_sold) / local_avg_sold) * 100, 1)
        sold_verdict = "overpriced" if sold_diff_pct > 8 else ("value" if sold_diff_pct < -5 else "fair")

    if not floor_area_sqm and EPC_API_KEY:
        floor_area_sqm = get_floor_area_from_epc(postcode, address)

    asking_psqm = local_avg_psqm = psqm_diff_pct = psqm_verdict = None
    if floor_area_sqm and floor_area_sqm > 0:
        asking_psqm = round(asking_price / floor_area_sqm)
        local_avg_psqm = get_local_avg_psqm(postcode, property_type)
        if local_avg_psqm:
            psqm_diff_pct = round(((asking_psqm - local_avg_psqm) / local_avg_psqm) * 100, 1)
            psqm_verdict = "overpriced" if psqm_diff_pct > 8 else ("value" if psqm_diff_pct < -5 else "fair")

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
        "local_avg_sold": local_avg_sold,
        "local_avg_sold_formatted": f"£{local_avg_sold:,}" if local_avg_sold else None,
        "sold_diff_pct": sold_diff_pct,
        "sold_verdict": sold_verdict,
        "hpi_adjustment": None,
        "asking_psqm": asking_psqm,
        "local_avg_psqm": local_avg_psqm,
        "psqm_diff_pct": psqm_diff_pct,
        "psqm_verdict": psqm_verdict,
        "verdict": verdict,
        "diff_pct": diff_pct,
        "days_on_market": None,
        "local_avg_dom": None,
        "dom_signal": None,
        "generated": datetime.now().strftime("%-d %B %Y"),
        "property_url": property_url,
    }

def send_report_email(to_email, report_html, postcode, verdict):
    try:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={
                "from": f"HouseOffer <{EMAIL_ADDRESS}>",
                "to": [to_email],
                "subject": f"Your free HouseOffer report — {postcode}",
                "html": report_html,
                "text": f"Your free HouseOffer report for {postcode}. Verdict: {verdict.upper()}."
            }
        )
        print(f"Resend: {r.status_code} {r.text}")
        return r.status_code == 200
    except Exception as e:
        print(f"Email error: {e}")
        return False

def send_holding_email(to_email, property_url):
    try:
        requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={
                "from": f"HouseOffer <{EMAIL_ADDRESS}>",
                "to": [to_email],
                "subject": "Your HouseOffer report — we need one more detail",
                "text": "Hi,\n\nThanks for your submission. Could you reply with:\n1. The property postcode\n2. The asking price\n3. Number of bedrooms\n4. Property type\n\nThe HouseOffer team"
            }
        )
    except Exception as e:
        print(f"Holding email error: {e}")

def notify_owner(to_email, property_url, postcode, verdict):
    try:
        requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={
                "from": f"HouseOffer <{EMAIL_ADDRESS}>",
                "to": [EMAIL_ADDRESS],
                "subject": f"New submission: {postcode} — {verdict}",
                "text": f"User: {to_email}\nProperty: {property_url}\nPostcode: {postcode}\nVerdict: {verdict}"
            }
        )
    except Exception as e:
        print(f"Owner notify error: {e}")

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

@app.route("/debug-sold")
def debug_sold():
    postcode = request.args.get("postcode", "WD4 9EW")
    property_type = request.args.get("type", "semi-detached")
    formatted = format_postcode(postcode)
    district = district_postcode(postcode)
    type_keys = normalise_type_sold(property_type)
    full_data = fetch_sold_prices(formatted)
    district_data = fetch_sold_prices(district)
    full_raw = full_data.get("data", {}).get("raw_data", []) if full_data else []
    district_raw = district_data.get("data", {}).get("raw_data", []) if district_data else []
    return jsonify({
        "postcode": formatted,
        "district": district,
        "full_matching": len([t for t in full_raw if t.get("type") in type_keys]),
        "district_matching": len([t for t in district_raw if t.get("type") in type_keys]),
    })

@app.route("/debug-report")
def debug_report():
    postcode = request.args.get("postcode", "WD4 9EW")
    asking_price = int(request.args.get("price", "675000"))
    property_type = request.args.get("type", "semi-detached")
    report = build_report_data("", asking_price, "3", property_type, postcode)
    return jsonify(report)

@app.route("/report", methods=["POST"])
def generate_report():
    data = request.get_json(silent=True) or request.form
    postcode = data.get("postcode", "")
    property_url = data.get("property_url", "")
    if not postcode and property_url:
        postcode = extract_postcode_from_url(property_url)
    if not postcode:
        return jsonify({"error": "Could not determine postcode."}), 400
    report = build_report_data(
        property_url=property_url,
        asking_price=int(str(data.get("asking_price", 0)).replace(",", "").replace("£", "")),
        bedrooms=data.get("bedrooms", "3"),
        property_type=data.get("property_type", "semi-detached"),
        postcode=postcode,
        floor_area_sqm=float(data.get("floor_area_sqm", 0) or 0) or None,
        address=data.get("address", ""),
    )
    return render_template("report_free.html", **report)

@app.route("/api/report-data", methods=["POST"])
def report_data_json():
    data = request.get_json(silent=True) or request.form
    postcode = data.get("postcode", "")
    property_url = data.get("property_url", "")
    if not postcode and property_url:
        postcode = extract_postcode_from_url(property_url)
    if not postcode:
        return jsonify({"error": "Could not determine postcode"}), 400
    report = build_report_data(
        property_url=property_url,
        asking_price=int(str(data.get("asking_price", 0)).replace(",", "").replace("£", "")),
        bedrooms=data.get("bedrooms", "3"),
        property_type=data.get("property_type", "semi-detached"),
        postcode=postcode,
        floor_area_sqm=float(data.get("floor_area_sqm", 0) or 0) or None,
        address=data.get("address", ""),
    )
    return jsonify(report)

@app.route("/submit", methods=["POST"])
def submit():
    data = request.get_json(silent=True) or request.form
    to_email      = data.get("email", "")
    property_url  = data.get("property-url", "") or data.get("property_url", "")
    asking_price  = int(str(data.get("asking_price", 0) or 0).replace(",", "").replace("£", "")) or 0
    bedrooms      = data.get("bedrooms", "3")
    property_type = data.get("property_type", "semi-detached")
    postcode      = data.get("postcode", "")
    address       = data.get("address", "")
    floor_area_sqm = float(data.get("floor_area_sqm", 0) or 0) or None

    if not to_email:
        return jsonify({"error": "Email address required"}), 400

    if property_url and (not postcode or not asking_price):
        scraped = scrape_rightmove(property_url)
        if not postcode:
            postcode = scraped.get("postcode") or ""
        if not asking_price:
            asking_price = scraped.get("asking_price") or 0

    if not postcode:
        send_holding_email(to_email, property_url)
        return jsonify({"status": "sent"})

    try:
        report = build_report_data(
            property_url=property_url,
            asking_price=asking_price,
            bedrooms=bedrooms,
            property_type=property_type,
            postcode=postcode,
            floor_area_sqm=floor_area_sqm,
            address=address,
        )
        report_html = render_template("report_free.html", **report)
        send_report_email(to_email, report_html, report["postcode"], report["verdict"])
        notify_owner(to_email, property_url, report["postcode"], report["verdict"])
        return jsonify({"status": "sent", "postcode": report["postcode"]})
    except Exception as e:
        print(f"Submit error: {e}")
        send_holding_email(to_email, property_url)
        return jsonify({"status": "sent"})

if __name__ == "__main__":
    app.run(debug=True, port=5000)
