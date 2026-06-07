import os
import re
import json
import time
import uuid
import base64
import hashlib
import requests
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from datetime import datetime
from hpi_data import get_hpi_index as hpi_index, get_current_hpi
from property_scraper import scrape_property_url

app = Flask(__name__)
CORS(app, origins=["https://houseoffer.uk", "https://www.houseoffer.uk", "https://houseoffer.netlify.app", "https://offerright.co.uk", "http://localhost:3000"])

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
        timestamp = datetime.utcnow().isoformat() + "Z"
        events.append({
            "type": event_type,
            "timestamp": timestamp,
            "extra": extra or {},
        })
        with open(path, "w") as f:
            json.dump(events, f)

        # Mirror to Google Sheets (fire-and-forget)
        post_to_sheets({
            "type": "event",
            "timestamp": timestamp,
            "uuid": report_id,
            "event_type": event_type,
            "extra": extra or {},
        })
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
# Google Sheets webhook (Apps Script web app) — receives submissions & events
SHEETS_WEBHOOK_URL = os.environ.get("GOOGLE_SHEETS_WEBHOOK_URL", "")
SHEETS_WEBHOOK_SECRET = os.environ.get("SHEETS_WEBHOOK_SECRET", "")
MIN_COMPARABLES = 10


def post_to_sheets(payload):
    """Fire-and-forget POST to the Google Sheets Apps Script webhook.
    Failures are logged but never block the response to the user."""
    if not SHEETS_WEBHOOK_URL or not SHEETS_WEBHOOK_SECRET:
        return
    try:
        body = dict(payload)
        body["secret"] = SHEETS_WEBHOOK_SECRET
        requests.post(SHEETS_WEBHOOK_URL, json=body, timeout=5)
    except Exception as e:
        print(f"Sheets webhook error: {e}")

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
    """Fill listing fields from Rightmove/Zoopla when a property URL is provided.
    Returns a 6-tuple: (postcode, asking_price, bedrooms, property_type, address, extra_dict)
    where extra_dict carries days_on_market and price-reduction fields from the scraper."""
    extra = {}
    if not property_url:
        return postcode, asking_price, bedrooms, property_type, address, extra

    scraped = scrape_property_url(property_url)
    if not postcode:
        postcode = scraped.get("postcode") or ""
    if not asking_price:
        asking_price = scraped.get("asking_price") or 0
    if scraped.get("bedrooms") is not None:
        bedrooms = scraped.get("bedrooms", bedrooms)
    if scraped.get("property_type"):
        property_type = scraped.get("property_type", property_type)
    if scraped.get("address") and not address:
        address = scraped.get("address")

    extra = {
        "scraper_days_on_market": scraped.get("days_on_market"),
        "price_reduced": scraped.get("price_reduced", False),
        "original_asking_price": scraped.get("original_asking_price"),
        "reduction_date": scraped.get("reduction_date"),
        "reduction_amount": scraped.get("reduction_amount"),
        "reduction_pct": scraped.get("reduction_pct"),
    }
    return postcode, asking_price, bedrooms, property_type, address, extra

EPC_API_BASE = "https://api.get-energy-performance-data.communities.gov.uk"

def _extract_floor_area(cert):
    """Read total floor area (sqm) from a full certificate response."""
    for field in ("total_floor_area", "total-floor-area", "totalFloorArea", "floor_area", "floor-area", "floorArea"):
        val = cert.get(field)
        if val is not None:
            try:
                area = float(val)
                if area > 0:
                    return area
            except (ValueError, TypeError):
                continue
    return None

def _epc_search(postcode):
    """Search the EPC register by postcode. Returns list of certificate summaries."""
    formatted = format_postcode(postcode)
    r = requests.get(
        f"{EPC_API_BASE}/api/domestic/search",
        params={"postcode": formatted, "page_size": 100},
        headers={"Accept": "application/json", "Authorization": f"Bearer {EPC_API_KEY}"},
        timeout=10
    )
    if r.status_code != 200:
        print(f"EPC search error: {r.status_code} — {r.text[:200]}")
        return []
    data = r.json().get("data", [])
    # When no certificates found, the API returns {"data": {"error": ...}} not a list
    return data if isinstance(data, list) else []

def _epc_fetch_certificate(certificate_number):
    """Fetch a full EPC certificate by its number. Returns the cert dict or None."""
    r = requests.get(
        f"{EPC_API_BASE}/api/certificate",
        params={"certificate_number": certificate_number},
        headers={"Accept": "application/json", "Authorization": f"Bearer {EPC_API_KEY}"},
        timeout=10
    )
    if r.status_code != 200:
        print(f"EPC certificate error: {r.status_code} — {r.text[:200]}")
        return None
    cert = r.json().get("data")
    return cert if isinstance(cert, dict) else None

def _leading_house_number(addr):
    """Extract the leading house number from an address string, e.g. '9 Chantry Close' -> '9'."""
    if not addr:
        return None
    m = re.match(r"\s*(\d+[A-Za-z]?)\b", addr.strip())
    return m.group(1).upper() if m else None

def _street_tokens(addr):
    """Significant street-name word tokens (len > 3), uppercased.
    Handles both '9 Chantry Close' and '9, Chantry Close, Kings Langley' formats."""
    if not addr:
        return set()
    # Strip a leading house number first (handles '9' and '9,' prefixes), then
    # take the first comma-separated segment of what remains as the street.
    stripped = re.sub(r"^\s*\d+[A-Za-z]?\s*,?\s*", "", addr.strip())
    first_seg = stripped.split(",")[0]
    return {t for t in re.sub(r"[^A-Za-z0-9 ]", " ", first_seg).upper().split() if len(t) > 3}

def _select_epc_match(results, address):
    """Confidently match the subject property's certificate.
    Requires the house number to match AND at least one street-name token to overlap.
    Returns None if no confident match — we omit £/sqm rather than guess a neighbour's floor area."""
    if not results or not address:
        return None
    subj_num = _leading_house_number(address)
    subj_streets = _street_tokens(address)
    if not subj_num:
        return None
    for r in results:
        line1 = r.get("addressLine1") or ""
        if _leading_house_number(line1) != subj_num:
            continue
        # House number matches — confirm street overlap if we have street tokens to check
        if subj_streets:
            cand_streets = _street_tokens(line1)
            if not (subj_streets & cand_streets):
                continue
        return r
    return None

def get_floor_area_from_epc(postcode, address=None):
    """Two-call EPC lookup: search by postcode → fetch certificate → read floor area."""
    try:
        results = _epc_search(postcode)
        if not results:
            return None
        match = _select_epc_match(results, address)
        if not match or not match.get("certificateNumber"):
            return None
        cert = _epc_fetch_certificate(match["certificateNumber"])
        if not cert:
            return None
        return _extract_floor_area(cert)
    except Exception as e:
        print(f"EPC lookup exception: {e}")
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

def fetch_sold_psqf(postcode):
    """Fetch SOLD £/sqft for an area (not asking-price listings)."""
    try:
        r = requests.get(
            "https://api.propertydata.co.uk/sold-prices-per-sqf",
            params={"key": PROPERTYDATA_API_KEY, "postcode": postcode},
            timeout=10
        )
        if r.status_code == 200:
            return r.json()
        print(f"sold-prices-per-sqf error: {r.status_code} — {r.text[:200]}")
    except Exception as e:
        print(f"sold-prices-per-sqf exception: {e}")
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
    # HPI-adjust all comparables to today's value before returning
    comparables = hpi_adjust_comparables(comparables, postcode)
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
    # Use HPI-adjusted price if available, otherwise fall back to raw price
    prices = sorted(c.get("adjusted_price") or c["price"] for c in comparables)
    n = len(prices)
    if n >= 5:
        q1 = n // 4
        q3 = n - q1
        trimmed = prices[q1:q3]
        return round(sum(trimmed) / len(trimmed)) if trimmed else round(sum(prices) / n)
    return round(sum(prices) / n)


def hpi_adjust_comparables(comparables, postcode):
    """Adjust each comparable's price to today's value using regional HPI.
    Adds an 'adjusted_price' field to each comparable. Falls back to raw
    price if HPI data is unavailable for any individual transaction."""
    if not comparables:
        return comparables
    try:
        region = postcode_to_region(postcode)
        current_hpi, _ = get_current_hpi(region)
        if not current_hpi or current_hpi <= 0:
            return comparables
    except Exception as e:
        print(f"hpi_adjust_comparables: could not get current HPI — {e}")
        return comparables

    adjusted = []
    for c in comparables:
        comp = dict(c)
        try:
            date_str = comp.get("date", "")
            sale_month = date_str[:7]  # "YYYY-MM"
            sale_hpi = hpi_index(region, sale_month) if sale_month else None
            if sale_hpi and sale_hpi > 0:
                comp["adjusted_price"] = round(comp["price"] * (current_hpi / sale_hpi))
            else:
                # HPI data missing for this month — use raw price as fallback
                comp["adjusted_price"] = comp["price"]
        except Exception as e:
            print(f"hpi_adjust_comparables: fallback for {comp.get('address','?')} — {e}")
            comp["adjusted_price"] = comp["price"]
        adjusted.append(comp)
    return adjusted

def _psqf_points(data, type_keys):
    """Extract type-matched points carrying both a £/sqf value and floor area (sqf)."""
    if not data:
        return []
    try:
        raw = data.get("data", {}).get("raw_data", [])
    except Exception:
        return []
    if not raw:
        print(f"_psqf_points: no raw_data — keys: {list(data.get('data', {}).keys())}")
        return []

    def psqf_value(p):
        for f in ("price_per_sqf", "sold_price_per_sqf", "psqf", "price_per_sqft"):
            if p.get(f):
                return p[f]
        return None

    points = []
    for p in raw:
        if p.get("type") in type_keys:
            v = psqf_value(p)
            if v:
                points.append({"psqf": v, "sqf": p.get("sqf")})
    if not points:
        print(f"_psqf_points: no matches for {type_keys}. Types present: {sorted({p.get('type') for p in raw})}")
    return points

def fetch_avg_dom(postcode):
    """Fetch average days on market from PropertyData API. Returns int or None."""
    try:
        r = requests.get(
            "https://api.propertydata.co.uk/avg-days-on-market",
            params={"key": PROPERTYDATA_API_KEY, "postcode": postcode},
            timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            inner = data.get("data") or {}
            for field in ("average_days_on_market", "avg_dom", "days_on_market", "average"):
                val = inner.get(field) if isinstance(inner, dict) else None
                if val is not None:
                    try:
                        return int(val)
                    except (TypeError, ValueError):
                        pass
    except Exception as e:
        print(f"avg_dom error: {e}")
    return None


def fetch_propertydata_avm(postcode, property_type, bedrooms=None):
    """Attempt PropertyData AVM/valuation endpoint. Returns dict or None."""
    try:
        params = {"key": PROPERTYDATA_API_KEY, "postcode": postcode, "property_type": property_type}
        if bedrooms:
            params["bedrooms"] = bedrooms
        r = requests.get(
            "https://api.propertydata.co.uk/valuation",
            params=params,
            timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            inner = data.get("data") or {}
            low = inner.get("lower_estimate") or inner.get("low") or inner.get("min")
            high = inner.get("upper_estimate") or inner.get("high") or inner.get("max")
            mid = inner.get("estimate") or inner.get("mid") or inner.get("value")
            if low and high:
                low, high = int(low), int(high)
                mid = int(mid) if mid else (low + high) // 2
                return {"low": low, "high": high, "mid": mid}
    except Exception as e:
        print(f"AVM error: {e}")
    return None


def fetch_asking_sold_ratio(postcode, property_type):
    """Fetch local asking-to-sold discount percentage from PropertyData.
    Returns discount as a positive float (e.g. 4.2 = 4.2% below asking), or None."""
    try:
        r = requests.get(
            "https://api.propertydata.co.uk/asking-vs-sold",
            params={"key": PROPERTYDATA_API_KEY, "postcode": postcode, "property_type": property_type},
            timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            inner = data.get("data") or {}
            for field in ("avg_discount_pct", "discount", "discount_pct", "pct_below_asking"):
                val = inner.get(field) if isinstance(inner, dict) else None
                if val is not None:
                    return abs(float(val))
    except Exception as e:
        print(f"asking_sold_ratio error: {e}")
    return None


def _hpi_month_offset(year_month: str, months: int) -> str:
    """Return a YYYY-MM string shifted by `months` from year_month (negative = earlier)."""
    try:
        y, m = int(year_month[:4]), int(year_month[5:7])
        total = y * 12 + m - 1 + months
        return f"{total // 12:04d}-{(total % 12) + 1:02d}"
    except Exception:
        return year_month


def get_psqm_benchmarks(postcode, property_type, floor_area_sqm=None):
    """Return both sold £/sqm benchmarks:
      - area_wide_psqm: all comparable-type homes
      - size_matched_psqm: homes within ±20% of subject floor area (only if >=3, else None)
    Size-matched is the accurate like-for-like; area-wide is broad market context."""
    type_keys = normalise_type_listings(property_type)
    formatted = format_postcode(postcode)
    points = _psqf_points(fetch_sold_psqf(formatted), type_keys)
    if not points:
        points = _psqf_points(fetch_sold_psqf(district_postcode(postcode)), type_keys)
    if not points:
        return {"area_wide_psqm": None, "size_matched_psqm": None, "size_matched_count": 0}

    # Area-wide average across all matching points
    area_wide_psqf = sum(p["psqf"] for p in points) / len(points)
    area_wide_psqm = round(price_per_sqft_to_sqm(area_wide_psqf))

    # Size-matched: homes within ±20% of subject floor area, needs >=3 to be reliable
    size_matched_psqm = None
    size_matched_count = 0
    if floor_area_sqm and floor_area_sqm > 0:
        subject_sqf = floor_area_sqm * 10.764
        lo, hi = subject_sqf * 0.8, subject_sqf * 1.2
        sized = [p for p in points if p.get("sqf") and lo <= p["sqf"] <= hi]
        if len(sized) >= 3:
            sm_psqf = sum(p["psqf"] for p in sized) / len(sized)
            size_matched_psqm = round(price_per_sqft_to_sqm(sm_psqf))
            size_matched_count = len(sized)

    return {
        "area_wide_psqm": area_wide_psqm,
        "size_matched_psqm": size_matched_psqm,
        "size_matched_count": size_matched_count,
    }


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

def _fmt(value):
    """Format an integer price as £X,XXX,XXX."""
    return f"£{value:,}" if value else None


def _method_dict(name, low, high, midpoint, source, available, weight=1):
    return {
        "name": name,
        "low": low,
        "high": high,
        "midpoint": midpoint,
        "source": source,
        "available": available,
        "weight": weight,
        "low_formatted": _fmt(low),
        "high_formatted": _fmt(high),
        "midpoint_formatted": _fmt(midpoint),
    }


def build_report_data(property_url, asking_price, bedrooms, property_type,
                      postcode, floor_area_sqm=None, address=None,
                      scraper_days_on_market=None, price_reduced=False,
                      original_asking_price=None, reduction_date=None,
                      reduction_amount=None, reduction_pct=None):
    formatted = format_postcode(postcode)
    region = postcode_to_region(postcode)
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
    size_matched_psqm = area_wide_psqm = None
    size_matched_count = 0
    psqm_basis = None
    psqm_implied_value = None
    if floor_area_sqm and floor_area_sqm > 0:
        asking_psqm = round(asking_price / floor_area_sqm)
        benchmarks = get_psqm_benchmarks(postcode, property_type, floor_area_sqm)
        size_matched_psqm = benchmarks["size_matched_psqm"]
        area_wide_psqm = benchmarks["area_wide_psqm"]
        size_matched_count = benchmarks["size_matched_count"]
        local_avg_psqm = size_matched_psqm or area_wide_psqm
        psqm_basis = "size_matched" if size_matched_psqm else ("area_wide" if area_wide_psqm else None)
        if local_avg_psqm:
            psqm_implied_value = round(floor_area_sqm * local_avg_psqm)
            psqm_diff_pct = round(((asking_psqm - local_avg_psqm) / local_avg_psqm) * 100, 1)
            psqm_verdict = "overpriced" if psqm_diff_pct > 8 else ("value" if psqm_diff_pct < -5 else "fair")

    # HPI-adjusted last sale
    hpi_adjustment = None
    hpi_adjusted_value = None
    try:
        last_sale = find_last_sale(postcode, address=address)
        if last_sale:
            hpi_adjustment = calculate_hpi_adjustment(
                last_sale["price"], last_sale["date"], region
            )
            if hpi_adjustment:
                hpi_adjusted_value = hpi_adjustment["adjusted_price"]
    except Exception as e:
        print(f"HPI section error: {e}")

    # Days on market: try PropertyData first, fall back to scraper
    local_avg_dom = fetch_avg_dom(postcode_used)
    days_on_market = scraper_days_on_market
    dom_signal = None
    if days_on_market and local_avg_dom:
        ratio = days_on_market / local_avg_dom
        dom_signal = "high" if ratio > 1.5 else ("medium" if ratio > 1.0 else "low")

    verdict = sold_verdict or psqm_verdict or "unknown"
    diff_pct = sold_diff_pct if sold_diff_pct is not None else psqm_diff_pct or 0

    # Build comparables list for report
    comparables_list = []
    try:
        sorted_comps = sorted(comparables, key=lambda x: x.get("date") or "", reverse=True)[:20]
        for c in sorted_comps:
            comparables_list.append({
                "address": c.get("address", ""),
                "date": c.get("date", ""),
                "price": c.get("price"),
                "price_formatted": f"£{c['price']:,}" if c.get("price") else "",
                "adjusted_price": c.get("adjusted_price"),
                "adjusted_price_formatted": f"£{c['adjusted_price']:,}" if c.get("adjusted_price") else "",
            })
    except Exception as e:
        print(f"comparables_list build error: {e}")

    # ── FOOTBALL FIELD: build seven valuation methods ──────────────────────────

    methods = []

    # Method 1: Comparable sold prices (weight 2)
    if local_avg_sold and comparables:
        try:
            adj_prices = sorted(
                c.get("adjusted_price") or c["price"] for c in comparables if c.get("price")
            )
            n = len(adj_prices)
            q1_idx = max(0, n // 4)
            q3_idx = min(n - 1, n - n // 4)
            m1_low = round(adj_prices[q1_idx])
            m1_high = round(adj_prices[q3_idx])
            m1_mid = local_avg_sold
            methods.append(_method_dict(
                "Comparable sold prices", m1_low, m1_high, m1_mid,
                "HM Land Registry (HPI-adjusted)", True, weight=2
            ))
        except Exception as e:
            print(f"Method 1 error: {e}")
            methods.append(_method_dict("Comparable sold prices", 0, 0, 0, "HM Land Registry", False, weight=2))
    else:
        methods.append(_method_dict("Comparable sold prices", 0, 0, 0, "HM Land Registry", False, weight=2))

    # Method 2: HPI-adjusted last sale (weight 2)
    if hpi_adjusted_value:
        m2_low = round(hpi_adjusted_value * 0.95)
        m2_high = round(hpi_adjusted_value * 1.05)
        methods.append(_method_dict(
            "HPI-adjusted last sale", m2_low, m2_high, round((m2_low + m2_high) / 2),
            "ONS House Price Index", True, weight=2
        ))
    else:
        methods.append(_method_dict("HPI-adjusted last sale", 0, 0, 0, "ONS House Price Index", False, weight=2))

    # Method 3: Price per square metre
    if psqm_implied_value and floor_area_sqm:
        m3_low = round(psqm_implied_value * 0.95)
        m3_high = round(psqm_implied_value * 1.05)
        methods.append(_method_dict(
            "Price per m²", m3_low, psqm_implied_value, m3_high,
            "EPC register + PropertyData sold £/m²", True
        ))
    else:
        methods.append(_method_dict("Price per m²", 0, 0, 0, "EPC register + PropertyData", False))

    # Method 4: Area price trend (12-month HPI range)
    if local_avg_sold:
        try:
            current_hpi, current_month = get_current_hpi(region)
            month_12m_ago = _hpi_month_offset(current_month, -12)
            hpi_12m = hpi_index(region, month_12m_ago)
            if current_hpi and hpi_12m and hpi_12m > 0:
                price_12m_ago = round(local_avg_sold / (current_hpi / hpi_12m))
                m4_low = round(min(local_avg_sold, price_12m_ago) * 0.98)
                m4_high = round(max(local_avg_sold, price_12m_ago) * 1.02)
                m4_mid = round((m4_low + m4_high) / 2)
                methods.append(_method_dict(
                    "Area price trend", m4_low, m4_high, m4_mid,
                    "ONS House Price Index", True
                ))
            else:
                methods.append(_method_dict("Area price trend", 0, 0, 0, "ONS House Price Index", False))
        except Exception as e:
            print(f"Method 4 error: {e}")
            methods.append(_method_dict("Area price trend", 0, 0, 0, "ONS House Price Index", False))
    else:
        methods.append(_method_dict("Area price trend", 0, 0, 0, "ONS House Price Index", False))

    # Method 5: Online estimate (AVM via PropertyData)
    try:
        avm = fetch_propertydata_avm(postcode_used, property_type, bedrooms)
        if avm:
            methods.append(_method_dict(
                "Automated valuation", avm["low"], avm["mid"], avm["high"],
                "Automated valuation model", True
            ))
        else:
            methods.append(_method_dict("Automated valuation", 0, 0, 0, "Automated valuation model", False))
    except Exception as e:
        print(f"Method 5 error: {e}")
        methods.append(_method_dict("Automated valuation", 0, 0, 0, "Automated valuation model", False))

    # Method 6: Lender valuation band
    base_candidates = [v for v in (local_avg_sold, hpi_adjusted_value, psqm_implied_value) if v]
    if base_candidates:
        lender_base = min(base_candidates)
        m6_low = round(lender_base * 0.90)
        m6_high = round(lender_base * 0.97)
        m6_mid = round((m6_low + m6_high) / 2)
        methods.append(_method_dict(
            "Lender valuation band", m6_low, m6_mid, m6_high,
            "Estimated lender valuation", True
        ))
    else:
        methods.append(_method_dict("Lender valuation band", 0, 0, 0, "Estimated lender valuation", False))

    # Method 7: Asking-to-sold discount
    try:
        discount_pct = fetch_asking_sold_ratio(postcode_used, property_type)
        is_national_fallback = False
        if discount_pct is None:
            discount_pct = 4.5
            is_national_fallback = True
        m7_low = round(asking_price * (1 - 0.055))
        m7_high = round(asking_price * (1 - 0.035))
        if not is_national_fallback:
            m7_low = round(asking_price * (1 - (discount_pct + 1) / 100))
            m7_high = round(asking_price * (1 - (discount_pct - 1) / 100))
        m7_mid = round((m7_low + m7_high) / 2)
        source_note = "National avg asking-to-sold discount" if is_national_fallback else "Local asking-to-sold discount"
        # Weight 0 = shown in table as context but excluded from weighted range calculation
        weight = 0 if is_national_fallback else 1
        methods.append(_method_dict(
            "Asking-to-sold discount", m7_low, m7_mid, m7_high,
            source_note, True, weight=weight
        ))
    except Exception as e:
        print(f"Method 7 error: {e}")
        methods.append(_method_dict("Asking-to-sold discount", 0, 0, 0, "Asking-to-sold discount", False, weight=0))

    # ── FOOTBALL FIELD WEIGHTED RANGE ─────────────────────────────────────────

    available_methods = [m for m in methods if m["available"] and m["weight"] > 0]
    weighted_low = weighted_high = weighted_midpoint = None
    recommended_offer = None

    if available_methods:
        total_weight = sum(m["weight"] for m in available_methods)
        weighted_low = round(sum(m["low"] * m["weight"] for m in available_methods) / total_weight)
        weighted_high = round(sum(m["high"] * m["weight"] for m in available_methods) / total_weight)
        weighted_midpoint = round((weighted_low + weighted_high) / 2)
        # Open with = lower third of range (always lowest of the three)
        # Target = midpoint
        # Walk away = weighted_low (always highest of the three shown to buyer)
        # Enforce ordering: open_offer < target < walk_away_ceiling
        open_offer = round(weighted_low + 0.30 * (weighted_high - weighted_low))
        target_price = weighted_midpoint
        walk_away = weighted_high
        # Safety check: ensure open < target < walk_away
        open_offer = min(open_offer, target_price - 1)
        walk_away = max(walk_away, target_price + 1)
        recommended_offer = open_offer

    # Chart axis bounds: include all method ranges and asking price
    chart_price_min = chart_price_max = None
    if available_methods:
        all_lows = [m["low"] for m in available_methods]
        all_highs = [m["high"] for m in available_methods]
        chart_price_min = int(min(all_lows + [asking_price]) * 0.96)
        chart_price_max = int(max(all_highs + [asking_price]) * 1.04)

    # Price reduction formatting
    original_asking_price_formatted = _fmt(original_asking_price)
    reduction_amount_formatted = _fmt(reduction_amount)

    return {
        "postcode": formatted,
        "postcode_used": postcode_used,
        "comparables_count": len(comparables),
        "comparables": comparables_list,
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
        "size_matched_psqm": size_matched_psqm,
        "size_matched_psqm_formatted": f"£{size_matched_psqm:,}/m²" if size_matched_psqm else None,
        "area_wide_psqm": area_wide_psqm,
        "area_wide_psqm_formatted": f"£{area_wide_psqm:,}/m²" if area_wide_psqm else None,
        "size_matched_count": size_matched_count,
        "psqm_basis": psqm_basis,
        "psqm_diff_pct": psqm_diff_pct,
        "psqm_verdict": psqm_verdict,
        "verdict": verdict,
        "diff_pct": diff_pct,
        "days_on_market": days_on_market,
        "local_avg_dom": local_avg_dom,
        "dom_signal": dom_signal,
        "price_reduced": price_reduced,
        "original_asking_price": original_asking_price,
        "original_asking_price_formatted": original_asking_price_formatted,
        "reduction_date": reduction_date,
        "reduction_amount": reduction_amount,
        "reduction_amount_formatted": reduction_amount_formatted,
        "reduction_pct": reduction_pct,
        "football_field": methods,
        "weighted_low": weighted_low,
        "weighted_high": weighted_high,
        "weighted_midpoint": weighted_midpoint,
        "weighted_low_formatted": _fmt(weighted_low),
        "weighted_high_formatted": _fmt(weighted_high),
        "weighted_midpoint_formatted": _fmt(weighted_midpoint),
        "recommended_offer": recommended_offer,
        "recommended_offer_formatted": _fmt(recommended_offer),
        "open_offer": open_offer if available_methods else None,
        "open_offer_formatted": _fmt(open_offer) if available_methods else None,
        "target_price": target_price if available_methods else None,
        "target_price_formatted": _fmt(target_price) if available_methods else None,
        "walk_away": walk_away if available_methods else None,
        "walk_away_formatted": _fmt(walk_away) if available_methods else None,
        "chart_price_min": chart_price_min,
        "chart_price_max": chart_price_max,
        "generated": datetime.now().strftime("%-d %B %Y"),
        "property_url": property_url,
    }

def send_report_email(to_email, report_html, postcode, verdict, report_url=None):
    """Send the user their report. The HTML should already be email-safe (report_email.html)."""
    try:
        # Plain-text fallback for clients that don't render HTML
        verdict_line = {
            "overpriced": "asking above what the market supports",
            "value": "priced below comparable sales",
            "fair": "priced fairly — but there's room to negotiate",
            "unknown": "we couldn't find enough local data for a verdict",
        }.get(verdict, "our verdict is in")

        text_body = (
            f"Your HouseOffer report for {postcode}\n\n"
            f"Verdict: This property is {verdict_line}.\n\n"
        )
        if report_url:
            text_body += f"View your full report online:\n{report_url}\n\n"
        text_body += "— The HouseOffer team\nhttps://houseoffer.uk"

        r = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={
                "from": f"HouseOffer <{EMAIL_ADDRESS}>",
                "to": [to_email],
                "subject": f"Your HouseOffer report — {postcode}",
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


@app.route("/track-upgrade")
def track_upgrade():
    """Alias for /track used by locked-section CTAs on the free report."""
    return track()


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
    if not re.fullmatch(r"[a-f0-9]{8,32}", report_id):
        return "Report not found", 404

    stored = load_report(report_id)
    if not stored:
        return ("<html><body style='font-family:sans-serif;padding:40px;text-align:center;'>"
                "<h1>Report not found</h1>"
                "<p>This report may have expired. Reports are kept for a limited time.</p>"
                "<p><a href='https://houseoffer.netlify.app'>Generate a new report →</a></p>"
                "</body></html>", 404)

    log_event(report_id, "report_viewed", {
        "user_agent": request.headers.get("User-Agent", "")[:200],
        "referer": request.headers.get("Referer", "")[:200],
    })

    report = stored.get("report", {})
    report_url = f"{BASE_URL.rstrip('/')}/r/{report_id}"
    paid = stored.get("paid", False)
    template = "report_paid.html" if paid else "report_free.html"
    return render_template(template, report_url=report_url, report_id=report_id, **report)


@app.route("/admin/unlock/<report_id>")
def admin_unlock(report_id):
    """Set paid=True for a given report UUID (manual unlock until Stripe is live)."""
    auth = request.args.get("key", "")
    if auth != os.environ.get("ADMIN_KEY", "set-an-admin-key"):
        return jsonify({"error": "unauthorized"}), 401
    if not re.fullmatch(r"[a-f0-9]{8,32}", report_id):
        return jsonify({"error": "invalid report_id"}), 400
    stored = load_report(report_id)
    if not stored:
        return jsonify({"error": "report not found"}), 404
    stored["paid"] = True
    save_report(report_id, stored)
    log_event(report_id, "report_unlocked", {})
    return jsonify({"status": "unlocked", "report_id": report_id})

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

@app.route("/debug-scrape-dates")
def debug_scrape_dates():
    """Dump raw PAGE_MODEL date-related fields for diagnosing DOM extraction."""
    from property_scraper import _fetch_html, _parse_rightmove_page_model
    url = request.args.get("url", "")
    if not url:
        return jsonify({"error": "Pass ?url= with a Rightmove listing URL"}), 400
    html = _fetch_html(url, referer="https://www.rightmove.co.uk/")
    if not html:
        return jsonify({"error": "Could not fetch page"}), 400
    model = _parse_rightmove_page_model(html)
    if not model:
        return jsonify({"error": "Could not parse PAGE_MODEL", "html_length": len(html)}), 400
    prop = model.get("propertyData") or model
    date_fields = {
        "listingUpdate": prop.get("listingUpdate"),
        "firstListedDate": prop.get("firstListedDate"),
        "dateAdded": prop.get("dateAdded"),
        "firstVisibleDate": prop.get("firstVisibleDate"),
        "addedOrReduced": prop.get("addedOrReduced"),
        "priceHistory": prop.get("priceHistory"),
        "listingHistory": prop.get("listingHistory"),
        "addedOn": prop.get("addedOn"),
        "reducedOn": prop.get("reducedOn"),
        "staticMapUrl": None,
    }
    # Also search HTML for date patterns
    import re
    html_dates = {
        "added_on_pattern": re.findall(r"(?:Added\s+on|First\s+listed)[:\s]+(\d{1,2}\s+\w+\s+\d{4})", html, re.IGNORECASE),
        "reduced_on_pattern": re.findall(r"Reduced\s+on\s+(\d{1,2}\s+\w+\s+\d{4})", html, re.IGNORECASE),
        "was_price_pattern": re.findall(r"[Ww]as\s+£[\d,]+", html),
    }
    return jsonify({"page_model_date_fields": date_fields, "html_patterns": html_dates})

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

@app.route("/debug-psqf")
def debug_psqf():
    """Dump the raw sold-prices-per-sqf response and both computed benchmarks.
    Usage: /debug-psqf?postcode=WD4&type=semi-detached&floor=141"""
    postcode = request.args.get("postcode", "WD4")
    property_type = request.args.get("type", "semi-detached")
    floor_area_sqm = float(request.args.get("floor", 0) or 0) or None
    type_keys = normalise_type_listings(property_type)
    formatted = format_postcode(postcode)

    raw_full = fetch_sold_psqf(formatted)
    used = raw_full if raw_full else fetch_sold_psqf(district_postcode(postcode))
    points = used.get("data", {}).get("raw_data", []) if used else []
    matched = _psqf_points(used, type_keys)

    benchmarks = get_psqm_benchmarks(postcode, property_type, floor_area_sqm)

    return jsonify({
        "postcode_tried": formatted,
        "floor_area_sqm": floor_area_sqm,
        "type_keys_we_filter_for": type_keys,
        "total_points_returned": len(points),
        "all_types_present": sorted({p.get("type") for p in points}) if points else [],
        "matched_points_count": len(matched),
        "benchmarks": benchmarks,
    })

@app.route("/debug-report")
def debug_report():
    postcode = request.args.get("postcode", "WD4 9EW")
    asking_price = int(request.args.get("price", "675000"))
    property_type = request.args.get("type", "semi-detached")
    address = request.args.get("address", "")
    report = build_report_data("", asking_price, "3", property_type, postcode, address=address)
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
    postcode, asking_price, bedrooms, property_type, address, extra = merge_scraped_listing(
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
        **extra,
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
    postcode, asking_price, bedrooms, property_type, address, extra = merge_scraped_listing(
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
        **extra,
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

    postcode, asking_price, bedrooms, property_type, address, extra = merge_scraped_listing(
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
            **extra,
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

        # Write the submission row to Google Sheets (Submissions tab)
        post_to_sheets({
            "type": "submission",
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "uuid": report_id,
            "email": to_email,
            "postcode": report["postcode"],
            "property_type": report["property_type"],
            "asking_price": asking_price,
            "verdict": report["verdict"],
            "buyer_estimate": buyer_estimate or "",
            "anchor_bias": anchor_bias,
            "property_url": property_url,
            "report_url": report_url,
        })

        # Log the initial submission as an event
        log_event(report_id, "submission_created", {
            "email": to_email,
            "postcode": report["postcode"],
            "verdict": report["verdict"],
            "asking_price": asking_price,
            "anchor_bias": anchor_bias,
        })

        # Render the email-safe (Gmail-friendly) version for delivery
        email_html = render_template("report_email.html", report_url=report_url, **report)
        send_report_email(to_email, email_html, report["postcode"], report["verdict"], report_url=report_url)
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
