# Valuation-Accuracy Batch Test — Loop Prompt

**Purpose**: Value a sample of live Rightmove listings and review our valuation
vs the asking price, so outliers can be found and the methodology tuned.

**Run from**: local Claude Code (laptop with real internet) OR just tap the URLs
on any device. The live Render backend does the scraping + valuation; the remote
Claude session's proxy blocks Rightmove/PropertyData so it cannot run this itself.

---

## How to run

### Step 1 — Kick the job

```
https://houseoffer-backend.onrender.com/batch-valuation-test?key=ADMIN_KEY&n=30&mode=random
```

- `n` = how many listings (max 40). Each is a full paid build = PropertyData credits.
- `mode=random` = random national sample. `mode=curated` = fixed diverse 10.
- `&urls=url1,url2,...` = value an explicit list instead.
- Returns a `job_id` and a `poll_url` immediately.

### Step 2 — Poll for results (wait 1–3 min)

```
https://houseoffer-backend.onrender.com/batch-valuation-test/<job_id>?key=ADMIN_KEY
```

`status` goes `building` → `ready`. Results stream in as each completes.

### Step 3 — Review

From the JSON, build a table sorted by `summary.outliers` (largest |gap| first):

| Label | Postcode | Type | Asking | Valuation (mid) | Gap % | Verdict | Comp conf | Floor area src |

Then read:
- `summary.median_abs_gap_pct` — typical distance between our valuation and asking
- `summary.confidence_histogram` — how often size-match engaged vs `area_only`
- `summary.outliers` — the rows to inspect by hand

### Step 4 — Diagnose each outlier

For every outlier ask: is the *listing* genuinely mispriced, or is *our* number
wrong? Tells:
- `comparable_confidence: area_only` + big gap → comparable set not size-matched
  (per-sqf coverage thin) — methodology, not the listing.
- `floor_area_source: unverified` → floor area was rejected (FIX 3) — expect £/sqm
  and AVM to be absent; gap driven by other methods.
- `bedrooms_source`/`property_type_source: unknown` → scrape missed attributes;
  comparables are type-broad — lower trust.
- `methods_available` low (1–2) → thin evidence, wide band, treat gap cautiously.

### Step 5 — Log

Append one line to `VALUATION_TEST_HISTORY.md` (create with a header if absent):

```
| <date> | <n valued_ok> | <median_abs_gap_pct>% | <outlier_count> | <one-line note> |
```

Do not edit existing rows. Propose ONE methodology improvement (file/function +
reasoning). Do not implement without sign-off.

---

## Notes

- First run `mode=curated` to confirm the tool works on known-good listings, then
  `mode=random&n=30` for breadth.
- If the random harvest yields few rows, it tops up from the curated set, so you
  always get data. Tune the live ID window with `&id_min=&id_max=` if random hit
  rate is poor.
- The admin key is chat/local only — never commit it.
