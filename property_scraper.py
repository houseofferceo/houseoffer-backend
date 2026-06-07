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
    "bedrooms": 3,
    "property_type": "semi-detached",
    "address": None,
    "source": None,
    "date_first_listed": None,
    "days_on_market": None,
    "price_reduced": False,
    "original_asking_price": None,
    "reduction_date": None,
    "reduction_amount": None,
    "reduction_pct": None,
}

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


def normalise_property_type(raw: str) -> str:
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
    return "semi-detached"


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
        except (TypeError, ValueError):
            pass
    if result["bedrooms"] == DEFAULT_RESULT["bedrooms"]:
        key_features = prop.get("keyFeatures") or []
        if isinstance(key_features, list):
            joined = " ".join(str(f) for f in key_features)
            match = re.search(r"(\d+)\s*bedroom", joined, re.IGNORECASE)
            if match:
                result["bedrooms"] = int(match.group(1))

    ptype = (
        prop.get("propertySubType")
        or prop.get("propertyType")
        or prop.get("propertyTypeFullDescription")
        or ""
    )
    result["property_type"] = normalise_property_type(str(ptype))

    addr = prop.get("address") or {}
    pc = _postcode_from_address(addr if isinstance(addr, dict) else {})
    if pc:
        result["postcode"] = pc
    if isinstance(addr, dict) and addr.get("displayAddress"):
        result["address"] = addr["displayAddress"]


def scrape_rightmove(url: str) -> dict:
    result = _empty_result()
    result["source"] = "rightmove"

    html = _fetch_html(url, referer="https://www.rightmove.co.uk/")
    if not html:
        return result

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
        except (TypeError, ValueError):
            pass

    ptype = _walk_find_first(
        listing,
        {"propertyType", "propertySubType", "propertyTypeFullDescription", "category"},
    )
    if ptype:
        result["property_type"] = normalise_property_type(str(ptype))

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
        return scrape_rightmove(url)
    if portal == "zoopla":
        return scrape_zoopla(url)
    print(f"Unknown property portal for URL: {url[:80]}")
    return _empty_result()
