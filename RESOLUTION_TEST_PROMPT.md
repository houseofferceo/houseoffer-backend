# Daily Address-Resolution Test — Loop Prompt

**Purpose**: Run the live resolver on a fixed batch of real Rightmove URLs, score
the results, and append one line to `RESOLUTION_TEST_HISTORY.md`.

**Run from**: local Claude Code (your laptop / machine with real internet access).
This prompt calls the live Render backend, which can reach Rightmove. It will not
work from a remote Claude Code session — the egress proxy blocks `onrender.com`.

**Frequency**: once a day via `/loop`.

---

## Instructions for Claude (do not edit this block)

You are running the daily address-resolution test for HouseOffer. Do the following
steps in order. Do NOT modify any source files. The only file you may write to is
`RESOLUTION_TEST_HISTORY.md` in the repo root.

### Step 1 — Call the test endpoint

Run this curl command and capture the full JSON response:

```bash
curl -s "https://houseoffer-backend.onrender.com/batch-resolve-test?key=ADMIN_KEY_HERE" | python3 -m json.tool
```

Replace `ADMIN_KEY_HERE` with the admin key (available in your local environment or
`.env` file — never commit it). If the backend is cold-starting, retry after 30s.

### Step 2 — Parse and score

From the JSON, extract:

- `run_at` — timestamp
- `total` — batch size (should be 10)
- `auto_resolve_pct` — auto-resolve rate
- `method_counts` — breakdown by method
- `miss_classes` — failure classification
- `scrape_errors` — count of properties where Rightmove fetch failed
- `results` array — one entry per URL

For each result, note:
- `url`, `label`, `address_scraped`, `resolved_address`, `method`, `auto_resolved`,
  `is_new_build`, `picker_candidates`, `error`

Compute:
- **Auto-resolve rate** = `auto_resolve_pct` from JSON
- **Correct-when-resolved rate**: For now, report "unverified" unless a result
  matches a known ground-truth address listed in Step 5 below. Flag any resolved
  address that looks wrong (e.g., a different street name from the label).
- **One-tap-needed rate** = picker_fallback / total × 100
- **Top miss class** = the key in `miss_classes` with the highest count

### Step 3 — Print the per-URL table

Print a markdown table:

| # | Label | Postcode | Address scraped | Resolved address | Method | Auto? | Picker cands |
|---|---|---|---|---|---|---|---|
...one row per result...

Then print the three headline rates and the miss-class ranking.

### Step 4 — Append to RESOLUTION_TEST_HISTORY.md

Append exactly one row to `RESOLUTION_TEST_HISTORY.md` (create the file with the
header if it does not exist):

```
| <date YYYY-MM-DD> | 10 | <auto_resolve_pct>% | <correct_pct or "unverified"> | <top_miss_class> | <one short note, e.g. "3 scrape errors — Rightmove rate-limited"> |
```

Do NOT edit any existing rows. Commit the file with message:
`resolution-test: <date> — <auto_resolve_pct>% auto-resolve`

### Step 5 — Ground truth (update as addresses are verified)

When a resolved address is manually confirmed correct, add it here so future runs
can compute `correct-when-resolved %` automatically.

| URL | Known correct address |
|---|---|
| https://www.rightmove.co.uk/properties/87488955 | Flat 51, 26 Viewforth, Edinburgh, EH10 4FF |

### Step 6 — Report

Print your full report. End with:
- The three headline rates (auto-resolve %, correct-when-resolved %, one-tap-needed %)
- Failure classes ranked by count
- Any confident-but-wrong resolved addresses (compare against Step 5 ground truth)
- One proposed improvement (file/function + one-paragraph reasoning). Do NOT implement it.

---

## Running this loop

From a terminal with Claude Code installed and internet access:

```bash
cd /path/to/houseoffer-backend
claude --print "$(cat RESOLUTION_TEST_PROMPT.md)" --dangerously-skip-permissions
```

Or as a daily cron via GitHub Actions — see `.github/workflows/resolution-test.yml`
(create that file if you want automated daily runs without keeping a laptop open).

---

## Notes

- If `scrape_errors` > 3, Rightmove is likely rate-limiting the backend IP. Note it
  in the history and skip scoring for that run.
- Listings can be removed from Rightmove (SSTC, withdrawn). If a URL returns
  `no postcode scraped`, replace it in the BATCH list in `app.py:batch_resolve_test`
  and update the ground truth table above.
- The admin key is never committed. Store it in a local `.env` or pass it as an
  environment variable: `ADMIN_KEY=$(cat .env | grep ADMIN_KEY | cut -d= -f2)`.
