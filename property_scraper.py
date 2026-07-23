"""
Fetch listing details from Rightmove and Zoopla property URLs.

Rightmove embeds PAGE_MODEL in the HTML (often compact/encoded). Zoopla uses
__NEXT_DATA__ when the request is not blocked. Set SCRAPER_PROXY_URL for a UK
residential proxy if Zoopla returns 403 from your host (e.g. Render).
"""
import json
import os
import re
from datetime import date, datetime
from typing import Any, Optional

import requests

DEFAULT_RESULT = {
    "postcode": None,
    "asking_price": 0,
    # bedrooms / property_type default to None (unknown) rather than a fabricated
    # "3-bed semi-detached". A guessed profile poisons the comparable filter and
    # the AVM; downstream code must treat None as "unknown" and flag low confidence
    # rather than silently re-introducing a default.
    "bedrooms": None,
    "bedrooms_source": "unknown",      # "scraped" | "keyfeatures" | "unknown"
    "bathrooms": None,                 # scraped from the listing; feeds the AVM
    "property_type": None,
    "property_type_source": "unknown", # "scraped" | "unknown"
    "floor_area_source": "unknown",    # "scraped" | "unknown" (validated to "epc"/"unverified" in app.py)
    "address": None,
    "source": None,
    "date_first_listed": None,
    "days_on_market": None,
    "price_reduced": False,
    "original_asking_price": None,
    "reduction_date": None,
    "reduction_amount": None,
    "reduction_pct": None,
    "floor_area_sqm": None,
    "delivery_point_id": None,
    "latitude": None,
    "longitude": None,
    "epc_cert_url": None,
    # Portal's own og:image for the listing — used by the crowd-voting share
    # page and WhatsApp preview cards. Hotlinks the portal CDN; never required.
    "main_photo_url": None,
    "is_new_build": False,
    # Special tenure / sale type detected from listing text — None for standard
    # open-market stock, else "shared_ownership" | "auction" | "retirement".
    # Asking price for these is NOT directly comparable to open-market value, so
    # app.py attaches an explicit caveat (it still returns a valuation).
    "sale_type": None,
    "description_house_number": None,
    # P0 2026-07-23: finer-grained listing attributes (incident 151864718).
    "property_subtype": None,   # e.g. "Detached Bungalow" — LR types can't see this
    "price_qualifier": None,    # e.g. "Guide Price", "Offers Over" — pricing language
    "listing_history": None,    # verbatim, e.g. "Reduced on 22/06/2026"
}

# Special-tenure / sale-type detection. Ordered: a shared-ownership listing that
# also quotes a "guide price" should read as shared_ownership, not auction.
_SALE_TYPE_PATTERNS = [
    ("shared_ownership", re.compile(
        r"\bshared\s+ownership\b|\b\d{1,3}\s*%\s*share\b"
        r"|\bpart[\s\-]?buy\b|\bpart[\s\-]?(?:buy|own)[\s/\-]+part[\s\-]?rent\b"
        r"|\bshared\s+equity\b"
        # Sub-market discount schemes (Cycle 3) — these price below open market the
        # same way shared ownership does (the E9 0CC miss was one of these).
        r"|\bdiscount(?:ed)?\s+market\s+(?:sale|value|home)\b"
        r"|\bfirst\s+homes?\s+scheme\b"
        r"|\b\d{1,3}\s*%\s*of\s*(?:the\s+)?(?:full\s+)?market\s+value\b",
        re.IGNORECASE)),
    ("retirement", re.compile(
        r"\bretirement\b|\bover[\s\-]?(?:55|60)s?\b|\bage[\s\-]?(?:restricted|exclusive)\b"
        r"|\bassisted\s+living\b|\bsheltered\s+(?:housing|accommodation)\b"
        r"|\bmccarthy\s*(?:&|and)?\s*stone\b|\bretirement\s+(?:living|village|apartment)\b",
        re.IGNORECASE)),
    # P0 fix 2026-07-23 (incident 151864718): "guide price" and the bare word
    # "auction" are OUT — guide price is routine agency wording (very common in
    # London/Essex) and flagged a standard Balgores sale as an auction on our
    # first organic user's report. Only explicit method-of-sale markers count,
    # and the structured page-model flag (AUCP) outranks text entirely.
    ("auction", re.compile(
        r"\bfor\s+sale\s+by\s+(?:public\s+|modern\s+|online\s+)?auction\b"
        r"|\bsold?\s+(?:via|by|at)\s+(?:modern\s+|online\s+)?auction\b"
        r"|\bmethod\s+of\s+sale\s*:?\s*auction\b"
        r"|\bauction\s+date\b"
        r"|\b(?:un)?conditional\s+auction\b"
        r"|\bmodern\s+method\s+of\s+auction\b"
        r"|\bauction(?:eer)?s?\s+(?:terms|pack|fees|conditions)\b",
        re.IGNORECASE)),
]

# Structured price qualifiers (prices.displayPriceQualifier) that are NOT
# auction signals — routine pricing language rendered as a soft note only.
_PRICE_QUALIFIERS = (
    "guide price", "offers over", "offers in excess of", "oiro",
    "offers in the region of", "offers invited", "fixed price", "from",
)


def detect_sale_type(*texts):
    """Return a special-tenure/sale-type label from any listing text, or None for
    standard open-market stock. Best-effort keyword match."""
    blob = " ".join(str(t) for t in texts if t)
    if not blob:
        return None
    for label, rx in _SALE_TYPE_PATTERNS:
        if rx.search(blob):
            return label
    return None

# Cycle 2: tightened to property-level new-build phrasing only. The old pattern
# matched bare "brand new" and "new development", which fire on "brand new kitchen",
# "brand new boiler" or "close to a new development" and produced false new-build
# caveats (HU18/LE13/BL9 in the random-40 test). Rightmove's own new-home tag
# (infoReelItems, handled below) remains a second, reliable signal.
_NEW_BUILD_RE = re.compile(
    r"\bnew[\s\-]?build\b"
    r"|\bnewly\s+(?:built|constructed)\b"
    r"|\bnew\s+homes?\b"
    r"|\bshow\s+home\b"
    r"|\boff[\s\-]?plan\b"
    r"|\bepc\s+to\s+follow\b"
    r"|\bepc\s+(?:rating\s+)?to\s+be\s+(?:confirmed|provided|issued)\b",
    re.IGNORECASE,
)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

UK_POSTCODE_RE = re.compile(r"([A-Z]{1,2}\d{1,2}[A-Z]?\s?\d[A-Z]{2})")


def _request_kwargs() -> dict:
    proxy = os.environ.get("SCRAPER_PROXY_URL", "").strip()
    if proxy:
        return {"proxies": {"http": proxy, "https": proxy}}
    return {}


def _fetch_html(url: str, referer: Optional[str] = None) -> Optional[str]:
    headers = {**BROWSER_HEADERS}
    if referer:
        headers["Referer"] = referer
    try:
        resp = requests.get(url, headers=headers, timeout=20, **_request_kwargs())
        # Rightmove may return 404/410 for delisted listings but still embed PAGE_MODEL
        if resp.status_code not in (200, 404, 410) or len(resp.text) < 5000:
            print(f"Scrape HTTP {resp.status_code} (len={len(resp.text)}) for {url[:80]}")
            return None
        return resp.text
    except Exception as exc:
        print(f"Scrape request error: {exc}")
        return None


def _empty_result() -> dict:
    return dict(DEFAULT_RESULT)


def detect_portal(url: str) -> Optional[str]:
    u = (url or "").lower()
    if "rightmove.co.uk" in u:
        return "rightmove"
    if "zoopla.co.uk" in u:
        return "zoopla"
    return None


def normalise_property_type(raw: str) -> Optional[str]:
    """Map a portal property-type string to our canonical type, or None if it
    cannot be recognised. Returns None (not a fabricated 'semi-detached') so the
    caller can flag the type as unknown rather than guessing."""
    t = (raw or "").lower()
    if "semi" in t:
        return "semi-detached"
    if "terraced" in t or "terrace" in t:
        return "terraced"
    if "detached" in t:
        return "detached"
    if "flat" in t or "apartment" in t or "maisonette" in t:
        return "flat"
    if "bungalow" in t:
        return "detached"
    return None


def parse_price(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, dict):
        for key in ("amount", "value", "price", "displayPrice"):
            if key in value and value[key] is not None:
                return parse_price(value[key])
        return 0
    digits = re.sub(r"[^0-9]", "", str(value))
    return int(digits) if digits else 0


def _extract_balanced_json(text: str, start: int = 0) -> Optional[str]:
    i = text.find("{", start)
    if i < 0:
        return None
    depth = 0
    in_str = esc = False
    for j in range(i, len(text)):
        c = text[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[i : j + 1]
    return None


def _find_json_objects(text: str):
    pos = 0
    decoder = json.JSONDecoder()
    while True:
        match = text.find("{", pos)
        if match == -1:
            break
        try:
            result, index = decoder.raw_decode(text[match:])
            yield result
            pos = match + index
        except ValueError:
            pos = match + 1


def _decode_page_model_refs(arr: list) -> dict:
    """Decode Rightmove compact PAGE_MODEL (array with integer references).

    Integers in objects point at array slots. Only slots holding dict/list are
    dereferenced further; scalars (str/int/float/bool/None) are terminal values.
    Without that rule, e.g. bedrooms -> 170 -> 2 wrongly follows index 2 (id).
    """
    cache: dict = {}

    def resolve(val: Any) -> Any:
        if isinstance(val, int) and 0 <= val < len(arr):
            if val in cache:
                return cache[val]
            cache[val] = None
            target = arr[val]
            if isinstance(target, (dict, list)):
                out = resolve(target)
            else:
                out = target
            cache[val] = out
            return out
        if isinstance(val, list):
            return [resolve(x) for x in val]
        if isinstance(val, dict):
            return {k: resolve(v) for k, v in val.items()}
        return val

    if not arr:
        return {}
    root = arr[0] if isinstance(arr, list) else arr
    decoded = resolve(root)
    return decoded if isinstance(decoded, dict) else {}


def _parse_rightmove_page_model(html: str) -> Optional[dict]:
    markers = ("PAGE_MODEL =", "window.PAGE_MODEL =", "window.PAGE_MODEL=")
    for marker in markers:
        idx = html.find(marker)
        if idx < 0:
            continue
        outer_blob = _extract_balanced_json(html, idx + len(marker))
        if not outer_blob:
            continue
        try:
            outer = json.loads(outer_blob)
        except json.JSONDecodeError:
            continue

        # Legacy: plain JSON with propertyData at top level
        if outer.get("propertyData"):
            return outer

        inner_raw = outer.get("data")
        if inner_raw is None:
            continue

        if outer.get("encoding") == "on" and isinstance(inner_raw, str):
            try:
                inner = json.loads(inner_raw)
            except json.JSONDecodeError:
                continue
            return _decode_page_model_refs(inner)

        if isinstance(inner_raw, dict):
            return inner_raw

    # Script-tag fallback (parsel-style)
    for script_match in re.finditer(
        r"<script[^>]*>\s*([^<]*PAGE_MODEL\s*=[^<]*)</script>",
        html,
        re.IGNORECASE | re.DOTALL,
    ):
        script_text = script_match.group(1)
        for obj in _find_json_objects(script_text):
            if obj.get("propertyData") or obj.get("encoding"):
                if obj.get("encoding") == "on" and isinstance(obj.get("data"), str):
                    try:
                        return _decode_page_model_refs(json.loads(obj["data"]))
                    except json.JSONDecodeError:
                        pass
                return obj

    return None


def _postcode_from_address(addr: dict) -> Optional[str]:
    if not addr:
        return None
    outcode = addr.get("outcode") or addr.get("postcodeOutcode") or ""
    incode = addr.get("incode") or addr.get("postcodeIncode") or ""
    if outcode and incode:
        return f"{outcode}{incode}".replace(" ", "").upper()
    for key in ("postcode", "postalCode", "zipcode"):
        if addr.get(key):
            return str(addr[key]).replace(" ", "").upper()
    return None


_MONTH_ABBR = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


def _parse_date_to_date(raw: str) -> Optional[date]:
    raw = raw.strip()
    # ISO / datetime format: "2024-01-15" or "2024-01-15T00:00:00.000Z"
    if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
        try:
            return datetime.fromisoformat(raw[:10]).date()
        except ValueError:
            pass
    # "28/05/2026" dd/mm/yyyy
    slash_m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})$", raw)
    if slash_m:
        try:
            return date(int(slash_m.group(3)), int(slash_m.group(2)), int(slash_m.group(1)))
        except ValueError:
            pass
    # "15 January 2024" or "15 Jan 2024"
    m = re.match(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", raw)
    if m:
        day, month_word, year = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        month_num = _MONTH_ABBR.get(month_word)
        if month_num:
            try:
                return date(year, month_num, day)
            except ValueError:
                pass
    return None


def _apply_rightmove_listing_dates(result: dict, prop: dict, html: str) -> None:
    """Extract first-listed date and price reduction info from Rightmove data."""
    first_listed_raw: Optional[str] = None

    # PAGE_MODEL fields
    listing_update = prop.get("listingUpdate") or {}
    update_date_str = str(listing_update.get("listingUpdateDate") or "")
    update_reason = (listing_update.get("listingUpdateReason") or "").lower()

    if update_reason == "new_listing" and update_date_str:
        first_listed_raw = update_date_str
    elif update_reason in ("price_reduced", "price_changed") and update_date_str:
        result["price_reduced"] = True
        reduction_dt = _parse_date_to_date(update_date_str)
        result["reduction_date"] = reduction_dt.isoformat() if reduction_dt else update_date_str[:10]

    # listingHistory.listingUpdateReason contains human-readable strings like
    # "Added on 28/05/2026" or "Reduced on 15/06/2026" -- parse these directly
    listing_history = prop.get("listingHistory") or {}
    if isinstance(listing_history, dict):
        history_reason = listing_history.get("listingUpdateReason") or ""
        added_m = re.search(r"(?:Added|First\s+listed)[^0-9]*(\d{1,2}/\d{1,2}/\d{4}|\d{1,2}\s+\w+\s+\d{4})", history_reason, re.IGNORECASE)
        reduced_m = re.search(r"Reduced[^0-9]*(\d{1,2}/\d{1,2}/\d{4}|\d{1,2}\s+\w+\s+\d{4})", history_reason, re.IGNORECASE)
        if added_m and not first_listed_raw:
            raw = added_m.group(1)
            # Convert dd/mm/yyyy to parseable format
            slash_m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", raw)
            first_listed_raw = f"{slash_m.group(1).zfill(2)}/{slash_m.group(2).zfill(2)}/{slash_m.group(3)}" if slash_m else raw
            if slash_m:
                try:
                    first_listed_raw = date(int(slash_m.group(3)), int(slash_m.group(2)), int(slash_m.group(1))).isoformat()
                except ValueError:
                    pass
        if reduced_m and not result["price_reduced"]:
            result["price_reduced"] = True
            raw = reduced_m.group(1)
            slash_m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", raw)
            if slash_m:
                try:
                    rd = date(int(slash_m.group(3)), int(slash_m.group(2)), int(slash_m.group(1)))
                    result["reduction_date"] = rd.isoformat()
                except ValueError:
                    pass
            else:
                rd = _parse_date_to_date(raw)
                result["reduction_date"] = rd.isoformat() if rd else raw

    # Explicit first-listed date fields
    for field in ("firstListedDate", "dateAdded", "firstVisibleDate"):
        val = prop.get(field)
        if val and not first_listed_raw:
            first_listed_raw = str(val)

    # Price history for original asking price
    price_history = prop.get("priceHistory") or []
    if isinstance(price_history, list) and len(price_history) >= 2:
        try:
            # History usually newest-first; last entry is original
            original_entry = price_history[-1]
            orig_price = parse_price(
                original_entry.get("price") or original_entry.get("amount") or 0
            )
            if orig_price and orig_price > 10_000:
                result["original_asking_price"] = orig_price
                if orig_price != result.get("asking_price", 0):
                    result["price_reduced"] = True
            # Use the oldest history date as first_listed if we don't have one
            if not first_listed_raw:
                hist_date = original_entry.get("date") or original_entry.get("changeDate") or ""
                if hist_date:
                    first_listed_raw = str(hist_date)
        except Exception:
            pass

    # HTML pattern fallbacks
    if html:
        if not first_listed_raw:
            added_m = re.search(
                r"(?:Added\s+on|First\s+listed[:\s]+)\s*(\d{1,2}\s+\w+\s+\d{4})",
                html, re.IGNORECASE,
            )
            if added_m:
                first_listed_raw = added_m.group(1)

        if not result["price_reduced"]:
            reduced_m = re.search(r"Reduced\s+on\s+(\d{1,2}\s+\w+\s+\d{4})", html, re.IGNORECASE)
            if reduced_m:
                result["price_reduced"] = True
                rd = _parse_date_to_date(reduced_m.group(1))
                result["reduction_date"] = rd.isoformat() if rd else reduced_m.group(1)

        if not result.get("original_asking_price"):
            was_m = re.search(r"[Ww]as\s+£([\d,]+)", html)
            if was_m:
                try:
                    orig = int(was_m.group(1).replace(",", ""))
                    if orig > 10_000:
                        result["original_asking_price"] = orig
                        result["price_reduced"] = True
                except ValueError:
                    pass

    # Parse first_listed and compute DOM
    if first_listed_raw:
        result["date_first_listed"] = first_listed_raw
        dt = _parse_date_to_date(first_listed_raw)
        if dt:
            result["days_on_market"] = (date.today() - dt).days

    # Compute reduction_amount / reduction_pct
    orig = result.get("original_asking_price")
    curr = result.get("asking_price", 0)
    if result["price_reduced"] and orig and curr and orig > curr > 0:
        result["reduction_amount"] = orig - curr
        result["reduction_pct"] = round((orig - curr) / orig * 100, 1)
    elif result["price_reduced"] and orig and curr and orig <= curr:
        # Original was not higher; clear false-positive
        result["original_asking_price"] = None
        result["price_reduced"] = False


def _house_number_from_description(description: str, street_address: str) -> Optional[str]:
    """Return a house number found as '{number} {street-word}' in the listing text."""
    if not description or not street_address:
        return None
    words = [w for w in re.sub(r"[^A-Za-z ]", " ", street_address).split() if len(w) > 2]
    if not words:
        return None
    pattern = rf"\b(\d+[A-Za-z]?)\s+{re.escape(words[0])}\b"
    m = re.search(pattern, description, re.IGNORECASE)
    return m.group(1) if m else None


def _apply_rightmove_property(result: dict, prop: dict) -> None:
    prices = prop.get("prices") or {}
    price = prices.get("primaryPrice") or prices.get("displayPrice") or prop.get("price")
    price_str = str(price or "").lower()
    # Skip rental pcm/pw — HouseOffer compares sale prices only
    if "pcm" not in price_str and "pw" not in price_str and "per week" not in price_str:
        parsed = parse_price(price)
        if parsed >= 10_000:
            result["asking_price"] = parsed

    beds = prop.get("bedrooms") or prop.get("beds")
    if beds is not None:
        try:
            beds_int = int(beds)
            if 0 < beds_int <= 10:
                result["bedrooms"] = beds_int
                result["bedrooms_source"] = "scraped"
        except (TypeError, ValueError):
            pass
    # keyFeatures fallback only when the structured field gave us nothing.
    if result["bedrooms"] is None:
        key_features = prop.get("keyFeatures") or []
        if isinstance(key_features, list):
            joined = " ".join(str(f) for f in key_features)
            match = re.search(r"(\d+)\s*bedroom", joined, re.IGNORECASE)
            if match:
                result["bedrooms"] = int(match.group(1))
                result["bedrooms_source"] = "keyfeatures"

    # Bathrooms sit right beside bedrooms in the listing page model — capture them
    # for the AVM (which otherwise assumes a single bathroom).
    baths = prop.get("bathrooms") or prop.get("baths")
    if baths is not None:
        try:
            baths_int = int(baths)
            if 0 < baths_int <= 10:
                result["bathrooms"] = baths_int
        except (TypeError, ValueError):
            pass

    ptype = (
        prop.get("propertySubType")
        or prop.get("propertyType")
        or prop.get("propertyTypeFullDescription")
        or ""
    )
    normalised_type = normalise_property_type(str(ptype))
    if normalised_type:
        result["property_type"] = normalised_type
        result["property_type_source"] = "scraped"
    else:
        # Hardening (Cycle 1, item 5): the structured type fields gave us nothing
        # recognisable — try the key features (often "Semi-Detached Family Home")
        # before giving up. We deliberately do NOT scan the free description, which
        # is noisy ("detached garage" etc.) and would re-introduce wrong defaults.
        kf = prop.get("keyFeatures")
        if isinstance(kf, list) and kf:
            nt = normalise_property_type(" ".join(str(f) for f in kf))
            if nt:
                result["property_type"] = nt
                result["property_type_source"] = "scraped"

    addr = prop.get("address") or {}
    pc = _postcode_from_address(addr if isinstance(addr, dict) else {})
    if pc:
        result["postcode"] = pc
    if isinstance(addr, dict) and addr.get("displayAddress"):
        result["address"] = addr["displayAddress"]

    # deliveryPointId: 8-digit delivery point key (likely Royal Mail UDPRN) —
    # uniquely identifies the exact property even when displayAddress has no number
    dp_id = prop.get("deliveryPointId") or (addr.get("deliveryPointId") if isinstance(addr, dict) else None)
    if dp_id:
        result["delivery_point_id"] = dp_id

    # Listing pin coordinates
    loc = prop.get("location") or {}
    if isinstance(loc, dict) and loc.get("latitude") and loc.get("longitude"):
        result["latitude"] = loc["latitude"]
        result["longitude"] = loc["longitude"]

    # EPC certificate link if the agent attached the official gov.uk certificate
    for entry in (prop.get("epcGraphs") or []):
        if not isinstance(entry, dict):
            continue
        epc_url = entry.get("url") or ""
        if "epc" in epc_url.lower() or "gov.uk" in epc_url.lower():
            result["epc_cert_url"] = epc_url
            break

    # Floor area from sizings array -- Rightmove format:
    # {"unit":"sqm","minimumSize":92,"maximumSize":92,"displayUnit":"sq. m."}
    sizings = prop.get("sizings") or []
    if isinstance(sizings, list):
        for s in sizings:
            if not isinstance(s, dict):
                continue
            unit = (s.get("unit") or "").lower()
            size = s.get("minimumSize") or s.get("maximumSize")
            if not size:
                continue
            try:
                size = float(size)
                if size <= 10:
                    continue
                if unit in ("sqm", "sq m", "sq. m.", "m2", "m²"):
                    result["floor_area_sqm"] = size
                    break
                elif unit in ("sqft", "sq ft", "sq. ft.", "ft2"):
                    result["floor_area_sqm"] = round(size / 10.764, 1)
                    break
            except (TypeError, ValueError):
                pass

    # Floor area fallback 1: infoReelItems (icon row under price)
    # e.g. {"type": "SIZE", "text": "129 sq. m"} or {"type": "FLOORAREA", ...}
    if not result.get("floor_area_sqm"):
        for item in (prop.get("infoReelItems") or []):
            if not isinstance(item, dict):
                continue
            item_type = (item.get("type") or "").upper()
            if item_type not in ("SIZE", "FLOORAREA", "FLOOR_AREA", "AREA"):
                continue
            raw = str(item.get("text") or item.get("value") or "")
            m = re.search(r"([\d,]+(?:\.\d+)?)\s*(?:m²|sqm|sq\.?\s*m)\b", raw, re.IGNORECASE)
            if m:
                try:
                    area = float(m.group(1).replace(",", ""))
                    if area > 10:
                        result["floor_area_sqm"] = area
                        break
                except (ValueError, TypeError):
                    pass
            m = re.search(r"([\d,]+(?:\.\d+)?)\s*(?:sq\.?\s*ft|sqft)\b", raw, re.IGNORECASE)
            if m:
                try:
                    area = float(m.group(1).replace(",", ""))
                    if area > 100:
                        result["floor_area_sqm"] = round(area / 10.764, 1)
                        break
                except (ValueError, TypeError):
                    pass

    # Floor area fallback 2: keyFeatures bullet list
    # Agents almost always include a "Total floor area: 129 m²" or "Approx 1,450 sq ft" bullet.
    if not result.get("floor_area_sqm"):
        for feat in (prop.get("keyFeatures") or []):
            feat_str = str(feat)
            m = re.search(r"([\d,]+(?:\.\d+)?)\s*(?:m²|sqm|sq\.?\s*m)\b", feat_str, re.IGNORECASE)
            if m:
                try:
                    area = float(m.group(1).replace(",", ""))
                    if area > 10:
                        result["floor_area_sqm"] = area
                        break
                except (ValueError, TypeError):
                    pass
            m = re.search(r"([\d,]+(?:\.\d+)?)\s*(?:sq\.?\s*ft|sqft)\b", feat_str, re.IGNORECASE)
            if m:
                try:
                    area = float(m.group(1).replace(",", ""))
                    if area > 100:
                        result["floor_area_sqm"] = round(area / 10.764, 1)
                        break
                except (ValueError, TypeError):
                    pass

    # Description text: new-build detection + house-number extraction
    description = ""
    text_block = prop.get("text")
    if isinstance(text_block, dict):
        description = str(text_block.get("description") or "")

    # Floor area fallback 3: description text, anchored to floor-area phrases only.
    # Room dimensions like "4.5m × 3.2m" are NOT matched — we only accept a bare
    # sqm/sqft figure that follows an explicit "floor area / living space / extends to" phrase.
    if description and not result.get("floor_area_sqm"):
        _FA_DESC_RE = re.compile(
            r"(?:floor\s+area|internal\s+area|living\s+space|accommodation\s+(?:extends?\s+to|of)|total\s+area)"
            r"[^.]{0,60}?([\d,]+(?:\.\d+)?)\s*(?:m²|sqm|sq\.?\s*m)\b",
            re.IGNORECASE,
        )
        m = _FA_DESC_RE.search(description)
        if m:
            try:
                area = float(m.group(1).replace(",", ""))
                if area > 10:
                    result["floor_area_sqm"] = area
            except (ValueError, TypeError):
                pass
        if not result.get("floor_area_sqm"):
            _FA_SQFT_RE = re.compile(
                r"(?:floor\s+area|internal\s+area|living\s+space|accommodation\s+(?:extends?\s+to|of)|total\s+area)"
                r"[^.]{0,60}?([\d,]+(?:\.\d+)?)\s*(?:sq\.?\s*ft|sqft)\b",
                re.IGNORECASE,
            )
            m = _FA_SQFT_RE.search(description)
            if m:
                try:
                    area = float(m.group(1).replace(",", ""))
                    if area > 100:
                        result["floor_area_sqm"] = round(area / 10.764, 1)
                except (ValueError, TypeError):
                    pass

    # Tag floor-area provenance once all listing-side extraction is done. app.py
    # validates this against street EPC areas and may downgrade it to "unverified".
    if result.get("floor_area_sqm"):
        result["floor_area_source"] = "scraped"

    if description and _NEW_BUILD_RE.search(description):
        result["is_new_build"] = True

    # Also flag from infoReelItems or tags if they carry new-build markers
    for item in (prop.get("infoReelItems") or []):
        if isinstance(item, dict) and "new" in str(item.get("type") or "").lower():
            result["is_new_build"] = True
            break

    # Property subtype (P0 2026-07-23): Rightmove's propertySubType ("Detached
    # Bungalow", "Retirement Property", "Park Home"…) is finer-grained than the
    # four Land Registry types we benchmark against — persisted so the report
    # can caveat subtype-blind comparables honestly.
    subtype = prop.get("propertySubType")
    if subtype and str(subtype).strip():
        result["property_subtype"] = str(subtype).strip()

    # Structured price qualifier ("Guide Price", "Offers Over", …) — pricing
    # language, NOT a method-of-sale signal. Rendered as a soft note.
    q = ((prop.get("prices") or {}).get("displayPriceQualifier") or "").strip()
    if q and q.lower() != "default":
        result["price_qualifier"] = q

    # Verbatim listing-history line ("Reduced on 22/06/2026" / "Added on …").
    lh_reason = (prop.get("listingHistory") or {}).get("listingUpdateReason")
    if lh_reason:
        result["listing_history"] = str(lh_reason).strip()

    # Special-tenure / sale-type detection (Cycle 1, item 4; hardened
    # 2026-07-23). The structured ad-targeting flags are authoritative when
    # present: AUCP (auction), SO (shared ownership), R (retirement). Free-text
    # matching is the fallback only — and the auction text pattern now requires
    # explicit method-of-sale wording (see _SALE_TYPE_PATTERNS).
    kf_blob = ""
    kf = prop.get("keyFeatures")
    if isinstance(kf, list):
        kf_blob = " ".join(str(f) for f in kf)
    targeting = {}
    for t in ((prop.get("dfpAdInfo") or {}).get("targeting") or []):
        if isinstance(t, dict) and t.get("key"):
            targeting[t["key"]] = [str(v).upper() for v in (t.get("value") or [])]
    if targeting:
        if targeting.get("AUCP") == ["TRUE"]:
            result["sale_type"] = "auction"
        elif targeting.get("SO") == ["TRUE"]:
            result["sale_type"] = "shared_ownership"
        elif targeting.get("R") == ["TRUE"]:
            result["sale_type"] = "retirement"
        else:
            # Structured flags present and all FALSE: never override with the
            # auction text fallback; shared-ownership/retirement text may still
            # catch schemes the flags miss (discount-market etc.).
            st = detect_sale_type(description, kf_blob, str(ptype),
                                  str(prop.get("propertyTypeFullDescription") or ""))
            result["sale_type"] = st if st != "auction" else None
    else:
        result["sale_type"] = detect_sale_type(
            description, kf_blob, str(ptype),
            str(prop.get("propertyTypeFullDescription") or ""), price_str,
        )

    # Extract house number from description when displayAddress omits it
    addr = result.get("address") or ""
    if description and addr and not re.match(r"\s*\d", addr):
        num = _house_number_from_description(description, addr)
        if num:
            result["description_house_number"] = num


def _extract_og_image(html: str) -> Optional[str]:
    """Pull the portal's own og:image URL out of the listing HTML (both
    attribute orders). Used for share-page previews; None when absent."""
    for pattern in (
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
    ):
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            url = m.group(1).strip()
            if url.startswith("http"):
                return url
    return None


def scrape_rightmove(url: str) -> dict:
    result = _empty_result()
    result["source"] = "rightmove"

    html = _fetch_html(url, referer="https://www.rightmove.co.uk/")
    if not html:
        return result
    result["main_photo_url"] = _extract_og_image(html)

    model = _parse_rightmove_page_model(html)
    if model:
        prop = model.get("propertyData") or model
        if isinstance(prop, dict):
            _apply_rightmove_property(result, prop)
            _apply_rightmove_listing_dates(result, prop, html)
    else:
        # HTML-only fallbacks for listing dates
        _apply_rightmove_listing_dates(result, {}, html)

    if not result["postcode"]:
        match = UK_POSTCODE_RE.search(html.upper())
        if match:
            result["postcode"] = match.group(1).replace(" ", "").upper()

    return result


def _walk_find_properties(obj: Any) -> Optional[list]:
    """Find the sold-property list inside the house-prices page state: the first
    list under a 'properties' key whose entries carry an address and location."""
    if isinstance(obj, dict):
        props = obj.get("properties")
        if isinstance(props, list) and props and all(
            isinstance(p, dict) and p.get("address") and isinstance(p.get("location"), dict)
            for p in props
        ):
            return props
        for v in obj.values():
            found = _walk_find_properties(v)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _walk_find_properties(item)
            if found is not None:
                return found
    return None


def fetch_sold_nearby(postcode: str) -> list:
    """Sold-price records for a postcode from Rightmove's house-prices page —
    the same data address-finder browser extensions match against. Unlike
    PropertyData's sold-prices feed, each record carries its own pin
    coordinates, so the listing pin can be matched property-to-property.
    Returns a list of {address, latitude, longitude, property_type, bedrooms,
    price, date}; empty list on any failure (never raises)."""
    pc = (postcode or "").strip().upper().replace(" ", "")
    if len(pc) < 5:
        return []
    slug = f"{pc[:-3]}-{pc[-3:]}".lower()
    html = _fetch_html(
        f"https://www.rightmove.co.uk/house-prices/{slug}.html",
        referer="https://www.rightmove.co.uk/house-prices.html",
    )
    if not html:
        return []

    state = None
    for marker in ("__PRELOADED_STATE__", "PRELOADED_STATE", "PAGE_MODEL"):
        idx = html.find(marker)
        if idx < 0:
            continue
        blob = _extract_balanced_json(html, idx + len(marker))
        if not blob:
            continue
        try:
            state = json.loads(blob)
            break
        except json.JSONDecodeError:
            continue
    if state is None:
        return []

    props = _walk_find_properties(state)
    if not props:
        return []

    records = []
    for p in props:
        loc = p.get("location") or {}
        lat = loc.get("lat") or loc.get("latitude")
        lng = loc.get("lng") or loc.get("longitude")
        try:
            lat = float(lat) if lat is not None else None
            lng = float(lng) if lng is not None else None
        except (TypeError, ValueError):
            lat = lng = None
        price = 0
        sold_date = None
        transactions = p.get("transactions") or []
        if isinstance(transactions, list) and transactions:
            # Transactions are newest-first; take the most recent sale
            tx = transactions[0]
            if isinstance(tx, dict):
                price = parse_price(tx.get("displayPrice") or tx.get("price"))
                dt = _parse_date_to_date(str(tx.get("dateSold") or tx.get("date") or ""))
                sold_date = dt.isoformat() if dt else None
        bedrooms = p.get("bedrooms")
        try:
            bedrooms = int(bedrooms) if bedrooms is not None else None
        except (TypeError, ValueError):
            bedrooms = None
        records.append({
            "address": str(p.get("address") or "").strip(),
            "latitude": lat,
            "longitude": lng,
            "property_type": p.get("propertyType"),
            "bedrooms": bedrooms,
            "price": price,
            "date": sold_date,
        })
    return records


def _deep_get(obj: Any, *paths: tuple) -> Any:
    for path in paths:
        cur = obj
        ok = True
        for key in path:
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                ok = False
                break
        if ok and cur is not None:
            return cur
    return None


def _walk_find_first(obj: Any, key_names: set) -> Any:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in key_names and v is not None:
                return v
            found = _walk_find_first(v, key_names)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _walk_find_first(item, key_names)
            if found is not None:
                return found
    return None


def _parse_json_ld(html: str) -> list:
    items = []
    for match in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.IGNORECASE | re.DOTALL,
    ):
        try:
            data = json.loads(match.group(1).strip())
            if isinstance(data, list):
                items.extend(data)
            else:
                items.append(data)
        except json.JSONDecodeError:
            continue
    return items


def _apply_zoopla_next_data(result: dict, page_props: dict) -> None:
    listing = (
        _deep_get(page_props, ("listingDetails",), ("listing",), ("property",), ("data", "listing"))
        or page_props
    )
    if not isinstance(listing, dict):
        listing = page_props

    price = _walk_find_first(
        listing,
        {"price", "displayPrice", "unformattedPrice", "priceValue", "rentPerMonth"},
    )
    parsed = parse_price(price)
    if parsed:
        result["asking_price"] = parsed

    beds = _walk_find_first(listing, {"bedrooms", "numBedrooms", "beds", "bedroomCount"})
    if beds is not None:
        try:
            result["bedrooms"] = int(beds)
            result["bedrooms_source"] = "scraped"
        except (TypeError, ValueError):
            pass

    baths = _walk_find_first(listing, {"bathrooms", "numBathrooms", "baths", "bathroomCount"})
    if baths is not None:
        try:
            b = int(baths)
            if 0 < b <= 10:
                result["bathrooms"] = b
        except (TypeError, ValueError):
            pass

    ptype = _walk_find_first(
        listing,
        {"propertyType", "propertySubType", "propertyTypeFullDescription", "category"},
    )
    if ptype:
        normalised_type = normalise_property_type(str(ptype))
        if normalised_type:
            result["property_type"] = normalised_type
            result["property_type_source"] = "scraped"

    postcode = _walk_find_first(
        listing,
        {"postcode", "postalCode", "outcode"},
    )
    if postcode and isinstance(postcode, str):
        result["postcode"] = postcode.replace(" ", "").upper()
    else:
        addr = listing.get("address") if isinstance(listing.get("address"), dict) else {}
        pc = _postcode_from_address(addr)
        if pc:
            result["postcode"] = pc
        display = listing.get("displayAddress") or listing.get("address")
        if isinstance(display, str):
            result["address"] = display
            match = UK_POSTCODE_RE.search(display.upper())
            if match and not result["postcode"]:
                result["postcode"] = match.group(1).replace(" ", "").upper()


def _apply_zoopla_json_ld(result: dict, items: list) -> None:
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("@type") not in ("Product", "SingleFamilyResidence", "Apartment", "House", "Residence", None):
            if item.get("@type") and "Offer" not in str(item.get("@type", "")):
                continue
        offers = item.get("offers") or {}
        if isinstance(offers, list) and offers:
            offers = offers[0]
        if not result["asking_price"]:
            result["asking_price"] = parse_price(
                offers.get("price") if isinstance(offers, dict) else item.get("price")
            )
        addr = item.get("address") or {}
        if isinstance(addr, dict):
            pc = _postcode_from_address(addr)
            if pc and not result["postcode"]:
                result["postcode"] = pc
            if addr.get("streetAddress") and not result["address"]:
                result["address"] = addr["streetAddress"]


def scrape_zoopla(url: str) -> dict:
    result = _empty_result()
    result["source"] = "zoopla"

    html = _fetch_html(url, referer="https://www.zoopla.co.uk/")
    if not html:
        return result
    result["main_photo_url"] = _extract_og_image(html)

    next_match = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        html,
        re.IGNORECASE | re.DOTALL,
    )
    if next_match:
        try:
            next_data = json.loads(next_match.group(1))
            page_props = next_data.get("props", {}).get("pageProps", {})
            _apply_zoopla_next_data(result, page_props)
        except json.JSONDecodeError as exc:
            print(f"Zoopla __NEXT_DATA__ parse error: {exc}")

    _apply_zoopla_json_ld(result, _parse_json_ld(html))

    # Listing date fallback from __NEXT_DATA__ (Zoopla field names vary)
    if next_match:
        try:
            next_data = json.loads(next_match.group(1))
            page_props = next_data.get("props", {}).get("pageProps", {})
            listing = (
                _deep_get(page_props, ("listingDetails",), ("listing",), ("property",))
                or page_props
            )
            for field in ("listingDate", "dateAdded", "firstListedDate", "publishedAt"):
                val = _walk_find_first(listing, {field})
                if val and isinstance(val, str):
                    from datetime import date as _date
                    dt = _parse_date_to_date(val)
                    if dt:
                        result["date_first_listed"] = val
                        result["days_on_market"] = (_date.today() - dt).days
                        break
        except Exception:
            pass

    # Meta / visible fallbacks
    if not result["asking_price"]:
        og_price = re.search(
            r'property=["\']og:price:amount["\'][^>]+content=["\'](\d+)',
            html,
            re.IGNORECASE,
        )
        if og_price:
            result["asking_price"] = int(og_price.group(1))

    if not result["postcode"]:
        match = UK_POSTCODE_RE.search(html.upper())
        if match:
            result["postcode"] = match.group(1).replace(" ", "").upper()

    return result


def scrape_property_url(url: str) -> dict:
    """Scrape Rightmove or Zoopla; returns shared listing field dict."""
    portal = detect_portal(url)
    if portal == "rightmove":
        result = scrape_rightmove(url)
    elif portal == "zoopla":
        result = scrape_zoopla(url)
    else:
        print(f"Unknown property portal for URL: {url[:80]}")
        return _empty_result()
    # Safety net: any path that produced a floor area but didn't tag its source
    # (e.g. Zoopla) is "scraped" — app.py revalidates against street EPC data.
    if result.get("floor_area_sqm") and result.get("floor_area_source") == "unknown":
        result["floor_area_source"] = "scraped"
    return result
