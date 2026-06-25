# Address Resolution Test History

One line per run. Append; never edit existing rows.
Columns: date | batch | auto-resolve % | correct-when-resolved % | top-miss-class | notes

---

| Date | Batch | Auto-resolve % | Correct-when-resolved % | Top miss class | Notes |
|---|---|---|---|---|---|
| 2026-06-24 | 10 | ~20–30% (predicted) | ~100% (conservative guardrails) | uniform-street | STATIC ANALYSIS ONLY — proxy blocked Rightmove + Render backend; outcomes derived from code-path analysis and known property metadata. See RESOLUTION_TEST_HISTORY_2026-06-24.md for full report. |
| 2026-06-25 | 10 | 0% | N/A (0 auto-resolved) | no-floor-area (5/10) | LIVE RUN via Render backend. 0 scrape errors. Key bug: EH10 flat "Flat 51, 26 Viewforth" not auto-resolved — _leading_house_number fails on "Flat N" prefix. 2 medium-confidence candidates (SE25, S10) correctly blocked. BS9 floor area likely wrong (358 sqm). |
