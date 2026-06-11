# HouseOffer Fixes Log

Running record of issues, root causes, decisions and the commits that fixed them.
UK English. All prices GBP.

---

## Fixed

### Walk-away price exceeded asking by £100k+ (WR2 5SG test)
- Date: 2026-06-10
- Symptom: Walk-away £528,000 vs asking £420,000 on a fairly-priced property.
- Root cause: Methods 1a/1b use Q3 (75th percentile) of all comparable sold
  prices as the high bound. The radius-based comparable set mixes house sizes,
  so large detached outliers (£621k-£663k) pushed Q3 to ~£600k, dragging the
  weighted high to £528k.
- Rule adopted: walk-away never exceeds asking price + 5%. Opening offer is
  always below asking (asking - £1,000 cap, applies on every verdict).
  After caps, ordering is re-enforced: open < target < walk-away.
- Commit: 0ebce79 (merged to main in b54c903).

### Opening offer above asking price
- Date: 2026-06-10 (earlier in session)
- Rule: open_offer = min(open_offer, asking_price - 1000), universal.
- Commit: b937e65.

### HPI last sale matching neighbour's property
- Date: 2026-06-10 (earlier in session)
- Root cause: street-token matching alone matched any house on the street.
- Fix: tiered matching in find_last_sale - exact postcode filter, then street
  tokens, then house number required when the subject address has one. If no
  house number and multiple candidates, return None and populate
  last_sale_candidates instead of guessing.
- Commit: c7d4820.

### £/sqm column empty in comparables table
- Date: 2026-06-10 (earlier in session)
- Root cause: PropertyData /sold-prices has no floor areas.
- Fix: cross-reference /sold-prices-per-sqf records (which carry address,
  price, sqf) by normalised address key.

---

### HPI-adjusted last sale shows n/a when listing has no house number
- Date: 2026-06-10
- Symptom: WR2 5SG report shows HPI last sale n/a despite the property having
  sold in 2024 (£398,050) and 2025 (£485,000) per Rightmove history.
- Root cause: Rightmove displayAddress is street-only (no house number) for
  this listing. find_last_sale step 3c correctly refuses to guess between
  multiple sold properties at the postcode (intended safe behaviour after the
  neighbour-match fix); the gap was no way to identify the subject property.
- Note: Rightmove's own sold-history XHR is off limits (ToS prohibits
  third-party use) and the data is not in PAGE_MODEL. Heuristic auto-match
  rejected - that caused the neighbour bug.
- Fix: candidates dropdown. When HPI fails and last_sale_candidates exist,
  both report templates show "select your property" listing each sold address
  at the postcode with date and price. Selection hits
  GET /r/<report_id>/select-address?address=..., which validates the choice
  against the stored candidates, rebuilds the report with the chosen address
  (house number now present), saves and redirects back. The chosen address
  also feeds the EPC floor-area lookup, so this can fix price per m2 too.
  Preview routes accept &address= override for admin testing.

### Candidates dropdown missing the subject property (10 Wilmot Drive case)
- Date: 2026-06-10
- Symptom: the property-selection dropdown did not list the subject property.
- Root cause: candidates were built from PropertyData /sold-prices, which is
  radius-based and capped at ~20 recent sales - older sales at the postcode
  get crowded out by newer sales on neighbouring streets. The list was also
  not filtered to the exact postcode, so it showed neighbouring streets.
- Fix: _fetch_land_registry_direct (Land Registry SPARQL, complete history
  for the exact postcode) is now the primary source for the dropdown, with
  PropertyData (exact-postcode filtered) as fallback. find_last_sale also
  merges SPARQL results into its candidate pool before the tiered matching,
  so selecting an address from the dropdown finds sales PropertyData lacks.
  Safety preserved: SPARQL results are exact-postcode by construction and the
  house-number-required matching still applies. Step 3b now treats records of
  the same property from both sources (different address formats) as one
  candidate. Verified with mocked-source unit tests including the
  wrong-house-number case (returns None, never a neighbour).

### HPI last sale: closure note
- Date: 2026-06-10
- Issue 1 closed. Verified on a live property (Wilmot Drive): the dropdown
  now lists exactly the properties with Land Registry records at the
  postcode (8 and 32 Wilmot Drive). The subject (number 10) has no
  registered prior sale, so HPI last sale is a legitimate n/a there - the
  other valuation methods carry the report. Not a data bug; the Land
  Registry cannot return what was never submitted.

### Address resolution from a Rightmove URL (no user input)
- Date: 2026-06-10
- Goal: identify the full address (house number) automatically when Rightmove
  gives only a street-level displayAddress. The full address unlocks HPI last
  sale, EPC floor area and price per m2.
- Options researched and recorded:
  a. deliveryPointId -> Royal Mail PAF lookup (likely UDPRN; 8-digit key in
     PAGE_MODEL). Deterministic if confirmed; ~3p per lookup via an address
     API (e.g. Ideal Postcodes). VERIFICATION PENDING - user to test a few
     known-address listings against free test credits.
  b. EPC register cross-match (free, built - see below).
  c. lat/long reverse geocoding: OS Places is paid (excluded from the OS free
     allowance due to PAF licensing); OS Open UPRN is free but means hosting
     a 1.4GB dataset; OSM/Nominatim coverage too patchy. Parked.
  d. PropertyData address-match-uprn: needs an address as INPUT, wrong
     direction. Not useful for discovery.
  e. Rightmove transactionHistory XHR: prohibited by ToS. Rejected.
- Step 1 shipped: scraper now extracts delivery_point_id, latitude/longitude
  and epc_cert_url from PAGE_MODEL (commit a0eefef). Visible via
  /debug-scrape.

### EPC cross-matching built (option b)
- Date: 2026-06-10
- epc_cross_match(postcode, address, property_type, floor_area_sqm):
  street-token filter on the postcode's EPC certificates, then full-cert
  verification on built form / property type, and floor area within 10% when
  the listing supplied one. Only acts on a UNIQUE survivor. If more
  candidates than the cert-fetch cap (10), gives up entirely rather than risk
  a false unique match among a partial subset.
- Wired into build_report_data: runs only when the listing address has no
  house number. A match resolves the address (driving HPI last sale and EPC
  floor area downstream) and supplies floor area if missing. Report carries
  resolved_address and address_resolution (accurate/approx) for transparency.
- Safety: ambiguity always returns None - the candidates dropdown remains the
  fallback. Verified with 8 mocked scenarios including the two-identical-
  detached-houses case (returns None) and flat addresses.
- Debug: /debug-epc-match?postcode=..&address=..&type=..&sqm=..&key=ADMIN

### Non-market sales polluting the comparable set (B23 7DY case)
- Date: 2026-06-11
- Symptom: a £46,597 "sale" (69 Bleak Hill Road) appeared among 14
  comparables averaging ~£243k, dragging the average and the verdict maths.
  Land Registry includes partial transfers, right-to-buy and inter-family
  transactions recorded at non-market values.
- Fix: median band in _filter_sold - comparables outside 50%-200% of the
  set's median price are excluded. Median chosen over mean because the
  outlier cannot drag it. Band kept loose so it removes junk data without
  shaping the genuine distribution (the legitimate £400k Marsh Hill sale
  survives). Only applied to sets of 5+ comparables. The property's own
  sale history (find_last_sale) is deliberately NOT filtered - a genuine
  £46k right-to-buy purchase is still the property's real history.
- Decision: absolute price floor (layer 1 of the proposal) not adopted -
  user chose median band only.

---

## Parked ideas (come back to these)

### Floor area from the floor plan image (Claude vision)
- Date parked: 2026-06-10
- Observation (user): Rightmove floor plan images nearly always print the
  total sq m, even when the listing's sizings field says "Ask agent".
- Proposed pipeline (build as one piece of work):
  1. Extract floorplan image URLs from PAGE_MODEL (floorplans array - not
     currently read by the scraper).
  2. Send the image to the Claude API (plain requests call, no new pip
     packages) with a prompt like: "return the total floor area in sq m
     printed on this plan, or null if absent". Use Claude Haiku - roughly a
     third of a penny per report.
  3. Feed the result into floor_area_sqm. This powers price per m2 directly
     AND sharpens the EPC cross-match (the 10% floor-area filter becomes
     available on nearly every listing instead of only those with sizings).
- Requirements: ANTHROPIC_API_KEY env var added in Render (user action -
  assistant does not touch Render config).
- Rejected alternative: pytesseract OCR - new pip dependency and less
  reliable on floor plan layouts than a vision model.
- Status: parked while user tests how the EPC cross-match performs on real
  properties without it.

### deliveryPointId = UDPRN verification (option 1 of address resolution)
- Date parked: 2026-06-10
- The scraper now captures delivery_point_id (visible in /debug-scrape).
- User action: sign up to Ideal Postcodes (free test credits), look up a few
  deliveryPointIds from listings with known addresses, compare. If confirmed,
  build a UDPRN resolver (~3p per report) as the primary address source,
  with EPC cross-match demoted to fallback/corroboration.

---

## Open issues (working in order)

### 2. Price per m2 shows n/a (IN PROGRESS - EPC cross-match may resolve;
    retest after deploy)
- Likely cause: EPC floor-area lookup failing for the subject (same
  street-only address problem, or no EPC record). Not yet diagnosed.

### 3. Rental yield shows n/a (WORKING - live test passed on B23 7DY)
- Live test 2026-06-11: raw response confirms unit "gbp_per_week" and the
  average at data.long_let.average (£286/week). Parsed monthly rent
  £1,239 - realistic for a 3-bed semi in Erdington. Closed.
- Root cause (two bugs): PropertyData /rents returns rents PER WEEK (docs:
  "multiply by 4.333" for monthly) but our code treated the value as
  monthly; and the average is nested under data.long_let.average while our
  code read data.average, found nothing and returned None.
- Fix: parse data.long_let.average (with fallbacks for other shapes),
  convert weekly to monthly. Debug: /debug-rents?postcode=..&bedrooms=..

### 3b. AVM shows n/a (WORKING - live test passed on B23 7DY)
- Live test 2026-06-11: /valuation-sale returned estimate £220,000, margin
  £10,000, confidence high. Two parameter fixes from the live errors:
  off_street_parking must be "1"/"0" not "true"; the margin field is an
  ABSOLUTE GBP figure, not a percentage (the percent assumption produced a
  -£21.78M low). Parsing now treats margin as absolute unless it carries a
  % sign. construction_date "1914_2000" was accepted (echoed as 1914-2000).
- Commits: 5b78e3c (parking value), plus the margin fix.
- Root cause: we called /valuation, but PropertyData's endpoint is
  /valuation-sale, and it requires internal_area (sq ft), construction_date,
  bathrooms, finish_quality, outdoor_space, off_street_parking.
- Fix: switched to /valuation-sale. Requires a floor area, so the method
  stays n/a without one (another reason for the parked floor-plan vision
  idea). Unknown fields sent as honest defaults: construction_date
  1914_2000, bathrooms 1, finish_quality average, outdoor_space garden
  (none for flats), off_street_parking true. Response parsed from estimate
  plus margin_of_error, with fallbacks.
- CAUTION: parameter values for construction_date etc. are best guesses -
  PropertyData docs block automated reading. First live test via
  /debug-avm?postcode=..&type=..&bedrooms=..&sqm=..&key=ADMIN shows the raw
  response; their errors list valid values, iterate from there. Also check
  the credit cost of /valuation-sale on the PropertyData dashboard.

### 4. Days on market missing from report (CLOSED - verified working)
- Verified 2026-06-11 on B23 7DY: listing added 4 March 2026, report showed
  99 days, and 4 Mar to 11 Jun is exactly 99 days. Pipeline traced and
  intact: scraper parses the "added on" date from PAGE_MODEL, app compares
  against PropertyData local average, templates render it with the
  dom_signal commentary. No code change needed.
