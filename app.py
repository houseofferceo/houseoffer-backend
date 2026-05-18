import os
import re
import json
import time
import uuid
import hashlib
import requests
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from datetime import datetime
from hpi_data import get_hpi_index as hpi_index, get_current_hpi
from property_scraper import scrape_property_url

app = Flask(__name__)
CORS(app, origins=["https://houseoffer.netlify.app", "https://offerright.co.uk", "http://localhost:3000"])

# ── REPORT STORAGE ────────────────────────────────────────────────────────────
# Reports stored as JSON files on disk under /tmp/reports/<uuid>.json
# Engagement events stored under /tmp/events/<uuid>.json
# Note: /tmp is ephemeral on Render — fine for now, swap to S3/Redis when needed
REPORTS_DIR = "/tmp/houseoffer_reports"
EVENTS_DIR = "/tmp/houseoffer_events"
os.makedirs(REPORTS_DIR, exist_ok=True)
os.makedirs(EVENTS_DIR, exist_ok=True)

def save_report(report_id, payload):
    """Persist report data to disk so /r/<uuid> can serve it later."""
    try:
        with open(os.path.join(REPORTS_DIR, f"{report_id}.json"), "w") as f:
            json.dump(payload, f)
        return True
    except Exception as e:
        print(f"save_report error: {e}")
        return False

def load_report(report_id):
    """Retrieve stored report data by UUID. Returns None if not found."""
    try:
        path = os.path.join(REPORTS_DIR, f"{report_id}.json")
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        print(f"load_report error: {e}")
        return None

def log_event(report_id, event_type, extra=None):
    """Append an engagement event for a given report UUID."""
    try:
        path = os.path.join(EVENTS_DIR, f"{report_id}.json")
        events = []
        if os.path.exists(path):
            with open(path) as f:
                events = json.load(f)
        events.append({
            "type": event_type,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "extra": extra or {},
        })
        with open(path, "w") as f:
            json.dump(events, f)
        return True
    except Exception as e:
        print(f"log_event error: {e}")
        return False

# ── DEDUP ─────────────────────────────────────────────────────────────────────
# Simple in-memory dedup cache: {hash: timestamp}
# Prevents same email+URL submission within 60s causing duplicate report emails
_RECENT_SUBMISSIONS = {}
DEDUP_WINDOW_SECONDS = 60

def _is_duplicate_submission(email, property_url):
    """Returns True if this email+URL was submitted within the last 60 seconds."""
    key = hashlib.sha256(f"{email.lower().strip()}|{property_url.strip()}".encode()).hexdigest()
    now = time.time()
    # Clean up old entries
    for k in list(_RECENT_SUBMISSIONS.keys()):
        if now - _RECENT_SUBMISSIONS[k] > DEDUP_WINDOW_SECONDS:
            del _RECENT_SUBMISSIONS[k]
    if key in _RECENT_SUBMISSIONS:
        return True
    _RECENT_SUBMISSIONS[key] = now
    return False

PROPERTYDATA_API_KEY = os.environ.get("PROPERTYDATA_API_KEY")
EPC_API_KEY = os.environ.get("EPC_API_KEY")
EMAIL_ADDRESS = os.environ.get("EMAIL_ADDRESS")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
# Base URL used when constructing shareable report links sent in emails
BASE_URL = os.environ.get("BASE_URL", "https://houseoffer-backend.onrender.com")
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

def extract_postcode_from_url(url):
    pc_pattern = r'([A-Z]{1,2}\d{1,2}[A-Z]?\s?\d[A-Z]{2})'
    match = re.search(pc_pattern, url.upper())
    if match:
        return match.group(1).replace(" ", "").upper()
    return None

def merge_scraped_listing(property_url, postcode, asking_price, bedrooms, property_type, address=""):
    """Fill listing fields from Rightmove/Zoopla when a property URL is provided."""
    if not property_url:
        return postcode, asking_price, bedrooms, property_type, address

    scraped = scrape_property_url(property_url)
    if not postcode:
        postcode = scraped.get("postcode") or ""
    if not asking_price:
        asking_price = scraped.get("asking_price") or 0
    # Beds/type are not on the landing form — always take from scrape when available
    if scraped.get("bedrooms") is not None:
        bedrooms = scraped.get("bedrooms", bedrooms)
    if scraped.get("property_type"):
        property_type = scraped.get("property_type", property_type)
    if scraped.get("address") and not address:
        address = scraped.get("address")
    return postcode, asking_price, bedrooms, property_type, address

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

def _all_sold_transactions(data):
    if not data:
        return []
    try:
        transactions = data.get("data", {}).get("raw_data", [])
        return [t for t in transactions if t.get("price") and t.get("price") < 2_000_000]
    except Exception:
        return []

def get_all_sold_at_postcode(postcode):
    """All Land Registry sales at this postcode (any property type)."""
    formatted = format_postcode(postcode)
    sales = _all_sold_transactions(fetch_sold_prices(formatted))
    if sales:
        return sales, formatted
    district = district_postcode(postcode)
    return _all_sold_transactions(fetch_sold_prices(district)), district

def _normalise_text(value):
    return re.sub(r"[^A-Z0-9 ]", " ", (value or "").upper()).split()

def _sale_matches_postcode(sale, postcode):
    addr = (sale.get("address") or "").upper().replace(" ", "")
    pc = format_postcode(postcode).replace(" ", "").upper()
    return pc in addr

def _sale_matches_address(sale, address):
    """Prefer sales matching the street/building name when multiple exist at one postcode."""
    if not address:
        return True
    street_tokens = [t for t in _normalise_text(address.split(",")[0]) if len(t) > 3]
    if not street_tokens:
        return True
    addr_tokens = set(_normalise_text(sale.get("address")))
    return any(t in addr_tokens for t in street_tokens)

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


# ── HPI ADJUSTMENT ─────────────────────────────────────────────────────────────

POSTCODE_TO_REGION = {
    "E": "london", "EC": "london", "N": "london", "NW": "london",
    "SE": "london", "SW": "london", "W": "london", "WC": "london",
    "AL": "east-of-england", "CB": "east-of-england", "CM": "east-of-england",
    "CO": "east-of-england", "EN": "east-of-england", "HP": "east-of-england",
    "IP": "east-of-england", "LU": "east-of-england", "MK": "east-of-england",
    "NR": "east-of-england", "PE": "east-of-england", "SG": "east-of-england",
    "SS": "east-of-england", "WD": "east-of-england",
    "B": "west-midlands-region", "CV": "west-midlands-region", "DY": "west-midlands-region",
    "ST": "west-midlands-region", "TF": "west-midlands-region",
    "WR": "west-midlands-region",
    "WS": "west-midlands-region", "WV": "west-midlands-region",
    "DE": "east-midlands", "LE": "east-midlands", "LN": "east-midlands",
    "NG": "east-midlands", "NN": "east-midlands",
    "BR": "south-east", "BN": "south-east", "CT": "south-east", "DA": "south-east",
    "GU": "south-east", "KT": "south-east", "ME": "south-east", "OX": "south-east",
    "PO": "south-east", "RG": "south-east", "RH": "south-east", "SL": "south-east",
    "SM": "south-east", "SN": "south-east", "SO": "south-east", "TN": "south-east",
    "TW": "south-east", "UB": "south-east",
    "BA": "south-west", "BH": "south-west", "BS": "south-west", "DT": "south-west",
    "EX": "south-west", "GL": "south-west", "PL": "south-west", "SP": "south-west",
    "TA": "south-west", "TQ": "south-west", "TR": "south-west",
    "BB": "north-west", "BL": "north-west", "CA": "north-west", "CH": "north-west",
    "CW": "north-west", "FY": "north-west", "LA": "north-west", "M": "north-west",
    "OL": "north-west", "PR": "north-west", "SK": "north-west", "WA": "north-west",
    "WN": "north-west",
    "BD": "yorkshire-and-the-humber", "HD": "yorkshire-and-the-humber",
    "HG": "yorkshire-and-the-humber", "HU": "yorkshire-and-the-humber",
    "HX": "yorkshire-and-the-humber", "LS": "yorkshire-and-the-humber",
    "S": "yorkshire-and-the-humber", "WF": "yorkshire-and-the-humber",
    "YO": "yorkshire-and-the-humber",
    "DH": "north-east", "DL": "north-east", "NE": "north-east",
    "SR": "north-east", "TS": "north-east",
    "CF": "wales", "LD": "wales", "LL": "wales", "NP": "wales",
    "SA": "wales", "SY": "wales",
}

def postcode_to_region(postcode):
    clean = postcode.strip().upper().replace(" ", "")
    for length in [2, 1]:
        prefix = clean[:length]
        if prefix in POSTCODE_TO_REGION:
            return POSTCODE_TO_REGION[prefix]
    return "england"

def find_last_sale(postcode, address=None):
    """Find the most recent sale of this property from Land Registry data at its postcode."""
    sales, _ = get_all_sold_at_postcode(postcode)
    if not sales:
        return None

    postcode_sales = [s for s in sales if _sale_matches_postcode(s, postcode)]
    if not postcode_sales:
        return None

    if address:
        matched = [s for s in postcode_sales if _sale_matches_address(s, address)]
        if matched:
            postcode_sales = matched

    return sorted(postcode_sales, key=lambda x: x.get("date", ""), reverse=True)[0]

def calculate_hpi_adjustment(last_sale_price, sale_date_str, region):
    """Adjust a historical price to today's value using regional HPI."""
    try:
        sale_month = sale_date_str[:7]
        sale_hpi = hpi_index(region, sale_month)
        current_hpi, current_month = get_current_hpi(region)
        if sale_hpi and current_hpi and sale_hpi > 0:
            adjusted = round(last_sale_price * (current_hpi / sale_hpi))
            growth_pct = round(((current_hpi - sale_hpi) / sale_hpi) * 100, 1)
            return {
                "adjusted_price": adjusted,
                "adjusted_price_formatted": f"£{adjusted:,}",
                "sale_price": last_sale_price,
                "sale_price_formatted": f"£{last_sale_price:,}",
                "sale_date": sale_date_str,
                "growth_pct": growth_pct,
            }
    except Exception as e:
        print(f"HPI adjustment error: {e}")
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

    # HPI-adjusted last sale
    hpi_adjustment = None
    try:
        region = postcode_to_region(postcode)
        last_sale = find_last_sale(postcode, address=address)
        if last_sale:
            hpi_adjustment = calculate_hpi_adjustment(
                last_sale["price"], last_sale["date"], region
            )
    except Exception as e:
        print(f"HPI section error: {e}")

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
        "hpi_adjustment": hpi_adjustment,
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

def send_report_email(to_email, report_html, postcode, verdict, report_url=None):
    """Send the user their report. If report_url provided, prepend a banner with the live link."""
    try:
        # Inject a "view online" banner at the top of the HTML if we have a report URL
        if report_url:
            banner = f'''
            <div style="background:#1a6b5a;padding:20px;text-align:center;font-family:sans-serif;">
              <p style="color:#e3f4ef;font-size:13px;margin:0 0 10px;letter-spacing:0.05em;">YOUR REPORT IS ALSO AVAILABLE ONLINE</p>
              <a href="{report_url}" style="display:inline-block;background:white;color:#1a6b5a;padding:10px 22px;border-radius:20px;font-weight:700;text-decoration:none;font-size:14px;">View live report →</a>
              <p style="color:rgba(255,255,255,0.7);font-size:12px;margin:10px 0 0;">Bookmark the link — your data stays accessible</p>
            </div>
            '''
            # Insert banner immediately after <body> tag
            if "<body" in report_html:
                report_html = re.sub(r"(<body[^>]*>)", r"\1" + banner, report_html, count=1)
            else:
                report_html = banner + report_html

        text_body = f"Your free HouseOffer report for {postcode}. Verdict: {verdict.upper()}."
        if report_url:
            text_body += f"\n\nView your full report online: {report_url}"

        r = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={
                "from": f"HouseOffer <{EMAIL_ADDRESS}>",
                "to": [to_email],
                "subject": f"Your free HouseOffer report — {postcode}",
                "html": report_html,
                "text": text_body,
            }
        )
        print(f"Resend: {r.status_code} {r.text}")
        return r.status_code == 200
    except Exception as e:
        print(f"Email error: {e}")
        return False



def notify_owner(to_email, property_url, postcode, verdict, buyer_estimate="", anchor_bias=None):
    try:
        requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={
                "from": f"HouseOffer <{EMAIL_ADDRESS}>",
                "to": [EMAIL_ADDRESS],
                "subject": f"New submission: {postcode} — {verdict}",
                "text": f"User: {to_email}\nProperty: {property_url}\nPostcode: {postcode}\nVerdict: {verdict}\nBuyer estimate: {buyer_estimate}\nAnchor bias: {anchor_bias}% above market"
            }
        )
    except Exception as e:
        print(f"Owner notify error: {e}")


@app.route("/track")
def track():
    """Track upgrade button clicks and redirect to pricing page."""
    tier = request.args.get("tier", "unknown")
    postcode = request.args.get("postcode", "unknown")
    verdict = request.args.get("verdict", "unknown")
    anchor = request.args.get("anchor", "unknown")
    report_id = request.args.get("rid", "")
    
    print(f"UPGRADE CLICK: tier=£{tier} postcode={postcode} verdict={verdict} anchor_bias={anchor} rid={report_id}")

    # Log engagement event tied to the report UUID (if we have one)
    if report_id:
        log_event(report_id, "upgrade_click", {
            "tier": tier,
            "postcode": postcode,
            "verdict": verdict,
            "anchor": anchor,
        })
    
    # Notify owner
    try:
        requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={
                "from": f"HouseOffer <{EMAIL_ADDRESS}>",
                "to": [EMAIL_ADDRESS],
                "subject": f"🔥 Upgrade click: £{tier} — {postcode} ({verdict})",
                "text": f"Someone clicked upgrade!\n\nTier: £{tier}\nPostcode: {postcode}\nVerdict: {verdict}\nAnchor bias: {anchor}\nReport ID: {report_id}\n\nThis is a hot lead."
            }
        )
    except Exception as e:
        print(f"Track notify error: {e}")
    
    from flask import redirect
    return redirect("https://houseoffer.netlify.app/#pricing")

@app.route("/r/<report_id>")
def view_report(report_id):
    """Serve a previously generated report by its UUID."""
    # Basic safety check on the UUID format
    if not re.fullmatch(r"[a-f0-9]{8,32}", report_id):
        return "Report not found", 404

    stored = load_report(report_id)
    if not stored:
        return ("<html><body style='font-family:sans-serif;padding:40px;text-align:center;'>"
                "<h1>Report not found</h1>"
                "<p>This report may have expired. Reports are kept for a limited time.</p>"
                "<p><a href='https://houseoffer.netlify.app'>Generate a new report →</a></p>"
                "</body></html>", 404)

    # Log the view
    log_event(report_id, "report_viewed", {
        "user_agent": request.headers.get("User-Agent", "")[:200],
        "referer": request.headers.get("Referer", "")[:200],
    })

    report = stored.get("report", {})
    report_url = f"{BASE_URL.rstrip('/')}/r/{report_id}"
    return render_template("report_free.html", report_url=report_url, report_id=report_id, **report)

@app.route("/log", methods=["POST"])
def log_engagement():
    """Receive engagement events from the report page (scroll depth, time on page, etc.)."""
    data = request.get_json(silent=True) or {}
    report_id = data.get("report_id", "")
    event_type = data.get("event", "")
    extra = data.get("extra", {})

    if not report_id or not event_type:
        return jsonify({"error": "report_id and event required"}), 400
    if not re.fullmatch(r"[a-f0-9]{8,32}", report_id):
        return jsonify({"error": "invalid report_id"}), 400
    if not re.fullmatch(r"[a-z0-9_]{1,40}", event_type):
        return jsonify({"error": "invalid event type"}), 400

    log_event(report_id, event_type, extra if isinstance(extra, dict) else {})
    return jsonify({"status": "logged"})

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

@app.route("/admin/events/<report_id>")
def admin_events(report_id):
    """Inspect engagement events for a specific report (basic auth via query param)."""
    auth = request.args.get("key", "")
    if auth != os.environ.get("ADMIN_KEY", "set-an-admin-key"):
        return jsonify({"error": "unauthorized"}), 401
    if not re.fullmatch(r"[a-f0-9]{8,32}", report_id):
        return jsonify({"error": "invalid report_id"}), 400
    path = os.path.join(EVENTS_DIR, f"{report_id}.json")
    if not os.path.exists(path):
        return jsonify({"events": []})
    with open(path) as f:
        return jsonify({"events": json.load(f)})

@app.route("/admin/recent")
def admin_recent():
    """List recent submissions for quick monitoring."""
    auth = request.args.get("key", "")
    if auth != os.environ.get("ADMIN_KEY", "set-an-admin-key"):
        return jsonify({"error": "unauthorized"}), 401
    out = []
    try:
        files = sorted(
            os.listdir(REPORTS_DIR),
            key=lambda f: os.path.getmtime(os.path.join(REPORTS_DIR, f)),
            reverse=True,
        )[:50]
        for fname in files:
            with open(os.path.join(REPORTS_DIR, fname)) as f:
                d = json.load(f)
            rid = fname.replace(".json", "")
            report = d.get("report", {})
            # Count events for this report
            events_path = os.path.join(EVENTS_DIR, f"{rid}.json")
            event_count = 0
            if os.path.exists(events_path):
                with open(events_path) as ef:
                    event_count = len(json.load(ef))
            out.append({
                "report_id": rid,
                "created_at": d.get("created_at"),
                "email": d.get("email"),
                "postcode": report.get("postcode"),
                "verdict": report.get("verdict"),
                "asking_price": report.get("asking_price"),
                "anchor_bias": d.get("anchor_bias"),
                "events": event_count,
                "url": f"{BASE_URL.rstrip('/')}/r/{rid}",
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"recent": out})

@app.route("/debug-scrape")
def debug_scrape():
    """Test scraper: /debug-scrape?url=https://www.rightmove.co.uk/properties/..."""
    url = request.args.get("url", "")
    if not url:
        return jsonify({"error": "Pass ?url= with a Rightmove or Zoopla listing URL"}), 400
    return jsonify(scrape_property_url(url))

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
    asking_price = int(str(data.get("asking_price", 0)).replace(",", "").replace("£", ""))
    bedrooms = data.get("bedrooms", "3")
    property_type = data.get("property_type", "semi-detached")
    address = data.get("address", "")
    if not postcode and property_url:
        postcode = extract_postcode_from_url(property_url) or ""
    postcode, asking_price, bedrooms, property_type, address = merge_scraped_listing(
        property_url, postcode, asking_price, bedrooms, property_type, address
    )
    if not postcode:
        return jsonify({"error": "Could not determine postcode."}), 400
    report = build_report_data(
        property_url=property_url,
        asking_price=asking_price,
        bedrooms=bedrooms,
        property_type=property_type,
        postcode=postcode,
        floor_area_sqm=float(data.get("floor_area_sqm", 0) or 0) or None,
        address=address,
    )
    return render_template("report_free.html", **report)

@app.route("/api/report-data", methods=["POST"])
def report_data_json():
    data = request.get_json(silent=True) or request.form
    postcode = data.get("postcode", "")
    property_url = data.get("property_url", "")
    asking_price = int(str(data.get("asking_price", 0)).replace(",", "").replace("£", ""))
    bedrooms = data.get("bedrooms", "3")
    property_type = data.get("property_type", "semi-detached")
    address = data.get("address", "")
    if not postcode and property_url:
        postcode = extract_postcode_from_url(property_url) or ""
    postcode, asking_price, bedrooms, property_type, address = merge_scraped_listing(
        property_url, postcode, asking_price, bedrooms, property_type, address
    )
    if not postcode:
        return jsonify({"error": "Could not determine postcode"}), 400
    report = build_report_data(
        property_url=property_url,
        asking_price=asking_price,
        bedrooms=bedrooms,
        property_type=property_type,
        postcode=postcode,
        floor_area_sqm=float(data.get("floor_area_sqm", 0) or 0) or None,
        address=address,
    )
    return jsonify(report)

@app.route("/submit", methods=["POST"])
def submit():
    data = request.get_json(silent=True) or request.form
    to_email      = data.get("email", "")
    property_url  = data.get("property-url", "") or data.get("property_url", "")
    buyer_estimate = data.get("buyer_estimate", "")
    asking_price  = int(str(data.get("asking_price", 0) or 0).replace(",", "").replace("£", "")) or 0
    bedrooms      = data.get("bedrooms", "3")
    property_type = data.get("property_type", "semi-detached")
    postcode      = data.get("postcode", "")
    address       = data.get("address", "")
    floor_area_sqm = float(data.get("floor_area_sqm", 0) or 0) or None

    if not to_email:
        return jsonify({"error": "Email address required"}), 400

    if not property_url:
        return jsonify({"error": "Property link required"}), 400

    if "rightmove.co.uk" not in property_url.lower():
        return jsonify({"error": "We currently support Rightmove links only. Zoopla support coming soon."}), 400

    # Dedup: silently ignore duplicate submissions within 60s window
    if _is_duplicate_submission(to_email, property_url):
        print(f"DUPLICATE submission blocked: {to_email} | {property_url[:80]}")
        return jsonify({"status": "sent", "deduped": True})

    postcode, asking_price, bedrooms, property_type, address = merge_scraped_listing(
        property_url, postcode, asking_price, bedrooms, property_type, address
    )

    if not postcode:
        return jsonify({"error": "Could not determine postcode from that link. Try a UK sale listing on Rightmove or Zoopla."}), 400
    if not asking_price:
        return jsonify({"error": "Could not determine asking price from that link. Use a for-sale listing (not to-rent)."}), 400

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

        # Generate a UUID for this report so the user can access it online
        report_id = uuid.uuid4().hex[:12]
        report_url = f"{BASE_URL.rstrip('/')}/r/{report_id}"

        # Calculate anchor bias before storing (it goes into the persistence payload)
        anchor_bias = None
        if buyer_estimate and report.get("local_avg_sold"):
            try:
                est = int(str(buyer_estimate).replace(",","").replace("£","").replace(" ",""))
                local = report["local_avg_sold"]
                anchor_bias = round(((est - local) / local) * 100, 1)
            except Exception:
                pass

        # Persist the report so /r/<uuid> can serve it later
        save_report(report_id, {
            "report": report,
            "email": to_email,
            "property_url": property_url,
            "buyer_estimate": buyer_estimate,
            "anchor_bias": anchor_bias,
            "created_at": datetime.utcnow().isoformat() + "Z",
        })

        # Log the initial submission as an event
        log_event(report_id, "submission_created", {
            "email": to_email,
            "postcode": report["postcode"],
            "verdict": report["verdict"],
            "asking_price": asking_price,
            "anchor_bias": anchor_bias,
        })

        report_html = render_template("report_free.html", report_url=report_url, **report)
        send_report_email(to_email, report_html, report["postcode"], report["verdict"], report_url=report_url)
        notify_owner(to_email, property_url, report["postcode"], report["verdict"], buyer_estimate, anchor_bias)

        return jsonify({
            "status": "sent",
            "postcode": report["postcode"],
            "report_id": report_id,
            "report_url": report_url,
        })
    except Exception as exc:
        print(f"Submit error: {exc}")
        return jsonify({"error": "Could not build report. Please try again."}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5000)
