import os
import re
import json
import math
import time
import uuid
import base64
import hashlib
import threading
import requests
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify, render_template, redirect
from flask_cors import CORS
from datetime import datetime
from hpi_data import get_hpi_index as hpi_index, get_current_hpi
from property_scraper import scrape_property_url, fetch_sold_nearby, normalise_property_type

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
# Sector (e.g. "LS17 9") queries cover a smaller area than the district but
# can still be thin for less-traded property types. Require more records than
# the full-postcode minimum to avoid a 10–15 comp IQM on a small sub-area
# dragging the market average far from the district consensus.
MIN_SECTOR_COMPARABLES = 25


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

def sector_postcode(postcode):
    """Return the postcode sector, e.g. 'LS17 9' from 'LS17 9NA'."""
    raw = postcode.strip().upper().replace(" ", "")
    if len(raw) < 4:
        return district_postcode(postcode)
    return f"{raw[:-3]} {raw[-3]}"

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
        "is_new_build": scraped.get("is_new_build", False),
    }

    # Full-address resolution (sold-record coordinate match). Enhancement only:
    # the address is swapped in solely on a high-confidence match, so every
    # report that works today keeps working unchanged when resolution fails.
    try:
        resolution = resolve_full_address(scraped)
    except Exception as e:
        print(f"merge_scraped_listing: address resolution error: {e}")
        resolution = {"address": None, "confidence": None}
    extra["resolved_address"] = resolution.get("address")
    extra["address_resolution"] = resolution.get("confidence")
    if (resolution.get("confidence") == "high" and resolution.get("address")
            and not _leading_house_number(address or "")):
        address = resolution["address"]
    elif not _leading_house_number(address or ""):
        # Coordinate/EPC resolution didn't find a house number; try the description.
        desc_num = scraped.get("description_house_number")
        if desc_num and address:
            address = f"{desc_num} {address}"

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

def _epc_built_form_matches(cert, property_type):
    """Compare our property type against the certificate's built form / property type.
    Returns True when the certificate is compatible OR when the certificate carries
    no type information (absence of data must not exclude the true property)."""
    pt = (property_type or "").lower()
    built = ""
    for f in ("built_form", "built-form", "builtForm"):
        if cert.get(f):
            built = str(cert[f]).lower()
            break
    prop_kind = ""
    for f in ("property_type", "property-type", "propertyType"):
        if cert.get(f):
            prop_kind = str(cert[f]).lower()
            break
    if not built and not prop_kind:
        return True
    if "flat" in pt or "apartment" in pt or "maisonette" in pt:
        if prop_kind:
            return "flat" in prop_kind or "maisonette" in prop_kind
        return True
    # Houses/bungalows: if the cert says it's a flat, exclude
    if prop_kind and ("flat" in prop_kind or "maisonette" in prop_kind):
        return False
    if not built:
        return True
    if "semi" in pt:
        return "semi" in built
    if "detached" in pt:
        return built.startswith("detached")
    if "terrace" in pt:
        return "terrace" in built
    return True

def epc_cross_match(postcode, address=None, property_type=None, floor_area_sqm=None,
                    max_cert_fetches=30, trace=None):
    """Identify the subject property's EPC certificate WITHOUT a house number, by
    cross-matching listing attributes against the postcode's certificates:
    1. Street-token filter using the (street-only) listing address.
    2. Fetch full certificates for the survivors (capped) and require built form /
       property type compatibility, plus floor area within 10% when the listing
       supplied one.
    3. Only act on a UNIQUE survivor - multiple plausible matches means None.
    If more distinct addresses survive step 1 than we are willing to verify, give up
    rather than risk declaring a false unique match among a partial subset.
    Returns {address, floor_area_sqm, confidence, certificate_number} or None."""
    if trace is None:
        trace = {}
    results = _epc_search(postcode)
    trace["certificates_at_postcode"] = len(results or [])
    if not results:
        trace["outcome"] = "no EPC certificates found at this postcode"
        return None
    subj_streets = _street_tokens(address) if address else set()
    trace["street_tokens"] = sorted(subj_streets)
    candidates = []
    seen_addr = set()
    for r in results:
        line1 = (r.get("addressLine1") or "").strip()
        if not line1 or line1.upper() in seen_addr:
            continue
        seen_addr.add(line1.upper())
        # Tokenise the whole certificate address (not just the first segment) so
        # flat-style addresses like "Flat 1, Wilmot Court" still match their street
        line1_tokens = {t for t in _normalise_text(line1) if len(t) > 3}
        if subj_streets and not (subj_streets & line1_tokens):
            continue
        candidates.append(r)
    trace["street_filter_survivors"] = [
        (c.get("addressLine1") or "").strip() for c in candidates
    ]
    if not candidates:
        trace["outcome"] = "no certificates matched the street tokens"
        return None
    if len(candidates) > max_cert_fetches:
        trace["outcome"] = (
            f"{len(candidates)} candidates exceeds the cert-fetch cap of "
            f"{max_cert_fetches}; refusing to risk a false unique match"
        )
        return None

    # Floor area is a precise discriminator; property type is not — the listing's
    # agent-entered type and the EPC assessor's type frequently disagree (e.g. a
    # whole street of "detached" listings recorded as semi-detached on the EPC).
    # So when we have a floor area, decide on THAT and use type only to break a
    # tie. Property type vetoes a candidate only when we have no floor area to
    # compare. (Earlier logic let the noisy type field reject every candidate
    # before the floor area was ever checked — 0% resolution despite good data.)
    area_matched = []   # cert floor area within 10% of the listing's
    type_only = []      # type-compatible, used only when no listing floor area
    rejected = []
    # Fetch the candidate certificates in parallel — a busy street can have
    # 20-30, and serial fetches would be slow enough to risk a request timeout.
    def _fetch(r):
        return r, (_epc_fetch_certificate(r.get("certificateNumber") or "") or {})
    with ThreadPoolExecutor(max_workers=8) as cert_pool:
        cert_pairs = list(cert_pool.map(_fetch, candidates))
    for r, cert in cert_pairs:
        cand_addr = (r.get("addressLine1") or "").strip()
        area = _extract_floor_area(cert)
        type_ok = (not property_type) or _epc_built_form_matches(cert, property_type)
        if floor_area_sqm and area:
            if abs(area - floor_area_sqm) / floor_area_sqm <= 0.10:
                area_matched.append({"summary": r, "floor_area_sqm": area, "type_ok": type_ok})
            else:
                rejected.append(f"{cand_addr}: floor area {area} sqm outside 10% of {floor_area_sqm}")
        elif type_ok:
            type_only.append({"summary": r, "floor_area_sqm": area, "type_ok": True})
        else:
            rejected.append(f"{cand_addr}: built form mismatch (no floor area to compare)")

    if floor_area_sqm:
        pool = area_matched
        if len(pool) > 1 and property_type:
            typed = [m for m in pool if m["type_ok"]]
            if len(typed) == 1:
                trace["tiebreak"] = "property type broke a floor-area tie"
                pool = typed
    else:
        pool = type_only

    trace["rejected"] = rejected
    trace["verified_matches"] = [
        (m["summary"].get("addressLine1") or "").strip() for m in pool
    ]
    if len(pool) != 1:
        trace["outcome"] = f"{len(pool)} verified matches; need exactly 1 to act"
        return None
    trace["outcome"] = "unique match"
    m = pool[0]
    confidence = "accurate" if (floor_area_sqm and m["floor_area_sqm"]) else "approx"
    return {
        "address": (m["summary"].get("addressLine1") or "").strip(),
        "floor_area_sqm": m["floor_area_sqm"],
        "confidence": confidence,
        "certificate_number": m["summary"].get("certificateNumber"),
    }

def _epc_resolution(postcode, address, property_type, floor_area_sqm):
    """Resolve the full address (EPC cross-match, when the listing address has no
    house number) and the floor area from the EPC register, as one unit of work
    so it can run in parallel with other fetches.
    Returns (resolved_address, confidence, floor_area_sqm); any element may be
    None. EPC data is best-effort - network errors never propagate."""
    resolved = None
    confidence = None
    try:
        if address and not _leading_house_number(address):
            xm = epc_cross_match(postcode, address, property_type, floor_area_sqm)
            if xm and xm.get("address"):
                resolved = xm["address"]
                confidence = xm["confidence"]
                if not floor_area_sqm and xm.get("floor_area_sqm"):
                    floor_area_sqm = xm["floor_area_sqm"]
    except Exception as e:
        print(f"EPC cross-match error: {e}")
    if not floor_area_sqm:
        try:
            floor_area_sqm = get_floor_area_from_epc(postcode, resolved or address)
        except Exception as e:
            print(f"EPC floor-area error: {e}")
    return resolved, confidence, floor_area_sqm


# Short-lived sold-prices cache: address resolution, comparables and the
# last-sale lookup all need the same response within one report build.
# Sharing it keeps PropertyData usage at one call per postcode AND stops
# rapid back-to-back identical calls tripping the API's rate limit (which
# made the comparables fetch fail and fall back to district-wide data).
_SOLD_PRICES_CACHE = {}
_SOLD_PRICES_TTL_SECONDS = 120
_sold_prices_cache_lock = threading.Lock()

def fetch_sold_prices(postcode):
    key = (postcode or "").upper().replace(" ", "")
    now = time.time()
    with _sold_prices_cache_lock:
        hit = _SOLD_PRICES_CACHE.get(key)
        if hit and now - hit[0] < _SOLD_PRICES_TTL_SECONDS:
            return hit[1]
    try:
        r = requests.get(
            "https://api.propertydata.co.uk/sold-prices",
            params={"key": PROPERTYDATA_API_KEY, "postcode": postcode},
            timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            with _sold_prices_cache_lock:
                # Cache successes only; failures should retry next call
                _SOLD_PRICES_CACHE[key] = (now, data)
                if len(_SOLD_PRICES_CACHE) > 200:
                    oldest = min(_SOLD_PRICES_CACHE, key=lambda k: _SOLD_PRICES_CACHE[k][0])
                    del _SOLD_PRICES_CACHE[oldest]
            return data
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
        # Try sector (e.g. 'LS17 9') before jumping to the full district ('LS17'),
        # which can be too broad and mix premium and cheap sub-areas.
        sector = sector_postcode(postcode)
        if sector != district_postcode(postcode):
            data = fetch_sold_prices(sector)
            sector_comps = _filter_sold(data, type_keys)
            if len(sector_comps) >= MIN_SECTOR_COMPARABLES:
                comparables = sector_comps
                postcode_used = sector
                broadened = True
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
        comps = [t for t in transactions if t.get("type") in type_keys and t.get("price") and t.get("price") < 2_000_000]
        # Median band: exclude non-market transactions (partial transfers,
        # right-to-buy, inter-family sales) that Land Registry records at
        # far-from-market values. Anchored on the median, which an outlier
        # cannot drag the way it drags the mean. Kept loose (50%-200%) so it
        # removes junk data without shaping the genuine distribution.
        if len(comps) >= 5:
            prices = sorted(c["price"] for c in comps)
            n = len(prices)
            median = prices[n // 2] if n % 2 else (prices[n // 2 - 1] + prices[n // 2]) / 2
            comps = [c for c in comps if 0.5 * median <= c["price"] <= 2.0 * median]
        return comps
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
    """Fetch average monthly rent from PropertyData. Returns float (monthly rent) or None.
    The /rents endpoint returns rents PER WEEK ("for monthly values, multiply by
    4.333" per the docs), with the average nested under data.long_let."""
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
            avg = None
            if isinstance(inner, dict):
                long_let = inner.get("long_let") or {}
                if isinstance(long_let, dict):
                    avg = long_let.get("average") or long_let.get("mean")
                # Older/other response shapes
                if not avg:
                    beds_key = str(bedrooms) if bedrooms else None
                    if beds_key:
                        beds_data = inner.get(beds_key) or {}
                        if isinstance(beds_data, dict):
                            avg = beds_data.get("average") or beds_data.get("mean") or beds_data.get("avg")
                if not avg:
                    avg = inner.get("average") or inner.get("mean") or inner.get("avg")
            if avg:
                weekly = float(avg)
                return weekly * 4.333
    except Exception as e:
        print(f"fetch_avg_rents error: {e}")
    return None


def _avm_property_type(property_type):
    """Map our property type to PropertyData /valuation-sale values."""
    pt = (property_type or "").lower()
    if "flat" in pt or "apartment" in pt or "maisonette" in pt:
        return "flat"
    if "semi" in pt:
        return "semi-detached_house"
    if "terrace" in pt:
        return "terraced_house"
    if "detached" in pt:
        return "detached_house"
    if "bungalow" in pt:
        return "detached_bungalow"
    return "semi-detached_house"

def fetch_propertydata_avm(postcode, property_type, bedrooms=None, floor_area_sqm=None):
    """PropertyData /valuation-sale AVM. Requires internal_area (sq ft), so this
    method is unavailable without a floor area. Fields we cannot know from the
    listing are sent as honest middle-of-the-road defaults (bathrooms 1, average
    finish), which adds noise - the method carries standard weight only.
    Returns {low, mid, high} or None."""
    if not floor_area_sqm or floor_area_sqm <= 0:
        return None
    try:
        params = {
            "key": PROPERTYDATA_API_KEY,
            "postcode": postcode,
            "internal_area": round(float(floor_area_sqm) * 10.764),
            "property_type": _avm_property_type(property_type),
            "construction_date": "1914_2000",
            "bedrooms": int(bedrooms) if bedrooms else 3,
            "bathrooms": 1,
            "finish_quality": "average",
            "outdoor_space": "none" if "flat" in (property_type or "").lower() else "garden",
            "off_street_parking": "1",
        }
        r = requests.get(
            "https://api.propertydata.co.uk/valuation-sale",
            params=params,
            timeout=15
        )
        if r.status_code != 200:
            print(f"AVM error: {r.status_code} — {r.text[:200]}")
            return None
        data = r.json()
        inner = data.get("result") or data.get("data") or {}
        if not isinstance(inner, dict):
            return None
        mid = inner.get("estimate") or inner.get("valuation") or inner.get("value") or inner.get("mid")
        low = inner.get("lower_estimate") or inner.get("low") or inner.get("min")
        high = inner.get("upper_estimate") or inner.get("high") or inner.get("max")
        # PropertyData returns an estimate plus a margin. The margin is an
        # absolute GBP figure (e.g. 10000) unless it carries a % sign.
        if mid and not (low and high):
            margin = inner.get("margin_of_error") or inner.get("margin")
            if margin is not None:
                try:
                    if "%" in str(margin):
                        pct = float(str(margin).replace("%", "").strip()) / 100
                        low = float(mid) * (1 - pct)
                        high = float(mid) * (1 + pct)
                    else:
                        m = float(margin)
                        low = float(mid) - m
                        high = float(mid) + m
                except (ValueError, TypeError):
                    pass
        if low and high:
            low, high = int(float(low)), int(float(high))
            mid = int(float(mid)) if mid else (low + high) // 2
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


def fetch_psqf_points(postcode, property_type):
    """Fetch and parse sold £/sqf records for the area: full postcode first,
    district fallback. Returns a list of points (possibly empty)."""
    type_keys = normalise_type_listings(property_type)
    points = _psqf_points(fetch_sold_psqf(format_postcode(postcode)), type_keys)
    if not points:
        points = _psqf_points(fetch_sold_psqf(district_postcode(postcode)), type_keys)
    return points


def get_psqm_benchmarks(postcode, property_type, floor_area_sqm=None, points=None):
    """Return both sold £/sqm benchmarks:
      - area_wide_psqm: all comparable-type homes
      - size_matched_psqm: homes within ±20% of subject floor area (only if >=3, else None)
    Size-matched is the accurate like-for-like; area-wide is broad market context.
    Pass prefetched `points` (from fetch_psqf_points) to avoid refetching."""
    if points is None:
        points = fetch_psqf_points(postcode, property_type)
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

    # Step 3b: subject has no house number. A single surviving candidate is the
    # subject ONLY if the subject address actually carries that candidate's
    # identifier — a building name like "Overdale" that we resolved, or a matching
    # number. A bare street-only listing ("Abbotsbury Road") shares no such token
    # with a lone neighbouring sale ("112a Abbotsbury Road"), so it must fall
    # through to the picker rather than borrow that neighbour's price.
    distinct = {
        _leading_house_number(s.get("address") or "") or (s.get("address") or "").upper()
        for s in postcode_sales
    }
    if len(distinct) == 1:
        cand = sorted(postcode_sales, key=lambda x: x.get("date", ""), reverse=True)[0]
        cand_addr = cand.get("address") or ""
        subj_upper = (address or "").upper()
        cand_num = _leading_house_number(cand_addr)
        if cand_num:
            # Numbered candidate: trust only if that exact number is in the subject
            if re.search(rf"(?<![\dA-Za-z]){re.escape(cand_num)}(?![\dA-Za-z])", subj_upper):
                return cand
        else:
            # Named candidate (e.g. "Overdale"): trust only if the name is in subject
            cand_name = cand_addr.split(",")[0].strip().upper()
            if cand_name and cand_name in subj_upper:
                return cand

    # Step 3c: cannot confidently identify the property — caller shows the picker
    return None


def _haversine_m(lat1, lon1, lat2, lon2):
    """Distance in metres between two WGS84 points."""
    rlat1, rlon1, rlat2, rlon2 = map(math.radians, (float(lat1), float(lon1), float(lat2), float(lon2)))
    h = (math.sin((rlat2 - rlat1) / 2) ** 2
         + math.cos(rlat1) * math.cos(rlat2) * math.sin((rlon2 - rlon1) / 2) ** 2)
    return 6371000 * 2 * math.asin(math.sqrt(h))


# Coordinate-match thresholds: Rightmove pins sit on the delivery point, so the
# true property's sold record is normally within a few metres of the listing pin.
COORD_MATCH_MAX_M = 15       # winner must be at most this far from the pin
COORD_RUNNERUP_MIN_M = 25    # ...and the runner-up at least this far (else ambiguous)
COORD_PLAUSIBLE_MAX_M = 40   # beyond this the nearest record is not this property
EPC_CORROBORATION_MAX_CERTS = 5


def _sold_type_compatible(record_type, property_type):
    """Does a sold record's type label fit the listing's property type?
    Records with no type information are never excluded."""
    if not record_type or not property_type:
        return True
    rt = str(record_type)
    if rt in normalise_type_sold(property_type):
        return True
    return normalise_property_type(rt) == normalise_property_type(property_type)


def _epc_corroborates(postcode, candidate_address, floor_area_sqm, epc_results=None):
    """Cross-check a candidate full address against the EPC register: the house
    number and street must match a certificate, and when the listing supplied a
    floor area the certificate's must agree within 10%. Returns True only on
    positive corroboration; any failure or missing data returns False."""
    if not EPC_API_KEY:
        return False
    try:
        results = epc_results if epc_results is not None else _epc_search(postcode)
        match = _select_epc_match(results, candidate_address)
        if not match:
            return False
        if floor_area_sqm and match.get("certificateNumber"):
            cert = _epc_fetch_certificate(match["certificateNumber"])
            area = _extract_floor_area(cert) if cert else None
            if area and abs(area - floor_area_sqm) / floor_area_sqm > 0.10:
                return False
        return True
    except Exception as e:
        print(f"EPC corroboration error: {e}")
        return False


def resolve_full_address(scraped):
    """Resolve the full street address (incl. house number) for a scraped listing
    by matching it against historical sold-price records — the address-finder
    extension method. Returns {"address", "confidence"} where confidence is:
      "high"   - single unambiguous match (coordinate hit or unique sold record,
                 or EPC-corroborated)
      "medium" - best of multiple plausible candidates (callers must NOT swap
                 the address in on medium)
      None     - no match (e.g. new build, never sold)
    Enhancement only: never raises, and costs at most one PropertyData call."""
    try:
        scraped = scraped or {}
        address = scraped.get("address") or ""
        postcode = scraped.get("postcode") or ""
        property_type = scraped.get("property_type") or ""
        latitude = scraped.get("latitude")
        longitude = scraped.get("longitude")
        floor_area_sqm = scraped.get("floor_area_sqm")

        if not postcode:
            return {"address": None, "confidence": None}
        # Listing already shows the house number — nothing to resolve
        if address and _leading_house_number(address):
            return {"address": address, "confidence": "high"}

        # ── Primary: coordinate match against Rightmove sold records ─────────
        # Each sold record carries its own pin; the record sitting on top of the
        # listing pin IS the property. Free scrape, no PropertyData credits.
        coord_candidate = None
        if latitude and longitude:
            try:
                nearby = fetch_sold_nearby(postcode)
            except Exception as e:
                print(f"resolve_full_address: sold-nearby fetch failed: {e}")
                nearby = []
            ranked = []
            for rec in nearby:
                # Numbered or named ("Overdale, Warminster Road") — a sold
                # record always identifies a single property either way
                if not (rec.get("address") or "").strip():
                    continue
                if rec.get("latitude") is None or rec.get("longitude") is None:
                    continue
                dist = _haversine_m(latitude, longitude, rec["latitude"], rec["longitude"])
                ranked.append((dist, rec))
            ranked.sort(key=lambda x: x[0])
            typed = [(d, r) for d, r in ranked
                     if _sold_type_compatible(r.get("property_type"), property_type)]
            pool = typed or ranked
            if pool:
                dist, rec = pool[0]
                runner_up = pool[1][0] if len(pool) > 1 else None
                if dist <= COORD_MATCH_MAX_M and (runner_up is None or runner_up >= COORD_RUNNERUP_MIN_M):
                    return {"address": rec["address"], "confidence": "high"}
                if dist <= COORD_PLAUSIBLE_MAX_M:
                    # Near but not decisive (offset pin or close neighbours):
                    # let the EPC register settle it
                    if _epc_corroborates(postcode, rec["address"], floor_area_sqm):
                        return {"address": rec["address"], "confidence": "high"}
                    coord_candidate = rec["address"]

        # ── Fallback: street-token match against postcode sale history ───────
        # The single extra PropertyData call per report lives here. Full
        # postcode only - the district fallback in get_all_sold_at_postcode
        # would burn a second call to return rows we filter out anyway.
        sales = _all_sold_transactions(fetch_sold_prices(format_postcode(postcode)))
        postcode_sales = [s for s in (sales or []) if _sale_matches_postcode(s, postcode)]
        if address:
            street_filtered = [s for s in postcode_sales if _sale_matches_address(s, address)]
            if street_filtered:
                postcode_sales = street_filtered

        # Dedup to distinct properties: by house number when there is one,
        # otherwise by the property/building name (named houses have no number)
        distinct = {}
        for s in sorted(postcode_sales, key=lambda x: x.get("date", ""), reverse=True):
            addr = (s.get("address") or "").strip()
            if not addr:
                continue
            key = _leading_house_number(addr) or addr.split(",")[0].upper().strip()
            if key and key not in distinct:
                distinct[key] = s
        candidates = list(distinct.values())
        if not candidates:
            return {"address": coord_candidate, "confidence": "medium" if coord_candidate else None}

        typed = [s for s in candidates if _sold_type_compatible(s.get("type"), property_type)]
        pool = typed or candidates

        # A street-name match alone CANNOT identify the specific property. A single
        # surviving candidate only means one house on this street sold and landed in
        # our data — NOT that it is the subject (which may simply never have sold, as
        # with 120 Abbotsbury Road, where the only DT4 0JS sale was a neighbouring
        # flat at 112a). Without the coordinate pin, only EPC corroboration (house
        # number + street + floor area agreeing with a certificate) can earn "high".
        # Everything else is "medium", which the caller must NOT swap in: the report
        # keeps the street-only address and offers the address-picker rather than
        # silently presenting a neighbour's sale as this property's history.
        if floor_area_sqm and EPC_API_KEY and len(pool) <= EPC_CORROBORATION_MAX_CERTS:
            try:
                epc_results = _epc_search(postcode)
                corroborated = [s for s in pool
                                if _epc_corroborates(postcode, s["address"], floor_area_sqm, epc_results)]
                if len(corroborated) == 1:
                    return {"address": corroborated[0]["address"], "confidence": "high"}
            except Exception as e:
                print(f"resolve_full_address: EPC disambiguation failed: {e}")

        best = coord_candidate or sorted(pool, key=lambda s: s.get("date", ""), reverse=True)[0]["address"]
        return {"address": best, "confidence": "medium"}
    except Exception as e:
        print(f"resolve_full_address error: {e}")
        return {"address": None, "confidence": None}


def get_last_sale_candidates(postcode):
    """Return all distinct sold properties at this postcode, deduplicated by address.
    Primary: Land Registry SPARQL (exact postcode).
    Secondary: PropertyData radius results filtered to exact postcode.
    Fallback: unfiltered PropertyData radius results (new/reassigned postcodes where
    Land Registry holds the sale under a neighbouring postcode code). Flagged with
    is_radius_fallback=True so the template can adjust its wording."""
    radius_fallback = False
    sales = _fetch_land_registry_direct(postcode)
    if not sales:
        pd_data = fetch_sold_prices(format_postcode(postcode))
        pd_all = _all_sold_transactions(pd_data)
        exact = [s for s in pd_all if _sale_matches_postcode(s, postcode)]
        if exact:
            sales = exact
        elif pd_all:
            # No exact postcode match in PropertyData either — postcode is new or was
            # recently reassigned. Surface the radius records anyway so the user can
            # still identify their property by sold price/date.
            sales = pd_all
            radius_fallback = True
    if not sales:
        return []
    seen = set()
    candidates = []
    for s in sorted(sales, key=lambda x: x.get("date", ""), reverse=True):
        addr = (s.get("address") or "").strip()
        if addr and addr not in seen:
            seen.add(addr)
            candidates.append({
                "address": addr,
                "last_date": s.get("date"),
                "last_price": s.get("price"),
                "radius_fallback": radius_fallback,
            })
    return candidates

def resolve_address_by_sale_fingerprint(postcode, sold_price, sold_date=None):
    """Pin the exact address by matching a user-supplied past sale against the
    postcode's Land Registry sold records. Land Registry prices are exact to the
    pound and the address travels with each record, so a (price[, year]) pair is
    a near-unique fingerprint within a postcode - more reliable than floor area.
    The user reads these figures off the Rightmove listing (the sale history is
    shown in their browser but not in the HTML we fetch, so it cannot be obtained
    server-side). Returns {address, confidence, candidates}:
      high   - exactly one distinct address matches -> auto-resolve
      medium - several addresses match (returns them for the picker)
      none   - no match (never sold / pre-1995 / figure not in our data)
    Never raises; costs no extra PropertyData call (SPARQL primary)."""
    try:
        target_price = int(re.sub(r"[^0-9]", "", str(sold_price or "")))
        if not postcode or not target_price:
            return {"address": None, "confidence": None, "candidates": []}
        # Full postcode history (address + price + date): SPARQL primary,
        # PropertyData backup - the same sources as the candidate dropdown.
        sales = _fetch_land_registry_direct(postcode)
        if not sales:
            pd_sales, _ = get_all_sold_at_postcode(postcode)
            sales = [s for s in (pd_sales or []) if _sale_matches_postcode(s, postcode)]
        year = None
        if sold_date:
            m = re.search(r"(?:19|20)\d{2}", str(sold_date))
            if m:
                year = m.group(0)
        matches = []
        for s in sales:
            price = s.get("price")
            if not price:
                continue
            # Exact price (both sides are Land Registry); a tiny tolerance only
            # absorbs a user mistyping the last digits.
            if abs(int(price) - target_price) > max(500, round(target_price * 0.003)):
                continue
            if year and (s.get("date") or "")[:4] and (s.get("date") or "")[:4] != year:
                continue
            matches.append(s)
        distinct = {}
        for s in sorted(matches, key=lambda x: x.get("date", ""), reverse=True):
            addr = (s.get("address") or "").strip()
            if addr and addr not in distinct:
                distinct[addr] = s
        addrs = list(distinct.keys())
        if len(addrs) == 1:
            return {"address": addrs[0], "confidence": "high", "candidates": addrs}
        if len(addrs) > 1:
            return {"address": None, "confidence": "medium", "candidates": addrs}
        return {"address": None, "confidence": None, "candidates": []}
    except Exception as e:
        print(f"resolve_address_by_sale_fingerprint error: {e}")
        return {"address": None, "confidence": None, "candidates": []}


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
                      reduction_date=None, reduction_amount=None, reduction_pct=None,
                      resolved_address=None, address_resolution=None,
                      is_new_build=False,
                      tier="paid"):
    """Build the full report payload.
    tier="free": cheap calls only - comparables, HPI maths, days on market,
      asking-to-sold, plus the SPARQL last sale (free, so the previous sold
      price, HPI-adjusted value and address dropdown appear on both tiers).
      EPC, £/sqf, rents and AVM are skipped, so those methods show n/a.
    tier="paid": everything, with independent external calls run in parallel."""
    paid_tier = tier != "free"
    formatted = format_postcode(postcode)
    region = postcode_to_region(postcode)

    if not floor_area_sqm and scraper_floor_area_sqm:
        floor_area_sqm = scraper_floor_area_sqm

    # ── DATA GATHERING ──────────────────────────────────────────────────────────
    # Independent external calls run in parallel: wall time becomes roughly the
    # slowest call rather than the sum of all of them. Phase 1 needs only the
    # raw listing inputs; phase 2 needs phase 1 outputs (postcode_used from the
    # comparables broadening, address/floor area from the EPC resolution).
    monthly_rent = None
    avm = None
    discount_pct = None
    local_avg_dom = None
    # resolved_address / address_resolution arrive as parameters from the
    # sold-record resolution in merge_scraped_listing; the EPC cross-match
    # below may still overwrite them with a better outcome
    last_sale = None
    psqf_points = []

    with ThreadPoolExecutor(max_workers=6) as pool:
        fut_comps = pool.submit(get_sold_comparables, postcode, property_type)
        fut_epc = fut_psqf = fut_rents = None
        if paid_tier:
            if EPC_API_KEY:
                fut_epc = pool.submit(_epc_resolution, postcode, address,
                                      property_type, floor_area_sqm)
            fut_psqf = pool.submit(fetch_psqf_points, postcode, property_type)
            fut_rents = pool.submit(fetch_avg_rents, formatted, property_type, bedrooms)

        comparables, postcode_used, broadened = fut_comps.result()

        fut_dom = pool.submit(fetch_avg_dom, postcode_used)
        fut_ratio = pool.submit(fetch_asking_sold_ratio, postcode_used, property_type)

        if fut_epc is not None:
            try:
                epc_resolved, epc_confidence, epc_area = fut_epc.result()
                if epc_resolved:
                    resolved_address = epc_resolved
                    address = epc_resolved
                    address_resolution = epc_confidence
                if not floor_area_sqm and epc_area:
                    floor_area_sqm = epc_area
            except Exception as e:
                print(f"EPC resolution error: {e}")

        fut_avm = None
        if paid_tier:
            fut_avm = pool.submit(fetch_propertydata_avm, postcode_used,
                                  property_type, bedrooms, floor_area_sqm)
        # Last sale uses Land Registry SPARQL (free, no PropertyData credits),
        # so both tiers get the previous sold price and HPI-adjusted value.
        fut_last = pool.submit(find_last_sale, postcode, address)

        if fut_psqf is not None:
            try:
                psqf_points = fut_psqf.result() or []
            except Exception as e:
                print(f"psqf fetch error: {e}")

        if fut_rents is not None:
            try:
                monthly_rent = fut_rents.result()
            except Exception as e:
                print(f"rents fetch error: {e}")

        if fut_avm is not None:
            try:
                avm = fut_avm.result()
            except Exception as e:
                print(f"AVM fetch error: {e}")

        if fut_last is not None:
            try:
                last_sale = fut_last.result()
            except Exception as e:
                print(f"last sale lookup error: {e}")

        try:
            local_avg_dom = fut_dom.result()
        except Exception as e:
            print(f"avg dom fetch error: {e}")

        try:
            discount_pct = fut_ratio.result()
        except Exception as e:
            print(f"asking-sold ratio fetch error: {e}")

    # ── DERIVED VALUES (all local computation from here) ────────────────────────

    local_avg_sold = avg_sold_price(comparables)

    sold_diff_pct = None
    sold_verdict = None
    if local_avg_sold:
        sold_diff_pct = round(((asking_price - local_avg_sold) / local_avg_sold) * 100, 1)
        sold_verdict = "overpriced" if sold_diff_pct > 8 else ("value" if sold_diff_pct < -5 else "fair")

    asking_psqm = local_avg_psqm = psqm_diff_pct = psqm_verdict = None
    size_matched_psqm = area_wide_psqm = None
    size_matched_count = 0
    psqm_basis = None
    psqm_implied_value = None

    benchmarks = get_psqm_benchmarks(postcode, property_type, floor_area_sqm,
                                     points=psqf_points)
    size_matched_psqm = benchmarks["size_matched_psqm"]
    area_wide_psqm = benchmarks["area_wide_psqm"]
    size_matched_count = benchmarks["size_matched_count"]

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

    # HPI-adjusted last sale (last_sale fetched in the parallel phase, paid only)
    hpi_adjustment = None
    hpi_adjusted_value = None
    last_sale_candidates = []
    try:
        if last_sale:
            hpi_adjustment = calculate_hpi_adjustment(
                last_sale["price"], last_sale["date"], region
            )
            if hpi_adjustment:
                hpi_adjusted_value = hpi_adjustment["adjusted_price"]
        else:
            # No confident match - build candidates list for the dropdown
            # on both tiers so the user can pick their address
            try:
                last_sale_candidates = get_last_sale_candidates(postcode)
            except Exception:
                pass
    except Exception as e:
        print(f"HPI section error: {e}")

    # Days on market: local average fetched in the parallel phase
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
                # Interquartile mean, matching the HPI-adjusted row's midpoint
                # method, so the difference between the two rows is purely the
                # HPI adjustment rather than a change of averaging method
                if n_raw >= 5:
                    trimmed = raw_prices[q1_raw:n_raw - q1_raw]
                    raw_avg_sold = round(sum(trimmed) / len(trimmed))
                else:
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

    # Method 5: Online estimate (AVM, fetched in the parallel phase, paid only)
    if avm:
        methods.append(_method_dict(
            "Automated valuation", avm["low"], avm["high"], avm["mid"],
            "Automated valuation model", True
        ))
    else:
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

    # Method 6b: Rental yield implied value (rent fetched in the parallel phase
    # using the FULL postcode - the broadened district postcode breaks /rents).
    # Gross yield for UK residential typically 4-6%; we use 5% as target yield.
    # Implied value = annual_rent / target_yield
    TARGET_GROSS_YIELD = 0.05
    try:
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

    # Method 7: Asking-to-sold discount (ratio fetched in the parallel phase)
    try:
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
        "address": address,
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
        "resolved_address": resolved_address,
        "address_resolution": address_resolution,
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
        "tier": tier,
        "is_new_build": is_new_build,
    }

# ── BACKGROUND REPORT BUILDS ──────────────────────────────────────────────────
# Paid-tier builds make many external calls and can exceed the request timeout,
# so they run in a daemon thread and store the result; /r/<id> serves a
# self-refreshing "generating" page until the stored status flips to ready.

def _now_iso():
    return datetime.utcnow().isoformat() + "Z"


def _start_paid_build_from_url(report_id, property_url, address_override=None):
    """Spawn a background thread: scrape the listing, build the paid-tier report
    and store it under report_id. Failures are recorded on the stored report."""
    def work():
        try:
            postcode, asking_price, bedrooms, property_type, address, extra = merge_scraped_listing(
                property_url, "", 0, "3", "semi-detached", ""
            )
            if not postcode:
                raise ValueError("Could not determine the postcode from that listing")
            log_event(report_id, "address_resolved", {
                "confidence": extra.get("address_resolution") or "none",
                "resolved_address": extra.get("resolved_address"),
            })
            if address_override:
                address = address_override
            report = build_report_data(
                property_url=property_url,
                asking_price=asking_price,
                bedrooms=bedrooms,
                property_type=property_type,
                postcode=postcode,
                floor_area_sqm=None,
                address=address,
                tier="paid",
                **extra,
            )
            stored = load_report(report_id) or {}
            stored.update({"report": report, "status": "ready", "paid": True})
            save_report(report_id, stored)
        except Exception as e:
            print(f"Background build error ({report_id}): {e}")
            stored = load_report(report_id) or {}
            stored.update({"status": "failed", "error": str(e)})
            save_report(report_id, stored)
    threading.Thread(target=work, daemon=True).start()


def _start_rebuild(report_id, stored, address=None, tier="paid"):
    """Spawn a background thread that rebuilds an existing stored report (after
    an address selection or a paid unlock). On failure the previous report data
    is kept and served rather than a dead page."""
    report = stored.get("report", {})
    def work():
        try:
            new_report = build_report_data(
                property_url=stored.get("property_url", "") or report.get("property_url", ""),
                asking_price=report.get("asking_price"),
                bedrooms=report.get("bedrooms", "3"),
                property_type=report.get("property_type", "semi-detached"),
                postcode=report.get("postcode", ""),
                floor_area_sqm=report.get("floor_area_sqm"),
                address=address or stored.get("selected_address"),
                scraper_days_on_market=report.get("days_on_market"),
                price_reduced=report.get("price_reduced", False),
                original_asking_price=report.get("original_asking_price"),
                reduction_date=report.get("reduction_date"),
                reduction_amount=report.get("reduction_amount"),
                reduction_pct=report.get("reduction_pct"),
                is_new_build=report.get("is_new_build", False),
                tier=tier,
            )
            latest = load_report(report_id) or {}
            latest["report"] = new_report
            latest["status"] = "ready"
            if address:
                latest["selected_address"] = address
            save_report(report_id, latest)
        except Exception as e:
            print(f"Report rebuild error ({report_id}): {e}")
            latest = load_report(report_id) or {}
            latest["status"] = "ready"
            save_report(report_id, latest)
    threading.Thread(target=work, daemon=True).start()


def _building_page(report_id):
    """Progress page shown while a report builds in the background. A spinner
    and a step-by-step checklist reassure the user the page is not frozen;
    JavaScript polls /r/<id>/status and reloads the moment the report is ready.
    A <noscript> meta refresh covers browsers with JavaScript disabled."""
    return """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<noscript><meta http-equiv="refresh" content="5"></noscript>
<title>Generating your report…</title>
<link href="https://fonts.googleapis.com/css2?family=Lora:wght@600;700&family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  body {font-family:'Plus Jakarta Sans',-apple-system,sans-serif;
        padding:64px 24px;text-align:center;color:#1e1c18;background:#f7f3ed;}
  h1 {font-family:'Lora',serif;font-size:1.5rem;margin-bottom:8px;color:#1e1c18;}
  .sub {color:#5c5849;max-width:30rem;margin:0 auto 32px;}
  .spinner {width:44px;height:44px;margin:0 auto 28px;border-radius:50%;
            border:4px solid #e0d9ce;border-top-color:#1a6b5a;
            animation:spin 0.9s linear infinite;}
  @keyframes spin {to {transform:rotate(360deg);}}
  ul.steps {list-style:none;padding:20px 24px;max-width:24rem;margin:0 auto;text-align:left;
            background:#ffffff;border:1px solid #e0d9ce;border-radius:14px;
            box-shadow:0 1px 4px rgba(30,28,24,0.06), 0 2px 12px rgba(30,28,24,0.04);}
  ul.steps li {padding:7px 0;color:#9b9488;font-size:0.95rem;transition:color 0.4s;}
  ul.steps li::before {content:"○";display:inline-block;width:1.5em;color:#e0d9ce;}
  ul.steps li.active {color:#1e1c18;font-weight:600;}
  ul.steps li.active::before {content:"●";color:#1a6b5a;animation:pulse 1.2s ease-in-out infinite;}
  ul.steps li.done {color:#5c5849;}
  ul.steps li.done::before {content:"✓";color:#238f77;}
  @keyframes pulse {50% {opacity:0.35;}}
  .hint {color:#9b9488;font-size:0.85rem;margin-top:32px;}
</style></head>
<body>
<div class="spinner"></div>
<h1>Generating your report…</h1>
<p class="sub">We are pulling live data from several sources. This usually takes
under a minute and the page will update by itself.</p>
<ul class="steps" id="steps">
  <li>Reading the property listing</li>
  <li>Pulling Land Registry sold prices</li>
  <li>Checking EPC records and floor area</li>
  <li>Fetching local rents and market data</li>
  <li>Running the valuation models</li>
  <li>Writing your report</li>
</ul>
<p class="hint">Stuck for more than a couple of minutes? Refresh manually or
re-submit the property link.</p>
<script>
(function () {
  var steps = document.querySelectorAll("#steps li");
  var i = 0;
  steps[0].className = "active";
  // Advance the checklist on a timer that roughly tracks the real build,
  // holding on the last step until the report actually arrives.
  var stepTimer = setInterval(function () {
    if (i >= steps.length - 1) { clearInterval(stepTimer); return; }
    steps[i].className = "done";
    i += 1;
    steps[i].className = "active";
  }, 6000);
  function poll() {
    fetch("/r/__REPORT_ID__/status", {cache: "no-store"})
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.status && d.status !== "building") { location.reload(); }
        else { setTimeout(poll, 3000); }
      })
      .catch(function () { setTimeout(poll, 5000); });
  }
  setTimeout(poll, 3000);
})();
</script>
</body></html>""".replace("__REPORT_ID__", report_id)


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

    # Background builds: serve the self-refreshing page until ready. Builds older
    # than 5 minutes are presumed dead (worker restart) - fall back to the previous
    # report data if there is any, otherwise mark failed.
    status = stored.get("status", "ready")
    if status == "building":
        started = stored.get("build_started_at") or stored.get("created_at") or ""
        stale = False
        try:
            t0 = datetime.fromisoformat(started.replace("Z", ""))
            stale = (datetime.utcnow() - t0).total_seconds() > 300
        except Exception:
            pass
        if not stale:
            return _building_page(report_id)
        if stored.get("report"):
            stored["status"] = "ready"
        else:
            stored["status"] = "failed"
            stored.setdefault("error", "Report build timed out")
        save_report(report_id, stored)
        status = stored["status"]
    if status == "failed":
        return ("<html><body style='font-family:sans-serif;padding:60px;text-align:center;'>"
                "<h1>Sorry, we could not build this report</h1>"
                f"<p style='color:#51606f;'>{stored.get('error', 'Unknown error')}</p>"
                "<p><a href='https://houseoffer.netlify.app'>Try again →</a></p>"
                "</body></html>", 500)

    log_event(report_id, "report_viewed", {
        "user_agent": request.headers.get("User-Agent", "")[:200],
        "referer": request.headers.get("Referer", "")[:200],
    })

    report = stored.get("report", {})
    report_url = f"{BASE_URL.rstrip('/')}/r/{report_id}"
    paid = stored.get("paid", False)
    template = "report_paid.html" if paid else "report_free.html"
    return render_template(template, report_url=report_url, report_id=report_id, **report)


@app.route("/r/<report_id>/status")
def report_status(report_id):
    """Lightweight build-status poll used by the generating page."""
    if not re.fullmatch(r"[a-f0-9]{8,32}", report_id):
        return jsonify({"status": "not_found"}), 404
    stored = load_report(report_id)
    if not stored:
        return jsonify({"status": "not_found"}), 404
    return jsonify({"status": stored.get("status", "ready")})


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

    # Rebuild in the background at the report's own tier: the chosen address
    # carries the house number, unlocking HPI last sale and EPC floor area.
    stored["status"] = "building"
    stored["build_started_at"] = _now_iso()
    save_report(report_id, stored)
    log_event(report_id, "address_selected", {"address": chosen})
    _start_rebuild(report_id, stored, address=chosen,
                   tier="paid" if stored.get("paid") else "free")
    return redirect(f"/r/{report_id}")


@app.route("/r/<report_id>/resolve-by-sale")
def resolve_by_sale(report_id):
    """Pin the exact address from a past sale the user reads off the Rightmove
    listing (price, optionally year), then rebuild on a unique match. The
    annotated picker handles 'I recognise my sold figure'; this handles 'I'll
    type it' and is the accuracy path when the candidate list is long."""
    if not re.fullmatch(r"[a-f0-9]{8,32}", report_id):
        return "Report not found", 404
    stored = load_report(report_id)
    if not stored:
        return "Report not found", 404
    report = stored.get("report", {})
    postcode = report.get("postcode", "")
    price = re.sub(r"[^0-9]", "", request.args.get("price", "") or "")
    date_raw = (request.args.get("date", "") or "").strip() or None
    if not price or not postcode:
        return redirect(f"/r/{report_id}")
    res = resolve_address_by_sale_fingerprint(postcode, price, date_raw)
    if res["confidence"] == "high" and res["address"]:
        stored["status"] = "building"
        stored["build_started_at"] = _now_iso()
        save_report(report_id, stored)
        log_event(report_id, "address_resolved_by_sale",
                  {"address": res["address"], "price": price, "date": date_raw})
        _start_rebuild(report_id, stored, address=res["address"],
                       tier="paid" if stored.get("paid") else "free")
    else:
        log_event(report_id, "sale_fingerprint_no_unique_match",
                  {"price": price, "date": date_raw,
                   "confidence": res["confidence"], "candidates": len(res["candidates"])})
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
    # Free-tier reports lack the paid-only data (EPC, last sale, rents, AVM,
    # per-sqf), so unlocking triggers a paid-tier rebuild in the background.
    rebuilding = (stored.get("report") or {}).get("tier") != "paid"
    if rebuilding:
        stored["status"] = "building"
        stored["build_started_at"] = _now_iso()
        save_report(report_id, stored)
        _start_rebuild(report_id, stored, tier="paid")
    else:
        save_report(report_id, stored)
    log_event(report_id, "report_unlocked", {})
    return jsonify({"status": "unlocked", "report_id": report_id, "rebuilding": rebuilding})

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

@app.route("/debug-listing-history")
def debug_listing_history():
    """Find the subject property's sold-price history on its own listing page.
    Rightmove shows a 'Property sale history' (e.g. 2013 £200,000) tied to the
    exact property. This reports whether those figures are embedded in the page
    HTML (extractable for free) or loaded by a separate API call, and where.
    Admin-key protected. Usage: /debug-listing-history?url=<rightmove>&key=ADMIN"""
    auth = request.args.get("key", "")
    if auth != os.environ.get("ADMIN_KEY", "set-an-admin-key"):
        return jsonify({"error": "Unauthorised"}), 403
    url = request.args.get("url", "")
    if not url:
        return jsonify({"error": "Pass ?url= with a Rightmove listing URL"}), 400
    from property_scraper import _fetch_html, _parse_rightmove_page_model
    html = _fetch_html(url, referer="https://www.rightmove.co.uk/")
    if not html:
        return jsonify({"fetched": False})
    # Distinctive figures from the example; also generic field-name probes
    probes = ["66000", "66,000", "200000", "200,000", "soldPropertyHistory",
              "salesHistory", "saleHistory", "priceHistory", "soldPrice",
              "transactions", "yearSold", "dateSold", "1998", "2013"]
    out = {"fetched": True, "length": len(html),
           "raw_contains": {p: (p in html) for p in probes}}
    for tok in ("soldPropertyHistory", "salesHistory", "saleHistory", "66000", "yearSold", "soldPrice"):
        i = html.find(tok)
        if i >= 0:
            out["snippet_token"] = tok
            out["snippet"] = html[max(0, i - 300):i + 900]
            break
    model = _parse_rightmove_page_model(html)
    prop = (model or {}).get("propertyData") or model or {}
    if isinstance(prop, dict):
        out["propertyData_keys"] = sorted(prop.keys())
        out["history_fields"] = {k: v for k, v in prop.items()
                                 if re.search(r"hist|sold|sale|transact", k, re.I)}
    return jsonify(out)

@app.route("/debug-soldfetch")
def debug_soldfetch():
    """Diagnose the Rightmove house-prices fetch that feeds coordinate matching.
    Tries several URL formats, reports HTTP status + length for each, and for any
    that load, which JSON markers are present and a snippet around the property
    data — so the parser can be fixed against the real page. Admin-key protected.
    Usage: /debug-soldfetch?postcode=DT4+0JS&key=ADMIN"""
    auth = request.args.get("key", "")
    if auth != os.environ.get("ADMIN_KEY", "set-an-admin-key"):
        return jsonify({"error": "Unauthorised"}), 403
    from property_scraper import BROWSER_HEADERS, _request_kwargs
    pc = (request.args.get("postcode", "") or "").upper().replace(" ", "")
    if len(pc) < 5:
        return jsonify({"error": "Pass ?postcode= e.g. DT4+0JS"}), 400
    outcode, incode = pc[:-3], pc[-3:]
    candidate_urls = [
        f"https://www.rightmove.co.uk/house-prices/{outcode}-{incode}.html".lower(),
        f"https://www.rightmove.co.uk/house-prices/{pc}.html".lower(),
        f"https://www.rightmove.co.uk/house-prices/search.html?searchLocation={outcode}+{incode}",
    ]
    attempts = []
    for u in candidate_urls:
        info = {"url": u}
        try:
            r = requests.get(u, headers=BROWSER_HEADERS, timeout=20,
                             allow_redirects=True, **_request_kwargs())
            html = r.text or ""
            info["status"] = r.status_code
            info["final_url"] = r.url
            info["length"] = len(html)
            if r.status_code == 200 and len(html) > 2000:
                markers = ["PAGE_MODEL", "__PRELOADED_STATE__", "__NEXT_DATA__",
                           "window.__PRELOADED_STATE__", '"properties"', '"location"',
                           '"latitude"', '"lat"', '"transactions"', '"displayPrice"',
                           "captcha", "Access Denied", "are not a robot", "blocked"]
                info["markers"] = {m: (m in html) for m in markers}
                anchor = -1
                for token in ('"properties"', '"location"', '"transactions"', '"latitude"'):
                    anchor = html.find(token)
                    if anchor >= 0:
                        info["anchor_token"] = token
                        break
                info["snippet"] = (html[max(0, anchor - 150):anchor + 1200]
                                   if anchor >= 0 else html[:1200])
        except Exception as e:
            info["error"] = str(e)
        attempts.append(info)
    # Also report what the live fetch_sold_nearby currently returns
    try:
        recs = fetch_sold_nearby(pc)
        attempts.append({"fetch_sold_nearby_count": len(recs),
                         "fetch_sold_nearby_sample": recs[:2]})
    except Exception as e:
        attempts.append({"fetch_sold_nearby_error": str(e)})
    return jsonify({"postcode": pc, "attempts": attempts})

@app.route("/debug-resolve")
def debug_resolve():
    """Trace full-address resolution for a listing: scraped fields, the sold
    records found near the pin (with distances), and the final decision.
    Admin-key protected (may burn one PropertyData call).
    Usage: /debug-resolve?url=https://www.rightmove.co.uk/properties/XXX&key=ADMIN"""
    auth = request.args.get("key", "")
    if auth != os.environ.get("ADMIN_KEY", "set-an-admin-key"):
        return jsonify({"error": "Unauthorised"}), 403
    url = request.args.get("url", "")
    if not url:
        return jsonify({"error": "Pass ?url= with a Rightmove listing URL"}), 400
    scraped = scrape_property_url(url)
    nearby = []
    nearby_error = None
    try:
        nearby = fetch_sold_nearby(scraped.get("postcode") or "")
    except Exception as e:
        nearby_error = str(e)
    lat, lng = scraped.get("latitude"), scraped.get("longitude")
    for rec in nearby:
        if lat and lng and rec.get("latitude") is not None and rec.get("longitude") is not None:
            rec["distance_m"] = round(_haversine_m(lat, lng, rec["latitude"], rec["longitude"]), 1)
    resolution = resolve_full_address(scraped)
    return jsonify({
        "scraped": scraped,
        "sold_nearby_count": len(nearby),
        "sold_nearby_error": nearby_error,
        "sold_nearby": sorted(nearby, key=lambda r: r.get("distance_m", 1e9))[:20],
        "resolution": resolution,
    })

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


@app.route("/debug-epc-resolve")
def debug_epc_resolve():
    """Measure the EPC cross-match auto-resolution rate against real listings.
    Scrapes each Rightmove URL, runs the EPC register cross-match on the scraped
    attributes (street + type + floor area), and reports whether it resolved a
    unique address. Accepts multiple ?url= params. Admin-key protected.
    Usage: /debug-epc-resolve?key=ADMIN&url=<rightmove1>&url=<rightmove2>"""
    auth = request.args.get("key", "")
    if auth != os.environ.get("ADMIN_KEY", "set-an-admin-key"):
        return jsonify({"error": "Unauthorised"}), 403
    urls = request.args.getlist("url")
    if not urls:
        return jsonify({"error": "Pass one or more ?url= Rightmove listing URLs"}), 400
    results = []
    resolved = 0
    for u in urls:
        row = {"url": u}
        try:
            sc = scrape_property_url(u)
            row["scraped"] = {
                "postcode": sc.get("postcode"),
                "address": sc.get("address"),
                "property_type": sc.get("property_type"),
                "floor_area_sqm": sc.get("floor_area_sqm"),
                "has_house_number": bool(_leading_house_number(sc.get("address") or "")),
            }
            if not sc.get("postcode"):
                row["outcome"] = "no postcode scraped"
            elif row["scraped"]["has_house_number"]:
                row["outcome"] = "listing already shows house number"
                row["resolved_address"] = sc.get("address")
                resolved += 1
            else:
                trace = {}
                match = epc_cross_match(sc.get("postcode"), sc.get("address"),
                                        sc.get("property_type"), sc.get("floor_area_sqm"),
                                        trace=trace)
                row["epc_trace"] = trace
                if match and match.get("address"):
                    row["resolved_address"] = match["address"]
                    row["confidence"] = match.get("confidence")
                    row["outcome"] = "EPC resolved"
                    resolved += 1
                else:
                    row["outcome"] = "EPC could not uniquely resolve"
        except Exception as e:
            row["outcome"] = f"error: {type(e).__name__}: {e}"
        results.append(row)
    return jsonify({
        "tested": len(urls),
        "resolved": resolved,
        "resolution_rate": f"{round(100 * resolved / len(urls))}%" if urls else "0%",
        "results": results,
    })

@app.route("/debug-epc-match")
def debug_epc_match():
    """Test EPC cross-matching without a house number.
    Usage: /debug-epc-match?postcode=WR2+5SG&address=Wilmot+Drive&type=detached&sqm=92&key=ADMIN"""
    auth = request.args.get("key", "")
    if auth != os.environ.get("ADMIN_KEY", "set-an-admin-key"):
        return jsonify({"error": "Unauthorised"}), 403
    postcode = request.args.get("postcode", "")
    if not postcode:
        return jsonify({"error": "Pass ?postcode= at minimum"}), 400
    address = request.args.get("address", "") or None
    property_type = request.args.get("type", "") or None
    sqm = float(request.args.get("sqm", 0) or 0) or None
    trace = {}
    try:
        match = epc_cross_match(postcode, address, property_type, sqm, trace=trace)
    except Exception as e:
        match = None
        trace["error"] = f"{type(e).__name__}: {e}"
    return jsonify({
        "postcode": format_postcode(postcode),
        "address": address,
        "property_type": property_type,
        "floor_area_sqm": sqm,
        "match": match,
        "trace": trace,
    })

@app.route("/debug-rents")
def debug_rents():
    """Raw PropertyData /rents response plus our parsed monthly value.
    Usage: /debug-rents?postcode=B23+7DY&bedrooms=3&key=ADMIN"""
    auth = request.args.get("key", "")
    if auth != os.environ.get("ADMIN_KEY", "set-an-admin-key"):
        return jsonify({"error": "Unauthorised"}), 403
    postcode = request.args.get("postcode", "")
    bedrooms = request.args.get("bedrooms", "")
    if not postcode:
        return jsonify({"error": "Pass ?postcode="}), 400
    params = {"key": PROPERTYDATA_API_KEY, "postcode": format_postcode(postcode)}
    if bedrooms:
        params["bedrooms"] = bedrooms
    raw_status = raw_body = None
    try:
        r = requests.get("https://api.propertydata.co.uk/rents", params=params, timeout=10)
        raw_status = r.status_code
        raw_body = r.json() if r.status_code == 200 else r.text[:500]
    except Exception as e:
        raw_body = str(e)
    monthly = fetch_avg_rents(format_postcode(postcode), "", bedrooms or None)
    return jsonify({
        "postcode": format_postcode(postcode),
        "raw_status": raw_status,
        "raw_response": raw_body,
        "parsed_monthly_rent": monthly,
    })

@app.route("/debug-avm")
def debug_avm():
    """Raw PropertyData /valuation-sale response plus our parsed result.
    Usage: /debug-avm?postcode=B23+7DY&type=semi-detached&bedrooms=3&sqm=85&key=ADMIN"""
    auth = request.args.get("key", "")
    if auth != os.environ.get("ADMIN_KEY", "set-an-admin-key"):
        return jsonify({"error": "Unauthorised"}), 403
    postcode = request.args.get("postcode", "")
    property_type = request.args.get("type", "semi-detached")
    bedrooms = request.args.get("bedrooms", "3")
    sqm = float(request.args.get("sqm", 0) or 0) or None
    if not postcode:
        return jsonify({"error": "Pass ?postcode= and ?sqm="}), 400
    params = {
        "key": PROPERTYDATA_API_KEY,
        "postcode": format_postcode(postcode),
        "internal_area": round(sqm * 10.764) if sqm else None,
        "property_type": _avm_property_type(property_type),
        "construction_date": "1914_2000",
        "bedrooms": bedrooms,
        "bathrooms": 1,
        "finish_quality": "average",
        "outdoor_space": "none" if "flat" in property_type.lower() else "garden",
        "off_street_parking": "1",
    }
    raw_status = raw_body = None
    try:
        r = requests.get("https://api.propertydata.co.uk/valuation-sale", params=params, timeout=15)
        raw_status = r.status_code
        raw_body = r.json() if r.status_code == 200 else r.text[:500]
    except Exception as e:
        raw_body = str(e)
    parsed = fetch_propertydata_avm(format_postcode(postcode), property_type, bedrooms, sqm)
    return jsonify({
        "params_sent": {k: v for k, v in params.items() if k != "key"},
        "raw_status": raw_status,
        "raw_response": raw_body,
        "parsed": parsed,
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
    if request.args.get("key") != os.environ.get("ADMIN_KEY", "set-an-admin-key"):
        return jsonify({"error": "Unauthorised"}), 403
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
    sparql_raw = _fetch_land_registry_direct(postcode)
    last_sale = find_last_sale(postcode, address=address or None)
    candidates = get_last_sale_candidates(postcode)
    hpi = None
    if last_sale:
        region = postcode_to_region(postcode)
        hpi = calculate_hpi_adjustment(last_sale["price"], last_sale["date"], region)
    return jsonify({
        "postcode": formatted,
        "district": district,
        "address_used": address or None,
        "sparql_count": len(sparql_raw),
        "sparql_sample": sparql_raw[:5],
        "full_postcode_total": len(full_raw),
        "full_matching_type": len([t for t in full_raw if t.get("type") in type_keys]),
        "district_matching_type": len([t for t in district_raw if t.get("type") in type_keys]),
        "last_sale_found": last_sale,
        "candidates_count": len(candidates),
        "candidates": candidates[:10],
        "hpi_adjustment": hpi,
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

@app.route("/batch-resolve-test")
def batch_resolve_test():
    """Run address resolution on a fixed test batch and return scored JSON.
    Admin-key protected. Intended for the daily resolution-quality loop.
    Usage: /batch-resolve-test?key=ADMIN_KEY
    Optional: &url0=...&url1=... to override individual batch entries (0-indexed)."""
    if request.args.get("key") != os.environ.get("ADMIN_KEY", "set-an-admin-key"):
        return jsonify({"error": "Unauthorised"}), 403

    BATCH = [
        {"url": "https://www.rightmove.co.uk/properties/174255275", "label": "3b terraced SE25 London"},
        {"url": "https://www.rightmove.co.uk/properties/171324848", "label": "2b terraced SE26 Sydenham"},
        {"url": "https://www.rightmove.co.uk/properties/174960656", "label": "3b terraced S10 Sheffield"},
        {"url": "https://www.rightmove.co.uk/properties/171389720", "label": "3b semi BS10 Bristol"},
        {"url": "https://www.rightmove.co.uk/properties/88209348",  "label": "5b detached BS9 Bristol"},
        {"url": "https://www.rightmove.co.uk/properties/173962388", "label": "2b flat B23 Birmingham"},
        {"url": "https://www.rightmove.co.uk/properties/171192797", "label": "1b flat new-build B1 Birmingham"},
        {"url": "https://www.rightmove.co.uk/properties/87488955",  "label": "3b flat EH10 Edinburgh"},
        {"url": "https://www.rightmove.co.uk/properties/174671255", "label": "3b semi LS25 Leeds"},
        {"url": "https://www.rightmove.co.uk/properties/174908915", "label": "4b detached new-build CW11 Sandbach"},
    ]
    # Allow individual URL overrides via ?url0=..., ?url1=... etc.
    for i in range(len(BATCH)):
        override = request.args.get(f"url{i}")
        if override:
            BATCH[i] = {"url": override, "label": f"override-{i}"}

    def _test_one(item):
        url = item["url"]
        row = {
            "url": url,
            "label": item["label"],
            "postcode": None,
            "address_scraped": None,
            "description_house_number": None,
            "is_new_build": False,
            "floor_area_sqm": None,
            "resolved_address": None,
            "resolution_confidence": None,
            "method": "picker",
            "auto_resolved": False,
            "picker_candidates": 0,
            "scrape_ok": False,
            "error": None,
        }
        try:
            scraped = scrape_property_url(url)
            postcode = scraped.get("postcode")
            addr = scraped.get("address") or ""
            row["postcode"] = postcode
            row["address_scraped"] = addr
            row["description_house_number"] = scraped.get("description_house_number")
            row["is_new_build"] = bool(scraped.get("is_new_build"))
            row["floor_area_sqm"] = scraped.get("floor_area_sqm")
            row["scrape_ok"] = bool(postcode)

            if not postcode:
                row["error"] = "no postcode scraped — Rightmove blocked or listing removed"
                return row

            # Tier 0: listing already carries the house/flat number
            if _leading_house_number(addr):
                row["method"] = "listing-has-number"
                row["resolved_address"] = addr
                row["resolution_confidence"] = "high"
                row["auto_resolved"] = True
                return row

            # Tier 1+2: coordinate / EPC resolution
            resolution = resolve_full_address(scraped)
            row["resolved_address"] = resolution.get("address")
            row["resolution_confidence"] = resolution.get("confidence")
            if resolution.get("confidence") == "high":
                row["method"] = "auto-resolved"  # coordinate or EPC
                row["auto_resolved"] = True
                return row

            # Tier 3: house number from description text (applied automatically in real flow)
            if row["description_house_number"] and addr:
                row["method"] = "description"
                row["resolved_address"] = f"{row['description_house_number']} {addr}"
                row["resolution_confidence"] = "medium"
                row["auto_resolved"] = True
                return row

            # Picker fallback
            row["method"] = "picker"
            candidates = get_last_sale_candidates(postcode)
            row["picker_candidates"] = len(candidates)

        except Exception as exc:
            row["error"] = str(exc)
        return row

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(_test_one, BATCH))

    # Summary
    total = len(results)
    auto = sum(1 for r in results if r.get("auto_resolved"))
    method_counts = {}
    for r in results:
        m = r.get("method", "unknown")
        method_counts[m] = method_counts.get(m, 0) + 1
    miss_classes = {}
    for r in results:
        if r.get("method") == "picker" or r.get("error"):
            cls = "new-build" if r.get("is_new_build") else (
                "scrape-error" if r.get("error") else (
                "no-floor-area" if not r.get("floor_area_sqm") else "uniform-street"))
            miss_classes[cls] = miss_classes.get(cls, 0) + 1

    return jsonify({
        "run_at": datetime.utcnow().isoformat() + "Z",
        "total": total,
        "auto_resolved": auto,
        "auto_resolve_pct": round(100 * auto / total) if total else 0,
        "picker_fallback": sum(1 for r in results if r.get("method") == "picker"),
        "new_build_count": sum(1 for r in results if r.get("is_new_build")),
        "scrape_errors": sum(1 for r in results if r.get("error")),
        "method_counts": method_counts,
        "miss_classes": miss_classes,
        "results": results,
    })


@app.route("/debug-report")
def debug_report():
    """Raw report JSON. Admin-key protected (paid tier burns API credits).
    Usage: /debug-report?postcode=..&price=..&type=..&tier=free|paid&key=ADMIN"""
    auth = request.args.get("key", "")
    if auth != os.environ.get("ADMIN_KEY", "set-an-admin-key"):
        return jsonify({"error": "Unauthorised"}), 403
    postcode = request.args.get("postcode", "WD4 9EW")
    asking_price = int(request.args.get("price", "675000"))
    property_type = request.args.get("type", "semi-detached")
    address = request.args.get("address", "")
    tier = request.args.get("tier", "paid")
    report = build_report_data("", asking_price, "3", property_type, postcode,
                               address=address, tier=tier)
    return jsonify(report)

@app.route("/preview-paid")
def preview_paid():
    """Build the paid report for any Rightmove URL without payment. Admin-key
    protected. The build runs in the background: this returns immediately with a
    redirect to /r/<id>, which shows a self-refreshing page until the report is
    ready. Optional &address= override for street-only listings.
    Usage: /preview-paid?url=https://www.rightmove.co.uk/properties/XXX&key=YOUR_ADMIN_KEY
    """
    auth = request.args.get("key", "")
    if auth != os.environ.get("ADMIN_KEY", "set-an-admin-key"):
        return jsonify({"error": "Unauthorised"}), 403
    property_url = request.args.get("url", "")
    if not property_url:
        return "Pass ?url= with a Rightmove URL and ?key= with your admin key", 400
    address_override = (request.args.get("address") or "").strip() or None
    report_id = uuid.uuid4().hex[:12]
    save_report(report_id, {
        "status": "building",
        "paid": True,
        "preview": True,
        "property_url": property_url,
        "created_at": _now_iso(),
        "build_started_at": _now_iso(),
    })
    _start_paid_build_from_url(report_id, property_url, address_override)
    return redirect(f"/r/{report_id}")


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
        tier="free",
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
        tier="free",
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
    # Paid tier on this public JSON endpoint requires the admin key (credits)
    tier = "free"
    if data.get("tier") == "paid" and data.get("key") == os.environ.get("ADMIN_KEY", "set-an-admin-key"):
        tier = "paid"
    report = build_report_data(
        property_url=property_url,
        asking_price=asking_price,
        bedrooms=bedrooms,
        property_type=property_type,
        postcode=postcode,
        floor_area_sqm=float(data.get("floor_area_sqm", 0) or 0) or None,
        address=address,
        tier=tier,
        **extra,
    )
    return jsonify(report)

def normalise_buyer_estimate(raw):
    """Buyers usually type shorthand like "285" or "285k" meaning £285,000.
    Returns the estimate in pounds with thousands separators ("285,000"),
    or "" if the input is not a number. Anything under 10,000 is treated
    as thousands - no UK property sells for less than £10,000."""
    s = str(raw or "").strip().lower().replace(",", "").replace("£", "").replace(" ", "")
    if not s:
        return ""
    multiplier = 1
    if s.endswith("k"):
        s = s[:-1]
        multiplier = 1000
    try:
        value = float(s) * multiplier
    except ValueError:
        return ""
    if 0 < value < 10000:
        value *= 1000
    return f"{int(round(value)):,}"


@app.route("/submit", methods=["POST"])
def submit():
    data = request.get_json(silent=True) or request.form
    to_email       = data.get("email", "")
    property_url   = data.get("property-url", "") or data.get("property_url", "")
    buyer_estimate = normalise_buyer_estimate(data.get("buyer_estimate", ""))
    # These may be pre-filled by the frontend; the scraper will override with
    # live values if it finds better data.
    asking_price   = int(str(data.get("asking_price", 0) or 0).replace(",", "").replace("£", "")) or 0
    bedrooms       = data.get("bedrooms", "3")
    property_type  = data.get("property_type", "semi-detached")
    postcode       = data.get("postcode", "")
    address        = data.get("address", "")
    floor_area_sqm = float(data.get("floor_area_sqm", 0) or 0) or None

    if not to_email:
        return jsonify({"error": "Email address required"}), 400
    if not property_url:
        return jsonify({"error": "Property link required"}), 400
    if "rightmove.co.uk" not in property_url.lower():
        return jsonify({"error": "We currently support Rightmove links only. Zoopla support coming soon."}), 400

    # Dedup on email + URL before we do any slow work
    if _is_duplicate_submission(to_email, property_url):
        print(f"DUPLICATE submission blocked: {to_email} | {property_url[:80]}")
        return jsonify({"status": "sent", "deduped": True})

    # Return immediately so Render's 30-second proxy timeout is never reached.
    # Everything else (scraping, report build, email, Sheets) runs in a daemon thread.
    report_id  = uuid.uuid4().hex[:12]
    report_url = f"{BASE_URL.rstrip('/')}/r/{report_id}"
    created_at = datetime.utcnow().isoformat() + "Z"

    save_report(report_id, {
        "status": "building",
        "build_started_at": created_at,
        "created_at": created_at,
        "email": to_email,
        "property_url": property_url,
        "buyer_estimate": buyer_estimate,
        "paid": False,
    })

    _url = property_url
    _ap  = asking_price
    _br  = bedrooms
    _pt  = property_type
    _pc  = postcode
    _fa  = floor_area_sqm
    _ad  = address
    _be  = buyer_estimate
    _rid = report_id
    _ru  = report_url
    _em  = to_email

    def _build():
        try:
            pc, ap, br, pt, ad, extra = merge_scraped_listing(
                _url, _pc, _ap, _br, _pt, _ad
            )
            if not pc:
                raise ValueError("Could not determine postcode from that link.")
            if not ap:
                raise ValueError("Could not determine asking price from that link.")
            log_event(_rid, "address_resolved", {
                "confidence": extra.get("address_resolution") or "none",
                "resolved_address": extra.get("resolved_address"),
            })

            report = build_report_data(
                property_url=_url,
                asking_price=ap,
                bedrooms=br,
                property_type=pt,
                postcode=pc,
                floor_area_sqm=_fa,
                address=ad,
                tier="free",
                **extra,
            )

            anchor_bias = None
            if _be and report.get("local_avg_sold"):
                try:
                    est   = int(str(_be).replace(",", "").replace("£", "").replace(" ", ""))
                    local = report["local_avg_sold"]
                    anchor_bias = round(((est - local) / local) * 100, 1)
                except Exception:
                    pass

            stored = load_report(_rid) or {}
            stored.update({"status": "ready", "report": report,
                           "buyer_estimate": _be, "anchor_bias": anchor_bias})
            save_report(_rid, stored)

            post_to_sheets({
                "type": "submission",
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "uuid": _rid, "email": _em,
                "postcode": report["postcode"],
                "property_type": report["property_type"],
                "asking_price": ap, "verdict": report["verdict"],
                "buyer_estimate": _be or "", "anchor_bias": anchor_bias,
                "property_url": _url, "report_url": _ru,
            })
            log_event(_rid, "submission_created", {
                "email": _em, "postcode": report["postcode"],
                "verdict": report["verdict"], "asking_price": ap,
                "anchor_bias": anchor_bias,
            })
            try:
                email_html = render_template("report_email.html", report_url=_ru, **report)
                send_report_email(_em, email_html, report["postcode"], report["verdict"], report_url=_ru)
                notify_owner(_em, _url, report["postcode"], report["verdict"], _be, anchor_bias)
            except Exception as e:
                print(f"Email error in background build ({_rid}): {e}")
        except Exception as exc:
            print(f"Background submit error ({_rid}): {exc}")
            stored = load_report(_rid) or {}
            stored["status"] = "failed"
            stored["error"] = str(exc)
            save_report(_rid, stored)

    threading.Thread(target=_build, daemon=True).start()

    # Return "sent" so existing frontend code that checks status === "sent" keeps working.
    # The report is still building; the user is redirected to report_url which shows
    # the generating page until the background thread marks status="ready".
    return jsonify({"status": "sent", "report_id": report_id, "report_url": report_url})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
