import os
import re
import json
import math
import time
import uuid
import base64
import hashlib
import hmac
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
# Reports and engagement events are stored as JSON files under DATA_DIR.
# On Render, DATA_DIR points at a mounted PERSISTENT DISK (/var/data) so reports
# and events survive deploys and restarts — a shareable /r/<id> link keeps working.
# Falls back to /tmp when DATA_DIR is unset (e.g. local dev), which is ephemeral.
DATA_DIR = os.environ.get("DATA_DIR", "/tmp")
REPORTS_DIR = os.path.join(DATA_DIR, "houseoffer_reports")
EVENTS_DIR = os.path.join(DATA_DIR, "houseoffer_events")
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

# ── CROWD VOTING STORAGE ──────────────────────────────────────────────────────
# Votes live as JSON files per report (fast local reads for the live feed) and
# every vote ALSO streams to the Google Sheets webhook, which is the durable
# copy: on a redeploy the live feed resets but no vote data is lost.
# Share slugs map a short public code (/v/xk29p) to a report_id.
VOTES_DIR = "/tmp/houseoffer_votes"
SLUGS_DIR = "/tmp/houseoffer_slugs"
os.makedirs(VOTES_DIR, exist_ok=True)
os.makedirs(SLUGS_DIR, exist_ok=True)
_votes_lock = threading.Lock()
MAX_VOTES_PER_REPORT = 500
_SLUG_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/O/1/l/i lookalikes


def _load_votes(report_id):
    try:
        path = os.path.join(VOTES_DIR, f"{report_id}.json")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        print(f"_load_votes error: {e}")
        return []


def _save_votes(report_id, votes):
    try:
        with open(os.path.join(VOTES_DIR, f"{report_id}.json"), "w") as f:
            json.dump(votes, f)
        return True
    except Exception as e:
        print(f"_save_votes error: {e}")
        return False


def _vote_summary(votes, exclude_token=None):
    """Feed payload for the report + voting pages. count/average cover ALL
    votes; the visible feed excludes the requester's own vote (the page
    renders that as a local "You" row) and caps the list."""
    count = len(votes)
    crowd_avg = round(sum(v["estimate"] for v in votes) / count) if count else None
    feed = [{"name": v.get("name") or None, "estimate": v["estimate"]}
            for v in reversed(votes) if v.get("token") != exclude_token][:50]
    return {"count": count, "crowd_avg": crowd_avg, "votes": feed}


def _slug_path(slug):
    return os.path.join(SLUGS_DIR, f"{slug}.json")


def _mint_vote_slug(report_id):
    """Create (or reuse) the short public voting slug for a report."""
    import secrets
    for _ in range(20):
        slug = "".join(secrets.choice(_SLUG_ALPHABET) for _ in range(5))
        if not os.path.exists(_slug_path(slug)):
            with open(_slug_path(slug), "w") as f:
                json.dump({"report_id": report_id,
                           "created_at": datetime.utcnow().isoformat() + "Z"}, f)
            return slug
    raise RuntimeError("could not mint a unique vote slug")


def _resolve_vote_slug(slug):
    """slug -> report_id, or None."""
    if not re.fullmatch(r"[a-z0-9]{4,10}", slug or ""):
        return None
    try:
        path = _slug_path(slug)
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return (json.load(f) or {}).get("report_id")
    except Exception as e:
        print(f"_resolve_vote_slug error: {e}")
        return None

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
# Stripe — £29 report unlock. Checkout sessions are created server-side via the
# REST API (same requests-based pattern as Resend/PropertyData, no SDK).
# Fulfilment (paid=True + paid-tier rebuild) runs from BOTH the success
# redirect and the webhook; whichever lands first unlocks, the other no-ops.
# While STRIPE_SECRET_KEY is unset, checkout CTAs fall back to the pricing page.
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_REPORT_PRICE_PENCE = int(os.environ.get("STRIPE_REPORT_PRICE_PENCE", "2900"))
# The "£29 Offer Report" product in the Stripe dashboard. Checkout bills
# against the product's reusable default Price (resolved once, created at
# STRIPE_REPORT_PRICE_PENCE if the product has none, then persisted under
# DATA_DIR). STRIPE_REPORT_PRICE_ID pins an explicit price and skips the
# lookup. If neither resolves, checkout falls back to inline price_data so
# a buyer is never blocked by a pricing hiccup.
STRIPE_REPORT_PRODUCT_ID = os.environ.get("STRIPE_REPORT_PRODUCT_ID", "prod_UrNYSfZHnc85pz")
STRIPE_REPORT_PRICE_ID = os.environ.get("STRIPE_REPORT_PRICE_ID", "")
STRIPE_PRICE_CACHE_PATH = os.path.join(DATA_DIR, "houseoffer_stripe", "report_price.json")
_stripe_price_lock = threading.Lock()
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

def _canonical_sold_type(raw):
    """Map ANY property-type label — from PropertyData, Land Registry or Rightmove
    — to one of our canonical types, or None. Robust to the many variants that
    caused zero-comp results (P1): 'terraced_house', 'Terraced', 'End Terrace',
    'Semi-Detached House', 'detached_bungalow', 'Apartment', etc. Order matters:
    'flat' and 'semi' are tested before 'detached' (semi-detached contains
    'detached')."""
    t = (raw or "").lower()
    if not t:
        return None
    if "flat" in t or "apartment" in t or "maisonette" in t:
        return "flat"
    if "semi" in t:
        return "semi-detached"
    if "terrace" in t:
        return "terraced"
    if "detached" in t:
        return "detached"
    if "bungalow" in t:
        return "detached"  # consistent with the scraper's normalise_property_type
    return None

def normalise_type_sold(property_type):
    """Legacy key-list form, kept for the address-resolution type check and debug
    endpoints. The production comparable filter now uses _canonical_sold_type."""
    if not property_type:
        return None
    mapping = {
        "semi-detached": ["semi-detached_house", "semi_detached_house", "Semi-Detached"],
        "detached":      ["detached_house", "Detached"],
        "terraced":      ["terraced_house", "Terraced"],
        "flat":          ["flat", "Flat"],
    }
    return mapping.get(property_type.lower())

def normalise_type_listings(property_type):
    """Legacy key-list form for debug endpoints. None when unknown."""
    if not property_type:
        return None
    mapping = {
        "semi-detached": ["semi-detached_house", "semi_detached_house"],
        "detached":      ["detached_house"],
        "terraced":      ["terraced_house"],
        "flat":          ["flat"],
    }
    return mapping.get(property_type.lower())

def price_per_sqft_to_sqm(p):
    return p * 10.764

def extract_postcode_from_url(url):
    pc_pattern = r'([A-Z]{1,2}\d{1,2}[A-Z]?\s?\d[A-Z]{2})'
    match = re.search(pc_pattern, url.upper())
    if match:
        return match.group(1).replace(" ", "").upper()
    return None

# Valid GB (UK) postcode AREA codes — the leading alpha prefix of a postcode.
# Used by the random-URL test harness to reject non-GB / placeholder listings
# (e.g. Republic of Ireland Eircodes "D83 1EB", placeholder codes "A71B 0AC")
# which are postcode-shaped but never resolve to real UK sales data.
VALID_GB_POSTCODE_AREAS = {
    "AB", "AL", "B", "BA", "BB", "BD", "BH", "BL", "BN", "BR", "BS", "BT", "CA",
    "CB", "CF", "CH", "CM", "CO", "CR", "CT", "CV", "CW", "DA", "DD", "DE", "DG",
    "DH", "DL", "DN", "DT", "DY", "E", "EC", "EH", "EN", "EX", "FK", "FY", "G",
    "GL", "GU", "GY", "HA", "HD", "HG", "HP", "HR", "HS", "HU", "HX", "IG", "IM",
    "IP", "IV", "JE", "KA", "KT", "KW", "KY", "L", "LA", "LD", "LE", "LL", "LN",
    "LS", "LU", "M", "ME", "MK", "ML", "N", "NE", "NG", "NN", "NP", "NR", "NW",
    "OL", "OX", "PA", "PE", "PH", "PL", "PO", "PR", "RG", "RH", "RM", "S", "SA",
    "SE", "SG", "SK", "SL", "SM", "SN", "SO", "SP", "SR", "SS", "ST", "SW", "SY",
    "TA", "TD", "TF", "TN", "TQ", "TR", "TS", "TW", "UB", "W", "WA", "WC", "WD",
    "WF", "WN", "WR", "WS", "WV", "YO", "ZE",
}

_GB_POSTCODE_RE = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]?\d[A-Z]{2}$")  # compact form

def is_valid_gb_postcode(postcode):
    """True only for structurally valid UK postcodes whose AREA is a real GB
    postcode area. Rejects Irish Eircodes ("D83 1EB") and placeholders ("A71B 0AC")
    that are postcode-shaped but not GB — the harvester's main pollution source."""
    if not postcode:
        return False
    compact = re.sub(r"\s+", "", str(postcode).strip().upper())
    if not _GB_POSTCODE_RE.match(compact):
        return False
    area_match = re.match(r"^[A-Z]{1,2}", compact)
    return bool(area_match) and area_match.group(0) in VALID_GB_POSTCODE_AREAS


def _coerce_bedrooms(value):
    """Caller-supplied bedrooms may be '', '3', 3 or None. Return an int in a
    sane range, or None for anything that isn't a real bedroom count."""
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if 0 < n <= 20 else None

def merge_scraped_listing(property_url, postcode, asking_price, bedrooms, property_type, address=""):
    """Fill listing fields from Rightmove/Zoopla when a property URL is provided.
    Returns a 6-tuple: (postcode, asking_price, bedrooms, property_type, address, extra_dict)
    where extra_dict carries days_on_market, price-reduction fields and provenance
    flags from the scraper. bedrooms/property_type are None when unknown (FIX 1) —
    callers must never substitute a default."""
    # Caller-supplied values (from a manual form) count as "user"-sourced known
    # data; an empty/blank value is treated as unknown, not a fabricated default.
    caller_beds = _coerce_bedrooms(bedrooms)
    caller_type = (property_type or "").strip() or None
    bedrooms, property_type = caller_beds, caller_type

    if not property_url:
        extra = {
            "bedrooms_source": "user" if caller_beds is not None else "unknown",
            "property_type_source": "user" if caller_type else "unknown",
            "floor_area_source": "unknown",
        }
        return postcode, asking_price, bedrooms, property_type, address, extra

    scraped = scrape_property_url(property_url)
    if not postcode:
        postcode = scraped.get("postcode") or ""
    if not asking_price:
        asking_price = scraped.get("asking_price") or 0

    # Scrape is authoritative for a URL; fall back to the caller value only when
    # the scrape couldn't read it. Provenance reflects the final value's origin.
    if scraped.get("bedrooms") is not None:
        bedrooms = scraped.get("bedrooms")
        beds_source = scraped.get("bedrooms_source", "scraped")
    else:
        bedrooms = caller_beds
        beds_source = "user" if caller_beds is not None else "unknown"

    if scraped.get("property_type"):
        property_type = scraped.get("property_type")
        type_source = scraped.get("property_type_source", "scraped")
    else:
        property_type = caller_type
        type_source = "user" if caller_type else "unknown"

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
        # Special tenure / sale type (Cycle 1, item 4): shared_ownership / auction /
        # retirement / None. Drives a caveat in the report; never withholds a value.
        "sale_type": scraped.get("sale_type"),
        # Official EPC certificate the listing links (Cycle 4c) — a direct,
        # address-free floor-area source.
        "epc_cert_url": scraped.get("epc_cert_url"),
        # Provenance flags (FIX 1) so the report/QC layer can see when bedrooms,
        # property type or floor area were unknown rather than truly read.
        "bedrooms_source": beds_source,
        "property_type_source": type_source,
        "floor_area_source": scraped.get("floor_area_source", "unknown"),
        # Subject coordinates for the bedroom/distance comparable engine (P2).
        "latitude": scraped.get("latitude"),
        "longitude": scraped.get("longitude"),
        # Subject bathrooms — scraped from the listing, feeds the AVM.
        "bathrooms": scraped.get("bathrooms"),
        # Portal og:image — powers the voting share page and WhatsApp previews.
        "main_photo_url": scraped.get("main_photo_url"),
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

_EPC_RRN_RE = re.compile(r"\d{4}-\d{4}-\d{4}-\d{4}-\d{4}")

def fetch_floor_area_from_cert_url(epc_cert_url):
    """Cycle 4c: floor area from the EXACT EPC certificate the listing links to.
    The gov.uk certificate URL embeds the certificate number (RRN), so this needs
    NO address match — bypassing the address-resolution bottleneck that leaves most
    listings with no floor area. Returns floor area in m² or None."""
    if not (EPC_API_KEY and epc_cert_url):
        return None
    m = _EPC_RRN_RE.search(epc_cert_url)
    if not m:
        return None
    try:
        cert = _epc_fetch_certificate(m.group(0))
        return _extract_floor_area(cert) if cert else None
    except Exception as e:
        print(f"EPC cert-url lookup exception: {e}")
        return None

def _leading_house_number(addr):
    """Extract the leading house/flat number from an address string.
    Handles '9 Chantry Close', '9A Chantry Close', and UK flat formats:
    'Flat 51, 26 Viewforth', 'Apartment 3, ...', 'Unit 12, ...'"""
    if not addr:
        return None
    s = addr.strip()
    # Plain leading digit: '9 Chantry Close' or '24, Tudor Road'
    m = re.match(r"(\d+[A-Za-z]?)\b", s)
    if m:
        return m.group(1).upper()
    # Flat/Apartment/Unit prefix: 'Flat 51, ...' or 'Apartment 3B, ...'
    m = re.match(r"(?:flat|apartment|apt|unit)\s+(\d+[A-Za-z]?)\b", s, re.IGNORECASE)
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


# ── FIX 3: floor-area sanity check ─────────────────────────────────────────────
# A scraped floor area can be physically impossible for the street (e.g. 164 m²
# where every EPC on the road tops out near 130 m²) — a mis-read that corrupts
# £/sqm and the AVM. We validate it against EPC data before it feeds any maths.
SUBJECT_EPC_TOLERANCE = 0.25   # accept scraped up to +25% over the subject's own EPC area
STREET_MAX_MULTIPLE = 1.30     # else reject if >30% above the largest EPC on the street
STREET_MIN_SAMPLE = 3          # never reject on fewer EPC samples than this

def _street_epc_floor_areas(postcode, address, cap=25):
    """Floor areas (sqm) of EPC certificates on the subject's street, capped to
    `cap` certificate fetches. Used only as a fallback when the subject's own EPC
    area can't be found. Best-effort; returns [] on any failure."""
    results = _epc_search(postcode)
    if not results:
        return []
    subj_streets = _street_tokens(address) if address else set()
    candidates, seen = [], set()
    for r in results:
        line1 = (r.get("addressLine1") or "").strip()
        if not line1 or line1.upper() in seen:
            continue
        seen.add(line1.upper())
        toks = {t for t in _normalise_text(line1) if len(t) > 3}
        if subj_streets and not (subj_streets & toks):
            continue
        candidates.append(r)
    candidates = candidates[:cap]
    if not candidates:
        return []

    def _f(r):
        cert = _epc_fetch_certificate(r.get("certificateNumber") or "")
        return _extract_floor_area(cert) if cert else None

    areas = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for a in pool.map(_f, candidates):
            try:
                a = float(a)
            except (TypeError, ValueError):
                continue
            if a and a > 0:
                areas.append(a)
    return areas

def validate_scraped_floor_area(postcode, address, scraped_sqm, property_type=None):
    """Decide whether a scraped floor area is plausible against EPC data.
    Returns a dict: {ok, replacement, reason, basis, sample}. Conservative —
    only flags a clear upper-bound outlier, and never on thin EPC evidence.
    Tier A: the subject's OWN EPC floor area (authoritative for this property);
    Tier B: the street's EPC floor-area envelope."""
    if not scraped_sqm or scraped_sqm <= 0:
        return {"ok": True, "replacement": None, "reason": "no floor area", "basis": "none", "sample": 0}

    # Tier A — subject's own EPC certificate.
    subject_area = None
    try:
        subject_area = get_floor_area_from_epc(postcode, address)
    except Exception as e:
        print(f"floor-area validation (subject EPC) error: {e}")
    if subject_area and subject_area > 0:
        if scraped_sqm > subject_area * (1 + SUBJECT_EPC_TOLERANCE):
            return {"ok": False, "replacement": subject_area,
                    "reason": f"scraped {scraped_sqm} m² exceeds the property's EPC area "
                              f"{subject_area} m² by >{int(SUBJECT_EPC_TOLERANCE*100)}%",
                    "basis": "subject-epc", "sample": 1}
        return {"ok": True, "replacement": None, "reason": "within subject EPC tolerance",
                "basis": "subject-epc", "sample": 1}

    # Tier B — street envelope (only when the subject's own EPC area is unavailable).
    try:
        areas = _street_epc_floor_areas(postcode, address)
    except Exception as e:
        print(f"floor-area validation (street EPC) error: {e}")
        areas = []
    if len(areas) >= STREET_MIN_SAMPLE:
        street_max = max(areas)
        if scraped_sqm > street_max * STREET_MAX_MULTIPLE:
            return {"ok": False, "replacement": None,
                    "reason": f"scraped {scraped_sqm} m² exceeds {STREET_MAX_MULTIPLE}× the "
                              f"largest EPC floor area on the street ({street_max} m²)",
                    "basis": "street", "sample": len(areas)}
        return {"ok": True, "replacement": None, "reason": "within street envelope",
                "basis": "street", "sample": len(areas)}

    # Not enough EPC evidence to judge — never reject on thin data.
    return {"ok": True, "replacement": None, "reason": "insufficient EPC data to validate",
            "basis": "none", "sample": len(areas)}


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
    # Retry on throttling/transient failure — under load this call competes with
    # the other PropertyData requests in a build, and a starved comp set produces
    # wild thin-set valuations. Retries protect the comparable count.
    for attempt in range(3):
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
            if r.status_code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(0.5 * (attempt + 1))
                continue
            return None
        except Exception:
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
                continue
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
    canonical = _canonical_sold_type(property_type)
    type_unknown = canonical is None
    formatted = format_postcode(postcode)
    comparables = _filter_sold(fetch_sold_prices(formatted), canonical)
    broadened = False
    postcode_used = formatted
    # Cycle 1, item 2: tier label drives the published confidence score.
    #   postcode = direct unit-postcode same-type match (HIGH)
    #   sector / district = geographically broadened same-type match (MEDIUM)
    #   region = area-wide, all-type last-resort fallback (LOW, but never blank)
    tier = "postcode"
    district = district_postcode(postcode)
    sector = sector_postcode(postcode)
    if len(comparables) < MIN_COMPARABLES:
        # Try sector (e.g. 'LS17 9') before jumping to the full district ('LS17'),
        # which can be too broad and mix premium and cheap sub-areas.
        if sector != district:
            sector_comps = _filter_sold(fetch_sold_prices(sector), canonical)
            if len(sector_comps) >= MIN_SECTOR_COMPARABLES:
                comparables = sector_comps
                postcode_used = sector
                broadened = True
                tier = "sector"
        if len(comparables) < MIN_COMPARABLES:
            district_comps = _filter_sold(fetch_sold_prices(district), canonical)
            # P1 fix: only broaden to the district if it actually yields MORE
            # comps. A failed/empty district call must never wipe out comps we
            # already found at the postcode/sector (a cause of zero-comp reports).
            if len(district_comps) > len(comparables):
                comparables = district_comps
                postcode_used = district
                broadened = True
                tier = "district"
    # Regional fallback (item 2): NEVER return "no comparables" when a valid
    # postcode exists. If the same-type tiers above produced nothing usable, widen
    # to ALL recent sales across the district (any property type), then the sector.
    # Triggers only when we would otherwise hand back an empty set.
    if not comparables:
        region_comps = _all_sold_transactions(fetch_sold_prices(district))
        region_pc = district
        if not region_comps and sector != district:
            region_comps = _all_sold_transactions(fetch_sold_prices(sector))
            region_pc = sector
        if region_comps:
            comparables = _median_trim(region_comps)
            postcode_used = region_pc
            broadened = True
            tier = "region"
    # HPI-adjust all comparables to today's value before returning
    comparables = hpi_adjust_comparables(comparables, postcode)
    return comparables, postcode_used, broadened, type_unknown, tier

# ── P2: bedroom-matched, distance-ranked comparables ───────────────────────────
# The Rightmove sold feed (fetch_sold_nearby) carries property_type, bedrooms and
# coordinates per sold property — data we already scrape but only used for address
# resolution. Here we use it to build a like-for-like comparable set: same type,
# same bedrooms, nearest first. Falls back to the Land Registry feed when thin.

NEARBY_RADII_MILES = [0.5, 1.0, 2.0]

def _haversine_miles(lat1, lng1, lat2, lng2):
    """Great-circle distance in miles, or None if any coordinate is missing."""
    if None in (lat1, lng1, lat2, lng2):
        return None
    R = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(a)))

def get_nearby_comparables(lat, lng, property_type, bedrooms, sold_records,
                           min_comps=None):
    """Bedroom- and distance-matched comparables from the Rightmove sold feed.
    Returns (comparables, meta). comparables are dicts {address, price, date,
    bedrooms, distance_miles} ready for HPI adjustment. meta carries the radius,
    bedroom band and a confidence label. Widening ladder (correctness > coverage):
    bedroom ±0 then ±1; radius 0.5 → 1 → 2 miles. Returns ([], meta) when subject
    coordinates or the feed are missing — caller then falls back to Land Registry."""
    if min_comps is None:
        min_comps = MIN_COMPARABLES
    meta = {"source": "rightmove_nearby", "radius_miles": None,
            "bedroom_band": None, "confidence": None, "count": 0}
    if lat is None or lng is None or not sold_records:
        return [], meta
    canonical = _canonical_sold_type(property_type)

    annotated = []
    for r in sold_records:
        d = _haversine_miles(lat, lng, r.get("latitude"), r.get("longitude"))
        if d is None or not r.get("price"):
            continue
        annotated.append({
            "address": r.get("address"), "price": r.get("price"),
            "date": r.get("date"), "bedrooms": r.get("bedrooms"),
            "distance_miles": round(d, 3),
            "_ctype": _canonical_sold_type(r.get("property_type")),
        })
    # Type filter (skip only when the subject type is unknown).
    pool = [r for r in annotated if canonical is None or r["_ctype"] == canonical]

    def pick(bed_band, radius):
        out = []
        for r in pool:
            if r["distance_miles"] > radius:
                continue
            if bedrooms is not None:
                rb = r.get("bedrooms")
                if rb is None:
                    if bed_band == 0:
                        continue  # strict rung needs a known, matching bed count
                elif abs(rb - bedrooms) > bed_band:
                    continue
            out.append(r)
        out.sort(key=lambda r: (r["distance_miles"], r.get("date") or ""))
        return out

    for bed_band in (0, 1):
        for radius in NEARBY_RADII_MILES:
            sel = pick(bed_band, radius)
            if len(sel) >= min_comps:
                meta.update({
                    "radius_miles": radius, "bedroom_band": bed_band,
                    "confidence": "bedroom_distance" if bed_band == 0 else "bedroom_distance_wide",
                    "count": len(sel)})
                return sel, meta

    # Nothing reached the minimum at any rung — hand back the widest attempt so the
    # caller can decide between this and the Land Registry fallback.
    sel = pick(1, NEARBY_RADII_MILES[-1])
    meta.update({"radius_miles": NEARBY_RADII_MILES[-1], "bedroom_band": 1,
                 "confidence": "insufficient", "count": len(sel)})
    return sel, meta

def _filter_sold(data, canonical_type):
    if not data:
        return []
    try:
        transactions = data.get("data", {}).get("raw_data", [])
        # canonical_type is None when the subject's property type is unknown
        # (FIX 1): skip type-filtering rather than fabricate a default. Otherwise
        # match by CANONICAL type (P1) so label variants — 'Terraced',
        # 'terraced_house', 'End Terrace' — all match and don't yield zero comps.
        comps = [t for t in transactions
                 if (canonical_type is None or _canonical_sold_type(t.get("type")) == canonical_type)
                 and t.get("price") and t.get("price") < 2_000_000]
        # Median band junk-trim (see _median_trim).
        return _median_trim(comps)
    except Exception:
        return []

def _median_trim(comps):
    """Exclude non-market transactions (partial transfers, right-to-buy,
    inter-family sales) that Land Registry records at far-from-market values.
    Anchored on the median, which an outlier cannot drag the way it drags the
    mean. Kept loose (50%-200%) so it removes junk without shaping the genuine
    distribution. Only applied once there are enough records to trust the median."""
    if len(comps) >= 5:
        prices = sorted(c["price"] for c in comps)
        n = len(prices)
        median = prices[n // 2] if n % 2 else (prices[n // 2 - 1] + prices[n // 2]) / 2
        return [c for c in comps if 0.5 * median <= c["price"] <= 2.0 * median]
    return comps

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

def _weighted_median(pairs):
    """Weighted median of (value, weight) pairs — the robust combiner for the
    football-field range (A4). Each method casts `weight` votes for its value;
    the median vote wins, so a single wild method cannot drag the headline the
    way it could under a weighted mean. Midpoint interpolation when the median
    falls exactly between two values keeps the result stable for even splits."""
    pts = sorted((v, w) for v, w in pairs if v is not None and w > 0)
    if not pts:
        return None
    total = sum(w for _, w in pts)
    half = total / 2.0
    cum = 0
    for i, (v, w) in enumerate(pts):
        cum += w
        if cum > half:
            return round(v)
        if cum == half:  # exact split: average this value with the next
            nxt = pts[i + 1][0] if i + 1 < len(pts) else v
            return round((v + nxt) / 2)
    return round(pts[-1][0])


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

def _psqf_points(data, canonical_type):
    """Extract type-matched points carrying both a £/sqf value and floor area (sqf).
    Matches by canonical type (P1) so label variants don't yield zero points."""
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
        # canonical_type is None when subject type is unknown (FIX 1): skip filter.
        if canonical_type is None or _canonical_sold_type(p.get("type")) == canonical_type:
            v = psqf_value(p)
            if v:
                points.append({
                    "psqf": v,
                    "sqf": p.get("sqf"),
                    "address": p.get("address"),
                    "price": p.get("price"),
                    # Phase A: carry the columns this feed already provides per sold
                    # property — used to build bedroom/size/distance-matched comps.
                    "bedrooms": p.get("bedrooms"),
                    "latitude": p.get("lat") or p.get("latitude"),
                    "longitude": p.get("lng") or p.get("longitude"),
                    "date": p.get("date"),
                    "property_type": p.get("type"),
                })
    if not points:
        # None-safe: some PropertyData £/sqf records have type=null, which can't be
        # sorted against strings (was a TypeError 500 in /debug-psqf).
        print(f"_psqf_points: no matches for {canonical_type}. "
              f"Types present: {sorted({(p.get('type') or '') for p in raw})}")
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


def fetch_bedroom_price(postcode, property_type, bedrooms):
    """PropertyData /prices — local average ASKING price for this EXACT bedroom
    count + type (Option D). Returns {average, low, high} or None. This is the
    bedroom-specific signal the Land Registry comparable average can't provide:
    Land Registry has no bedroom data, but /prices filters live listings by it.
    Asking-based — the caller applies the asking-to-sold discount to imply value."""
    if not bedrooms or not property_type:
        return None
    try:
        params = {
            "key": PROPERTYDATA_API_KEY,
            "postcode": format_postcode(postcode),
            "bedrooms": int(bedrooms),
            "type": _avm_property_type(property_type),
        }
        # Retry once on throttling/transient failure — /prices is the last of
        # several PropertyData calls per build and is the first to get rate-limited.
        r = None
        for attempt in range(2):
            r = requests.get("https://api.propertydata.co.uk/prices", params=params, timeout=10)
            if r.status_code == 200:
                break
            if r.status_code in (429, 500, 502, 503):
                time.sleep(0.6 * (attempt + 1))
                continue
            break
        if r is None or r.status_code != 200:
            print(f"fetch_bedroom_price: {getattr(r, 'status_code', 'no-response')} — "
                  f"{getattr(r, 'text', '')[:200]}")
            return None
        inner = (r.json() or {}).get("data") or {}
        if not isinstance(inner, dict):
            return None
        # Average across the response shapes PropertyData uses.
        avg = (inner.get("average") or inner.get("mean") or inner.get("avg")
               or inner.get("average_price"))
        # Confidence band: explicit percentiles if present, else points, else ±12%.
        low = (inner.get("10pc") or inner.get("25pc") or inner.get("low")
               or inner.get("percentile_25") or inner.get("lower"))
        high = (inner.get("90pc") or inner.get("75pc") or inner.get("high")
                or inner.get("percentile_75") or inner.get("upper"))
        if not avg:
            points = inner.get("points") or inner.get("raw_data") or []
            prices = []
            if isinstance(points, list):
                for p in points:
                    if isinstance(p, dict):
                        v = p.get("price") or p.get("asking_price") or p.get("value")
                        if v:
                            try:
                                prices.append(float(v))
                            except (TypeError, ValueError):
                                pass
            if prices:
                prices.sort()
                avg = sum(prices) / len(prices)
                low = low or prices[len(prices) // 4]
                high = high or prices[min(len(prices) - 1, len(prices) - len(prices) // 4)]
        if not avg:
            return None
        avg = float(avg)
        low = float(low) if low else round(avg * 0.88)
        high = float(high) if high else round(avg * 1.12)
        return {"average": round(avg), "low": round(low), "high": round(high)}
    except Exception as e:
        print(f"fetch_bedroom_price error: {e}")
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

def fetch_propertydata_avm(postcode, property_type, bedrooms=None, floor_area_sqm=None,
                           bathrooms=None):
    """PropertyData /valuation-sale AVM. Requires internal_area (sq ft), so this
    method is unavailable without a floor area. Fields we cannot know from the
    listing are sent as honest middle-of-the-road defaults (bathrooms 1, average
    finish), which adds noise - the method carries standard weight only.
    Returns {low, mid, high} or None."""
    if not floor_area_sqm or floor_area_sqm <= 0:
        return None
    # Correctness > coverage: the AVM keys off property type and bedrooms. If
    # either is unknown (FIX 1), skip the call rather than send a fabricated
    # "3-bed semi" — a guessed profile produces a confident-but-wrong valuation.
    if not property_type or not bedrooms:
        return None
    try:
        params = {
            "key": PROPERTYDATA_API_KEY,
            "postcode": postcode,
            "internal_area": round(float(floor_area_sqm) * 10.764),
            "property_type": _avm_property_type(property_type),
            "construction_date": "1914_2000",
            "bedrooms": int(bedrooms),
            # Real scraped bathroom count where we have it; fall back to 1 only
            # when the listing didn't state it.
            "bathrooms": int(bathrooms) if bathrooms else 1,
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
    canonical = _canonical_sold_type(property_type)
    points = _psqf_points(fetch_sold_psqf(format_postcode(postcode)), canonical)
    if not points:
        points = _psqf_points(fetch_sold_psqf(district_postcode(postcode)), canonical)
    return points


SIZE_MATCH_TOLERANCE = 0.20  # ±20% floor-area band for like-for-like matching

def _within_size_band(area_sqf, subject_sqf, tol=SIZE_MATCH_TOLERANCE):
    """True when a comparable's floor area (sqf) is within ±tol of the subject.
    Shared by the £/sqm benchmark and the headline comparable size-match (FIX 2)
    so both use one definition of 'same size'."""
    if not area_sqf or not subject_sqf or subject_sqf <= 0:
        return False
    return subject_sqf * (1 - tol) <= area_sqf <= subject_sqf * (1 + tol)

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
        sized = [p for p in points if _within_size_band(p.get("sqf"), subject_sqf)]
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
        # FIX 2026-07-16 (Wemborough HA7 2ED): the SPARQL API lives at
        # /landregistry/query — the old /sparql path serves an HTML error page,
        # so this "primary" source silently returned [] on EVERY report ever
        # built: last-sale history was stale (121 Wemborough's two 2025 sales
        # missing) and the address picker was missing properties (no. 95).
        resp = requests.get(
            "https://landregistry.data.gov.uk/landregistry/query",
            params={"query": query, "output": "json"},
            timeout=10,
            headers={"Accept": "application/sparql-results+json"},
        )
        if resp.status_code != 200:
            print(f"LAND REGISTRY DIRECT FAILED: HTTP {resp.status_code} for {pc} — "
                  f"falling back to PropertyData radius data (incomplete history)")
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


_CONFIDENCE_ORDER = {"high": 2, "medium": 1, "low": 0}
# Independent matched-sold corroboration thresholds (Cycle 2). HIGH requires the
# bedroom/size/distance-matched SOLD comps (psqf feed) to agree with the published
# midpoint within CORROBORATE; a gap above CONFLICT means the methods disagree and
# the estimate is capped to MEDIUM with a "methods disagree" caveat.
_MATCHED_SOLD_CORROBORATE = 0.12
_MATCHED_SOLD_CONFLICT = 0.20
# Cycle 3 sanity gate: when our valuation diverges from the asking price beyond
# these ratios, the listing is almost certainly non-standard (shared ownership,
# short lease, auction guide, mis-listing, wrong type) — see E9 0CC (+130%) and
# CF63 (−88%). Normal over/under-pricing never reaches these bounds, so the gate
# only catches genuine anomalies. Such listings are capped to LOW + flagged.
_ASKING_ANOMALY_HIGH_RATIO = 1.5   # our value > 1.5× asking
_ASKING_ANOMALY_LOW_RATIO = 0.6    # our value < 0.6× asking
# Cycle 4b premium-property guard: a would-be-HIGH whose value sits this far BELOW
# asking is almost always an under-captured premium/larger home (the area comps and
# the matched-sold are both biased low and corroborate each other) rather than
# genuine confidence — seen on NW3/CO4/W6 prime stock. Demote such rows to MEDIUM.
_PREMIUM_UNDERVALUE_RATIO = 0.75   # our value < 0.75× asking (i.e. >25% below)

def _resolve_confidence(comparable_tier, comparable_confidence, type_unknown,
                        comp_count, sale_type, is_new_build, has_value,
                        matched_sold_value=None, weighted_midpoint=None,
                        asking_anomaly=False, asking_price=None):
    """Single source of truth for the PUBLISHED confidence score + caveat. We always
    return a valuation for any listing with a usable postcode — this only encodes how
    much to trust it and why.

    Cycle 2 fix: HIGH no longer means "enough comps at the postcode" (that gave HIGH
    to bedroom/size-BLIND averages — the flat misses N19/B1/N8/EH10/B23). HIGH now
    requires EITHER a genuinely bedroom/size-matched headline set OR independent
    matched-sold comps that AGREE with the published number. When the headline set is
    only area-matched it is MEDIUM, and when an available matched-sold signal
    DISAGREES sharply the estimate is capped to MEDIUM and flagged.

    Returns (score, reasons, caveat)."""
    reasons = []
    # The headline comparable SET was itself bedroom/size matched (FIX 2 path).
    headline_matched = comparable_confidence in (
        "bedroom_matched", "size_matched", "bedroom_distance", "bedroom_distance_wide")
    # Independent corroboration / conflict from the matched-sold (psqf) signal.
    corroborated = conflict = False
    if matched_sold_value and weighted_midpoint:
        div = abs(matched_sold_value - weighted_midpoint) / weighted_midpoint
        corroborated = div <= _MATCHED_SOLD_CORROBORATE
        conflict = div > _MATCHED_SOLD_CONFLICT

    if headline_matched or corroborated:
        score = "high"
        if corroborated and not headline_matched:
            reasons.append("recent sales of similar (bedroom-matched) homes corroborate this estimate")
    elif comparable_tier in ("postcode", "sector", "district"):
        score = "medium"
        if comparable_tier == "postcode" and comp_count >= MIN_COMPARABLES:
            # B4 (2026-07-14): name the SUBJECT of this caveat — it describes the
            # core comparable set, not the report's other signals (a separate
            # bedroom-matched context row otherwise reads as a contradiction).
            reasons.append("our core comparable set is matched on property type and "
                           "area; a size-matched set wasn't available at this address")
        elif comparable_tier == "postcode":
            reasons.append("based on a small number of sales in this exact postcode")
        else:
            reasons.append("limited sales in the immediate postcode — estimate based on "
                           f"the wider {comparable_tier} area")
    elif comparable_tier == "region":
        score = "low"
        reasons.append("very few like-for-like sales nearby — estimate based on "
                       "broader area-wide sales of all property types")
    else:
        score = "medium"

    def downgrade(to):
        return to if _CONFIDENCE_ORDER[to] < _CONFIDENCE_ORDER[score] else score

    if conflict:
        score = downgrade("medium")
        reasons.append("valuation methods disagree on this property — treat the estimate with caution")
    if type_unknown:
        score = downgrade("low")
        reasons.append("property type unclear — estimate is less precise as a result")

    # Premium-property guard (Cycle 4b): don't claim HIGH when our value sits well
    # below asking on a non-anomaly. That gap is usually a premium/larger home our
    # comparables under-capture (both methods biased low, corroborating each other),
    # not real confidence. Demote to MEDIUM with a plain reason. (Extreme gaps are
    # already LOW via the anomaly gate.)
    if (score == "high" and asking_price and weighted_midpoint
            and weighted_midpoint < asking_price * _PREMIUM_UNDERVALUE_RATIO):
        score = "medium"
        reasons.append("our valuation is well below the asking price — often a premium "
                       "or larger property that local comparable sales under-capture")

    caveats = []
    # Cycle 3 sanity gate: a valuation far from the asking price is a red flag for a
    # non-standard listing even when our comps are good (E9 0CC: bedroom-matched
    # comps said £495k, asking £215k — an undetected shared-ownership share).
    if asking_anomaly:
        score = downgrade("low")
        reasons.append("asking price is far from the comparable evidence")
        caveats.append(
            "The asking price is very different from the sold-price evidence we found. "
            "That usually means a non-standard listing — shared ownership, a short "
            "lease, an auction guide price, or a mis-listing — so treat this estimate "
            "with particular caution.")
    if sale_type:
        score = downgrade("medium")
        label = {
            "shared_ownership": "a shared-ownership / part-buy listing",
            "auction": "an auction or guide-price listing",
            "retirement": "a retirement / age-restricted listing",
        }.get(sale_type, "a special-tenure listing")
        caveats.append(
            f"This looks like {label}. Its asking price is not directly comparable "
            "to open-market value, so treat this estimate as indicative only.")
    if is_new_build:
        caveats.append(
            "This looks like a new-build; new-builds usually carry a premium over "
            "the resale sales this estimate is based on.")
    if not has_value:
        score = "low"
    if reasons and score != "high":
        caveats.append(f"Confidence is {score}: " + "; ".join(reasons) + ".")
    caveat = " ".join(caveats) if caveats else None
    return score, reasons, caveat


def _resolve_seller_signal(days_on_market, local_avg_dom, dom_signal,
                           price_reduced, reduction_pct, reduction_date,
                           discount_pct, last_sale_price=None,
                           last_sale_date=None, asking_price=None):
    """Single source of truth for the published seller-motivation signal on the
    paid report. Mirrors _resolve_confidence: combines the public pressure
    signals (time on market vs local average, recorded price reductions, the
    local asking-vs-sold discount) into a strong/moderate/weak score plus
    plain-English evidence lines. Missing inputs are stated honestly and cap
    how high the score can go — they are never silently skipped.

    Weighting rationale (2026-07-05 session): time on market is the strongest
    single public signal (0-2 pts, negative when the listing is moving at or
    faster than the local pace — that actively counters a pressure read); a
    recorded price reduction is direct evidence the seller has already moved
    (0-2 pts by size); the area asking-vs-sold discount is area-level context,
    not property-specific, so it only nudges (±1 pt).

    F1/F2 (2026-07-14, CEO-approved tiering): the property's OWN sale history is
    the strongest motivation evidence we hold and was previously ignored.
    Bought <12 months ago → strong signal (+2); 12-24 months → moderate (+1);
    >24 months → no signal. Additionally, re-listing at or roughly at the
    previous purchase price (≤3% above — flat-to-loss once buying costs are
    counted) adds +1 and its own prominent line: a seller exiting, not profiting.

    Returns (score, reasons, summary)."""
    reasons = []
    points = 0
    dom_comparable = bool(days_on_market and local_avg_dom)

    # F1/F2: recent-resale signal, stated FIRST when present — it is the most
    # property-specific evidence in the score.
    resale_months = None
    try:
        y, mo = int(str(last_sale_date)[:4]), int(str(last_sale_date)[5:7])
        now = datetime.utcnow()
        resale_months = (now.year - y) * 12 + (now.month - mo)
    except (ValueError, TypeError):
        pass
    if resale_months is not None and 0 <= resale_months < 24 and last_sale_price:
        if resale_months < 12:
            points += 2
            reasons.insert(0, f"the seller bought this property only {resale_months} "
                              f"months ago for {_fmt(last_sale_price)} — re-listing this "
                              "quickly is a strong motivation signal")
        else:
            points += 1
            reasons.insert(0, f"the seller bought this property {resale_months} months "
                              f"ago for {_fmt(last_sale_price)} — a fairly quick return "
                              "to market")
        if asking_price and asking_price <= last_sale_price * 1.03:
            # The combo (<12mo AND at/below purchase price) is the strongest
            # leverage evidence we can give a buyer — it alone justifies STRONG.
            points += 2 if resale_months < 12 else 1
            reasons.insert(1, f"and they're asking {_fmt(asking_price)} — at or barely above "
                              "what they paid, a loss once buying costs are counted. "
                              "This looks like a seller who needs out, not one chasing profit")

    if dom_comparable:
        if dom_signal == "high":
            points += 2
            reasons.append(f"listed {days_on_market} days vs a local average of "
                           f"{local_avg_dom} — well past the point most sellers get nervous")
        elif dom_signal == "medium":
            points += 1
            reasons.append(f"listed {days_on_market} days vs a local average of "
                           f"{local_avg_dom} — starting to sit")
        else:
            points -= 1
            reasons.append(f"only {days_on_market} days on the market vs a local average of "
                           f"{local_avg_dom} — no time pressure on the seller yet")
    elif days_on_market:
        reasons.append(f"listed {days_on_market} days, but we couldn't fetch the local "
                       "average to compare against")
    else:
        reasons.append("we couldn't read how long this listing has been on the market")

    if price_reduced:
        if reduction_pct and reduction_pct >= 5:
            points += 2
            reasons.append(f"the price has already been cut by {reduction_pct}% — "
                           "the seller's position has publicly softened")
        else:
            points += 1
            reasons.append("the price has already been reduced once — "
                           "the seller has publicly moved")
    else:
        reasons.append("no price reduction recorded — the seller hasn't publicly moved yet")

    if discount_pct is not None:
        if discount_pct >= 5:
            points += 1
            reasons.append(f"buyers in this area achieve on average {discount_pct}% "
                           "below asking — local sellers expect to negotiate")
        elif discount_pct <= 2:
            points -= 1
            reasons.append(f"local sales complete close to asking (avg {discount_pct}% "
                           "discount) — sellers around here tend to hold firm")
        else:
            reasons.append(f"buyers in this area achieve on average {discount_pct}% "
                           "below asking — a typical negotiating margin")
    else:
        reasons.append("no local asking-vs-sold data available for this area")

    score = "strong" if points >= 3 else ("moderate" if points >= 1 else "weak")
    # Honesty cap: without the time-on-market comparison (the primary signal)
    # we never claim STRONG on secondary evidence alone. Exception (F2): the
    # <12-month resale at/below the purchase price is property-specific primary
    # evidence in its own right, so it is never capped.
    recent_flat_resale = (resale_months is not None and resale_months < 12
                          and last_sale_price and asking_price
                          and asking_price <= last_sale_price * 1.03)
    if score == "strong" and not dom_comparable and not recent_flat_resale:
        score = "moderate"
        reasons.append("score capped: without the time-on-market comparison we "
                       "won't claim strong motivation on secondary signals alone")

    summary = {
        "strong": "Multiple public signals point to a seller under pressure. "
                  "Negotiate with confidence — the clock is on your side.",
        "moderate": "Some pressure signals are present, but this is not a seller who "
                    "has to sell. Negotiate on the evidence, not on urgency.",
        "weak": "Little public evidence of seller pressure. Your leverage here is "
                "the data below, not the clock.",
    }[score]
    return score, reasons, summary


def build_report_data(property_url, asking_price, bedrooms, property_type,
                      postcode, floor_area_sqm=None, address=None,
                      scraper_days_on_market=None, scraper_floor_area_sqm=None,
                      price_reduced=False, original_asking_price=None,
                      reduction_date=None, reduction_amount=None, reduction_pct=None,
                      resolved_address=None, address_resolution=None,
                      is_new_build=False,
                      bedrooms_source="unknown", property_type_source="unknown",
                      floor_area_source="unknown",
                      latitude=None, longitude=None, bathrooms=None,
                      sale_type=None, epc_cert_url=None, main_photo_url=None,
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

    # Confidence flags, refined by FIX 2 (comparables) and FIX 3 (floor area).
    comparable_confidence = "area_only"
    floor_area_confidence = "high"
    floor_area_sqm_raw = None  # set by FIX 3 when a scraped area is rejected
    # P2 comparable-source signals (default to the Land Registry path).
    comparable_source = "land_registry"
    # Cycle 1, item 2/3: which geographic tier produced the headline comparable set
    # (postcode/sector/district/region). Set by get_sold_comparables; drives the
    # published confidence score below.
    comparable_tier = "postcode"
    comparable_radius_miles = None
    comparable_bedroom_band = None
    # Diagnostics: None = nearby feed not attempted (no subject coords);
    # 0 = feed returned nothing; >0 = how many records / bed+distance matches.
    nearby_feed_count = None
    nearby_match_count = None

    if not floor_area_sqm and scraper_floor_area_sqm:
        floor_area_sqm = scraper_floor_area_sqm

    # ── DATA GATHERING ──────────────────────────────────────────────────────────
    # Independent external calls run in parallel: wall time becomes roughly the
    # slowest call rather than the sum of all of them. Phase 1 needs only the
    # raw listing inputs; phase 2 needs phase 1 outputs (postcode_used from the
    # comparables broadening, address/floor area from the EPC resolution).
    monthly_rent = None
    avm = None
    bedroom_price = None  # Option D: bedroom-specific local asking price
    discount_pct = None
    local_avg_dom = None
    # resolved_address / address_resolution arrive as parameters from the
    # sold-record resolution in merge_scraped_listing; the EPC cross-match
    # below may still overwrite them with a better outcome
    last_sale = None
    psqf_points = []

    with ThreadPoolExecutor(max_workers=6) as pool:
        fut_comps = pool.submit(get_sold_comparables, postcode, property_type)
        # P2: fetch the Rightmove sold feed (bedroom/type/coordinate-tagged) when we
        # have subject coordinates to distance-match against. Land Registry remains
        # the fallback and cross-check.
        fut_nearby = None
        if latitude is not None and longitude is not None:
            fut_nearby = pool.submit(fetch_sold_nearby, postcode)
        fut_epc = fut_psqf = fut_rents = fut_bedroom_price = fut_cert_area = None
        if paid_tier:
            if EPC_API_KEY:
                fut_epc = pool.submit(_epc_resolution, postcode, address,
                                      property_type, floor_area_sqm)
                # Cycle 4c: when the listing has no floor area but links its official
                # EPC certificate, fetch that exact certificate's area directly (no
                # address match needed) — the main floor-area coverage lever.
                if epc_cert_url and not floor_area_sqm:
                    fut_cert_area = pool.submit(fetch_floor_area_from_cert_url, epc_cert_url)
            fut_psqf = pool.submit(fetch_psqf_points, postcode, property_type)
            fut_rents = pool.submit(fetch_avg_rents, formatted, property_type, bedrooms)
            # Option D: bedroom-specific local price (the like-for-like signal the
            # Land Registry comparable average lacks). Needs known beds + type.
            fut_bedroom_price = (pool.submit(fetch_bedroom_price, formatted, property_type, bedrooms)
                                 if (bedrooms and property_type) else None)

        comparables, postcode_used, broadened, type_unknown, comparable_tier = fut_comps.result()
        if type_unknown:
            # No type filter could be applied — comparables mix property types.
            comparable_confidence = "low"

        # ── P2: prefer bedroom + distance-matched comps from the Rightmove feed ──
        # when it yields enough like-for-like sales; else keep the Land Registry set.
        lr_avg_for_xcheck = avg_sold_price(comparables) if comparables else None
        if fut_nearby is not None and not type_unknown:
            try:
                sold_records = fut_nearby.result() or []
            except Exception as e:
                print(f"fetch_sold_nearby error: {e}")
                sold_records = []
            nearby_feed_count = len(sold_records)
            if sold_records:
                nearby, nmeta = get_nearby_comparables(
                    latitude, longitude, property_type, bedrooms, sold_records)
                nearby_match_count = nmeta["count"]
                if (nmeta["confidence"] in ("bedroom_distance", "bedroom_distance_wide")
                        and len(nearby) >= MIN_COMPARABLES):
                    comparables = hpi_adjust_comparables(nearby, postcode)
                    postcode_used = formatted
                    broadened = False
                    comparable_source = "rightmove_nearby"
                    comparable_confidence = nmeta["confidence"]
                    comparable_radius_miles = nmeta["radius_miles"]
                    comparable_bedroom_band = nmeta["bedroom_band"]

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
                    # Tag provenance so the report/QC layer knows the area is
                    # EPC-derived (was left as "unknown" — a mislabelling bug).
                    floor_area_source = "epc"
                    floor_area_confidence = "high"
            except Exception as e:
                print(f"EPC resolution error: {e}")

        # Cycle 4c: direct EPC-certificate floor area (exact, address-free) when the
        # address-matched lookup above still found nothing.
        if not floor_area_sqm and fut_cert_area is not None:
            try:
                cert_area = fut_cert_area.result()
                if cert_area:
                    floor_area_sqm = cert_area
                    floor_area_source = "epc"
                    floor_area_confidence = "high"
            except Exception as e:
                print(f"EPC cert-url area error: {e}")

        # ── FIX 3: sanity-check a SCRAPED floor area before it feeds the AVM,
        # £/sqm or size-match. EPC-derived areas are already authoritative and
        # skip this. On rejection we prefer the property's own EPC area; if none
        # is available we drop the corrupt value rather than valuing on it.
        if paid_tier and floor_area_sqm and floor_area_source == "scraped":
            try:
                fa_check = validate_scraped_floor_area(
                    postcode, address, floor_area_sqm, property_type)
            except Exception as e:
                print(f"floor-area validation error: {e}")
                fa_check = {"ok": True}
            if not fa_check.get("ok"):
                floor_area_sqm_raw = floor_area_sqm
                print(f"FIX3 rejected scraped floor area: {fa_check.get('reason')}")
                if fa_check.get("replacement"):
                    floor_area_sqm = fa_check["replacement"]
                    floor_area_source = "epc"
                    floor_area_confidence = "high"
                else:
                    floor_area_sqm = None
                    floor_area_source = "unverified"
                    floor_area_confidence = "low"

        fut_avm = None
        if paid_tier:
            fut_avm = pool.submit(fetch_propertydata_avm, postcode_used,
                                  property_type, bedrooms, floor_area_sqm, bathrooms)
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

        if fut_bedroom_price is not None:
            try:
                bedroom_price = fut_bedroom_price.result()
            except Exception as e:
                print(f"bedroom price fetch error: {e}")

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

    # Address-normalised floor areas from the £/sqf feed — the only sold feed
    # carrying per-property floor area. Used to size-match the headline
    # comparables (FIX 2) and to enrich the comparables table for display.
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

    # ── PHASE B: bedroom + distance (+ size) matched SOLD comparables ───────────
    # Built from the £/sqf feed's per-property bedrooms / coordinates / floor area
    # (Phase A now carries them). Reuses the P2 matcher. Scored as a new weighted
    # method here; promotion to the PRIMARY comparable signal is Phase C.
    matched_sold_value = None
    matched_sold_low = matched_sold_high = None
    matched_sold_count = 0
    matched_sold_confidence = None
    if latitude is not None and longitude is not None and not type_unknown and psqf_points:
        try:
            msel, msmeta = get_nearby_comparables(
                latitude, longitude, property_type, bedrooms, psqf_points, min_comps=6)
            matched_sold_confidence = msmeta.get("confidence")
            # Optional ±20% floor-area refinement when it keeps a usable set.
            if floor_area_sqm and floor_area_sqm > 0:
                subj_sqf = floor_area_sqm * 10.764
                sized = [c for c in msel if _within_size_band(c.get("sqf"), subj_sqf)]
                if len(sized) >= 5:
                    msel = sized
                    matched_sold_confidence = f"{matched_sold_confidence}+size"
            matched_sold_count = len(msel)
            if len(msel) >= 5:
                adj = hpi_adjust_comparables(msel, postcode)
                prices = sorted(c.get("adjusted_price") or c["price"] for c in adj if c.get("price"))
                if prices:
                    n = len(prices)
                    q1, q3 = max(0, n // 4), min(n - 1, n - n // 4)
                    matched_sold_low, matched_sold_high = round(prices[q1]), round(prices[q3])
                    matched_sold_value = avg_sold_price(adj)
        except Exception as e:
            print(f"matched-sold comparables error: {e}")

    # ── FIX 2: real ±20% size-matching of the headline comparable set ──────────
    # The /sold-prices feed has no floor area, so we join it to the £/sqf feed by
    # address and keep only comparables within ±20% of the subject's floor area.
    # Correctness > coverage: if too few size-matched comps remain we do NOT
    # broaden the area — we keep the best available set and flag low confidence.
    # The median-band junk-trim in _filter_sold still runs as a secondary filter.
    comparables_for_avg = comparables
    comparable_count_size_matched = 0
    if floor_area_sqm and floor_area_sqm > 0 and psqf_lookup:
        subject_sqf = floor_area_sqm * 10.764
        size_matched = []
        for c in comparables:
            key = re.sub(r"[^A-Z0-9]", "", (c.get("address") or "").upper())
            info = psqf_lookup.get(key)
            if info and _within_size_band(info["sqm"] * 10.764, subject_sqf):
                size_matched.append(c)
        comparable_count_size_matched = len(size_matched)
        if len(size_matched) >= MIN_COMPARABLES:
            comparables_for_avg = size_matched
            comparable_confidence = "size_matched"
            # Bedroom precision tier: tighten to same-bedroom comps only when it
            # keeps the set at or above the minimum — never as a hard filter.
            if bedrooms:
                same_beds = [c for c in size_matched
                             if _coerce_bedrooms(c.get("bedrooms")) == bedrooms]
                if len(same_beds) >= MIN_COMPARABLES:
                    comparables_for_avg = same_beds
                    comparable_confidence = "bedroom_matched"
        # else: too few size-matched comps. Do NOT downgrade here — a
        # bedroom_distance label from P2 already reflects a stronger constraint
        # than "area_only"; only the Land Registry path keeps the default.

    local_avg_sold = avg_sold_price(comparables_for_avg)

    # P2 cross-check: when we valued off the Rightmove nearby feed, compare against
    # the Land Registry average and surface any large divergence for the QC layer.
    lr_vs_rightmove_divergence_pct = None
    if (comparable_source == "rightmove_nearby" and local_avg_sold
            and lr_avg_for_xcheck):
        lr_vs_rightmove_divergence_pct = round(
            ((local_avg_sold - lr_avg_for_xcheck) / lr_avg_for_xcheck) * 100, 1)

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
        # Candidates power the confirm-address modal dropdown on BOTH paths —
        # even a confident match can be the wrong property, and picking from
        # the sold list is the cheapest correction (Land Registry SPARQL
        # first, so normally no PropertyData spend).
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

    # Seller-motivation signal (2026-07-05, paid-tier section). Uses the RAW
    # local discount_pct — method 7 below substitutes a national fallback into
    # this variable, which must not masquerade as local evidence here.
    seller_signal_score, seller_signal_reasons, seller_signal_summary = (
        _resolve_seller_signal(days_on_market, local_avg_dom, dom_signal,
                               price_reduced, reduction_pct, reduction_date,
                               discount_pct,
                               # F1/F2: the property's own sale history — the
                               # signal the fixture showed we were ignoring.
                               last_sale_price=(last_sale or {}).get("price"),
                               last_sale_date=(last_sale or {}).get("date"),
                               asking_price=asking_price))
    local_sold_discount_pct = discount_pct

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

    # Method 2: HPI-adjusted last sale. A5 (2026-07-14): the property's OWN sale
    # is the strongest evidence we hold, so its weight scales with recency —
    # a recent sale (<5y) outweighs any comparable at 3; 5-10y counts 2; older
    # sales (pre-renovation risk) count 1. Safe to weight highly because
    # find_last_sale only returns confident address matches (returns None and
    # populates the candidates picker rather than guessing a neighbour's sale).
    if hpi_adjusted_value:
        m2_weight = 2
        try:
            sale_year = int(str((last_sale or {}).get("date", ""))[:4])
            age_years = datetime.utcnow().year - sale_year
            m2_weight = 3 if age_years < 5 else (2 if age_years < 10 else 1)
        except (ValueError, TypeError):
            pass
        m2_low = round(hpi_adjusted_value * 0.95)
        m2_high = round(hpi_adjusted_value * 1.05)
        methods.append(_method_dict(
            "HPI-adjusted last sale", m2_low, m2_high, round((m2_low + m2_high) / 2),
            "ONS House Price Index", True, weight=m2_weight
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
                # 2026-07-16 (Wemborough HA7 2ED): weight 0 — context-only. This
                # method IS the comparable average, HPI-shifted 12 months: zero
                # independent information. Letting it vote gave the size-blind
                # comparable signal up to FOUR correlated votes (unadjusted x1,
                # HPI-adjusted x2, trend x1), which outvoted the size-aware
                # methods 2-2 on the 275m² Wemborough semi (£680k vs £1.25M ask).
                methods.append(_method_dict(
                    "Area price trend", m4_low, m4_high, m4_mid,
                    "ONS House Price Index (context only)", True, weight=0
                ))
            else:
                methods.append(_method_dict("Area price trend", 0, 0, 0, "ONS House Price Index (context only)", False, weight=0))
        except Exception as e:
            print(f"Method 4 error: {e}")
            methods.append(_method_dict("Area price trend", 0, 0, 0, "ONS House Price Index (context only)", False, weight=0))
    else:
        methods.append(_method_dict("Area price trend", 0, 0, 0, "ONS House Price Index (context only)", False, weight=0))

    # Method 5: Online estimate (AVM, fetched in the parallel phase, paid only)
    if avm:
        methods.append(_method_dict(
            "Automated valuation", avm["low"], avm["high"], avm["mid"],
            "Automated valuation model", True
        ))
    else:
        methods.append(_method_dict("Automated valuation", 0, 0, 0, "Automated valuation model", False))

    # Method 6: Estimated lender range — SYNTHETIC (min of our other methods
    # x 0.90-0.97, no independent data source). Labelled "(modelled)" so it is
    # never mistaken for actual lender data alongside the sourced methods.
    base_candidates = [v for v in (local_avg_sold, hpi_adjusted_value, psqm_implied_value) if v]
    if base_candidates:
        lender_base = min(base_candidates)
        m6_low = round(lender_base * 0.90)
        m6_high = round(lender_base * 0.97)
        m6_mid = round((m6_low + m6_high) / 2)
        # A2 (2026-07-14): weight 0 — context-only. Circular by construction
        # (90-97% of our own lowest estimate): it can only ever drag the range
        # down and contains no independent information.
        methods.append(_method_dict(
            "Estimated lender range (modelled)", m6_low, m6_high, m6_mid,
            "Modelled — not sourced from lender data (context only)", True, weight=0
        ))
    else:
        methods.append(_method_dict("Estimated lender range (modelled)", 0, 0, 0,
                                    "Modelled — not sourced from lender data (context only)", False, weight=0))

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
            # A3 (2026-07-14): weight 0 — context-only. The 4-6% yield assumption
            # spans ±33% by construction (LS29 fixture: a £127k-wide band), far
            # too noisy to vote on the headline number.
            methods.append(_method_dict(
                "Rental yield implied value", m_rent_low, m_rent_high, m_rent_mid,
                f"PropertyData avg rents ({_fmt(round(monthly_rent))}/mo) (context only)", True, weight=0
            ))
        else:
            methods.append(_method_dict("Rental yield implied value", 0, 0, 0, "PropertyData avg rents (context only)", False, weight=0))
    except Exception as e:
        print(f"Method 6b error: {e}")
        methods.append(_method_dict("Rental yield implied value", 0, 0, 0, "PropertyData avg rents (context only)", False, weight=0))

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

    # Method 8 (Option D): Bedroom-matched local price. The bedroom-specific local
    # ASKING average (PropertyData /prices), converted to an implied sold value via
    # the local asking-to-sold discount. This is the like-for-like-by-bedroom signal
    # the Land Registry comparable average structurally cannot provide.
    bedroom_implied_value = None
    bedroom_local_avg_asking = None
    try:
        if bedroom_price and bedroom_price.get("average"):
            bedroom_local_avg_asking = bedroom_price["average"]
            disc = (discount_pct if discount_pct is not None else 4.5) / 100.0
            bedroom_implied_value = round(bedroom_price["average"] * (1 - disc))
            m8_low = round(bedroom_price["low"] * (1 - disc))
            m8_high = round(bedroom_price["high"] * (1 - disc))
            # A1 (2026-07-14): weight 0 — context-only. Bedroom-matched asking has
            # no SIZE dimension: on the LS29 fixture it averaged 2-bed flats into
            # an 83m² stone terrace and read £100k below every sourced method
            # while double-weighted — the primary cause of the mispriced trio.
            # The size-aware successor is Method 9 (matched sold comparables),
            # which earns weight only once hard ±20% size-matching lands (B.1).
            methods.append(_method_dict(
                f"Bedroom-matched local price ({bedrooms}-bed)",
                m8_low, m8_high, bedroom_implied_value,
                "PropertyData local asking by bedroom, less sold discount (context only)", True, weight=0
            ))
        else:
            methods.append(_method_dict("Bedroom-matched local price", 0, 0, 0,
                                        "PropertyData local asking by bedroom (context only)", False, weight=0))
    except Exception as e:
        print(f"Method 8 error: {e}")
        methods.append(_method_dict("Bedroom-matched local price", 0, 0, 0,
                                    "PropertyData local asking by bedroom (context only)", False, weight=0))

    # Method 9 (Phase B): bedroom/size/distance-matched SOLD comparables, from the
    # £/sqf feed's per-property bedrooms+coordinates+floor area. Added at weight 1
    # for SCORING — not yet the primary signal (that's Phase C, after the batch
    # shows it tracks asking on its own).
    # Weight 0 = shown for scoring but EXCLUDED from the weighted valuation. The
    # batch showed this method reads high (permissive widening pulls in larger/
    # pricier sold comps), so it must not affect live valuations until tightened
    # (Phase B.1: enforce ±20% size as the hard like-for-like constraint).
    if matched_sold_value:
        methods.append(_method_dict(
            f"Matched sold comparables ({bedrooms}-bed)" if bedrooms else "Matched sold comparables",
            matched_sold_low or round(matched_sold_value * 0.95),
            matched_sold_high or round(matched_sold_value * 1.05),
            matched_sold_value,
            "PropertyData sold £/sqf feed — bedroom/size/distance matched (scoring only)", True, weight=0
        ))
    else:
        methods.append(_method_dict("Matched sold comparables", 0, 0, 0,
                                    "PropertyData sold £/sqf feed — bedroom/size/distance matched (scoring only)",
                                    False, weight=0))

    # A1 (2026-07-14): the "bedroom signal leads" down-weight is REMOVED along
    # with Option D's vote — with the bedroom-matched method context-only, the
    # comparable average keeps its full weight. The bedroom-blindness that this
    # down-weight compensated for is now handled by the robust (median) combiner
    # below plus the thin-set guardrail. Field kept False for payload compatibility.
    comparable_downweighted_for_bedroom = False

    # ── GUARDRAIL: contain thin, bedroom-blind comparable averages ─────────────
    # A small Land Registry comparable set carries no bedroom/size info, so it can
    # average a 2-bed in with large houses and produce a wild headline (e.g. a
    # £550k 2-bed "valued" at £679k). When the set is thin AND the comparable
    # method disagrees sharply with the other signals, drop it from the weighted
    # range and flag low confidence rather than letting it dominate.
    comparable_outlier_excluded = False
    thin_blind = (comparable_source == "land_registry"
                  and len(comparables) < MIN_COMPARABLES)
    if thin_blind:
        comparable_confidence = "low"
        comp_method = next((m for m in methods
                            if m["name"] == "Comparable sales (HPI-adjusted)" and m["available"]), None)
        others = [m for m in methods if m["available"] and m["weight"] > 0
                  and m["name"] not in ("Comparable sales (HPI-adjusted)",
                                        "Comparable sales (unadjusted)")]
        if comp_method and comp_method.get("midpoint") and others:
            omids = sorted(m["midpoint"] for m in others if m.get("midpoint"))
            if omids:
                omed = omids[len(omids) // 2]
                if omed and abs(comp_method["midpoint"] - omed) / omed > 0.25:
                    # Outlier on thin, bedroom-blind data — exclude both comparable
                    # methods from the weighted range (the set is already flagged
                    # low-confidence, so dropping a 25%+ outlier is safe).
                    comparable_outlier_excluded = True
                    for m in methods:
                        if m["name"] in ("Comparable sales (HPI-adjusted)",
                                         "Comparable sales (unadjusted)"):
                            m["weight"] = 0

    # ── GUARDRAIL (2026-07-16, Wemborough HA7 2ED): size-blind comps on an ─────
    # atypically-sized subject. When we KNOW the subject's floor area, the
    # headline comparable set is NOT size/bedroom-matched, and the £/m² method
    # (the only size-aware voter) disagrees with the comparable average by >25%,
    # the comp set is not like-for-like for this house — typically a heavily
    # extended home (275m² five-bed) valued against standard local stock. Drop
    # the size-blind comparable methods from the vote (mirrors the thin-set
    # guardrail above) and let the size-aware and asking-anchored methods lead.
    size_mismatch_excluded = False
    if (not comparable_outlier_excluded
            and floor_area_sqm and psqm_implied_value and local_avg_sold
            and comparable_confidence not in (
                "size_matched", "bedroom_matched",
                "bedroom_distance", "bedroom_distance_wide")
            and abs(psqm_implied_value - local_avg_sold) / local_avg_sold > 0.25):
        size_mismatch_excluded = True
        for m in methods:
            if m["name"] in ("Comparable sales (HPI-adjusted)",
                             "Comparable sales (unadjusted)"):
                m["weight"] = 0

    # ── FOOTBALL FIELD WEIGHTED RANGE ─────────────────────────────────────────

    available_methods = [m for m in methods if m["available"] and m["weight"] > 0]
    weighted_low = weighted_high = weighted_midpoint = None
    recommended_offer = None
    # Offer figures are only computed when at least one weighted method is
    # available; initialise them so the references below (and the return dict)
    # never hit an unbound local when a listing has no usable valuation method.
    open_offer = target_price = walk_away = None

    if available_methods:
        # A4 (2026-07-14): WEIGHTED MEDIAN, not weighted mean. A mean is not
        # robust to outliers — one bad method (LS29: bedroom-matched £100k low,
        # double-weighted) hijacked the headline. With ~4 voting methods a
        # trimmed mean IS essentially a median, so we use the honest version;
        # consistent in spirit with the interquartile mean already applied
        # inside avg_sold_price for the comparables themselves.
        weighted_low = _weighted_median([(m["low"], m["weight"]) for m in available_methods])
        weighted_high = _weighted_median([(m["high"], m["weight"]) for m in available_methods])
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
    rendered_methods = [m for m in methods if m["available"] and m.get("low")]
    if rendered_methods:
        all_lows = [m["low"] for m in rendered_methods]
        all_highs = [m["high"] for m in rendered_methods]
        chart_price_min = int(min(all_lows + [asking_price]) * 0.96)
        chart_price_max = int(max(all_highs + [asking_price]) * 1.04)

    # Price reduction formatting
    original_asking_price_formatted = _fmt(original_asking_price)
    reduction_amount_formatted = _fmt(reduction_amount)

    # ── PUBLISHED CONFIDENCE SCORE + CAVEAT (Cycle 1, item 3) ──────────────────
    # One resolver combines the comparable tier, type certainty, thin-set, special
    # tenure and new-build into a high/medium/low score and a buyer-facing caveat.
    # We still publish a number for every listing with a usable postcode.
    # Cycle 3 sanity gate: flag a valuation that diverges wildly from the asking
    # price (probable special tenure / lease / mis-listing) — drives the caveat below.
    asking_anomaly = False
    if asking_price and weighted_midpoint:
        ratio = weighted_midpoint / asking_price
        asking_anomaly = (ratio > _ASKING_ANOMALY_HIGH_RATIO
                          or ratio < _ASKING_ANOMALY_LOW_RATIO)
    confidence_score, confidence_reasons, confidence_caveat = _resolve_confidence(
        comparable_tier=comparable_tier,
        comparable_confidence=comparable_confidence,
        type_unknown=type_unknown,
        comp_count=len(comparables_for_avg),
        sale_type=sale_type,
        is_new_build=is_new_build,
        has_value=weighted_midpoint is not None,
        matched_sold_value=matched_sold_value,
        weighted_midpoint=weighted_midpoint,
        asking_anomaly=asking_anomaly,
        asking_price=asking_price,
    )
    # Divergence between the independent matched-sold signal and the published
    # midpoint — surfaced for the QC layer and the batch test (drives the HIGH gate).
    matched_sold_divergence_pct = None
    if matched_sold_value and weighted_midpoint:
        matched_sold_divergence_pct = round(
            (weighted_midpoint - matched_sold_value) / matched_sold_value * 100, 1)

    # ── B3 (2026-07-14): asking-price guard rail at every confidence level ─────
    # If the evidence-anchored midpoint deviates >15% from asking, don't ship it
    # silently: downgrade confidence one tier and flag. 15% not 10% because the
    # verdict logic calls a listing overpriced at >8%, so honest verdicts on
    # overpriced stock legitimately produce 8-15% gaps; beyond 15% the more likely
    # story is our data going wrong. (The ±40/50% anomaly gate remains the outer
    # rail; the fixture's fair-verdict-with-13%-trio case is caught by C3 below.)
    valuation_asking_divergence_pct = None
    valuation_guard_triggered = False
    if asking_price and weighted_midpoint:
        valuation_asking_divergence_pct = round(
            (weighted_midpoint - asking_price) / asking_price * 100, 1)
        if abs(valuation_asking_divergence_pct) > 15 and not asking_anomaly:
            valuation_guard_triggered = True
            confidence_score = {"high": "medium", "medium": "low"}.get(
                confidence_score, "low")
            note = ("our estimate sits well away from the asking price — "
                    "confidence reduced pending stronger local evidence")
            confidence_reasons.append(note)
            confidence_caveat = f"{confidence_caveat} {note.capitalize()}." if confidence_caveat else f"{note.capitalize()}."

    # Size-mismatch guardrail fired: the published number leans on the £/m² and
    # asking-anchored methods, not the local comparable average. Cap confidence
    # at medium and tell the buyer why in plain language.
    if size_mismatch_excluded:
        if confidence_score == "high":
            confidence_score = "medium"
        note = ("this home's floor area is well outside the local norm, so the "
                "usual comparable-sales average was set aside in favour of "
                "size-based £/m² evidence")
        confidence_reasons.append(note)
        confidence_caveat = f"{confidence_caveat} {note.capitalize()}." if confidence_caveat else f"{note.capitalize()}."

    # ── ASKING-ANCHOR v1 (CEO-approved 2026-07-17, supersedes B2) ──────────────
    # The published trio is a NEGOTIATING POSITION anchored to the asking price
    # via the market's negotiability signals (the Frontier anchor: local
    # asking-to-sold discount, time on market, reduction history), with the
    # evidence midpoint pulling the anchor in proportion to confidence.
    # LOW confidence changes the composition (pure asking-anchor), not whether
    # the buyer gets a trio. Two overrides keep us honest: the asking-anomaly
    # gate, and the overpricing guardrail — when the evidence says the asking
    # price itself is materially inflated, we do NOT silently anchor to it.
    trio_anchor = "evidence"
    trio_anchor_note = None
    overpricing_flag = False
    overpricing_flag_level = None
    if asking_price and available_methods and open_offer:
        _cfg = ASKING_ANCHOR_V1
        _div_below_pct = ((asking_price - weighted_midpoint) / weighted_midpoint * 100
                          if weighted_midpoint else 0.0)
        _op_threshold = (_cfg["overpricing_flag_pct"]["low"] if confidence_score == "low"
                         else _cfg["overpricing_flag_pct"]["medium_plus"])
        if asking_anomaly:
            # Anomaly override (retained in all cases): the asking price itself
            # is suspect, so the evidence-led trio stands and the existing
            # anomaly presentation gates apply.
            trio_anchor = "evidence"
            trio_anchor_note = (
                "The asking price is far out of line with the sold evidence — "
                "these numbers are anchored to the evidence, and the whole "
                "estimate should be treated with caution.")
        elif _div_below_pct > _op_threshold:
            # Overpricing guardrail: evidence says the asking price is inflated.
            # Keep the evidence-led trio and say so prominently.
            overpricing_flag = True
            overpricing_flag_level = confidence_score
            trio_anchor = "evidence"
            trio_anchor_note = (
                "This listing looks materially overpriced against local sold "
                "evidence, so these numbers are anchored to the evidence — not "
                "the asking price. Consider whether to offer at all.")
        else:
            anchor_pct, _fb = _frontier_anchor(
                local_sold_discount_pct, days_on_market, local_avg_dom,
                reduction_pct, price_reduced)
            open_disc = min(anchor_pct * _cfg["open_discount_factor"],
                            _cfg["max_open_discount_pct"])
            asking_open = asking_price * (1 - open_disc / 100)
            _w = _cfg["avm_blend"].get(confidence_score, 0.0)
            open_offer = round(((1 - _w) * asking_open + _w * open_offer) / 1000) * 1000
            target_price = round((asking_price - (asking_price - open_offer)
                                  * _cfg["target_discount_ratio"]) / 1000) * 1000
            if confidence_score in ("high", "medium") and weighted_high:
                _walk = weighted_high * (1 + _cfg["walk_headroom_pct"] / 100)
            else:
                _walk = asking_price * (1 - _cfg["walk_asking_discount_pct"] / 100)
            # §6 hard rules: nothing exceeds asking; walk ≥ target ≥ open.
            walk_away = round(min(_walk, asking_price) / 1000) * 1000
            target_price = min(target_price, asking_price)
            walk_away = max(walk_away, target_price)
            open_offer = min(open_offer, target_price)
            recommended_offer = open_offer
            trio_anchor = "asking_blend" if _w else "asking"
            _basis = ["this area's asking-to-sold discounts"]
            if days_on_market and local_avg_dom:
                _basis.append("time on market")
            if price_reduced:
                _basis.append("the price-cut history")
            if _w:
                trio_anchor_note = (
                    "A negotiating position, not a valuation: built from "
                    + ", ".join(_basis)
                    + ", blended with our independent value estimate "
                    f"({confidence_score.upper()} confidence).")
            else:
                trio_anchor_note = (
                    "A negotiating position, not a valuation: local evidence is "
                    "thin, so these numbers are built purely from the asking "
                    "price and " + ", ".join(_basis) + ".")

    # ── C1 (2026-07-14): the trio must sit inside the Offer Frontier ───────────
    # The report promises the Frontier never goes below the valuation floor or
    # above walk-away; the reverse must also hold — the opening offer can never
    # sit BELOW the Frontier's own deepest (Aggressive) position, which the
    # Frontier itself describes as "beyond what typically clears here".
    open_offer_frontier_clamped = False
    # Not applied when the overpricing guardrail fired: an opening far below
    # the frontier is then deliberate (the asking price itself is inflated).
    if open_offer and asking_price and trio_anchor == "evidence" and not overpricing_flag:
        anchor_pct, _fb = _frontier_anchor(
            local_sold_discount_pct, days_on_market, local_avg_dom,
            reduction_pct, price_reduced)
        deep_pct = min(2.0 * anchor_pct * (1.0625 if _fb else 1.0), _FRONTIER_DEEP_CAP_PCT)
        frontier_deep_price = asking_price - round(asking_price * deep_pct / 100 / 500) * 500
        if weighted_low:
            frontier_deep_price = max(min(frontier_deep_price, walk_away or frontier_deep_price),
                                      min(weighted_low, walk_away or weighted_low))
        if open_offer < frontier_deep_price:
            moved_pct = (frontier_deep_price - open_offer) / open_offer * 100
            open_offer = round(frontier_deep_price / 1000) * 1000
            open_offer = min(open_offer, (target_price or open_offer + 1000) - 1000)
            recommended_offer = open_offer
            if moved_pct > 2:
                open_offer_frontier_clamped = True
                confidence_reasons.append(
                    "our evidence-based opening position sat below what typically "
                    "clears in this market — it was raised to the frontier floor")

    # ── C3 (2026-07-14): verdict and recommendation must agree ─────────────────
    # The verdict derives from asking-vs-comparable-average; the trio from the
    # weighted range. When the verdict says "fair" but the target implies a
    # >10% discount (the LS29 case: "fairly priced" + a 13%-below trio), recompute
    # the verdict from the final midpoint so one story ships, and flag it.
    verdict_reconciled = False
    if (verdict == "fair" and asking_price and target_price
            and abs(asking_price - target_price) / asking_price > 0.10
            and weighted_midpoint):
        mid_diff_pct = round(((asking_price - weighted_midpoint) / weighted_midpoint) * 100, 1)
        verdict = "overpriced" if mid_diff_pct > 8 else ("value" if mid_diff_pct < -5 else "fair")
        sold_diff_pct = mid_diff_pct
        verdict_reconciled = True

    # Recompute the open-offer context percentages: the guards above (B2 anchor,
    # C1 clamp) may have moved the trio after the first computation.
    if open_offer and asking_price:
        open_offer_vs_asking_pct = round(((asking_price - open_offer) / asking_price) * 100, 1)
    if open_offer and local_avg_sold:
        open_offer_vs_comps_pct = round(((local_avg_sold - open_offer) / local_avg_sold) * 100, 1)

    return {
        "postcode": formatted,
        "postcode_used": postcode_used,
        "address": address,
        # Count behind the headline average (the size-matched subset when FIX 2
        # tightened the set), so "based on N comparable sales" stays truthful.
        "comparables_count": len(comparables_for_avg),
        "comparables": comparables_list,
        "search_broadened": broadened,
        "asking_price": asking_price,
        "asking_price_formatted": f"£{asking_price:,}",
        "bedrooms": bedrooms,
        "bathrooms": bathrooms,
        "property_type": property_type,
        # Display-safe label for inline prose: never renders the literal "None".
        "property_type_label": property_type or "property",
        "floor_area_sqm": floor_area_sqm,
        # Provenance / confidence flags (FIX 1-3) for the report UI and QC layer.
        "bedrooms_source": bedrooms_source,
        "property_type_source": property_type_source,
        "floor_area_source": floor_area_source,
        "floor_area_confidence": floor_area_confidence,
        # Raw scraped area when FIX 3 rejected it as implausible (else None) —
        # kept for the QC layer; never fed into the valuation maths.
        "floor_area_sqm_raw": floor_area_sqm_raw,
        "comparable_confidence": comparable_confidence,
        # 2026-07-14 guards: B2 trio anchor basis (+ buyer-facing note when
        # asking-anchored), B3 divergence guard, C1 frontier clamp, C3 verdict
        # reconciliation — all surfaced for the report UI and QC layer.
        "trio_anchor": trio_anchor,
        "trio_anchor_note": trio_anchor_note,
        # Asking-anchor v1: overpricing guardrail (evidence midpoint materially
        # below asking → trio stays evidence-led and the report says so).
        "overpricing_flag": overpricing_flag,
        "overpricing_flag_level": overpricing_flag_level,
        "valuation_asking_divergence_pct": valuation_asking_divergence_pct,
        "valuation_guard_triggered": valuation_guard_triggered,
        "open_offer_frontier_clamped": open_offer_frontier_clamped,
        "verdict_reconciled": verdict_reconciled,
        # Cycle 1: published confidence model — high/medium/low + plain-English
        # caveat, plus the comparable tier and any detected special sale type.
        "confidence_score": confidence_score,
        "confidence_reasons": confidence_reasons,
        "confidence_caveat": confidence_caveat,
        "comparable_tier": comparable_tier,
        "sale_type": sale_type,
        # Cycle 3: valuation diverged far from asking (probable non-standard listing).
        "asking_anomaly": asking_anomaly,
        "comparable_count_size_matched": comparable_count_size_matched,
        # P2: where the comparable set came from and how it was matched.
        "comparable_source": comparable_source,
        "comparable_radius_miles": comparable_radius_miles,
        "comparable_bedroom_band": comparable_bedroom_band,
        "nearby_feed_count": nearby_feed_count,
        "nearby_match_count": nearby_match_count,
        "lr_vs_rightmove_divergence_pct": lr_vs_rightmove_divergence_pct,
        # Guardrail: true when a thin, bedroom-blind comparable outlier was dropped
        # from the weighted range to avoid a misleading headline valuation.
        "comparable_outlier_excluded": comparable_outlier_excluded,
        "size_mismatch_excluded": size_mismatch_excluded,
        # Option D: bedroom-specific local price signal.
        "bedroom_local_avg_asking": bedroom_local_avg_asking,
        "bedroom_implied_value": bedroom_implied_value,
        "comparable_downweighted_for_bedroom": comparable_downweighted_for_bedroom,
        # Phase B: bedroom/size/distance-matched SOLD comparables (scored, not yet primary).
        "matched_sold_value": matched_sold_value,
        "matched_sold_count": matched_sold_count,
        "matched_sold_confidence": matched_sold_confidence,
        # Cycle 2: agreement between the independent matched-sold signal and the
        # published midpoint (drives the HIGH gate; large gap = methods disagree).
        "matched_sold_divergence_pct": matched_sold_divergence_pct,
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
        # Seller-motivation signal (paid-tier section). Context/display only —
        # none of these feed the offer calculation. The raw local discount also
        # anchors the render-time offer frontier (_offer_frontier).
        "seller_signal_score": seller_signal_score,
        "seller_signal_reasons": seller_signal_reasons,
        "seller_signal_summary": seller_signal_summary,
        "local_sold_discount_pct": local_sold_discount_pct,
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
        "main_photo_url": main_photo_url,
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
            # Pass None (not "3"/"semi-detached") as the bedrooms/type fallback:
            # if the scrape can't read them they must stay unknown, never default.
            postcode, asking_price, bedrooms, property_type, address, extra = merge_scraped_listing(
                property_url, "", 0, None, None, ""
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
                bedrooms=report.get("bedrooms"),
                property_type=report.get("property_type"),
                postcode=report.get("postcode", ""),
                floor_area_sqm=report.get("floor_area_sqm"),
                # 2026-07-17 (Wemborough validation): unlock rebuilds ran with
                # address=None whenever the buyer hadn't used the picker, so the
                # PAID rebuild silently dropped the last-sale anchor, the F1/F2
                # seller-motivation signal and the EPC/£-per-sqf address matches
                # the free build already had. Fall back to the address the
                # original build resolved (buyer-confirmed on the G1 page).
                # An explicit picker choice still wins.
                address=(address or stored.get("selected_address")
                         or report.get("resolved_address") or report.get("address")),
                scraper_days_on_market=report.get("days_on_market"),
                price_reduced=report.get("price_reduced", False),
                original_asking_price=report.get("original_asking_price"),
                reduction_date=report.get("reduction_date"),
                reduction_amount=report.get("reduction_amount"),
                reduction_pct=report.get("reduction_pct"),
                is_new_build=report.get("is_new_build", False),
                bedrooms_source=report.get("bedrooms_source", "unknown"),
                property_type_source=report.get("property_type_source", "unknown"),
                floor_area_source=report.get("floor_area_source", "unknown"),
                sale_type=report.get("sale_type"),
                latitude=report.get("latitude"),
                longitude=report.get("longitude"),
                bathrooms=report.get("bathrooms"),
                main_photo_url=report.get("main_photo_url"),
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

    # Homepage £29 clicks have no report yet — checkout is per-report, so the
    # funnel starts at the free-report form (next=form). Report-page CTAs go
    # straight to Stripe via /r/<id>/checkout and never come through here.
    if request.args.get("next") == "form":
        # ?unlock=29 makes the homepage show the "free report first" callout
        return redirect("https://houseoffer.uk/?unlock=29#hero-form")
    return redirect("https://houseoffer.uk/#pricing")

# ── POST-UNLOCK BUYER PROFILE (2026-07-05) ───────────────────────────────────
# Three single-choice questions shown after unlock, before the paid report
# renders. Always skippable — a paying customer is never blocked from their
# report. Answers personalise the report at DISPLAY TIME only: the stored
# open/target/walk_away values are never modified.

BUYER_PROFILE_FIELDS = {
    "position": {"first_time", "sold_stc", "need_to_sell", "cash", "investor"},
    "attachment": {"several", "this_one", "the_one"},
    "timeline": {"fast", "one_three", "flexible"},
}


# ── OFFER POSITIONING FRONTIER (Frontier v2, CEO-approved 2026-07-05) ─────────
# Replaces both the Task 3 static DOM buckets and the Task 5(B) numeric stance
# shift (approved supersession): a continuous curve anchored on the local
# asking-to-sold discount, shifted by time on market, presented as three named
# positions. Computed at RENDER TIME from stored values — display layer only,
# works retroactively for already-stored paid reports, and the stored
# open/target/walk_away are never modified.

_FRONTIER_RISK = {
    "secure": "Very likely to be taken seriously. Low risk of losing it on "
              "price — but you leave the most money on the table.",
    "balanced": "In line with what actually clears in this market once time on "
                "market is counted. Serious and defensible — expect "
                "negotiation, not offence.",
    "aggressive": "Beyond what typically clears here. Real chance of rejection "
                  "or being beaten by another buyer — take this position only "
                  "if you can genuinely walk away.",
}
# CEO 2026-07-05: 2.2·A read too bold (16% on the stale/soft example). Deep end
# is 2.0·A with a hard ceiling — the frontier never shows more than this many
# percent below asking, whatever the inputs.
_FRONTIER_DEEP_CAP_PCT = 13.0
_FRONTIER_NATIONAL_DISCOUNT = 4.5  # same national fallback as method 7

# ── ASKING-ANCHOR v1 (CEO-approved 2026-07-17) ────────────────────────────────
# HouseOffer is a negotiation-advice product, not an AVM. The published trio
# anchors to the asking price via the market's negotiability signals — the
# local asking-to-sold discount, time on market and reduction history, i.e.
# the same _frontier_anchor the Offer Frontier is built from — with the
# independent evidence midpoint pulling the anchor in proportion to confidence.
# The evidence engine is demoted to (a) the overpricing guardrail and (b) the
# buyer's justification pack. Every parameter lives here so the prospective
# cohort can tune them without touching engine code.
ASKING_ANCHOR_V1 = {
    # opening discount = frontier anchor A × this factor (between the
    # Frontier's Balanced and Aggressive positions), hard-capped below
    "open_discount_factor": 1.25,
    "max_open_discount_pct": 12.0,
    # target sits this fraction of the opening discount below asking
    "target_discount_ratio": 0.5,
    # how much the evidence midpoint pulls the opening anchor, by confidence
    "avm_blend": {"high": 0.45, "medium": 0.20, "low": 0.0},
    # walk-away: marginally above the evidence ceiling when we trust it…
    "walk_headroom_pct": 1.5,
    # …else referenced to asking (LOW confidence)
    "walk_asking_discount_pct": 2.0,
    # overpricing guardrail: evidence midpoint this far below asking →
    # prominent flag + evidence-led trio (never silently anchor to a number
    # we believe is inflated)
    "overpricing_flag_pct": {"medium_plus": 15.0, "low": 25.0},
}


def _frontier_anchor(local_discount_pct, days, avg_dom, reduction_pct, price_reduced):
    """The Frontier's anchor discount A (in %), shared by the render-time
    _offer_frontier and the build-time guards (B2 asking-anchored trio, C1
    trio-inside-frontier clamp) so the two can never drift apart.
    Returns (anchor_pct, is_fallback)."""
    is_fallback = local_discount_pct is None
    d_bar = _FRONTIER_NATIONAL_DISCOUNT if is_fallback else local_discount_pct
    m = (min(max(0.5 + 0.5 * (days / avg_dom), 0.75), 1.75)
         if (days and avg_dom) else 1.0)
    b = (1.0 if (reduction_pct or 0) >= 5 else (0.5 if price_reduced else 0.0))
    return min(max(d_bar * m + b, 1.0), 12.0), is_fallback


def _offer_frontier(report, profile=None):
    """Build the offer-positioning frontier from values already on the stored
    report. Returns None when the report lacks the essentials (asking price /
    walk-away), else a dict with the anchor, honesty flags and three positions.

    Model (FRONTIER_V2_PROPOSAL, approved): pressure multiplier
    m = clamp(0.5 + 0.5·r, 0.75, 1.75) on the local asking-to-sold discount,
    plus a price-cut bonus (+1.0pp for a ≥5% cut, +0.5pp for any), clamped to
    [1%, 12%] as the anchor A. Positions: SECURE 0.5A–A, BALANCED A–1.5A,
    AGGRESSIVE 1.5A–2.0A hard-capped at _FRONTIER_DEEP_CAP_PCT. National
    fallback widens each band ×1.25 and is labelled plainly. Qualitative risk
    only — no acceptance probabilities anywhere.

    Guardrails: implied prices are floored at weighted_low and HARD-CAPPED at
    the stored walk_away (explicit min() below) — a position whose whole range
    falls outside collapses onto the bound and says so."""
    asking = report.get("asking_price")
    walk = report.get("walk_away")
    if not asking or not walk:
        return None
    floor = report.get("weighted_low")

    d_local = report.get("local_sold_discount_pct")
    days, avg_dom = report.get("days_on_market"), report.get("local_avg_dom")
    dom_shifted = bool(days and avg_dom)
    anchor, is_fallback = _frontier_anchor(
        d_local, days, avg_dom,
        report.get("reduction_pct"), report.get("price_reduced"))

    bands = {
        "secure": (0.5 * anchor, anchor),
        "balanced": (anchor, 1.5 * anchor),
        "aggressive": (1.5 * anchor, 2.0 * anchor),
    }
    positions = []
    for key in ("secure", "balanced", "aggressive"):
        lo, hi = bands[key]
        if is_fallback:
            mid, half = (lo + hi) / 2, (hi - lo) / 2 * 1.25
            lo, hi = mid - half, mid + half
        hi = min(hi, _FRONTIER_DEEP_CAP_PCT)
        lo = max(min(lo, hi), 0.0)
        lo, hi = round(lo * 2) / 2, round(hi * 2) / 2  # 0.5pp steps, no false precision
        price_hi = asking - round(asking * lo / 100 / 500) * 500  # shallow end → higher £
        price_lo = asking - round(asking * hi / 100 / 500) * 500  # deep end → lower £

        collapsed = None
        if floor and price_hi < floor:
            # Whole range below the data-justified floor: the evidence can't
            # credibly support opening lower — collapse onto the floor.
            collapsed = "floor"
            price_lo = price_hi = floor
        elif floor and price_lo < floor:
            price_lo = floor
        # HARD GUARDRAIL (explicit cap, not a convention): no frontier position
        # ever displays a price above the stored data-derived walk_away.
        if price_lo > walk:
            collapsed = "ceiling"
            price_lo = price_hi = walk
        else:
            price_hi = min(price_hi, walk)

        pct_label = f"about {hi:g}%" if lo == hi else f"{lo:g}–{hi:g}%"
        price_label = (_fmt(price_lo) if price_lo == price_hi
                       else f"{_fmt(price_lo)}–{_fmt(price_hi)}")
        positions.append({
            "key": key, "name": key.capitalize(),
            "lo_pct": lo, "hi_pct": hi, "pct_label": pct_label,
            "price_lo": price_lo, "price_hi": price_hi, "price_label": price_label,
            "collapsed": collapsed, "risk": _FRONTIER_RISK[key],
        })

    emphasis, _ = _profile_emphasis(profile)
    return {
        "anchor_pct": round(anchor, 1),
        "is_fallback": is_fallback,
        "dom_shifted": dom_shifted,
        "days_on_market": days if dom_shifted else None,
        "positions": positions,
        "emphasis": emphasis,
        "walk_away_formatted": _fmt(walk),
    }


# ── C2a (2026-07-14): buyer answers drive the report ─────────────────────────
# The three post-unlock answers stop being cosmetic copy and set the buyer's
# recommended Frontier position, position the DISPLAYED opening offer inside
# that band, and call out explicitly where the answers fight the evidence.
# Render-time only: the stored trio is never modified, so every personalised
# report is auditable against its data-only numbers and reversible per report.
# Hard guardrails retained: the Frontier bands are already floored at
# weighted_low and capped at walk_away, so the personalised open inherits both.

# Each answer pushes the recommended stance: +1 toward Aggressive (buyer can
# afford rejection), -1 toward Secure (buyer can't), 0 neutral. The rationale
# strings are shown to the buyer verbatim as "because you said…" drivers.
_PROFILE_STANCE = {
    "attachment": {
        "several": (1, "you're comparing several properties"),
        "this_one": (0, "you want this one at the right price"),
        "the_one": (-1, "you don't want to lose this one"),
    },
    "timeline": {
        "flexible": (1, "your timeline is flexible"),
        "one_three": (0, "your timeline is standard"),
        "fast": (-1, "you want to complete fast (repeated rejections cost you time)"),
    },
    "position": {
        "cash": (1, "you're a cash buyer"),
        "investor": (1, "you're buying as an investor"),
        "first_time": (0, "you're chain-free"),
        "sold_stc": (0, "you're proceedable"),
        "need_to_sell": (-1, "your offer isn't fully proceedable yet (it needs to be more attractive to compete)"),
    },
}


def _profile_emphasis(profile):
    """Combine all three buyer answers into the recommended Frontier position.
    Returns (emphasis, drivers): net push >= +1 -> aggressive, <= -1 -> secure,
    else balanced. Drivers lists only the answers that pushed, each with its
    direction, so the report can show the buyer exactly which answer moved
    which recommendation. No profile -> balanced with no drivers (the
    pre-questionnaire default)."""
    drivers = []
    score = 0
    for field in ("attachment", "position", "timeline"):
        push, said = _PROFILE_STANCE[field].get((profile or {}).get(field) or "", (None, None))
        if push is None:
            continue
        score += push
        if push:
            drivers.append({"field": field, "push": push, "said": said})
    emphasis = "aggressive" if score >= 1 else ("secure" if score <= -1 else "balanced")
    return emphasis, drivers


def _personalise_offer(report, profile, frontier):
    """Render-time personalisation payload (C2a). Places the displayed opening
    offer inside the buyer's recommended Frontier band (clamped, so it only
    moves when the answers call for a different depth than the data-only open),
    and lists the conflicts between what the buyer said and what the evidence
    shows. Target and walk-away are NEVER touched — the walk-away is the line
    the data can defend, whoever the buyer is."""
    if not profile or not frontier:
        return None
    emphasis, drivers = _profile_emphasis(profile)
    pos = next((p for p in frontier["positions"] if p["key"] == emphasis), None)
    base_open = report.get("open_offer") or report.get("recommended_offer")
    if not pos or not base_open:
        return None

    personal_open = int(min(max(base_open, pos["price_lo"]), pos["price_hi"]))
    moved = personal_open != base_open
    asking = report.get("asking_price")
    local_avg_sold = report.get("local_avg_sold")
    vs_asking_pct = (round((asking - personal_open) / asking * 100, 1)
                     if asking else None)
    vs_comps_pct = (round((local_avg_sold - personal_open) / local_avg_sold * 100, 1)
                    if local_avg_sold else None)

    # Where the answers and the evidence disagree, say so explicitly — the
    # recommendation still follows the buyer's answers, but never silently.
    signal = report.get("seller_signal_score")
    walk_fmt = frontier.get("walk_away_formatted")
    conflicts = []
    if emphasis == "aggressive" and signal == "weak":
        conflicts.append(
            "Your answers point to the Aggressive position, but the seller-motivation "
            "evidence shows little pressure on this seller. An aggressive opening here "
            "carries a real chance of flat rejection — play it only if you can "
            "genuinely walk away.")
    if emphasis == "secure" and signal == "strong":
        conflicts.append(
            "Your answers point to the Secure position, but the evidence shows a "
            "seller under real pressure. Protecting the purchase here likely means "
            "leaving money on the table — consider opening one position deeper than "
            "your instinct says.")
    if profile.get("timeline") == "fast" and signal == "weak":
        conflicts.append(
            "You want to move fast, but nothing suggests this seller does. Your speed "
            "is worth less to an unpressured seller — don't expect it to buy much off "
            "the price here.")
    if profile.get("attachment") == "the_one" and report.get("verdict") == "overpriced":
        conflicts.append(
            "You've told us this is THE one — and the evidence says it's asking above "
            "what the market supports. That combination is exactly how buyers overpay. "
            f"Your walk-away of {walk_fmt} does not move, however much you want it.")

    if drivers:
        saids = [d["said"] for d in drivers]
        joined = saids[0] if len(saids) == 1 else ", ".join(saids[:-1]) + " and " + saids[-1]
        stance_reason = (f"Because {joined}, your answers point to the "
                         f"{pos['name']} position on the Offer Frontier below.")
    else:
        stance_reason = ("Your answers balance out between leverage and urgency, so we've "
                         "kept you on the Balanced position — in line with what actually "
                         "clears in this market.")

    return {
        "emphasis": emphasis,
        "band_name": pos["name"],
        "band_price_label": pos["price_label"],
        "personal_open": personal_open,
        "personal_open_formatted": _fmt(personal_open),
        "base_open": base_open,
        "base_open_formatted": _fmt(base_open),
        "moved": moved,
        "direction": ("deeper" if personal_open < base_open else "higher") if moved else None,
        "vs_asking_pct": vs_asking_pct,
        "vs_comps_pct": vs_comps_pct,
        "drivers": drivers,
        "stance_reason": stance_reason,
        "conflicts": conflicts,
    }


@app.route("/r/<report_id>/buyer-profile", methods=["POST"])
def buyer_profile(report_id):
    """Store the post-unlock questionnaire answers (or an explicit skip) on the
    report, then send the buyer to their report. Invalid values are dropped
    per-field; a submission with no usable answer counts as a skip so the
    report always renders with neutral defaults."""
    if not re.fullmatch(r"[a-f0-9]{8,32}", report_id):
        return "Report not found", 404
    stored = load_report(report_id)
    if not stored:
        return "Report not found", 404

    data = request.form if request.form else (request.get_json(silent=True) or {})
    if data.get("skip"):
        stored["buyer_profile_skipped"] = True
        save_report(report_id, stored)
        log_event(report_id, "buyer_profile_skipped", {})
        return redirect(f"/r/{report_id}")

    answers = {}
    for field, allowed in BUYER_PROFILE_FIELDS.items():
        v = (data.get(field) or "").strip()
        answers[field] = v if v in allowed else None
    if not any(answers.values()):
        stored["buyer_profile_skipped"] = True
        save_report(report_id, stored)
        log_event(report_id, "buyer_profile_skipped", {"reason": "no_valid_answers"})
        return redirect(f"/r/{report_id}")

    answers["answered_at"] = _now_iso()
    stored["buyer_profile"] = answers
    stored.pop("buyer_profile_skipped", None)
    save_report(report_id, stored)
    log_event(report_id, "buyer_profile", answers)
    report = stored.get("report") or {}
    post_to_sheets({
        "type": "buyer_profile", "timestamp": _now_iso(), "uuid": report_id,
        "position": answers.get("position") or "",
        "attachment": answers.get("attachment") or "",
        "timeline": answers.get("timeline") or "",
        "postcode": report.get("postcode"), "verdict": report.get("verdict"),
        "asking_price": report.get("asking_price"),
        "report_url": f"{BASE_URL.rstrip('/')}/r/{report_id}",
    })
    return redirect(f"/r/{report_id}")


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
                "<p><a href='https://houseoffer.uk'>Generate a new report →</a></p>"
                "</body></html>", 404)

    # Background builds: serve the self-refreshing page until ready. Builds older
    # than 5 minutes are presumed dead (worker restart) - fall back to the previous
    # report data if there is any, otherwise mark failed.
    status = stored.get("status", "ready")
    # G1: pre-build confirmation state — no PropertyData credit has been spent
    # yet; the page asks "is this the property?" and offers a correction path.
    # This state can sit indefinitely (abandoning it costs nothing).
    if status == "awaiting_confirmation":
        ci = stored.get("confirm_inputs") or {}
        extra = ci.get("extra") or {}
        return render_template(
            "confirm_property.html", report_id=report_id,
            address=extra.get("resolved_address") or ci.get("address"),
            raw_address=ci.get("address"),
            postcode=ci.get("postcode"),
            property_type=ci.get("property_type"),
            bedrooms=ci.get("bedrooms"),
            floor_area_sqm=ci.get("floor_area_sqm") or (extra.get("scraper_floor_area_sqm")),
            asking_price=ci.get("asking_price"),
            resolution=extra.get("address_resolution"))
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
                "<p><a href='https://houseoffer.uk'>Try again →</a></p>"
                "</body></html>", 500)

    log_event(report_id, "report_viewed", {
        "user_agent": request.headers.get("User-Agent", "")[:200],
        "referer": request.headers.get("Referer", "")[:200],
    })

    report = stored.get("report", {})
    report_url = f"{BASE_URL.rstrip('/')}/r/{report_id}"
    paid = stored.get("paid", False)

    # Post-unlock questions: paid reports ask three quick single-choice
    # questions before first render, so the report is written for the buyer's
    # situation. Always skippable — never blocks a paying customer.
    if paid and not stored.get("buyer_profile") and not stored.get("buyer_profile_skipped"):
        log_event(report_id, "buyer_questions_shown", {})
        return render_template(
            "buyer_questions.html", report_id=report_id,
            address=report.get("resolved_address") or report.get("address"),
            postcode=report.get("postcode"))

    template = "report_paid.html" if paid else "report_free.html"
    profile = stored.get("buyer_profile") if paid else None
    # Frontier v2: display-layer positioning computed from stored values at
    # render time (works for previously stored paid reports too). The stored
    # numbers themselves are passed through untouched.
    frontier = _offer_frontier(report, profile) if paid else None
    # C2a: buyer answers drive the displayed numbers/copy — render-time only,
    # stored trio untouched (auditable and reversible per report).
    personalisation = _personalise_offer(report, profile, frontier) if paid else None
    return render_template(template, report_url=report_url, report_id=report_id,
                           buyer_profile=profile, offer_frontier=frontier,
                           personalisation=personalisation,
                           **report)


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


# Cap on rebuild-triggering address corrections per report: each free-tier
# rebuild costs ~3-5 PropertyData calls, so this bounds the spend per report.
MAX_ADDRESS_CORRECTIONS = 2


@app.route("/r/<report_id>/confirm-address", methods=["POST"])
def confirm_address(report_id):
    """Address-confirmation modal on the report page. A plain confirmation is
    logged and costs nothing (it also validates the address-resolution
    pipeline). A corrected address triggers a background rebuild — the same
    path as the sold-candidates picker — capped per report."""
    if not re.fullmatch(r"[a-f0-9]{8,32}", report_id):
        return jsonify({"error": "report not found"}), 404
    stored = load_report(report_id)
    if not stored:
        return jsonify({"error": "report not found"}), 404

    data = request.get_json(silent=True) or {}
    corrected = (data.get("address") or "").strip()

    if not corrected:
        stored["address_confirmed"] = True
        save_report(report_id, stored)
        report = stored.get("report") or {}
        log_event(report_id, "address_confirmed", {
            "address": report.get("resolved_address") or report.get("address"),
        })
        return jsonify({"status": "confirmed", "rebuilding": False})

    if len(corrected) > 200:
        return jsonify({"error": "address too long"}), 400
    if stored.get("status") == "building":
        return jsonify({"error": "report is already rebuilding"}), 409
    corrections = int(stored.get("address_corrections") or 0)
    if corrections >= MAX_ADDRESS_CORRECTIONS:
        log_event(report_id, "address_correction_capped", {"address": corrected})
        return jsonify({"error": "correction limit reached", "rebuilding": False}), 429

    stored["address_corrections"] = corrections + 1
    stored["status"] = "building"
    stored["build_started_at"] = _now_iso()
    save_report(report_id, stored)
    log_event(report_id, "address_corrected",
              {"address": corrected, "correction_n": corrections + 1})
    _start_rebuild(report_id, stored, address=corrected,
                   tier="paid" if stored.get("paid") else "free")
    return jsonify({"status": "rebuilding", "rebuilding": True})


# ── CROWD VOTING ROUTES ───────────────────────────────────────────────────────

@app.route("/r/<report_id>/share-link", methods=["POST"])
def create_share_link(report_id):
    """Mint (or reuse) the short public voting link for a report. Recipients
    land on /v/<slug> — a lightweight page that costs no PropertyData calls."""
    if not re.fullmatch(r"[a-f0-9]{8,32}", report_id):
        return jsonify({"error": "report not found"}), 404
    stored = load_report(report_id)
    if not stored:
        return jsonify({"error": "report not found"}), 404
    slug = stored.get("vote_slug")
    if not slug or not os.path.exists(_slug_path(slug)):
        slug = _mint_vote_slug(report_id)
        stored["vote_slug"] = slug
        save_report(report_id, stored)
        log_event(report_id, "share_link_created", {"slug": slug})
    return jsonify({"slug": slug, "url": f"{BASE_URL.rstrip('/')}/v/{slug}"})


@app.route("/api/vote", methods=["POST"])
def submit_vote():
    """Record a crowd vote against a report — from the report page (report_id)
    or a share link (slug). One vote per voter token per property; voting
    again updates the existing vote. Every vote streams to the Sheets webhook
    as its own row (type=vote), the durable copy of this data asset."""
    data = request.get_json(silent=True) or {}
    report_id = (data.get("report_id") or "").strip()
    source = "report"
    slug = (data.get("slug") or "").strip()
    if not report_id and slug:
        report_id = _resolve_vote_slug(slug) or ""
        source = "share"
    if not re.fullmatch(r"[a-f0-9]{8,32}", report_id):
        return jsonify({"error": "report not found"}), 404
    stored = load_report(report_id)
    if not stored:
        return jsonify({"error": "report not found"}), 404
    report = stored.get("report") or {}

    try:
        estimate = int(str(data.get("estimate", "")).replace(",", "").replace("£", "").strip())
    except (ValueError, TypeError):
        return jsonify({"error": "estimate must be a number"}), 400
    if not (1_000 <= estimate <= 100_000_000):
        return jsonify({"error": "estimate out of range"}), 400
    name = re.sub(r"[<>\"&]", "", str(data.get("name") or "")).strip()[:30]

    token = request.cookies.get("ho_voter") or uuid.uuid4().hex
    now = _now_iso()
    with _votes_lock:
        votes = _load_votes(report_id)
        existing = next((v for v in votes if v.get("token") == token), None)
        if existing:
            existing.update({"estimate": estimate, "name": name or existing.get("name"),
                             "updated_at": now})
        elif len(votes) >= MAX_VOTES_PER_REPORT:
            return jsonify({"error": "vote limit reached for this property"}), 429
        else:
            votes.append({"token": token, "name": name, "estimate": estimate,
                          "source": source, "created_at": now})
        _save_votes(report_id, votes)

    # Durable copy: one Sheets row per vote, tagged for later analysis
    # (crowd vs. expert vs. eventual sold price — backlog items 63/64).
    post_to_sheets({
        "type": "vote",
        "timestamp": now,
        "uuid": report_id,
        "slug": slug or stored.get("vote_slug") or "",
        "source": source,
        "voter_name": name,
        "estimate": estimate,
        "updated": bool(existing),
        "asking_price": report.get("asking_price"),
        "our_valuation": report.get("weighted_midpoint"),
        "verdict": report.get("verdict"),
        "postcode": report.get("postcode"),
        "property_url": report.get("property_url") or stored.get("property_url", ""),
        "report_url": f"{BASE_URL.rstrip('/')}/r/{report_id}",
    })
    log_event(report_id, "vote_cast", {"source": source, "estimate": estimate,
                                       "updated": bool(existing)})

    resp = jsonify(_vote_summary(votes, exclude_token=token))
    resp.set_cookie("ho_voter", token, max_age=180 * 24 * 3600, samesite="Lax")
    return resp


@app.route("/api/votes/<report_id>")
def get_votes(report_id):
    """Live vote feed for a report. The requester's own vote is excluded from
    the visible list (pages render it locally as "You")."""
    if not re.fullmatch(r"[a-f0-9]{8,32}", report_id):
        return jsonify({"error": "report not found"}), 404
    token = request.cookies.get("ho_voter")
    return jsonify(_vote_summary(_load_votes(report_id), exclude_token=token))


@app.route("/v/<slug>")
def voting_page(slug):
    """Lightweight crowd-voting page behind a share link. Serves entirely from
    the stored report — no scraping, no PropertyData calls, no signup. The
    asking price is only revealed AFTER the visitor locks in their number, so
    their estimate stays unanchored."""
    report_id = _resolve_vote_slug(slug)
    stored = load_report(report_id) if report_id else None
    report = (stored or {}).get("report") or {}
    if not report:
        return render_template("vote_page.html", expired=True, slug=slug), 404
    log_event(report_id, "vote_page_viewed", {
        "slug": slug, "referer": request.headers.get("Referer", "")[:200]})
    return render_template(
        "vote_page.html",
        expired=False,
        slug=slug,
        report_id=report_id,
        vote_url=f"{BASE_URL.rstrip('/')}/v/{slug}",
        report_url=f"{BASE_URL.rstrip('/')}/r/{report_id}",
        address=report.get("address") or report.get("postcode"),
        postcode=report.get("postcode"),
        property_type=report.get("property_type"),
        bedrooms=report.get("bedrooms"),
        main_photo_url=report.get("main_photo_url"),
        asking_price=report.get("asking_price"),
        asking_price_formatted=report.get("asking_price_formatted"),
        weighted_midpoint=report.get("weighted_midpoint"),
        weighted_midpoint_formatted=report.get("weighted_midpoint_formatted"),
    )


# ── PAYMENTS (STRIPE) ─────────────────────────────────────────────────────────
# Flow: report CTA -> /r/<id>/checkout (creates a Stripe Checkout session,
# redirects to Stripe's hosted page) -> buyer pays -> Stripe redirects to
# /r/<id>/checkout/success (verified server-side, unlocks immediately) AND
# fires checkout.session.completed at /stripe/webhook (the backstop if the
# buyer closes the tab before returning). Both paths call _unlock_report,
# which is idempotent, so double-fulfilment is a no-op.

def _unlock_report(report_id, source, extra=None, force=False):
    """Shared unlock primitive: set paid=True and, because free-tier reports
    lack the paid-only data (EPC, last sale, rents, AVM, per-sqf), trigger a
    paid-tier rebuild in the background. Idempotent unless force=True (admin):
    an already-paid report — or one whose paid rebuild is in flight — is left
    alone. Returns None if the report doesn't exist."""
    stored = load_report(report_id)
    if not stored:
        return None
    already_paid = bool(stored.get("paid"))
    needs_rebuild = (stored.get("report") or {}).get("tier") != "paid"
    in_flight = stored.get("status") == "building"
    if already_paid and not force and (not needs_rebuild or in_flight):
        return {"status": "already_unlocked", "report_id": report_id,
                "rebuilding": needs_rebuild and in_flight, "newly_unlocked": False}
    stored["paid"] = True
    if needs_rebuild:
        stored["status"] = "building"
        stored["build_started_at"] = _now_iso()
        save_report(report_id, stored)
        _start_rebuild(report_id, stored, tier="paid")
    else:
        save_report(report_id, stored)
    log_event(report_id, "report_unlocked", dict(extra or {}, source=source))
    return {"status": "unlocked", "report_id": report_id,
            "rebuilding": needs_rebuild, "newly_unlocked": not already_paid}


def _fulfil_stripe_payment(report_id, session, source):
    """Unlock after a VERIFIED Stripe payment (verified session retrieval or
    signature-checked webhook — never from client-supplied data alone) and
    record the sale durably. Only the first caller notifies; repeats no-op."""
    result = _unlock_report(report_id, source=source, extra={
        "session_id": session.get("id", ""),
        "amount_total": session.get("amount_total"),
    })
    if not result or not result.get("newly_unlocked"):
        return result
    stored = load_report(report_id) or {}
    report = stored.get("report") or {}
    amount = (session.get("amount_total") or 0) / 100
    currency = (session.get("currency") or "gbp").upper()
    buyer_email = ((session.get("customer_details") or {}).get("email")
                   or stored.get("email", ""))
    post_to_sheets({
        "type": "payment",
        "timestamp": _now_iso(),
        "uuid": report_id,
        "amount": amount,
        "currency": currency,
        "email": buyer_email,
        "postcode": report.get("postcode"),
        "session_id": session.get("id", ""),
        "report_url": f"{BASE_URL.rstrip('/')}/r/{report_id}",
    })
    try:
        requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={
                "from": f"HouseOffer <{EMAIL_ADDRESS}>",
                "to": [EMAIL_ADDRESS],
                "subject": f"💰 Payment received: £{amount:,.2f} — {report.get('postcode') or report_id}",
                "text": (f"Report unlocked by Stripe payment.\n\n"
                         f"Amount: £{amount:,.2f} {currency}\nBuyer: {buyer_email}\n"
                         f"Postcode: {report.get('postcode')}\n"
                         f"Report: {BASE_URL.rstrip('/')}/r/{report_id}\n"
                         f"Session: {session.get('id', '')}"),
            },
            timeout=10,
        )
    except Exception as e:
        print(f"Payment notify error: {e}")
    return result


def _stripe_key_mode():
    """'test' or 'live' — cached price ids are mode-specific, so a key swap
    (test dry-run -> live) must invalidate the cache rather than send a
    test-mode price to the live API."""
    return "test" if STRIPE_SECRET_KEY.startswith("sk_test") else "live"


def _resolve_report_price():
    """Stripe Price id for the £29 report checkout. Resolution order: the
    STRIPE_REPORT_PRICE_ID pin, the persisted cache, the product's
    default_price in Stripe (dashboard-managed, so a price change there flows
    through), else create a £STRIPE_REPORT_PRICE_PENCE price on the product
    once and set it as the default. Returns "" when nothing resolves — the
    caller falls back to inline price_data."""
    if STRIPE_REPORT_PRICE_ID:
        return STRIPE_REPORT_PRICE_ID
    if not (STRIPE_SECRET_KEY and STRIPE_REPORT_PRODUCT_ID):
        return ""
    with _stripe_price_lock:
        try:
            if os.path.exists(STRIPE_PRICE_CACHE_PATH):
                with open(STRIPE_PRICE_CACHE_PATH) as f:
                    cached = json.load(f) or {}
                if (cached.get("product") == STRIPE_REPORT_PRODUCT_ID
                        and cached.get("mode") == _stripe_key_mode()
                        and cached.get("price")):
                    return cached["price"]
        except Exception as e:
            print(f"Stripe price cache read error: {e}")

        price_id = ""
        try:
            r = requests.get(
                f"https://api.stripe.com/v1/products/{STRIPE_REPORT_PRODUCT_ID}",
                auth=(STRIPE_SECRET_KEY, ""), timeout=15)
            if r.status_code == 200:
                default_price = r.json().get("default_price")
                if isinstance(default_price, str):
                    price_id = default_price
                else:
                    r2 = requests.post(
                        "https://api.stripe.com/v1/prices",
                        data={"currency": "gbp",
                              "unit_amount": str(STRIPE_REPORT_PRICE_PENCE),
                              "product": STRIPE_REPORT_PRODUCT_ID},
                        auth=(STRIPE_SECRET_KEY, ""), timeout=15)
                    if r2.status_code == 200:
                        price_id = r2.json().get("id", "")
                        requests.post(
                            f"https://api.stripe.com/v1/products/{STRIPE_REPORT_PRODUCT_ID}",
                            data={"default_price": price_id},
                            auth=(STRIPE_SECRET_KEY, ""), timeout=15)
                    else:
                        print(f"Stripe price create error: {r2.status_code} — {r2.text[:200]}")
            else:
                print(f"Stripe product lookup error: {r.status_code} — {r.text[:200]}")
        except Exception as e:
            print(f"Stripe price resolve error: {e}")

        if price_id:
            try:
                os.makedirs(os.path.dirname(STRIPE_PRICE_CACHE_PATH), exist_ok=True)
                with open(STRIPE_PRICE_CACHE_PATH, "w") as f:
                    json.dump({"product": STRIPE_REPORT_PRODUCT_ID,
                               "price": price_id,
                               "mode": _stripe_key_mode()}, f)
            except Exception as e:
                print(f"Stripe price cache write error: {e}")
        return price_id


@app.route("/r/<report_id>/checkout")
def start_checkout(report_id):
    """Create a Stripe Checkout session for the £29 report unlock and send the
    buyer to Stripe's hosted payment page. Falls back to the pricing page when
    Stripe isn't configured or errors, so the CTA is never a dead link."""
    if not re.fullmatch(r"[a-f0-9]{8,32}", report_id):
        return jsonify({"error": "report not found"}), 404
    stored = load_report(report_id)
    if not stored:
        return jsonify({"error": "report not found"}), 404
    if stored.get("paid"):
        return redirect(f"/r/{report_id}")

    report = stored.get("report") or {}
    src = re.sub(r"[^a-z0-9_]", "", request.args.get("src", ""))[:40]
    log_event(report_id, "checkout_started", {
        "src": src, "postcode": report.get("postcode"),
        "verdict": report.get("verdict"),
    })
    if not STRIPE_SECRET_KEY:
        return redirect("https://houseoffer.uk/#pricing")

    address = report.get("address") or report.get("postcode") or ""
    params = {
        "mode": "payment",
        "client_reference_id": report_id,
        "metadata[report_id]": report_id,
        "payment_intent_data[metadata][report_id]": report_id,
        "line_items[0][quantity]": "1",
        "success_url": f"{BASE_URL.rstrip('/')}/r/{report_id}/checkout/success?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{BASE_URL.rstrip('/')}/r/{report_id}",
    }
    price_id = _resolve_report_price()
    if price_id:
        params["line_items[0][price]"] = price_id
    else:
        # Inline pricing fallback — a pricing hiccup must never block a buyer.
        params["line_items[0][price_data][currency]"] = "gbp"
        params["line_items[0][price_data][unit_amount]"] = str(STRIPE_REPORT_PRICE_PENCE)
        if STRIPE_REPORT_PRODUCT_ID:
            params["line_items[0][price_data][product]"] = STRIPE_REPORT_PRODUCT_ID
        else:
            params["line_items[0][price_data][product_data][name]"] = "HouseOffer Offer Report"
            if address:
                params["line_items[0][price_data][product_data][description]"] = \
                    f"Full negotiation report for {address}"[:250]
    if stored.get("email"):
        params["customer_email"] = stored["email"]
    try:
        r = requests.post("https://api.stripe.com/v1/checkout/sessions",
                          data=params, auth=(STRIPE_SECRET_KEY, ""), timeout=15)
        if r.status_code == 200 and r.json().get("url"):
            return redirect(r.json()["url"], code=303)
        print(f"Stripe checkout error ({report_id}): {r.status_code} — {r.text[:300]}")
    except Exception as e:
        print(f"Stripe checkout exception ({report_id}): {e}")
    return redirect("https://houseoffer.uk/#pricing")


@app.route("/r/<report_id>/checkout/success")
def checkout_success(report_id):
    """Buyer lands here after paying. The session is re-fetched from Stripe
    with the secret key (the query string alone proves nothing) and, if paid,
    the report unlocks immediately — no waiting on webhook delivery."""
    if not re.fullmatch(r"[a-f0-9]{8,32}", report_id):
        return jsonify({"error": "report not found"}), 404
    session_id = request.args.get("session_id", "")
    if STRIPE_SECRET_KEY and re.fullmatch(r"cs_[A-Za-z0-9_]+", session_id):
        try:
            r = requests.get(f"https://api.stripe.com/v1/checkout/sessions/{session_id}",
                             auth=(STRIPE_SECRET_KEY, ""), timeout=15)
            session = r.json() if r.status_code == 200 else {}
            if (session.get("payment_status") == "paid"
                    and (session.get("metadata") or {}).get("report_id") == report_id):
                _fulfil_stripe_payment(report_id, session, source="stripe_success")
        except Exception as e:
            print(f"Stripe success verify error ({report_id}): {e}")
    return redirect(f"/r/{report_id}")


def _verify_stripe_signature(payload, sig_header, secret, tolerance=300):
    """Manual Stripe-Signature check (HMAC-SHA256 over 't.payload'), per
    https://docs.stripe.com/webhooks — keeps us SDK-free like the rest of
    the codebase. Rejects stale timestamps to block replay."""
    try:
        pairs = [p.split("=", 1) for p in sig_header.split(",") if "=" in p]
        timestamp = next(v for k, v in pairs if k.strip() == "t")
        candidates = [v.strip() for k, v in pairs if k.strip() == "v1"]
        if not candidates or abs(time.time() - int(timestamp)) > tolerance:
            return False
        signed = f"{timestamp}.".encode() + payload
        expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        return any(hmac.compare_digest(expected, c) for c in candidates)
    except Exception:
        return False


@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    """Stripe webhook endpoint (configure checkout.session.completed and
    checkout.session.async_payment_succeeded in the dashboard). The durable
    fulfilment path — covers buyers who never return from Stripe's page."""
    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")
    if not STRIPE_WEBHOOK_SECRET or not _verify_stripe_signature(
            payload, sig_header, STRIPE_WEBHOOK_SECRET):
        return jsonify({"error": "invalid signature"}), 400
    try:
        event = json.loads(payload)
    except ValueError:
        return jsonify({"error": "invalid payload"}), 400
    if event.get("type") in ("checkout.session.completed",
                             "checkout.session.async_payment_succeeded"):
        session = (event.get("data") or {}).get("object") or {}
        rid = ((session.get("metadata") or {}).get("report_id")
               or session.get("client_reference_id") or "")
        if session.get("payment_status") == "paid" and re.fullmatch(r"[a-f0-9]{8,32}", rid):
            _fulfil_stripe_payment(rid, session, source="stripe_webhook")
    return jsonify({"received": True})


@app.route("/admin/unlock/<report_id>")
def admin_unlock(report_id):
    """Set paid=True for a given report UUID (manual unlock / support tool)."""
    auth = request.args.get("key", "")
    if auth != os.environ.get("ADMIN_KEY", "set-an-admin-key"):
        return jsonify({"error": "unauthorized"}), 401
    if not re.fullmatch(r"[a-f0-9]{8,32}", report_id):
        return jsonify({"error": "invalid report_id"}), 400
    result = _unlock_report(report_id, source="admin", force=True)
    if result is None:
        return jsonify({"error": "report not found"}), 404
    return jsonify({"status": result["status"], "report_id": report_id,
                    "rebuilding": result["rebuilding"]})

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

@app.route("/white-paper")
@app.route("/white-paper/")
def white_paper():
    """Public methodology white paper. Static page; safe to link from the
    marketing site (e.g. houseoffer.uk/white-paper -> this route)."""
    return render_template("white_paper.html")

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
    # P1 changed _psqf_points to match by CANONICAL type — pass that, not the old
    # key-list (which never matched, so every call hit the no-match debug path).
    canonical = _canonical_sold_type(property_type)
    formatted = format_postcode(postcode)

    raw_full = fetch_sold_psqf(formatted)
    used = raw_full if raw_full else fetch_sold_psqf(district_postcode(postcode))
    points = used.get("data", {}).get("raw_data", []) if used else []
    matched = _psqf_points(used, canonical)

    benchmarks = get_psqm_benchmarks(postcode, property_type, floor_area_sqm)

    matched_raw = [p for p in points if _canonical_sold_type(p.get("type")) == canonical]
    return jsonify({
        "postcode_tried": formatted,
        "floor_area_sqm": floor_area_sqm,
        "canonical_type_we_filter_for": canonical,
        "total_points_returned": len(points),
        # None-safe sort: £/sqf records can have type=null.
        "all_types_present": sorted({(p.get("type") or "") for p in points}) if points else [],
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


# ── Valuation-accuracy batch test ──────────────────────────────────────────────
# Scrape N live Rightmove sale listings, run a full paid valuation on each, and
# report our valuation vs the asking price so outliers can be reviewed and the
# methodology tuned. Heavy (one paid build per URL → many API credits), so it
# runs as a background job and the results are polled by job id.

_CURATED_VALUATION_BATCH = [
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

def _harvest_random_rightmove(n, id_min, id_max, attempts_cap):
    """Sample random Rightmove property IDs and keep the ones that are live SALE
    listings (have a postcode and a sale asking price). Over-samples because many
    IDs are dead, rentals or SSTC. Best-effort; returns whatever it found."""
    import random
    found, seen, attempts = [], set(), 0
    while len(found) < n and attempts < attempts_cap:
        attempts += 1
        pid = random.randint(id_min, id_max)
        if pid in seen:
            continue
        seen.add(pid)
        url = f"https://www.rightmove.co.uk/properties/{pid}"
        try:
            s = scrape_property_url(url)
        except Exception:
            continue
        # Cycle 1, item 1: GB-residential validity filter. Keep only live SALE
        # listings whose postcode is a genuine GB postcode — drops the non-GB /
        # placeholder junk (14 of 40 last run) that isn't a real valuation case.
        if (s.get("postcode") and s.get("asking_price")
                and is_valid_gb_postcode(s.get("postcode"))):
            found.append({"url": url, "label": f"random {s.get('postcode')}"})
    return found

def _valuation_test_row(url, label=None):
    """Full paid valuation for one URL, reduced to the fields needed to review
    valuation-vs-asking accuracy. Never raises — errors are captured in the row."""
    row = {
        "url": url, "label": label, "postcode": None, "property_type": None,
        "property_type_source": None, "bedrooms": None, "bedrooms_source": None,
        "asking_price": None, "valuation_midpoint": None, "valuation_low": None,
        "valuation_high": None, "gap_vs_asking_pct": None, "verdict": None,
        "comparable_confidence": None, "comparables_count": None,
        "confidence_score": None, "comparable_tier": None, "sale_type": None,
        "confidence_caveat": None, "asking_anomaly": None,
        "size_matched_count": None, "floor_area_sqm": None,
        "floor_area_source": None, "floor_area_confidence": None,
        "methods_available": None, "error": None,
    }
    try:
        pc, ap, beds, ptype, addr, extra = merge_scraped_listing(url, "", 0, None, None, "")
        if not pc:
            row["error"] = "no postcode (blocked / removed / not a sale listing)"
            return row
        report = build_report_data(
            property_url=url, asking_price=ap, bedrooms=beds, property_type=ptype,
            postcode=pc, floor_area_sqm=None, address=addr, tier="paid", **extra)
        mid = report.get("weighted_midpoint")
        ask = report.get("asking_price")
        row.update({
            "postcode": report.get("postcode"),
            "property_type": report.get("property_type"),
            "property_type_source": report.get("property_type_source"),
            "bedrooms": report.get("bedrooms"),
            "bathrooms": report.get("bathrooms"),
            "bedrooms_source": report.get("bedrooms_source"),
            "asking_price": ask,
            "valuation_midpoint": mid,
            "valuation_low": report.get("weighted_low"),
            "valuation_high": report.get("weighted_high"),
            "verdict": report.get("verdict"),
            "comparable_confidence": report.get("comparable_confidence"),
            "confidence_score": report.get("confidence_score"),
            "comparable_tier": report.get("comparable_tier"),
            "sale_type": report.get("sale_type"),
            "confidence_caveat": report.get("confidence_caveat"),
            "asking_anomaly": report.get("asking_anomaly"),
            "comparable_source": report.get("comparable_source"),
            "comparable_outlier_excluded": report.get("comparable_outlier_excluded"),
            "matched_sold_value": report.get("matched_sold_value"),
            "matched_sold_count": report.get("matched_sold_count"),
            "matched_sold_confidence": report.get("matched_sold_confidence"),
            "matched_sold_divergence_pct": report.get("matched_sold_divergence_pct"),
            "bedroom_local_avg_asking": report.get("bedroom_local_avg_asking"),
            "bedroom_implied_value": report.get("bedroom_implied_value"),
            "comparable_radius_miles": report.get("comparable_radius_miles"),
            "nearby_feed_count": report.get("nearby_feed_count"),
            "nearby_match_count": report.get("nearby_match_count"),
            "comparables_count": report.get("comparables_count"),
            "size_matched_count": report.get("comparable_count_size_matched"),
            "floor_area_sqm": report.get("floor_area_sqm"),
            "floor_area_source": report.get("floor_area_source"),
            "floor_area_confidence": report.get("floor_area_confidence"),
            "methods_available": sum(1 for m in (report.get("football_field") or [])
                                     if m.get("available")),
        })
        # Positive gap = our valuation ABOVE asking; negative = below (listing may
        # be priced above what our methodology supports).
        if ask and mid:
            row["gap_vs_asking_pct"] = round((mid - ask) / ask * 100, 1)
    except Exception as e:
        row["error"] = str(e)
    return row

def _assemble_valuation_batch(n, mode, urls_arg, id_min, id_max):
    if urls_arg:
        batch = [{"url": u.strip(), "label": "supplied"}
                 for u in re.split(r"[,\s]+", urls_arg) if u.strip()][:n]
    elif mode == "curated":
        batch = list(_CURATED_VALUATION_BATCH[:n])
    else:
        batch = _harvest_random_rightmove(n, id_min, id_max, attempts_cap=n * 6)
        if len(batch) < n:  # top up from curated so a thin harvest still returns data
            have = {b["url"] for b in batch}
            batch += [b for b in _CURATED_VALUATION_BATCH if b["url"] not in have][:n - len(batch)]
    return batch[:n]

def _accuracy_block(rows):
    """Accuracy summary for a set of valued rows: within-10%, within-20%, median
    |gap|. Used overall and per confidence tier so we can see whether low-confidence
    numbers really are worse (Cycle 1 report format)."""
    gaps = sorted(abs(r["gap_vs_asking_pct"]) for r in rows
                  if r.get("gap_vs_asking_pct") is not None)
    if not gaps:
        return {"n": len(rows), "within_10pct": 0, "within_20pct": 0, "median_abs_gap_pct": None}
    return {
        "n": len(rows),
        "within_10pct": sum(1 for g in gaps if g <= 10),
        "within_20pct": sum(1 for g in gaps if g <= 20),
        "median_abs_gap_pct": gaps[len(gaps) // 2],
    }

def _summarise_valuation_results(results, n, outlier_threshold):
    # Category (a): no usable postcode at all → we couldn't generate a report.
    no_postcode = [r for r in results
                   if r.get("error") and "postcode" in str(r["error"]).lower()]
    ok = [r for r in results if not r["error"] and r["valuation_midpoint"]]
    gaps = sorted(abs(r["gap_vs_asking_pct"]) for r in ok if r["gap_vs_asking_pct"] is not None)
    conf_hist = {}
    for r in ok:
        c = r["comparable_confidence"] or "n/a"
        conf_hist[c] = conf_hist.get(c, 0) + 1
    # Accuracy broken out by published confidence score (high/medium/low).
    by_score = {}
    for score in ("high", "medium", "low"):
        tier_rows = [r for r in ok if r.get("confidence_score") == score]
        if tier_rows:
            by_score[score] = _accuracy_block(tier_rows)
    score_hist = {}
    for r in ok:
        s = r.get("confidence_score") or "n/a"
        score_hist[s] = score_hist.get(s, 0) + 1
    tier_hist = {}
    for r in ok:
        t = r.get("comparable_tier") or "n/a"
        tier_hist[t] = tier_hist.get(t, 0) + 1
    sale_type_hist = {}
    for r in ok:
        st = r.get("sale_type")
        if st:
            sale_type_hist[st] = sale_type_hist.get(st, 0) + 1
    asking_anomaly_count = sum(1 for r in ok if r.get("asking_anomaly"))
    outliers = sorted(
        [r for r in ok if r["gap_vs_asking_pct"] is not None
         and abs(r["gap_vs_asking_pct"]) >= outlier_threshold],
        key=lambda r: -abs(r["gap_vs_asking_pct"]))
    return {
        "requested": n, "valued_ok": len(ok), "errors": len(results) - len(ok),
        # Category (a) = no resolvable postcode; categories (b)+(c) = valued_ok.
        "no_usable_postcode": len(no_postcode),
        "valued_with_confidence": len(ok),
        "median_abs_gap_pct": gaps[len(gaps) // 2] if gaps else None,
        "within_10pct": sum(1 for g in gaps if g <= 10),
        "within_20pct": sum(1 for g in gaps if g <= 20),
        "accuracy_by_confidence": by_score,
        "confidence_score_histogram": score_hist,
        "comparable_tier_histogram": tier_hist,
        "sale_type_histogram": sale_type_hist,
        "asking_anomaly_count": asking_anomaly_count,
        "confidence_histogram": conf_hist,
        "outlier_threshold_pct": outlier_threshold,
        "outlier_count": len(outliers), "outliers": outliers,
    }

@app.route("/batch-valuation-test")
def batch_valuation_test():
    """Value N live Rightmove listings and report valuation-vs-asking with outliers
    flagged. Admin-key protected. Args:
      &sync=1          run inline and return results in ONE response (no polling;
                       survives instance spin-down). n is capped to 8 in sync mode.
      &n=12            how many listings (max 40; each is a paid build = credits)
      &mode=random     random national sample (default); &mode=curated for the
                       fixed diverse batch
      &urls=a,b,c      value an explicit list instead (comma/space separated)
      &concurrency=N   parallel builds (1-6)
      &id_min=&id_max= tune the random Rightmove ID range
      &outlier=15      |gap%| at/above which a row is flagged an outlier
    Default (no sync): kicks a background job, returns job_id; poll
    /batch-valuation-test/<job_id>?key=... — but that can be lost if the instance
    spins down, so &sync=1 is preferred for small batches."""
    if request.args.get("key") != os.environ.get("ADMIN_KEY", "set-an-admin-key"):
        return jsonify({"error": "Unauthorised"}), 403
    sync = request.args.get("sync") in ("1", "true", "yes")
    # Async runs can go up to 100 (for a durable random sample streamed to Sheets);
    # sync stays small to fit the request timeout.
    n = max(1, min(int(request.args.get("n", "12")), 100))
    if sync:
        n = min(n, 8)  # keep within the request timeout — no background thread
    # &sheets=1 streams each completed row to the Google Sheets webhook so a large
    # background run survives instance spin-down (results land in the sheet live).
    stream_sheets = request.args.get("sheets") in ("1", "true", "yes")
    concurrency = max(1, min(int(request.args.get("concurrency", "6" if sync else "4")), 6))
    mode = request.args.get("mode", "random")
    urls_arg = request.args.get("urls")
    id_min = int(request.args.get("id_min", "150000000"))
    id_max = int(request.args.get("id_max", "176000000"))
    outlier_threshold = float(request.args.get("outlier", "15"))

    # ── Synchronous mode: do the work during the request, return results directly.
    if sync:
        batch = _assemble_valuation_batch(n, mode, urls_arg, id_min, id_max)
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            results = list(pool.map(lambda it: _valuation_test_row(it["url"], it.get("label")), batch))
        return jsonify({
            "kind": "valuation-test", "status": "ready", "mode": mode,
            "run_at": _now_iso(), "completed": len(results), "results": results,
            "summary": _summarise_valuation_results(results, n, outlier_threshold),
        })

    job_id = uuid.uuid4().hex[:12]
    save_report(job_id, {
        "kind": "valuation-test", "status": "building", "run_at": _now_iso(),
        "n": n, "mode": mode, "completed": 0, "results": [],
    })

    def work():
        try:
            batch = _assemble_valuation_batch(n, mode, urls_arg, id_min, id_max)
            results = []
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                for r in pool.map(lambda it: _valuation_test_row(it["url"], it.get("label")), batch):
                    results.append(r)
                    if stream_sheets:
                        # Durable per-row stream — survives instance spin-down.
                        post_to_sheets({
                            "type": "valuation_test",
                            "run_id": job_id,
                            "run_at": _now_iso(),
                            "url": r.get("url"), "postcode": r.get("postcode"),
                            "property_type": r.get("property_type"),
                            "bedrooms": r.get("bedrooms"),
                            "asking_price": r.get("asking_price"),
                            "valuation_midpoint": r.get("valuation_midpoint"),
                            "gap_vs_asking_pct": r.get("gap_vs_asking_pct"),
                            "verdict": r.get("verdict"),
                            "comparable_confidence": r.get("comparable_confidence"),
                            "comparable_source": r.get("comparable_source"),
                            "matched_sold_value": r.get("matched_sold_value"),
                            "bedroom_implied_value": r.get("bedroom_implied_value"),
                            "floor_area_sqm": r.get("floor_area_sqm"),
                            "error": r.get("error"),
                        })
                    st = load_report(job_id) or {}
                    st["results"] = results
                    st["completed"] = len(results)
                    save_report(job_id, st)
            st = load_report(job_id) or {}
            st.update({
                "status": "ready", "completed": len(results),
                "summary": _summarise_valuation_results(results, n, outlier_threshold),
            })
            save_report(job_id, st)
        except Exception as e:
            st = load_report(job_id) or {}
            st.update({"status": "failed", "error": str(e)})
            save_report(job_id, st)

    threading.Thread(target=work, daemon=True).start()
    return jsonify({
        "job_id": job_id,
        "status": "building",
        "n": n, "mode": mode,
        "poll_url": f"/batch-valuation-test/{job_id}?key=YOUR_ADMIN_KEY",
        "note": "Background job (can be lost if the instance spins down). For a "
                "reliable one-shot, add &sync=1 (n capped to 8).",
    })

@app.route("/batch-valuation-test/<job_id>")
def batch_valuation_test_status(job_id):
    """Poll the status/results of a batch valuation test job. Admin-key protected."""
    if request.args.get("key") != os.environ.get("ADMIN_KEY", "set-an-admin-key"):
        return jsonify({"error": "Unauthorised"}), 403
    st = load_report(job_id)
    if not st or st.get("kind") != "valuation-test":
        return jsonify({"error": "unknown job_id"}), 404
    return jsonify(st)


@app.route("/debug-report")
def debug_report():
    """Raw report JSON. Admin-key protected (paid tier burns API credits).
    Usage: /debug-report?postcode=..&price=..&type=..&tier=free|paid&key=ADMIN
    Test-only knobs (exercise FIX 2 size-match / FIX 3 floor-area sanity without a
    Rightmove scrape):
      &sqm=NN     inject a subject floor area (treated as a scraped value)
      &beds=N     inject bedrooms (default 3; pass 'unknown' to test FIX 1 path)"""
    auth = request.args.get("key", "")
    if auth != os.environ.get("ADMIN_KEY", "set-an-admin-key"):
        return jsonify({"error": "Unauthorised"}), 403
    postcode = request.args.get("postcode", "WD4 9EW")
    asking_price = int(request.args.get("price", "675000"))
    property_type = request.args.get("type", "semi-detached")
    address = request.args.get("address", "")
    tier = request.args.get("tier", "paid")
    sqm_arg = request.args.get("sqm")
    floor_area_sqm = float(sqm_arg) if sqm_arg else None
    beds_arg = request.args.get("beds", "3")
    bedrooms = None if beds_arg == "unknown" else beds_arg
    report = build_report_data("", asking_price, bedrooms, property_type, postcode,
                               floor_area_sqm=floor_area_sqm,
                               # mark an injected floor area as scraped so FIX 3 runs
                               floor_area_source="scraped" if floor_area_sqm else "unknown",
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
        property_url, "", 0, None, None, ""
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
    bedrooms = data.get("bedrooms") or None
    property_type = data.get("property_type") or None
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
    bedrooms = data.get("bedrooms") or None
    property_type = data.get("property_type") or None
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


def _run_free_build(report_id, inputs):
    """Build the free report AFTER the user confirmed the property (G1). Every
    PropertyData credit is spent inside this function — nothing upstream of the
    confirmation costs anything. `inputs` is the confirm_inputs dict stored on
    the report at resolution time (possibly amended by user corrections)."""
    _rid, _url = report_id, inputs.get("property_url")
    _em, _be = inputs.get("email"), inputs.get("buyer_estimate")
    _ru = inputs.get("report_url")
    try:
        report = build_report_data(
            property_url=_url,
            asking_price=inputs["asking_price"],
            bedrooms=inputs.get("bedrooms"),
            property_type=inputs.get("property_type"),
            postcode=inputs["postcode"],
            floor_area_sqm=inputs.get("floor_area_sqm"),
            address=inputs.get("address"),
            tier="free",
            **(inputs.get("extra") or {}),
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

        # Seed the buyer's own estimate (given on the request form, before
        # they saw any data) as the first crowd vote, so friends arriving
        # via the share link always see the number that started it.
        try:
            seed_est = int(str(_be or "").replace(",", "").replace("£", "").replace(" ", ""))
            if 1_000 <= seed_est <= 100_000_000:
                with _votes_lock:
                    votes = _load_votes(_rid)
                    if not any(v.get("token") == f"owner-{_rid}" for v in votes):
                        votes.append({"token": f"owner-{_rid}", "name": "The buyer",
                                      "estimate": seed_est, "source": "owner_seed",
                                      "created_at": _now_iso()})
                        _save_votes(_rid, votes)
                        post_to_sheets({
                            "type": "vote", "timestamp": _now_iso(),
                            "uuid": _rid, "slug": "", "source": "owner_seed",
                            "voter_name": "The buyer", "estimate": seed_est,
                            "updated": False,
                            "asking_price": report.get("asking_price"),
                            "our_valuation": report.get("weighted_midpoint"),
                            "verdict": report.get("verdict"),
                            "postcode": report.get("postcode"),
                            "property_url": _url, "report_url": _ru,
                        })
        except (ValueError, TypeError):
            pass

        post_to_sheets({
            "type": "submission",
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "uuid": _rid, "email": _em,
            "postcode": report["postcode"],
            "property_type": report["property_type"],
            "asking_price": inputs["asking_price"], "verdict": report["verdict"],
            "buyer_estimate": _be or "", "anchor_bias": anchor_bias,
            "property_url": _url, "report_url": _ru,
        })
        log_event(_rid, "submission_created", {
            "email": _em, "postcode": report["postcode"],
            "verdict": report["verdict"], "asking_price": inputs["asking_price"],
            "anchor_bias": anchor_bias,
        })
        try:
            # render_template in a daemon thread needs an explicit app context —
            # without it the report email silently fails (latent in the old
            # in-route closure too, surfaced by the G1 flow test).
            with app.app_context():
                email_html = render_template("report_email.html", report_url=_ru, **report)
            send_report_email(_em, email_html, report["postcode"], report["verdict"], report_url=_ru)
            notify_owner(_em, _url, report["postcode"], report["verdict"], _be, anchor_bias)
        except Exception as e:
            print(f"Email error in background build ({_rid}): {e}")
    except Exception as exc:
        print(f"Background build error ({_rid}): {exc}")
        stored = load_report(_rid) or {}
        stored["status"] = "failed"
        stored["error"] = str(exc)
        save_report(_rid, stored)


@app.route("/r/<report_id>/confirm-build", methods=["POST"])
def confirm_build(report_id):
    """G1: the explicit 'Yes, that's the property' gate. Applies any manual
    corrections, then spawns the credit-spending build. Idempotent — a report
    already past confirmation just redirects to itself."""
    if not re.fullmatch(r"[a-f0-9]{8,32}", report_id):
        return "Report not found", 404
    stored = load_report(report_id)
    if not stored:
        return "Report not found", 404
    if stored.get("status") != "awaiting_confirmation":
        return redirect(f"/r/{report_id}")

    ci = stored.get("confirm_inputs") or {}
    data = request.form if request.form else (request.get_json(silent=True) or {})
    corrected = []
    extra = ci.get("extra") or {}
    new_addr = (data.get("address") or "").strip()
    new_pc = (data.get("postcode") or "").strip().upper()
    new_beds = _coerce_bedrooms(data.get("bedrooms"))
    new_type = normalise_property_type(data.get("property_type") or "")
    if new_addr and new_addr != (ci.get("address") or ""):
        ci["address"] = new_addr
        extra["resolved_address"] = None
        extra["address_resolution"] = "user"
        corrected.append("address")
    if new_pc and re.fullmatch(r"[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}", new_pc):
        if new_pc != (ci.get("postcode") or "").upper():
            ci["postcode"] = new_pc
            extra["resolved_address"] = None
            extra["address_resolution"] = "user"
            corrected.append("postcode")
    if new_beds and new_beds != ci.get("bedrooms"):
        ci["bedrooms"] = new_beds
        extra["bedrooms_source"] = "user"
        corrected.append("bedrooms")
    if new_type and new_type != ci.get("property_type"):
        ci["property_type"] = new_type
        extra["property_type_source"] = "user"
        corrected.append("property_type")
    ci["extra"] = extra

    stored["confirm_inputs"] = ci
    stored["status"] = "building"
    stored["build_started_at"] = _now_iso()
    save_report(report_id, stored)
    log_event(report_id, "address_confirmed",
              {"corrected_fields": corrected, "postcode": ci.get("postcode")})
    threading.Thread(target=_run_free_build, args=(report_id, ci), daemon=True).start()
    return redirect(f"/r/{report_id}")


@app.route("/submit", methods=["POST"])
def submit():
    data = request.get_json(silent=True) or request.form
    to_email       = data.get("email", "")
    property_url   = data.get("property-url", "") or data.get("property_url", "")
    buyer_estimate = normalise_buyer_estimate(data.get("buyer_estimate", ""))
    # These may be pre-filled by the frontend; the scraper will override with
    # live values if it finds better data.
    asking_price   = int(str(data.get("asking_price", 0) or 0).replace(",", "").replace("£", "")) or 0
    bedrooms       = data.get("bedrooms") or None
    property_type  = data.get("property_type") or None
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

    def _resolve():
        # G1 (2026-07-14): this thread does the FREE work only — scrape + address
        # resolution (Rightmove page + EPC, zero PropertyData credits). The paid
        # valuation build runs ONLY after the user explicitly confirms the
        # property on /r/<id>; a wrong silent match would make every downstream
        # number confidently wrong, and an abandoned confirmation costs nothing.
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
            stored = load_report(_rid) or {}
            stored.update({
                "status": "awaiting_confirmation",
                "confirm_inputs": {
                    "property_url": _url, "email": _em, "buyer_estimate": _be,
                    "report_url": _ru, "asking_price": ap, "bedrooms": br,
                    "property_type": pt, "postcode": pc, "address": ad,
                    "floor_area_sqm": _fa, "extra": extra,
                },
            })
            save_report(_rid, stored)
            # Light email so the confirmation link isn't lost if the tab closes.
            # The full report email is sent after the confirmed build completes.
            try:
                confirm_html = (
                    "<p>Nearly there — one click to check we've matched the right "
                    "property, then we'll run your full valuation.</p>"
                    f"<p><a href='{_ru}'>Confirm your property &rarr;</a></p>")
                send_report_email(_em, confirm_html, pc, "unknown", report_url=_ru)
            except Exception as e:
                print(f"Confirm email error ({_rid}): {e}")
        except Exception as exc:
            print(f"Background submit error ({_rid}): {exc}")
            stored = load_report(_rid) or {}
            stored["status"] = "failed"
            stored["error"] = str(exc)
            save_report(_rid, stored)

    threading.Thread(target=_resolve, daemon=True).start()

    # Return "sent" so existing frontend code that checks status === "sent" keeps working.
    # The report is still building; the user is redirected to report_url which shows
    # the generating page until the background thread marks status="ready".
    return jsonify({"status": "sent", "report_id": report_id, "report_url": report_url})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
