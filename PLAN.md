# DFRI Execution Plan

Status values: `PENDING`, `IN_PROGRESS`, `BLOCKED`, `PASS`, `DEVIATED`.

This is the controlling execution map for `DFRI_BUILD_SPEC.md` v1.0. Sections 1 and 2 of the spec override every task below. A milestone may be marked complete only after its acceptance criteria pass from a fresh clone through the documented `make` targets and its milestone report records reproducible evidence.

## Global gates and invariants

- `G0 — Provenance`: never synthesize an unavailable value. Persist a `BLOCKED` record with URL, error, and timestamp.
- `G1 — Source legality`: use only free/public inputs whose terms permit the intended derived outputs. Stop and escalate only for a legal/ToS conflict or an unavoidable paid credential.
- `G2 — Point in time`: all model reads pass through the Vintage Guard and filter `release_date <= as_of`.
- `G3 — Publication honesty`: modeled quantities always publish ordered bands, tier badges, and provenance.
- `G4 — Determinism`: frozen inputs plus `AS_OF` and fixed versions/seeds produce byte-identical outputs.
- `G5 — Calendar clock`: M2 weekly prediction and G.19 grading workflows take priority over all non-blocking work.
- `G6 — Attribution sequencing`: M3 may begin once the public M2 clock is verified self-running;
  M2 remains open until two consecutive scheduled weekly cycles and at least one automatic
  first-print grade pass. Owner deviation D-006 changes only the M3 start gate.
- `G7 — Milestone proof`: each milestone closes only after `make bootstrap && make verify` succeeds from a fresh clone and `MILESTONE_REPORTS/M{n}.md` is written.
- `G8 — Capability proof`: any commit claiming a capability includes the test that demonstrates it.

## Ordered dependency graph

1. M0 foundation and contracts.
2. M1 production ingest, depending on M0 schemas, clients, and Vintage Guard.
3. M2 nowcast and public scoreboard, depending on M1 Board G.19/H.8 current and dated-release history plus publication primitives.
4. Run the M2 live-cycle observation window in the background: two consecutive scheduled weekly
   cycles, including automatic grading when a G.19 release matures a prediction.
5. M3 P0 attribution may begin after step 3's clock is verified self-running; it does not waive or
   complete step 4.
6. M4 publication hardening, depending on stable M2/M3 output contracts.
7. M5 scale, depending on M3 per-company evidence and M4 publication gates.

Independent source-ingest work may proceed in parallel only where it cannot weaken the M2 calendar-clock priority. A hard-blocked track is recorded in `DEVIATIONS.md` after 24 hours and another eligible track is selected.

## M0 — Foundation (repo, CI, contracts)

### M0.1 Repository and toolchain scaffold

- `PASS` Initialize Git and create the prescribed repository layout: `src/dfri/{ingest,lake,nowcast,attribution,publish,api}`, `tests/{unit,property,integration,fixtures}`, `lake`, `published`, `site`, `docs/ADRs`, `ops`, and `MILESTONE_REPORTS`.
- `PASS` Define Python 3.12 project metadata and lock dependencies with `uv`; configure ruff, strict mypy for `src/`, pytest, hypothesis, and coverage thresholds.
- `PASS` Add `.gitignore`, `.env.example` containing variable names only, and secret-safe logging/redaction tests. Never read or print `.env` values.
- `PASS` Add a portable `Makefile` with `bootstrap`, `lint`, `typecheck`, `test`, `replay`, `verify`, and later `publish` targets; document Windows prerequisites and commands.
- `PASS` Add `README.md`, `QUESTIONS.md`, and `DEVIATIONS.md` with explicit operating rules and environment variable names.
- `PASS` Add GitHub Actions PR CI for lint, type checks, tests, and deterministic replay. Local verification, public push runs `30967935599` and `30968775292`, and pull-request run `30969726354` pass. Q-001 is resolved.

Dependencies: spec read-through. Evidence: scaffold tree, tool configs, fresh-environment command transcript, CI workflow.

### M0.2 Curated schemas and append-only lake primitives

- `PASS` Implement strict schemas for every table in §5, including provenance, vintage, methodology, and publication fields.
- `PASS` Implement raw/validated/curated/published path contracts, content checksums, stable sorting, atomic writes, and append-only collision rules.
- `PASS` Add schema/property tests for required columns, types, append-only behavior, and deterministic Parquet bytes.

Dependencies: M0.1. Evidence: unit/property tests and replay artifact hashes.

### M0.3 Live-verified series registry

- `PASS` Implement registry definitions and verification receipts for Board G.19 series `DTCTL.M`, `DTCTLR.M`, `DTCTLN.M`, `DTCTL_N.M`, `DTCTLR_N.M`, and `DTCTLN_N.M`; Board H.8 series `B1247NCBA`, `B1029NCBA`, and `B3248NCBA`; and direct BEA/Census replacements for the context series formerly named `PCE`, `PCEDG`, `PCEND`, `PCES`, `RSAFS`, and `PI` in the spec.
- `PASS` Verify each series title, units/unit multiplier, frequency, source attributes, terms URL, and redistribution/license note against the live authoritative source before setting `verified_at`.
- `PASS` Confirm Board dated G.19 and H.8 release archives extend through at least 2015; the prescribed 2018 backtest window remains viable without revised-data substitution.
- `PASS` Verify the Board series registry through code against the release-page SDMX packages, not the retiring DDP package-builder UI.
- `PASS` Verify and pin the SEC companyfacts, submissions, Archives, and EFTS contracts; verify the current Reg AB II asset-class scope and card-trust 10-D requirement against current SEC primary sources.
- `PASS` Verify and pin Census MRTS/MARTS datasets/endpoints and selected NAICS contracts.
- `PASS` Verify and pin BEA NIPA API contracts for Table 2.4.5U.
- `PASS` Record corrected best-known identifiers and source contracts in `DEVIATIONS.md` with authoritative evidence.
- `PASS` Add hard-fail tests for metadata mismatches and receipt tests that prove unverified sources cannot publish.

Dependencies: M0.2 and live network/API access. Evidence: redacted verification receipts containing metadata and timestamps, never credentials.

### M0.4 Release calendar

- `PASS` Implement `releases_calendar` schema and deterministic seeding.
- `PASS` Verify authoritative G.19, H.8, NY Fed, Census, and BEA schedules, then seed 12 months of expected dates with source links.
- `PASS` Represent unknown/unannounced dates explicitly as `BLOCKED`, never inferred dates presented as official.
- `PASS` Test time zones, Friday-holiday boundaries, status distinctions, deterministic Parquet, and stable replay.

Dependencies: M0.2 and live source verification. Evidence: curated calendar rows and tests.

### M0.5 Source clients

- `PASS` Build shared HTTP behavior: descriptive User-Agent (`ops@camelon.app` for EDGAR), <=10 req/s EDGAR limiter, bounded exponential backoff, checksums, idempotency keys, structured errors, and credential redaction.
- `PASS` Implement the Federal Reserve Board SDMX client for current G.19/H.8 data and dated-release archive discovery/parsing for first-print vintages.
- `PASS` Implement EDGAR submissions/companyfacts/Archives/EFTS clients with accession provenance.
- `PASS` Implement Census client with dataset/variable verification.
- `PASS` Implement BEA client with parameter/table verification.
- `PASS` Archive legally redistributable real response fixtures with source URL, retrieval timestamp, checksum, and minimal necessary content.
- `PASS` Pass offline fixture tests and opt-in live smoke tests for every client.

Dependencies: M0.2–M0.3. Evidence: fixture and live-smoke test results with secrets redacted.

### M0.6 Deterministic seed replay

- `PASS` Commit a small frozen, real-source seed snapshot and pin the expected published tree hash.
- `PASS` Implement `make replay AS_OF=<date>` using fixed seeds, stable order, normalized timestamps, and atomic append-only publication.
- `PASS` Run replay twice in isolated disposable directories and assert byte-identical published trees.

Dependencies: M0.2 and enough of M0.5 to define input contracts. Evidence: deterministic tree-hash test.

### M0.7 Vintage Guard

- `PASS` Implement the single model-facing `guard.read(series, as_of)` path and reject observations released after `as_of`.
- `PASS` Add a poisoned-future canary test, first demonstrating failure without the guard and then passing through the guard.
- `PASS` Add an import/AST boundary test that fails when `nowcast` or `attribution` directly reads lake tables.

Dependencies: M0.2. Evidence: leak-canary and boundary tests.

### M0.8 Cold verification and report

- `PASS` Run `make bootstrap && make verify` from a fresh clone with credentials supplied only via environment variables. The documented Windows equivalents passed at commit `83efb52`; the clone itself required no credentials because live smoke is a separate opt-in gate.
- `PASS` Write `MILESTONE_REPORTS/M0.md` with every M0 AC marked pass/fail, evidence links, open deviations, and the M1 task map. M0 is complete after pull-request run `30969726354` passed.

Dependencies: M0.1–M0.7 all passing or explicitly deviated. M0 completion gate.

## M1 — Ingest complete

### M1.1 Board histories

- `PASS` Backfill all Board dated G.19 and H.8 releases from 2015 through the current manifests with page-declared release timestamps and first-print identification. The live local validation covers 139 G.19 pages and 605 H.8 pages (8,094 raw rows); generated lake/log artifacts remain ignored.
- `PASS` Ingest current Board SDMX historical snapshots as explicitly revised context, distinct from dated first-print rows. Live validation covers 822 G.19 rows and 1,809 H.8 rows from 2015 onward; a second live run appended nothing.
- `PASS` Validate manifest coverage, per-page/series row counts, uniqueness, units, release/vintage dates, checksums, idempotent reruns, and fail-closed source exceptions through a durable validator and real fixtures.

Dependencies: M0 source clients, schemas, and guard.

### M1.2 Public macro/category histories

- `PASS` Ingest NY Fed HHDC quarterly balances, originations, and delinquency transitions. The live Q1 2026 retrieval contains 945 rows across 21 verified series from 2015Q1; both a disposable proof and the primary ignored lake appended once and returned `already_present=true` on the second run.
- `PASS` Ingest Census MARTS monthly category histories. The live 2015-01 through 2026-06 retrieval contains 828 rows across six verified seasonally adjusted monthly-sales series; a second run appended nothing.
- `PASS` Ingest BEA monthly context histories. The live 2015-01 through 2026-06 retrieval contains 1,932 rows across 14 verified NIPA/Underlying Detail series; BEA response-envelope volatility is excluded from the canonical row checksum, and a second run appended nothing.
- `PASS` Add real archived BEA/Census parser fixtures and source-specific metadata, alignment, freshness, credential-redaction, checksum, value, and idempotency gates.
- `PASS` Add the corresponding real fixture and quality gates for NY Fed HHDC. The attributed minimal workbook retains exact 2015Q1–2026Q1 values from five required sheets, renders cleanly, has zero formula errors, and is covered by discovery, metadata, period, value, attribution, checksum, conflict, and idempotency tests.

Dependencies: M0 contracts/clients; NY Fed client extension.

### M1.3 P0 and lender filing facts

- `PASS` Verify a current consumer-facing S&P 500 membership snapshot against two maintained public sources and preserve the dated evidence. The attributed Wikimedia revision contains 503 share-class rows/500 issuers and reconciles exactly to the SEC's public 2026-03-31 SPY N-PORT filing after six explicit dated post-period transitions; the live default path rechecks the pinned snapshot and fails closed on drift.
- `PASS` Select/confirm ten P0 companies from evidence quality; log substitutions. The prescribed GM, F, AMZN, WMT, TGT, LOW, HD, and BBY remain; ULTA and TSCO fill the two open slots because their live 10-Ks provide direct PLCC ownership/underwriting/program/lender evidence. No prescribed candidate was substituted.
- `PASS` Ingest XBRL company facts for P0 candidates and lender evidence sources; implement HTML segment-footnote fallback with accession and snippet hash. The live primary lake contains 9,376 latest-10-K facts across ten P0 and seven lender/captive issuers; Amazon's 22-row segment table is stored with accession, source checksum, normalized snippet, and pinned snippet hash. An unchanged second run appended zero batches.
- `PASS` Add an actual 10-K footnote fixture and parser tests. The minimal Amazon 2025 Form 10-K Note 10 fixture retains exact periods, segment labels, units, and values with SEC source/fixture checksums and public-domain provenance.

Dependencies: M0 EDGAR client and membership verification.

### M1.4 Auto ABS-EE

- `PASS` Verify candidate trust identities/CIKs and filing availability before selection. The pinned registry uses Ford Credit Auto Owner Trust 2023-A, AMCAR 2023-1, GMCAR 2022-3, Drive 2024-1, Honda 2023-4, and Toyota 2022-A; every exact SEC submissions name and every month-end filing in its selected window was reverified live.
- `PASS` Parse EX-102 asset XML for at least six trusts spanning prime/subprime with at least 12 months each. All six trusts have 12 contiguous monthly aggregates (72 trust-months, 2,232,709 reported asset snapshots); the exact GMCAR prospectus defines the prime anchor and the exact AMCAR prospectus defines the subprime anchor.
- `PASS` Keep loan-level rows raw/private to the lake; publish only curated aggregates. The 72 immutable SEC source documents remain under the ignored `_private` lake boundary as deterministic gzip files with atomic receipt directories; the curated schema has no asset number, borrower score, geography, vehicle, or other loan-level field, and `published/` contains no XML.
- `PASS` Add actual EX-102 fixtures and aggregation/schema tests. The minimal AMCAR 2023-1 fixture retains the exact first Schedule AL asset from the 2026-06-30 filing with original/fixture checksums; parser tests cover namespace, period, identity, duplicate, numeric, boolean, privacy, coverage-denominator, append-only, and idempotency contracts.

Dependencies: M0 EDGAR client and schemas.

### M1.5 Card 10-D

- `PASS` Verify candidate card trust identities/CIKs and distribution-report contracts. The live registry pins American Express Credit Account Master Trust, Citibank Credit Card Issuance Trust, and BA Credit Card Trust, including the distinct trust/archive CIKs required by multi-filer EDGAR accessions and every historical EX-99 filename variant in the selected window.
- `PASS` Parse at least three trusts for receivables, principal payment rate, yield, and charge-offs. The live lake contains 36 unique monthly 10-D aggregates (12 per trust). All 36 report receivables, a source-defined principal payment rate, portfolio yield, and charge-off rate; Amex and BA also report dollar charge-offs for 24 months, while Citi's 12 months are explicitly `NOT_REPORTED` for the dollar amount and retain its reported Credit Loss Component rate.
- `PASS` Add actual 10-D fixtures and evidence-linked parser tests. The source-shaped June 2026 Amex fixture pins full-source and fixture checksums. Every aggregate stores the accession, trust/archive CIKs, primary and exhibit documents, exact source labels/rows, scaling rule, source checksum, archive-index checksum, and evidence hash; focused tests cover all three trust formats, amount absence, BA thousand-dollar scaling, identity drift, parser drift, continuity, schema, idempotency, and recovery gates.

Dependencies: verified SEC rule/filing contracts and M0 EDGAR client.

### M1.6 Freshness and spot audit

- `PASS` Implement the `/v1/health` precursor calculation from release-calendar SLAs with explicit `GREEN`, `STALE`, `BLOCKED`, and `OPTIONAL-DEGRADED` states. The live precursor is `GREEN` across ten explicit lanes: all 50 registered Board, BEA, Census, and NY Fed macro series; 17 SEC XBRL issuers; the required Amazon HTML fallback; five active Auto ABS trusts; Toyota 2022-A's separately labeled terminal history; and three card 10-D trusts. Toyota's latest distribution report shows an optional purchase and zero ending note balances, so its archived history is refreshed by verification time instead of being falsely judged as a stale active trust.
- `PASS` Implement a deterministic, seeded spot-audit tool that compares 20 stored rows to live authoritative sources and records redacted receipts. The live gate passed 20/20 rows with at least one row from each of Board G.19, Board H.8, BEA, Census, and NY Fed; a first run correctly returned `BLOCKED` when Census changed, and the same seed passed after that response was appended as a new immutable snapshot.
- `PASS` Prove every parser has a real fixture and that zero fabricated values exist. Real, checksummed fixtures cover Board current/history/manifests, BEA, Census, NY Fed, EDGAR submissions/companyfacts/10-K HTML, S&P membership evidence, Auto ABS-EE, and card 10-D. Parser tests pin source labels, units, periods, source and fixture hashes, and fail on drift. The 20-row live spot audit passes; revised-only snapshots, absent optional fields, terminal histories, and Citi's unreported charge-off dollar amount all remain explicitly labeled rather than synthesized.

Dependencies: M1.1–M1.5.

### M1.7 Cold verification and report

- `PASS` Pass fresh-clone `make bootstrap && make verify` plus full ingest verification. The first disposable Windows clone exposed checksum drift from Git CRLF conversion of archived fixtures; `.gitattributes` now preserves fixture bytes. A second clean clone at commit `f2ea8be` passed bootstrap, Ruff, formatting, strict mypy over 32 source files, 207 tests at 85.41% coverage, and three deterministic replay tests. The primary ignored lake separately passed all ten health lanes and a fresh 20/20 live-source spot audit.
- `PASS` Write `MILESTONE_REPORTS/M1.md` with all M1 AC evidence and the M2 task map.

Dependencies: all M1 ACs.

## M2 — Nowcast and public scoreboard

### M2.1 First-print target dataset and baselines

- `PASS` Derive monthly revolving (`DTCTLR.M`) and nonrevolving (`DTCTLN.M`) flows from point-in-time Board first-print levels through the Vintage Guard. The Board-only replacement reads the preliminary target-month and revised prior-month levels from the same immutable dated G.19 release, not from consecutive preliminary prints. The canonical v1 live backfill contains 139 months per target from 2014-11 through 2026-05; a full 139-page recheck returned `already_present=139` despite volatile wrapper markup. Both derived series are pinned in `board_target_registry.json`, and model reads reject future releases, gaps, duplicate releases, malformed provenance, or direct lake access.
- `PASS` Implement random-walk, seasonal-naive, and AR(2) baselines with expanding-window evaluation. Each target has 303 deterministic baseline forecasts over January 2018 through May 2026, with strictly prior training data and evidence-sensitive input hashes. Tests cover exact random-walk/seasonal behavior, an exact AR(2) process, expanding-window boundaries, rank failure, continuity, finite values, and release-time monotonicity.

Dependencies: M1 Fed histories and Vintage Guard.

### M2.2 Bridge and state-space models

- `PASS` Recover point-in-time retail sales from the official dated Census MARTS release PDFs. The live append-only lane contains 137 continuous release-coherent `DELTA_RETAIL_SALES.M` rows from January 2015 through May 2026; each flow subtracts the preliminary prior-month adjusted total from the advance target-month total in the same Table 1. The current Census API's revised-only history remains excluded from historical model reads.
- `PASS` Implement the deterministic ragged-edge ridge bridge using point-in-time H.8 weekly changes, release-available first-print retail flow, explicit H.8 coverage/pacing, and month seasonality. The production `bridge-ridge-v2-alpha10` regularization prevents missing-retail/partial-H.8 leverage explosions while preserving explicit missingness. The 101-month backtest has revolving MAE/RMSE/sign accuracy 4,633/7,948/87.1% versus best-naive MAE 7,487; nonrevolving is 5,302/9,666/71.3% versus best-naive MAE 4,906.
- `PASS` Implement the statsmodels mixed-frequency Kalman candidate. Its latent monthly G.19 flow is observed through up to five separate within-month H.8 Wednesday-change channels plus first-print retail when available; future weekly slots and unreleased retail remain missing Kalman observations. Parameters are estimated only from the prior expanding window, forecasts carry state-derived bands and evidence hashes, and malformed weekly alignment is rejected.
- `PASS` Apply the headline eligibility gate without promoting the more complex model. On the 101-month primary revolving target, state-space MAE is 5,073 versus bridge v2 MAE 4,633, so §6.2 keeps the bridge as headline. State-space nonrevolving MAE is 5,228 versus bridge v2 5,302, but it under-covers its intervals and remains behind AR(2) MAE 4,906.

Dependencies: M2.1.

### M2.3 Backtest credibility report

- `PASS` Backtest January 2018 through May 2026 over 101 strictly expanding-window forecasts per target, using dated Board G.19/H.8 releases, dated Census MARTS releases, and release-coherent first-print grades selected through the Vintage Guard.
- `PASS` Report MAE, RMSE, 80/95% interval coverage, and three-way acceleration sign accuracy for all three baselines, the bridge, and state-space candidate in canonical JSON plus stable Markdown.
- `PASS` Evaluate the §6.3 primary-target bars in code. `bridge-ridge-v2-alpha10` beats the best naive AR(2) MAE by 38.1%, has 71.3%/93.1% empirical 80/95 coverage, and reaches 87.1% acceleration-sign accuracy; all prescribed primary bars pass, so no unmet-bar deviation is required.
- `PASS` Add the deterministic `make backtest` / `make.cmd backtest` path, canonical report hash, exact first/last vintage URLs and checksums, comparable-period enforcement, duplicate/incomplete rejection, and repeat-write byte identity tests. Stable artifacts are `reports/M2_BACKTEST.md` and `reports/m2_backtest.json`.

Dependencies: M2.1–M2.2.

### M2.4 Immutable prediction and grading loop

- `PASS` Implement append-only prediction writes with stable SHA-256 IDs keyed by model/input-hash/target identity. The first accepted execution timestamp is preserved across later retries; model/input hashes and bands are immutable. A cross-process atomic write gate, duplicate-ledger detection, and attempted-edit rejection tests enforce the contract.
- `PASS` Implement first-print grading as a separate append-only ledger. Grades bind a prediction to the exact Board G.19 vintage URL, first-print value, release timestamp, and absolute error; duplicate evidence is idempotent, changed evidence is rejected, immature predictions remain open, and full stored re-grade integrity is checked against raw first-print targets.
- `PASS` Implement idempotent `scoreboard-predict` and `scoreboard-grade` commands. Input identity is keyed to the latest stored Thursday/Friday H.8 release and covers every unreleased month through its latest Wednesday observation; `made_at` is the actual first job execution time. Grading runs only after the stored first-print release boundary. Both commands write content-addressed attempt receipts. The corrected local August 4 run appended four July 31-input-origin v2 predictions and a later-timestamp retry appended zero while preserving the first timestamp; current targets remain immature. External scheduling remains M2.6.
- `PASS` Persist the public prediction, grade, and first-publication ledgers in `state/ledgers/` on the default branch, with canonical row hashes and exact Parquet byte hashes. PRs [17](https://github.com/Cameloo1/dfri/pull/17) and [18](https://github.com/Cameloo1/dfri/pull/18) migrated 10 existing batches / 14 rows byte-identically. Hosted run [31281435353](https://github.com/Cameloo1/dfri/actions/runs/31281435353) restored and remerged the same manifest as a zero-append no-op, while a fresh public-main clone recovered all rows without an Actions artifact. Artifacts remain a redundant cache only; see `STATE_DURABILITY_REPORT.md`.

Dependencies: M2.3 and M0/M1 publication primitives.

### M2.5 Scoreboard publication

- `PASS` Implement stable prediction/scoreboard JSON, CSV, and typed Parquet feeds with schema documentation plus methodology, data-vintage, publication-time, mode, and CC BY-NC fields. Publication is built in a disposable directory, atomically promoted, refuses unmanaged destinations, removes stale managed paths, and is byte-identical for pinned inputs.
- `PASS` Build the no-JS static Home, Scoreboard, Methodology, and immutable prediction-permalink pages with nested 80/95% bands, first-print actuals/errors/provenance, accessible text equivalents, optional sorting enhancement, and local-only assets. The filtered four-row v2 preview explicitly excludes eight pre-public smoke rows, has four permalinks, totals 57,168 bytes, and reproduced manifest hash `3793b59199975166b89ada7be0355489b99c0ae32ef87fd7839f823d44414be7` on two documented builds.
- `PENDING` Verify scheduled H.8 and G.19 completion within four hours. The active default-branch workflow and [public scoreboard](https://cameloo1.github.io/dfri/) are deployed. Manual bootstrap run `30968841429` published four predictions, zero grades, and 15 manifest-verified files; its candidate and accepted state artifacts are byte-identical (`8d13c7f865da0a2c9a8a7f0d33f8d126bb6c3514b9fc5ee1aa98cf989cc50970`) and restore 1,029 allowlisted files with manifest hash `b9fbad744eb5d9e3b8ee47e58f6e1ac945830e16d86fca0f2286fab74185b87a`. Its receipt correctly failed the four-hour SLA because a manual bootstrap used the July 31 release 367,826 seconds later; it is not live-cycle evidence. Genuine scheduled-cycle latency remains pending.

Dependencies: M2.4.

### M2.6 Two-live-cycle gate

- `PENDING` Observe and preserve evidence for two consecutive scheduled weekly cycles; do not backfill these receipts.
- `PENDING` Verify matured predictions are automatically graded on the relevant G.19 first print.
- `PASS` Release the M3 start gate after the active public workflow, accepted state recovery, Pages
  deployment, and both registered cron lanes were verified. Continue M2.6 in the background; no
  manual or backfilled run counts toward its remaining evidence.

Dependencies: public M2.5 deployment and calendar time.

### M2.7 Cold verification and report

- `IN_PROGRESS` Pass scheduler/public URL checks. Workflow `327469010` is active, Pages is HTTPS and restricted to `main`, every live manifest file and permalink passes, and accepted state recovery passes. Two scheduled cycles remain. A disposable no-local clone of capability commit `7fe2884` completed the documented locked `make.cmd bootstrap` and `make.cmd verify` path on Python 3.12.13: lint/format and strict typing passed, 309 tests passed at 85.05% coverage, and all three determinism tests passed. The production append-count fix passes public CI with 310 tests at 85.10% coverage.
- `PENDING` Write `MILESTONE_REPORTS/M2.md`, including metric bars or logged deviations and two-cycle evidence.

Dependencies: M2.1–M2.6.

## M3 — Attribution P0 (eligible after the verified live-clock gate)

### M3.1 Assumption Registry and matrices

- `PASS` Populate every non-observed Matrix A/B/model parameter with an assumption ID, source, tier, evidence, prior/band, sensitivity note, version, and active state.
- `PASS` Build Matrix A v1 with row-sum and evidence checks.
- `PASS` Build Matrix B v1 for ten P0 companies with non-negative weights, denominator evidence, and membership snapshot refs.
- `PASS` Keep the complete versioned Matrix A, Matrix B, and Assumption Registry sources public
  under the addendum escape hatch so an unauthenticated cold clone can reproduce every rendered
  assumption and company result.

Dependencies: verified public M2 clock and M1 category/company data. M2.6 continues independently.

### M3.2 Company facts and Monte Carlo

- `PASS` Produce evidence-linked US consumer revenue denominator bands for ten P0 companies.
- `PASS` Implement 20,000-draw deterministic Monte Carlo using registered triangular priors and publish 10th/50th/90th percentiles.
- `PASS` Pass Hypothesis property tests for band ordering, finite flows, monotone percentiles, Matrix bounds, and tier shares summing to one.
- `PASS` Compute the homepage aggregate as total estimated debt-funded consumer revenue across
  covered companies divided by their total estimated U.S. consumer revenue. Prohibit equal,
  market-cap, price, or other market-data weighting in code and tests.

Dependencies: M3.1.

### M3.3 P0 pages and independent recompute

- `PASS` Publish ten company pages with "estimated" DFR% bands, tier breakdowns, <=15-word Tier 1 quotes, accession links, assumption IDs, and top-five sensitivity sections. All ten public Pages URLs return HTTP 200.
- `PASS` Implement `tools/recompute_check.py` without attribution-engine code reuse; AMZN, GM, and WMT match within 0.17 percentage points on the mid.
- `PASS` Pass live provenance link checking for all 17 unique attribution evidence URLs; receipt is retained under ignored `.local/evidence/`.

Dependencies: M3.2.

### M3.4 Cold verification and report

- `PASS` Pass fresh-clone verification and public-page checks. Two disposable Windows clones pass the locked bootstrap, 338-test verify at 85.19% coverage, deterministic replay, and independent recompute; PR CI, main CI, and all live URLs pass.
- `PASS` Write `MILESTONE_REPORTS/M3.md` with all P0 evidence and the M4 task map.

Dependencies: all M3 ACs.

## M4 — Publication hardening

### M4.1 Versioned feeds and API

- `PASS` Publish all §8.1 CSV/JSON/Parquet feeds plus `/v1/feeds/schema.json` with CC BY-NC 4.0 row/header metadata. Every stable live Pages feed returns HTTP 200 with an ETag and `max-age=600` cache control.
- `DEFERRED` D-010 defers the nine public FastAPI endpoints and the public API contract. The tested read-only implementation and generated `docs/openapi-v1.json` remain committed as dormant capability, but no endpoint is claimed live.
- `DEFERRED` D-010 also defers public rate-limit, CORS, ETag/cache, and p95 latency acceptance. The local benchmark remains implementation evidence only; it is not a public-service pass.

Dependencies: stable M2/M3 schemas.

### M4.2 Static site quality

- `PASS` Build and deploy Home, Scoreboard, ten Company, Methodology, and append-only Changelog pages from versioned outputs and `branding.yaml`; maintenance deployment run [30977028395](https://github.com/Cameloo1/dfri/actions/runs/30977028395) accepted the Pages artifact and state before its expected non-cycle four-hour receipt failure.
- `PASS` Enforce evidence/tier/band copy contracts, <=500 KB pages, estimated <1 s 4G loads, WCAG AA contrast, and server-rendered SVGs. Axe 4.12.1 reports zero violations on all 14 pages, and all 14 retain complete main content with JavaScript disabled. The 390×844 rendered pass has no horizontal overflow.
- `PASS` Generate and serve methodology v1.0.0 plus the complete read-only Assumption Registry; all ten live company pages and both attribution feeds return HTTP 200.

Dependencies: M4.1 and M3 outputs.

### M4.3 Publication operation

- `PASS` Wire cross-platform deterministic replay into `make publish` and CI. Primary and filtered Windows cold clones produce byte-identical 260,567-byte output with manifest `0c3a75163385275028533fe7a51fc44f62b227197159714cc874f704c3a74fe5`; PR run [30976832058](https://github.com/Cameloo1/dfri/actions/runs/30976832058) and main run [30976933576](https://github.com/Cameloo1/dfri/actions/runs/30976933576) pass. Published changelog history is prefix-protected in CI.
- `PASS` Active workflow `327542973` runs hourly at minute 17 and retains owner-readable receipts for 90 days. Run [30977114871](https://github.com/Cameloo1/dfri/actions/runs/30977114871) is green for all nine required site/feed URLs and nowcast freshness. D-010 makes the absent API an explicit `DEFERRED` non-requirement; configuring `DFRI_API_BASE_URL` later automatically makes API checks required.
- `PASS` Verify failed-ingest append boundaries plus injected failed-build, failed-promotion, retry, and rollback-to-last-good paths. Prior managed output remains byte-identical, incomplete staging is removed, and unmanaged destinations are never overwritten.

Dependencies: M4.1–M4.2.

### M4.4 Cold verification and report

- `PASS` The filtered fresh clone at `8785cbf` passed locked bootstrap, 372-test verification at 85.21% coverage, deterministic replay, and deterministic publish; the Pages-only live receipt is green and API-specific checks remain explicitly deferred.
- `PASS` Write `MILESTONE_REPORTS/M4.md` with all evidence, D-010 rows, the M5 task map, and the ready-to-execute dormant serverless appendix.

Dependencies: all M4 ACs.

## M5 — Scale

### M5.1 Fifty-company coverage

- `PASS` Expand membership/evidence mapping to 50 consumer-facing S&P 500 companies using the same per-company gates as M3. Methodology 1.1.0 pins 40 live-verified additions alongside the immutable original ten.
- `PASS` Publish a dated exclusion list with one-line reasons and preserve historical membership. The registry partitions all 81 verified Consumer Discretionary/Staples candidates into 50 included and 31 explicitly excluded rows and retains the 1.0.0 coverage record.

Dependencies: M3 and M4 contracts.

### M5.2 Quarterly refresh and performance

- `PASS` Implement the new-10-Q quarterly refresh path through recompute and append-only publish. The job selects a complete first-print Board quarter, applies point-in-time same-tag SEC TTM facts, reweights Matrix B, recomputes all 50 companies, and appends one content-addressed record.
- `PASS` Demonstrate one production quarterly refresh with evidence and recovery logs. Run [30986960228](https://github.com/Cameloo1/dfri/actions/runs/30986960228) appended `qrf_c745102ac7134269b53f1323`, updating 35 of 50 denominators for 2026-Q1. Exact production retry [31033209605](https://github.com/Cameloo1/dfri/actions/runs/31033209605) appended zero and preserved that ID; an allowlisted state-bundle pack/unpack restored the same record and source hash. The committed pre-deployment demo remains reproducibility evidence and is source-semantically deduplicated from the live feed.
- `PASS` Measure before optimizing and keep the full CI path below 30 minutes. PR run [30986517315](https://github.com/Cameloo1/dfri/actions/runs/30986517315) retained a 107-second locked-bootstrap + verify + publish receipt against the 1,800-second budget; the final idempotency PR and main runs completed in 2m00s and 2m02s.

Dependencies: M5.1.

### M5.3 Methodology comparison and report

- `PASS` Publish a sensitivity-analysis page comparing methodology versions without rewriting history. The comparison renders complete 1.0.0 and 1.1.0 bands for the original ten plus midpoint deltas; the dated coverage ledger remains separate.
- `PASS` Pass fresh-clone verification and write `MILESTONE_REPORTS/M5.md`. A new clone of corrected public main `21ad8fcec8cfac230100dd139eacd717403c4e91` contains neither excluded control document and passes locked bootstrap, 389-test verification at 85.04% coverage, deterministic replay, deterministic publish, and 56 Axe/no-JavaScript pages. Production run [30986960228](https://github.com/Cameloo1/dfri/actions/runs/30986960228) appended the accepted refresh; exact retry [31033209605](https://github.com/Cameloo1/dfri/actions/runs/31033209605) appended zero and preserved its ID.

Dependencies: M5.1–M5.2.

## Day-14 review packet

- `PASS` Include every milestone report completed by the review date: M0, M1, M3, M4, and M5. M2 is correctly withheld until its calendar gate passes.
- `PASS` Write `MILESTONE_REPORTS/DAY14_SUMMARY.md`: one page covering live capability, evidence-backed pending/deferred work, and Q-003 through Q-007.
- `PASS` Keep `QUESTIONS.md` and `DEVIATIONS.md` current and distinguish verified behavior from intent. Q-001/Q-002 are resolved, Q-003–Q-007 remain non-blocking, D-010 remains a deferral rather than a pass, and M2 remains calendar-pending.

## Immediate next actions

1. Preserve the first two genuine scheduled weekly prediction cycles and their deployment receipts.
2. Verify the relevant G.19 release automatically grades the matured predictions.
3. Preserve M4's Pages-only uptime receipts and keep the D-010 serverless appendix dormant until a trigger occurs.
4. Hold the verified 50-company M5 boundary until one of Q-003–Q-007 is answered; do not add market data, a paid vendor, or a public API by inference.
