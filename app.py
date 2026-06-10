import os
import re
import json
import time
import uuid
import base64
import hashlib
import requests
from flask import Flask, request, jsonify, render_template, redirect
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
        "scraper_floor_area_sqm": scraped.get("floor_area_sqm"),
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
                points.append({
                    "psqf": v,
                    "sqf": p.get("sqf"),
                    "address": p.get("address"),
                    "price": p.get("price"),
                })
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


def fetch_avg_rents(postcode, property_type, bedrooms=None):
    """Fetch average monthly rent from PropertyData. Returns float (monthly rent) or None."""
    try:
        params = {"key": PROPERTYDATA_API_KEY, "postcode": postcode}
        if bedrooms:
            params["bedrooms"] = str(bedrooms)
        r = requests.get(
            "https://api.propertydata.co.uk/rents",
            params=params,
            timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            inner = data.get("data") or {}
            # Try bedroom-specific first, then overall average
            beds_key = str(bedrooms) if bedrooms else None
            avg = None
            if beds_key and isinstance(inner, dict):
                beds_data = inner.get(beds_key) or {}
                avg = beds_data.get("average") or beds_data.get("mean") or beds_data.get("avg")
            if not avg and isinstance(inner, dict):
                avg = inner.get("average") or inner.get("mean") or inner.get("avg")
            if avg:
                return float(avg)
    except Exception as e:
        print(f"fetch_avg_rents error: {e}")
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
        "psqf_points": points,
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

def _fetch_land_registry_direct(postcode):
    """Query Land Registry SPARQL endpoint directly for sales at this postcode."""
    try:
        pc = format_postcode(postcode).upper()
        query = f"""
PREFIX lrppi: <http://landregistry.data.gov.uk/def/ppi/>
PREFIX lrcommon: <http://landregistry.data.gov.uk/def/common/>
SELECT ?address ?amount ?date WHERE {{
  ?transx lrppi:propertyAddress ?addr ;
          lrppi:pricePaid ?amount ;
          lrppi:transactionDate ?date .
  ?addr lrcommon:postcode "{pc}" .
  OPTIONAL {{ ?addr lrcommon:paon ?paon }}
  OPTIONAL {{ ?addr lrcommon:saon ?saon }}
  OPTIONAL {{ ?addr lrcommon:street ?street }}
  BIND(CONCAT(COALESCE(?saon,""), " ", COALESCE(?paon,""), " ", COALESCE(?street,""), " {pc}") AS ?address)
}}
ORDER BY DESC(?date)
LIMIT 50
"""
        resp = requests.get(
            "https://landregistry.data.gov.uk/sparql",
            params={"query": query, "output": "json"},
            timeout=10,
            headers={"Accept": "application/sparql-results+json"},
        )
        if resp.status_code != 200:
            return []
        bindings = resp.json().get("results", {}).get("bindings", [])
        results = []
        for b in bindings:
            price = int(float(b["amount"]["value"])) if b.get("amount") else None
            date = b["date"]["value"][:10] if b.get("date") else None
            addr = re.sub(r"\s+", " ", b["address"]["value"]).strip() if b.get("address") else pc
            if price and price < 2_000_000:
                results.append({"address": addr, "price": price, "date": date})
        return results
    except Exception as e:
        print(f"land_registry_direct error: {e}")
        return []


def find_last_sale(postcode, address=None):
    """Find the most recent sale of this specific property from Land Registry data.

    Strategy:
    1. Filter PropertyData radius results to the exact full postcode (cuts out neighbours),
       then merge in Land Registry SPARQL results — PropertyData is radius-based and capped
       at ~20 recent sales, so older sales at the postcode are missing from it.
    2. Further filter by street-name tokens from the address.
    3a. If the subject address has a house number, require it to match — confident match.
    3b. If no house number but all surviving candidates are the same property, use the
        most recent sale — still confident.
    3c. If no house number and multiple distinct properties survive, return None — caller
        populates the candidates dropdown rather than guessing a neighbour's sale."""
    sales, _ = get_all_sold_at_postcode(postcode)

    # Step 1: exact postcode filter, plus complete postcode history from Land Registry
    postcode_sales = [s for s in (sales or []) if _sale_matches_postcode(s, postcode)]
    postcode_sales += _fetch_land_registry_direct(postcode)
    if not postcode_sales:
        return None

    # Step 2: street-token filter (when address provided)
    if address:
        street_filtered = [s for s in postcode_sales if _sale_matches_address(s, address)]
        if street_filtered:
            postcode_sales = street_filtered

    # Step 3a: house number available — require it to match
    subj_num = _leading_house_number(address) if address else None
    if subj_num:
        num_matched = [s for s in postcode_sales
                       if _leading_house_number(s.get("address") or "") == subj_num]
        if num_matched:
            return sorted(num_matched, key=lambda x: x.get("date", ""), reverse=True)[0]
        return None

    # Step 3b: no house number but all candidates are the same property (the two data
    # sources can return the same sale in different address formats) — confident enough
    distinct = {
        _leading_house_number(s.get("address") or "") or (s.get("address") or "").upper()
        for s in postcode_sales
    }
    if len(distinct) == 1:
        return sorted(postcode_sales, key=lambda x: x.get("date", ""), reverse=True)[0]

    # Step 3c: multiple distinct properties, no house number — cannot identify property
    return None


def get_last_sale_candidates(postcode):
    """Return all distinct sold properties at this postcode, deduplicated by address.
    Used to build a 'select your property' dropdown when auto-match fails.
    Primary source is the Land Registry SPARQL endpoint (complete history for the
    exact postcode); falls back to PropertyData radius results filtered to the
    exact postcode if SPARQL is unavailable."""
    sales = _fetch_land_registry_direct(postcode)
    if not sales:
        pd_sales, _ = get_all_sold_at_postcode(postcode)
        sales = [s for s in (pd_sales or []) if _sale_matches_postcode(s, postcode)]
    if not sales:
        return []
    seen = set()
    candidates = []
    for s in sorted(sales, key=lambda x: x.get("date", ""), reverse=True):
        addr = (s.get("address") or "").strip()
        if addr and addr not in seen:
            seen.add(addr)
            candidates.append({"address": addr, "last_date": s.get("date"), "last_price": s.get("price")})
    return candidates

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
                      scraper_days_on_market=None, scraper_floor_area_sqm=None,
                      price_reduced=False, original_asking_price=None,
                      reduction_date=None, reduction_amount=None, reduction_pct=None):
    formatted = format_postcode(postcode)
    region = postcode_to_region(postcode)
    comparables, postcode_used, broadened = get_sold_comparables(postcode, property_type)
    local_avg_sold = avg_sold_price(comparables)

    sold_diff_pct = None
    sold_verdict = None
    if local_avg_sold:
        sold_diff_pct = round(((asking_price - local_avg_sold) / local_avg_sold) * 100, 1)
        sold_verdict = "overpriced" if sold_diff_pct > 8 else ("value" if sold_diff_pct < -5 else "fair")

    if not floor_area_sqm and scraper_floor_area_sqm:
        floor_area_sqm = scraper_floor_area_sqm
    if not floor_area_sqm and EPC_API_KEY:
        floor_area_sqm = get_floor_area_from_epc(postcode, address)

    asking_psqm = local_avg_psqm = psqm_diff_pct = psqm_verdict = None
    size_matched_psqm = area_wide_psqm = None
    size_matched_count = 0
    psqm_basis = None
    psqm_implied_value = None

    # Always fetch psqf data — needed for comparables enrichment even without subject floor area
    benchmarks = get_psqm_benchmarks(postcode, property_type, floor_area_sqm)
    size_matched_psqm = benchmarks["size_matched_psqm"]
    area_wide_psqm = benchmarks["area_wide_psqm"]
    size_matched_count = benchmarks["size_matched_count"]
    psqf_points = benchmarks.get("psqf_points", [])

    # Build address lookup from psqf records for comparables enrichment
    psqf_lookup = {}
    for pt in psqf_points:
        addr = pt.get("address")
        sqf = pt.get("sqf")
        if addr and sqf and sqf > 0:
            key = re.sub(r"[^A-Z0-9]", "", addr.upper())
            sqm = round(sqf / 10.764, 1)
            price = pt.get("price")
            psqm_val = round(price / sqm) if price else round(price_per_sqft_to_sqm(pt["psqf"]))
            psqf_lookup[key] = {"sqm": sqm, "psqm": psqm_val}

    if floor_area_sqm and floor_area_sqm > 0:
        asking_psqm = round(asking_price / floor_area_sqm)
        local_avg_psqm = size_matched_psqm or area_wide_psqm
        psqm_basis = "size_matched" if size_matched_psqm else ("area_wide" if area_wide_psqm else None)
        if local_avg_psqm:
            psqm_implied_value = round(floor_area_sqm * local_avg_psqm)
            psqm_diff_pct = round(((asking_psqm - local_avg_psqm) / local_avg_psqm) * 100, 1)
            psqm_verdict = "overpriced" if psqm_diff_pct > 8 else ("value" if psqm_diff_pct < -5 else "fair")

    # HPI-adjusted last sale
    hpi_adjustment = None
    hpi_adjusted_value = None
    last_sale_candidates = []
    try:
        last_sale = find_last_sale(postcode, address=address)
        if last_sale:
            hpi_adjustment = calculate_hpi_adjustment(
                last_sale["price"], last_sale["date"], region
            )
            if hpi_adjustment:
                hpi_adjusted_value = hpi_adjustment["adjusted_price"]
        else:
            # No confident match — build candidates list for dropdown
            try:
                last_sale_candidates = get_last_sale_candidates(postcode)
            except Exception:
                pass
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
            c_price = c.get("price")
            c_sqm = c.get("floor_area_sqm") or c.get("sqm")
            if not c_sqm:
                c_addr_key = re.sub(r"[^A-Z0-9]", "", (c.get("address") or "").upper())
                psqf_match = psqf_lookup.get(c_addr_key)
                if psqf_match:
                    c_sqm = psqf_match["sqm"]
            c_psqm = round(c_price / c_sqm) if c_price and c_sqm and c_sqm > 0 else None
            comparables_list.append({
                "address": c.get("address", ""),
                "date": c.get("date", ""),
                "price": c_price,
                "price_formatted": f"£{c_price:,}" if c_price else "",
                "adjusted_price": c.get("adjusted_price"),
                "adjusted_price_formatted": f"£{c['adjusted_price']:,}" if c.get("adjusted_price") else "",
                "floor_area_sqm": c_sqm,
                "psqm": c_psqm,
                "psqm_formatted": f"£{c_psqm:,}/m²" if c_psqm else "",
            })
    except Exception as e:
        print(f"comparables_list build error: {e}")

    # ── FOOTBALL FIELD: build seven valuation methods ──────────────────────────

    methods = []

    # Method 1a: Raw comparable sold prices (no HPI adjustment, weight 1, context)
    raw_avg_sold = None
    if comparables:
        try:
            raw_prices = sorted(c["price"] for c in comparables if c.get("price"))
            if raw_prices:
                n_raw = len(raw_prices)
                q1_raw = max(0, n_raw // 4)
                q3_raw = min(n_raw - 1, n_raw - n_raw // 4)
                raw_avg_sold = round(sum(raw_prices) / n_raw)
                methods.append(_method_dict(
                    "Comparable sales (unadjusted)", raw_prices[q1_raw], raw_prices[q3_raw], raw_avg_sold,
                    "HM Land Registry (no HPI adjustment)", True, weight=1
                ))
            else:
                methods.append(_method_dict("Comparable sales (unadjusted)", 0, 0, 0, "HM Land Registry", False, weight=1))
        except Exception as e:
            print(f"Method 1a error: {e}")
            methods.append(_method_dict("Comparable sales (unadjusted)", 0, 0, 0, "HM Land Registry", False, weight=1))
    else:
        methods.append(_method_dict("Comparable sales (unadjusted)", 0, 0, 0, "HM Land Registry", False, weight=1))

    # Method 1b: Comparable sold prices HPI-adjusted (weight 2)
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
                "Comparable sales (HPI-adjusted)", m1_low, m1_high, m1_mid,
                "HM Land Registry (HPI-adjusted)", True, weight=2
            ))
        except Exception as e:
            print(f"Method 1b error: {e}")
            methods.append(_method_dict("Comparable sales (HPI-adjusted)", 0, 0, 0, "HM Land Registry", False, weight=2))
    else:
        methods.append(_method_dict("Comparable sales (HPI-adjusted)", 0, 0, 0, "HM Land Registry", False, weight=2))

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
            "Price per m²", m3_low, m3_high, psqm_implied_value,
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
                "Automated valuation", avm["low"], avm["high"], avm["mid"],
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
            "Lender valuation band", m6_low, m6_high, m6_mid,
            "Estimated lender valuation", True
        ))
    else:
        methods.append(_method_dict("Lender valuation band", 0, 0, 0, "Estimated lender valuation", False))

    # Method 6b: Rental yield implied value
    # Gross yield for UK residential typically 4-6%; we use 5% as target yield
    # Implied value = annual_rent / target_yield
    TARGET_GROSS_YIELD = 0.05
    try:
        monthly_rent = fetch_avg_rents(postcode_used, property_type, bedrooms)
        if monthly_rent and monthly_rent > 100:
            annual_rent = monthly_rent * 12
            # Range: 4% yield (higher value) to 6% yield (lower value)
            m_rent_high = round(annual_rent / 0.04)
            m_rent_low = round(annual_rent / 0.06)
            m_rent_mid = round(annual_rent / TARGET_GROSS_YIELD)
            methods.append(_method_dict(
                "Rental yield implied value", m_rent_low, m_rent_high, m_rent_mid,
                f"PropertyData avg rents ({_fmt(round(monthly_rent))}/mo)", True, weight=1
            ))
        else:
            methods.append(_method_dict("Rental yield implied value", 0, 0, 0, "PropertyData avg rents", False, weight=1))
    except Exception as e:
        print(f"Method 6b error: {e}")
        methods.append(_method_dict("Rental yield implied value", 0, 0, 0, "PropertyData avg rents", False, weight=1))

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
            "Asking-to-sold discount", m7_low, m7_high, m7_mid,
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
        open_offer = round(round(weighted_low + 0.30 * (weighted_high - weighted_low)) / 1000) * 1000
        target_price = round(weighted_midpoint / 1000) * 1000
        walk_away = round(weighted_high / 1000) * 1000
        # Safety check: ensure open < target < walk_away
        open_offer = min(open_offer, target_price - 1000)
        walk_away = max(walk_away, target_price + 1000)
        # Hard rule: never recommend an opening offer at or above the asking price —
        # opening above asking destroys credibility. Applies on every verdict.
        if asking_price:
            open_offer = min(open_offer, asking_price - 1000)
        # Hard rule: walk-away ceiling never exceeds 5% above the asking price.
        # Wide comparable sets (mixed house sizes) can inflate the weighted high
        # well past asking, which is poor advice for a buyer.
        if asking_price:
            walk_away = min(walk_away, round(asking_price * 1.05 / 1000) * 1000)
        # On overpriced properties, cap walk_away at asking_price - £1k so we never
        # recommend paying above asking for something priced above comparables
        if verdict == "overpriced" and asking_price:
            walk_away = min(walk_away, asking_price - 1000)
        # Re-enforce ordering after caps: open < target < walk_away
        target_price = min(target_price, walk_away - 1000)
        open_offer = min(open_offer, target_price - 1000)
        recommended_offer = open_offer

    # Confidence text: how far open offer is below asking vs below comparables
    open_offer_vs_asking_pct = None
    open_offer_vs_comps_pct = None
    if open_offer and asking_price:
        open_offer_vs_asking_pct = round(((asking_price - open_offer) / asking_price) * 100, 1)
    if open_offer and local_avg_sold:
        open_offer_vs_comps_pct = round(((local_avg_sold - open_offer) / local_avg_sold) * 100, 1)

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
        "last_sale_candidates": last_sale_candidates,
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
        "open_offer_vs_asking_pct": open_offer_vs_asking_pct,
        "open_offer_vs_comps_pct": open_offer_vs_comps_pct,
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


@app.route("/r/<report_id>/select-address")
def select_address(report_id):
    """User picks their property from the last-sale candidates dropdown.
    Rebuilds the report with the chosen address (which carries the house
    number Rightmove withheld) so HPI last sale and EPC floor area can match,
    then redirects back to the report."""
    if not re.fullmatch(r"[a-f0-9]{8,32}", report_id):
        return "Report not found", 404
    stored = load_report(report_id)
    if not stored:
        return "Report not found", 404

    chosen = (request.args.get("address") or "").strip()
    report = stored.get("report", {})
    candidates = report.get("last_sale_candidates") or []
    if not chosen or chosen not in {c.get("address") for c in candidates}:
        return redirect(f"/r/{report_id}")

    try:
        new_report = build_report_data(
            property_url=stored.get("property_url", ""),
            asking_price=report.get("asking_price"),
            bedrooms=report.get("bedrooms", "3"),
            property_type=report.get("property_type", "semi-detached"),
            postcode=report.get("postcode", ""),
            floor_area_sqm=report.get("floor_area_sqm"),
            address=chosen,
            scraper_days_on_market=report.get("days_on_market"),
            price_reduced=report.get("price_reduced", False),
            original_asking_price=report.get("original_asking_price"),
            reduction_date=report.get("reduction_date"),
            reduction_amount=report.get("reduction_amount"),
            reduction_pct=report.get("reduction_pct"),
        )
        stored["report"] = new_report
        stored["selected_address"] = chosen
        save_report(report_id, stored)
        log_event(report_id, "address_selected", {"address": chosen})
    except Exception as e:
        print(f"select_address rebuild error: {e}")
    return redirect(f"/r/{report_id}")


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

@app.route("/version")
def version():
    """Diagnostic: which commit is running and which routes are registered.
    RENDER_GIT_COMMIT is set automatically by Render on every deploy."""
    return jsonify({
        "commit": os.environ.get("RENDER_GIT_COMMIT", "unknown")[:12],
        "branch": os.environ.get("RENDER_GIT_BRANCH", "unknown"),
        "routes": sorted(str(r) for r in app.url_map.iter_rules()),
    })

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

@app.route("/debug-epc")
def debug_epc():
    """Test EPC floor area lookup. Usage: /debug-epc?postcode=WD4+9EW&address=9+Chantry+Close&key=ADMIN"""
    auth = request.args.get("key", "")
    if auth != os.environ.get("ADMIN_KEY", "set-an-admin-key"):
        return jsonify({"error": "Unauthorised"}), 403
    postcode = request.args.get("postcode", "")
    address = request.args.get("address", "")
    if not postcode:
        return jsonify({"error": "Pass ?postcode= and ?address="}), 400
    # Raw API call so we can see the actual response
    formatted_pc = format_postcode(postcode)
    raw_status = None
    raw_body = None
    try:
        raw_r = requests.get(
            f"{EPC_API_BASE}/api/domestic/search",
            params={"postcode": formatted_pc, "page_size": 100},
            headers={"Accept": "application/json", "Authorization": f"Bearer {EPC_API_KEY}"},
            timeout=10
        )
        raw_status = raw_r.status_code
        raw_body = raw_r.text[:500]
    except Exception as e:
        raw_body = str(e)

    results = _epc_search(postcode)
    match = _select_epc_match(results, address)
    floor_area = None
    cert = None
    if match:
        cert = _epc_fetch_certificate(match.get("certificateNumber", ""))
        floor_area = _extract_floor_area(cert) if cert else None
    return jsonify({
        "postcode": formatted_pc,
        "address": address,
        "raw_api_status": raw_status,
        "raw_api_body": raw_body,
        "epc_api_key_set": bool(EPC_API_KEY),
        "epc_results_count": len(results),
        "epc_results_sample": results[:3],
        "match": match,
        "floor_area_sqm": floor_area,
    })


@app.route("/debug-scrape-dates")
def debug_scrape_dates():
    """Dump raw PAGE_MODEL fields for diagnosing date and floor area extraction."""
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
    }
    floor_fields = {
        "floorAreaValue": prop.get("floorAreaValue"),
        "floorArea": prop.get("floorArea"),
        "floorAreaSqM": prop.get("floorAreaSqM"),
        "floorAreaSqFt": prop.get("floorAreaSqFt"),
        "totalFloorArea": prop.get("totalFloorArea"),
        "sizings": prop.get("sizings"),
        "propertySize": prop.get("propertySize"),
        "keyFeatures_floor": [f for f in (prop.get("keyFeatures") or []) if "m²" in str(f) or "sqft" in str(f).lower() or "sq ft" in str(f).lower() or "sqm" in str(f).lower()],
        "top_level_keys": list(prop.keys()) if isinstance(prop, dict) else [],
    }
    sold_fields = {
        "soldPropertyType": prop.get("soldPropertyType"),
        "tags": prop.get("tags"),
        "misInfo": prop.get("misInfo"),
        "text": prop.get("text"),
        "infoReelItems": prop.get("infoReelItems"),
        "features": prop.get("features"),
    }
    import re
    html_dates = {
        "added_on_pattern": re.findall(r"(?:Added\s+on|First\s+listed)[:\s]+(\d{1,2}\s+\w+\s+\d{4})", html, re.IGNORECASE),
        "reduced_on_pattern": re.findall(r"Reduced\s+on\s+(\d{1,2}\s+\w+\s+\d{4})", html, re.IGNORECASE),
        "floor_area_pattern": re.findall(r"(\d+(?:\.\d+)?)\s*(?:m²|sq\.?\s*m|sqm)", html, re.IGNORECASE),
        "sqft_pattern": re.findall(r"(\d+(?:\.\d+)?)\s*(?:sq\.?\s*ft|sqft)", html, re.IGNORECASE),
    }
    # Sold-history hunt: where does the "Year sold / Sold price" table live?
    sold_history = {
        "prices_key": prop.get("prices"),
        "soldHistory": prop.get("soldHistory"),
        "saleHistory": prop.get("saleHistory"),
        "transactionHistory": prop.get("transactionHistory"),
        "priceHistory_full": prop.get("priceHistory"),
        "html_has_year_sold": bool(re.search(r"[Yy]ear\s+sold", html)),
        "html_has_sold_price": bool(re.search(r"[Ss]old\s+price", html)),
        "html_keys_with_sold": sorted(set(re.findall(r'"(\w*[Ss]old\w*)"\s*:', html)))[:30],
        "html_keys_with_transaction": sorted(set(re.findall(r'"(\w*[Tt]ransaction\w*)"\s*:', html)))[:30],
        "html_around_485000": [html[max(0, m.start()-120):m.start()+40] for m in re.finditer(r"485[,.]?000", html)][:3],
        "html_around_398050": [html[max(0, m.start()-120):m.start()+40] for m in re.finditer(r"398[,.]?050", html)][:3],
    }
    # transactionHistory endpoint needs encId + deliveryPointId — locate both
    id_hunt = {
        "encId_top": prop.get("encId"),
        "id_top": prop.get("id"),
        "buildingId": prop.get("buildingId"),
        "address_raw": prop.get("address"),
        "location": prop.get("location"),
        "propertyUrls": prop.get("propertyUrls"),
        "html_keys_with_delivery": sorted(set(re.findall(r'"(\w*[Dd]elivery\w*)"\s*:', html)))[:20],
        "html_around_deliveryPointId": [html[max(0, m.start()-10):m.start()+80] for m in re.finditer(r"deliveryPointId", html)][:3],
        "html_around_108499996": [html[max(0, m.start()-80):m.start()+40] for m in re.finditer(r"108499996", html)][:3],
    }
    return jsonify({"date_fields": date_fields, "floor_area_fields": floor_fields, "sold_fields": sold_fields, "html_patterns": html_dates, "sold_history_hunt": sold_history, "id_hunt": id_hunt})

@app.route("/debug-sold")
def debug_sold():
    postcode = request.args.get("postcode", "WD4 9EW")
    address = request.args.get("address", "")
    property_type = request.args.get("type", "semi-detached")
    formatted = format_postcode(postcode)
    district = district_postcode(postcode)
    type_keys = normalise_type_sold(property_type)
    full_data = fetch_sold_prices(formatted)
    district_data = fetch_sold_prices(district)
    full_raw = full_data.get("data", {}).get("raw_data", []) if full_data else []
    district_raw = district_data.get("data", {}).get("raw_data", []) if district_data else []
    last_sale = find_last_sale(postcode, address=address or None)
    return jsonify({
        "postcode": formatted,
        "district": district,
        "address_used": address or None,
        "full_postcode_total": len(full_raw),
        "full_matching_type": len([t for t in full_raw if t.get("type") in type_keys]),
        "district_matching_type": len([t for t in district_raw if t.get("type") in type_keys]),
        "last_sale_found": last_sale,
        "all_sales_at_postcode": full_raw[:5],
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

    matched_raw = [p for p in points if p.get("type") in type_keys]
    return jsonify({
        "postcode_tried": formatted,
        "floor_area_sqm": floor_area_sqm,
        "type_keys_we_filter_for": type_keys,
        "total_points_returned": len(points),
        "all_types_present": sorted({p.get("type") for p in points}) if points else [],
        "matched_points_count": len(matched),
        "benchmarks": benchmarks,
        "sample_matched_records": matched_raw[:3],
    })

@app.route("/debug-report")
def debug_report():
    postcode = request.args.get("postcode", "WD4 9EW")
    asking_price = int(request.args.get("price", "675000"))
    property_type = request.args.get("type", "semi-detached")
    address = request.args.get("address", "")
    report = build_report_data("", asking_price, "3", property_type, postcode, address=address)
    return jsonify(report)

@app.route("/preview-paid")
def preview_paid():
    """Render paid report for any Rightmove URL without payment. Admin-key protected.
    Usage: /preview-paid?url=https://www.rightmove.co.uk/properties/XXX&key=YOUR_ADMIN_KEY
    """
    auth = request.args.get("key", "")
    if auth != os.environ.get("ADMIN_KEY", "set-an-admin-key"):
        return jsonify({"error": "Unauthorised"}), 403
    property_url = request.args.get("url", "")
    if not property_url:
        return "Pass ?url= with a Rightmove URL and ?key= with your admin key", 400
    postcode, asking_price, bedrooms, property_type, address, extra = merge_scraped_listing(
        property_url, "", 0, "3", "semi-detached", ""
    )
    if not postcode:
        return "Could not determine postcode from that URL.", 400
    # Optional &address= override: supply the full address (with house number)
    # when Rightmove's displayAddress is street-only
    if request.args.get("address"):
        address = request.args.get("address").strip()
    report = build_report_data(
        property_url=property_url,
        asking_price=asking_price,
        bedrooms=bedrooms,
        property_type=property_type,
        postcode=postcode,
        floor_area_sqm=None,
        address=address,
        **extra,
    )
    return render_template("report_paid.html", **report)


@app.route("/preview-free")
def preview_free():
    """Render free report for any Rightmove URL without payment. Admin-key protected.
    Usage: /preview-free?url=https://www.rightmove.co.uk/properties/XXX&key=YOUR_ADMIN_KEY
    """
    auth = request.args.get("key", "")
    if auth != os.environ.get("ADMIN_KEY", "set-an-admin-key"):
        return jsonify({"error": "Unauthorised"}), 403
    property_url = request.args.get("url", "")
    if not property_url:
        return "Pass ?url= with a Rightmove URL and ?key= with your admin key", 400
    postcode, asking_price, bedrooms, property_type, address, extra = merge_scraped_listing(
        property_url, "", 0, "3", "semi-detached", ""
    )
    if not postcode:
        return "Could not determine postcode from that URL.", 400
    # Optional &address= override: supply the full address (with house number)
    # when Rightmove's displayAddress is street-only
    if request.args.get("address"):
        address = request.args.get("address").strip()
    report = build_report_data(
        property_url=property_url,
        asking_price=asking_price,
        bedrooms=bedrooms,
        property_type=property_type,
        postcode=postcode,
        floor_area_sqm=None,
        address=address,
        **extra,
    )
    return render_template("report_free.html", **report)


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
