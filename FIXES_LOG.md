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

---

## Open issues (working in order)

### 2. Price per m2 shows n/a (IN PROGRESS - EPC cross-match may resolve;
    retest after deploy)
- Likely cause: EPC floor-area lookup failing for the subject (same
  street-only address problem, or no EPC record). Not yet diagnosed.

### 3. Rental yield shows n/a
- Likely cause: PropertyData /rents returning no data for this
  postcode/type/bedrooms combination. Not yet diagnosed.

### 4. Days on market missing from report
- Previously worked. Likely lost in a merge/overwrite. Not yet diagnosed.
