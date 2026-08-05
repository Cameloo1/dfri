# DFRI Day-14 Review Summary

As of 2026-08-05, DFRI is a public, reproducible Board/SEC/Census/BEA/NY Fed pipeline with a live
GitHub Pages publication at [cameloo1.github.io/dfri](https://cameloo1.github.io/dfri/). The
completed acceptance reports are [M0](M0.md), [M1](M1.md), [M3](M3.md), [M4](M4.md), and
[M5](M5.md). M2 has no completion report because its prescribed calendar evidence has not yet
matured.

## What exists and is live

- M0/M1: locked Python/Node builds, blocking privacy and secret guards, append-only source storage,
  vintage-safe reads, source legality records, live audits, and first-print Board G.19/H.8 history.
  FRED/ALFRED is absent under D-001.
- M2 capability: primary-target bridge nowcasts, baselines, state-space comparison, 101-month
  point-in-time backtests, immutable prediction/grade ledgers, recovery state, a public scoreboard,
  and active prediction/grade cron lanes. All prescribed backtest bars pass.
- M3: methodology 1.0.0 and evidence-linked DFR% bands for the original ten companies, with
  independent recomputation and live provenance checks.
- M4: complete versioned Pages feeds and schema, Home/Scoreboard/Company/Methodology/Changelog
  pages, deterministic publishing, recovery tests, accessibility/no-JavaScript gates, and hourly
  uptime/freshness monitoring. The public FastAPI runtime is deliberately deferred under D-010.
- M5: methodology 1.1.0, exactly 50 companies, 31 dated exclusions, 50 company pages, 74
  assumptions, 163 Matrix B rows, version comparison, quarterly company history, and a recovered
  production 2026-Q1 refresh. The live refresh ID is `qrf_c745102ac7134269b53f1323`; its
  revenue-weighted aggregate estimated DFR% band is 2.978273% / 3.222378% / 3.476520%.

The final public main commit `21ad8fcec8cfac230100dd139eacd717403c4e91` passes PR CI, main CI,
a new Windows cold clone with 389 tests at 85.04% coverage, deterministic replay, and 56-page
accessibility/no-JavaScript verification. The exact production retry appended zero rows, preserved
the accepted refresh ID, republished one live quarterly row, and passed the independent uptime
workflow.

## Open, deferred, or blocked

- M2 is **calendar-pending, not implementation-complete**. Workflow `327469010` became active at
  2026-08-05 01:57 UTC, after the prior day's cron windows. At this review boundary, zero genuine
  scheduled cycles were due or counted; the first eligible check is 2026-08-05 21:17 UTC. M2 still requires two consecutive scheduled weekly cycles
  and one automatic grade against a real G.19 first print. Manual runs are ineligible.
- D-010 leaves the public FastAPI service and its public latency/rate-limit/CORS/cache criteria
  **DEFERRED**, not passed. JSON/CSV/Parquet Pages feeds are the v1 access surface. The dormant
  Cloudflare Workers-first plan is ready only for an external access request or unwieldy M5 feeds.
- There is no active credential or source-terms blocker. D-001 remains the permanent Board-source
  replacement for prohibited FRED/ALFRED use. No market data or paid vendor is required.

## Five decisions with the highest leverage

1. Q-003 — Who, besides the owner, must approve a new Matrix A, Matrix B, or assumption version?
2. Q-004 — Should the next coverage investment deepen Tier 1 evidence for the 50 or expand beyond
   the S&P 500?
3. Q-005 — What empirical calibration bar should permit narrowing an attribution band?
4. Q-006 — When outsiders depend on quarterly feeds, is the Monday check sufficient or should a
   filing-event trigger be added?
5. Q-007 — What uses and terms should a future commercial data license permit?

Until those answers arrive, prior methodology versions remain immutable, the 50-company universe
holds, bands do not narrow editorially, the weekly no-op-safe refresh remains, and public feeds
stay CC BY-NC 4.0 with commercial rights reserved.
