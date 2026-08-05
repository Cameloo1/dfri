# DFRI

DFRI is a provenance-first system for estimating what share of consumer-facing company revenue is financed by net new consumer debt. It publishes modeled results as bands, separates observed inputs from assumptions, and keeps weekly nowcast predictions immutable so they can be graded against later first-print releases.

Current execution status lives in [PLAN.md](PLAN.md). Intended behavior is not treated as shipped
behavior; milestone reports record the acceptance evidence that exists. The controlling build
specification and agent operating prompt are deliberately excluded from the public publication lane.

## Current state

M0 passes the local, fresh-clone, push-CI, and pull-request-CI gates. M1 Board, public
macro/category, issuer-fact, Auto ABS, and card-trust histories pass their ingest, live-audit,
health, and fresh-clone gates. The
[M1 milestone report](MILESTONE_REPORTS/M1.md) records the evidence. M2 first-print target,
baseline, ragged-edge bridge, state-space candidate, and reproducible backtest gates now pass.
Immutable prediction/grading ledgers, idempotent local jobs, and the deterministic feed/static-site
builder also pass. The [public scoreboard](https://cameloo1.github.io/dfri/) and active external
clock are now deployed; two genuine scheduled weekly cycles and automatic first-print grading
remain. M3 attribution is complete and live for the ten P0 companies; its public/cold-clone
evidence is in the [M3 milestone report](MILESTONE_REPORTS/M3.md). M4's versioned feed, static
site, deterministic publish, read-only FastAPI, latency, axe, no-JavaScript, recovery, and hourly
monitoring contracts pass locally. A durable public API host is not configured, so M4 is not
claimed complete. No M2 completion is claimed early.

## Outputs, sources, and evidence tiers

DFRI publishes immutable weekly predictions of Federal Reserve G.19 consumer-credit flows and,
after attribution ships, quarterly estimates of debt-funded revenue for covered companies. Modeled
company results are always `[low, mid, high]` bands. The homepage aggregate is revenue-weighted:
total estimated debt-funded consumer revenue across covered companies divided by their total
estimated U.S. consumer revenue. It is never equal-weighted or market-cap-weighted.

- **Tier 1 — Observed:** a disclosure directly connects financing with a company's sales.
- **Tier 2 — Category-mapped:** consumer-credit flow is modeled into spending categories and then
  companies using evidence-linked Matrix A and Matrix B weights.
- **Tier 3 — Fungible:** debt that cannot be assigned more directly is allocated using an explicit,
  widest-band fungibility assumption.

Primary inputs are Federal Reserve Board dated G.19 and H.8 releases, SEC EDGAR filings, Census
MARTS releases, BEA product-level consumer spending, and the New York Fed Household Debt and
Credit workbook. DFRI uses no market data, price feeds, TradingView data, or paid vendor inputs.

## Licensing and mapping-source policy

Code, tests, methodology source, site templates, and the nowcast engine are licensed under the
[Apache License 2.0](LICENSE). Published DFRI feeds are licensed under
[CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/): free for non-commercial use with
attribution. Commercial licensing is reserved; contact `ops@camelon.app`. Third-party source
artifacts retain their own notices, including the separately attributed CC BY-SA 4.0 membership
snapshot.

The optional private mappings split is not used. Matrix A, Matrix B, and the Assumption Registry
are public alongside their rendered read-only methodology output. A private dependency would break unauthenticated cold-clone
verification or make published numbers depend on hidden inputs. This is the addendum's explicit
escape-hatch path and is recorded in `DEVIATIONS.md`.

## Privacy and public-repository posture

This repository intentionally publishes all code, tests, methodology, Matrix A and Matrix B
mappings, assumptions, aggregate company estimates, and immutable nowcast predictions needed to
audit a DFRI result. It never intentionally publishes credentials, local environment files,
drafts, control documents, personal information, or private workstation identifiers. CI blocks
secret patterns, committed control documents, and absolute user or workstation paths in Markdown;
the publication commands also reject either excluded control document if it is staged.

All published DFRI data is aggregate. DFRI does not redistribute personal, borrower, account,
vehicle, loan-level, or other row-level consumer data. Source pipelines that process granular SEC
disclosures retain those artifacts only behind an ignored local boundary and publish aggregate
statistics.

## Prerequisites

- Python 3.12, managed by `uv`
- `uv` 0.11 or newer
- Node.js 24 and a local Chrome channel for the axe/no-JavaScript publication gate
- GNU Make for the canonical CI/fresh-clone commands; Windows users may run the equivalent `make.cmd`
- Network access for opt-in live source verification

Required secret environment variables:

- `BEA_API_KEY`
- `CENSUS_API_KEY`

Federal Reserve Board G.19/H.8 downloads and SEC EDGAR do not use API keys. DFRI identifies itself to EDGAR with a descriptive User-Agent containing `ops@camelon.app` and enforces the SEC request-rate boundary. Secrets are read only from process environment variables, are never committed, and must not be printed. Locally, `uv run --env-file .env ...` may inject an ignored `.env` into the child process without exposing values.

## Development commands

Canonical commands:

```sh
make bootstrap
make verify
make replay AS_OF=2024-01-31
make attribution
make recompute-check
make provenance-check
make publish
```

Windows equivalents:

```bat
make.cmd bootstrap
make.cmd verify
set AS_OF=2024-01-31
make.cmd replay
make.cmd attribution
make.cmd recompute-check
make.cmd provenance-check
make.cmd publish
```

`make replay` writes a deterministic seed publication under `published/replay`. Runtime lake and publication artifacts are ignored by Git unless a reviewed, stable fixture or public report is intentionally promoted.

`make publish` validates the committed OpenAPI and append-only changelog contracts, rebuilds the
complete public site twice from a frozen copy of the first four genuine public predictions,
requires byte-identical output, measures the local API budget, and runs axe plus a true
JavaScript-disabled render across every page. Its receipts remain under `.local/evidence/`; the
frozen replay does not count as a scheduled live cycle.

Live verification is opt-in and uses the supplied environment only:

```sh
uv run --env-file .env python -m dfri.ingest.verify --output .local/evidence/source-verification.json
```

With credentials already present in the process environment, the equivalent targets are
`make live-smoke` and `make.cmd live-smoke`. The verifier checks Board G.19/H.8 packages and
2015 archive depth, BEA and Census registry metadata, and SEC submissions, companyfacts,
Archives, and EFTS response contracts. Its ignored JSON receipt contains source URLs,
checksums, counts, terms findings, and timestamps but no credential-bearing URL.

The receipt excludes credential values. A source is not marked verified merely because its endpoint returned HTTP 200: its authoritative title, units, frequency, endpoint contract, and applicable automated-access/storage/derivative-redistribution terms must match the registry.

## P0 company attribution

The M3 engine publishes quarterly estimates for GM, F, AMZN, WMT, TGT, LOW, HD, BBY, ULTA,
and TSCO. The first version uses the complete 2026-Q1 set of Board G.19 first prints; it does not
substitute a revised or incomplete Q2 observation. For each company, annual SEC XBRL revenue is
converted to a quarterly denominator and multiplied by a registered U.S.-consumer-share prior.
The numerator follows the prescribed formula: credit flow × Matrix A product/category weight ×
Matrix B category/company weight.

`make attribution` performs 20,000 deterministic triangular-prior draws and writes the stable
machine report to `reports/dfri_companies.json`. `make recompute-check` independently re-evaluates
AMZN, GM, and WMT using only the committed JSON inputs and the formula; it imports no attribution
engine code and requires each midpoint to match within ±0.5 percentage points.
`make provenance-check` is the explicit live gate for every external evidence link and writes its
ignored timestamped receipt to `.local/evidence/`.

The static publisher emits all required v1 attribution feeds and ten no-JavaScript company pages:

- `v1/feeds/dfri_companies.{csv,json,parquet}`
- `v1/feeds/assumptions.{csv,json}`
- `v1/feeds/schema.json`

Each company page shows the word “estimated” with its DFR% band, Tier 1/2/3 shares, a specific SEC
filing link and short observed-evidence excerpt, every relevant assumption ID, and the five inputs
with the largest Monte Carlo correlation to the result. The homepage aggregate divides the total
estimated debt-funded revenue by the total estimated U.S. consumer revenue across the covered
companies; no price, market-cap, TradingView, or vendor data enters the computation.

## Membership snapshot

The current S&P 500 membership snapshot is a separately attributed CC BY-SA 4.0 artifact. It
pins the Wikimedia page revision and all 503 share-class rows, representing 500 issuers. The
verification command cross-checks those issuer identities against the SEC's public March 31,
2026 SPY N-PORT filing, then applies six explicit post-period transitions with dated evidence.
It fails closed on an unregistered constituent change, source-shape change, missing transition,
or unresolved issuer-name mismatch:

```sh
make membership-verify
```

The Windows equivalent is `make.cmd membership-verify`. The ignored receipt records both source
checksums, the Wikimedia revision, the SEC accession, and the reconciliation counts. It does not
store or republish an S&P Global constituent file. Updating the checked-in snapshot requires the
explicit `--refresh-snapshot --snapshot-output ...` operator path after reviewing any change.

## SEC issuer facts and filing evidence

The filing-facts pipeline verifies each registered issuer's current SEC identity and latest 10-K
before it accepts the companyfacts response. The P0 set is GM, F, AMZN, WMT, TGT, LOW, HD, BBY,
ULTA, and TSCO. Ulta and Tractor Supply fill the two open selection slots because their latest
10-Ks provide direct private-label-card ownership, underwriting, program, and lender evidence.
No prescribed candidate was substituted.

```sh
make filing-facts
```

The Windows equivalent is `make.cmd filing-facts`; `FILING_ROLE=p0` or `FILING_ROLE=lender`
selects a bounded recovery lane. The default run ingests latest-10-K XBRL observations for the
ten P0 issuers plus Synchrony, Bread, Capital One, historical standalone Discover, Affirm, Ford
Motor Credit, and GM Financial. Exact XBRL values are stored as JSON scalars to avoid precision
loss. Missing XBRL labels/descriptions remain null rather than being invented.

Amazon's actual 2025 10-K provides the first HTML segment-footnote fallback. Its evidence row
stores the filing accession, immutable document checksum, canonical 22-row extracted table,
normalized evidence snippet, and pinned snippet hash. The current primary lake contains 9,376
XBRL rows across all 17 issuers and one HTML evidence row; an unchanged replay appends nothing.

## Auto ABS-EE aggregates

The Auto ABS pipeline pins six exact trust CIK/name pairs and twelve contiguous monthly Form
ABS-EE filings for each: Ford Credit Auto Owner Trust 2023-A, AmeriCredit Automobile Receivables
Trust 2023-1, GM Financial Consumer Automobile Receivables Trust 2022-3, Drive Auto Receivables
Trust 2024-1, Honda Auto Receivables 2023-4 Owner Trust, and Toyota Auto Receivables 2022-A
Owner Trust. The trust prospectuses define GMCAR as prime and AMCAR as primarily subprime, so
the required credit-spectrum comparison does not rely on a DFRI-invented label.

```sh
make auto-abs
```

The Windows equivalent is `make.cmd auto-abs`. `AUTO_ABS_TRUST=trust_id` selects one durable
recovery lane. A bounded canary additionally requires the explicit
`AUTO_ABS_ARGS="--max-filings-per-trust 1 --allow-partial"` gate and never claims complete
coverage.

EX-102 bodies are streamed directly into deterministic gzip files beneath the ignored
`.local/lake/raw/_private/sec_auto_abs_ee` boundary; an accession directory becomes visible only after
both the source and its checksum receipt are complete. The live backfill retains 72 unique SEC
source documents and publishes no loan-level rows. The only non-private output is 72 aggregate
rows covering 2,232,709 reported asset snapshots. The aggregate schema excludes asset IDs and
borrower, geography, vehicle, and credit-score fields.

Five selected trusts remain active through June 2026. Toyota Auto Receivables 2022-A is retained
as a twelve-month terminal history through March 2026: its latest distribution report records an
optional purchase and zero ending balances for every note class. The health policy therefore
checks the five active trusts against monthly subject-period age and checks the separately labeled
Toyota terminal archive by its latest live verification time. It does not call a terminated trust
stale or silently exempt an active one.

Optional source fields are never coerced. Counts and balance denominators disclose coverage for
current interest rate, remaining term, asset-added status, and recoveries; Ford recovery-only
records are counted separately from active-loan metric records. The actual AMCAR EX-102 fixture
pins both the full source checksum and the extracted fixture checksum. A second all-trust live
run appended zero raw archives and zero aggregate batches.

## Credit-card trust 10-D aggregates

The card pipeline pins American Express Credit Account Master Trust, Citibank Credit Card
Issuance Trust, and BA Credit Card Trust by their exact SEC submissions identities. The registry
separates each trust CIK from the archive-path CIK because Amex and Citi are multi-filer
accessions. It also pins the trust-specific EX-99 filename and metric-label contracts rather than
assuming a common filing layout.

```sh
make card-trust
```

The Windows equivalent is `make.cmd card-trust`. `CARD_TRUST=trust_id` selects a recovery lane.
A bounded canary requires the explicit
`CARD_TRUST_ARGS="--max-filings-per-trust 1 --allow-partial"` gate and does not claim complete
coverage.

The live lake contains 36 unique monthly aggregates from July 2025 through June 2026: twelve per
trust. Every row retains the accession, primary and exhibit documents, exact source metric rows,
source and archive-index checksums, and evidence hash. Receivables, principal payment rate,
portfolio yield, and charge-off rate are reported for every month. Amex and BA report dollar
charge-offs for 24 trust-months. Citi reports its Credit Loss Component rate but no pool-level
dollar charge-off in the selected exhibit, so that amount remains null with status
`NOT_REPORTED`; DFRI does not derive one. BA's disclosed charge-off table is explicitly in
thousands of dollars, and its registered scale is applied before storage. The actual Amex 10-D
fixture pins the complete source checksum and the normalized fixture checksum. An unchanged live
replay appended zero aggregate batches.

## Board history backfill

The Board backfill reads the official G.19 and H.8 `releaseDates.json` manifests, fetches each
dated release with 0.5-second pacing, and writes one immutable Parquet batch per release. It
stores actual page-declared release timestamps, including the small set of live-verified Board
manifest/path exceptions pinned in the series registry. No FRED or ALFRED endpoint is used.

```sh
make board-backfill
```

The default window begins on 2015-01-01. A bounded canary or recovery run can be selected
without changing code:

```sh
make board-backfill BOARD_RELEASE=g19 BOARD_START=2024-01-01 BOARD_ARGS="--max-items 1"
```

The command keeps an atomic checkpoint and append-only event receipts under ignored `.local/`
state. Failed pages remain `FAILED` with evidence; a rerun skips completed batches and retries
only failed or missing checkpoints. A complete unbounded run also validates manifest coverage,
row counts, series composition, checksums, units, release/vintage timestamps, duplicates, and
first-print selection before returning success.

The separate current-snapshot command ingests the Board DDP ZIPs from 2015 onward as revised
context. It checks the release manifest both before and after each fetch, rejects stale or
non-final data, and never labels current revised history as a dated first print:

```sh
make board-snapshot
```

Current snapshots and dated archive rows share the strict raw schema but have distinct source
URLs and independent validators. Repeating an unchanged snapshot is idempotent by URL and
authoritative data checksum. BEA checksums cover canonicalized data rows rather than the
volatile API response envelope.

## Board first-print nowcast targets

The M2 target backfill reads each immutable dated G.19 page and subtracts the prior-month
seasonally adjusted level from the preliminary target-month level shown in that same release.
This release-coherent calculation is the Board replacement for an ALFRED as-of-vintage
`ΔREVOLSL` or `ΔNONREVSL`; subtracting consecutive preliminary prints is prohibited because it
would discard revisions visible when the target was first released.

```sh
make board-targets
```

The Windows equivalent is `make.cmd board-targets`. The command is paced, checkpointed,
append-only, and resumable. It writes the pinned derived series `DELTA_DTCTLR.M` and
`DELTA_DTCTLN.M` with the exact dated page URL, checksum, release timestamp, and target month.
The checksum covers canonical parsed level/flow evidence rather than volatile page-wrapper HTML.
The complete run validates page coverage and monthly continuity before success. Model code can
read these rows only through the Vintage Guard.

## BEA and Census histories

The context-history command reads the ignored `.env` without displaying credentials, ingests
BEA 2.4.5U product detail plus broad PCE/personal-income series, and ingests six Census MARTS
retail categories from 2015 onward:

```sh
make context-history
```

These APIs expose revised histories rather than first-print vintages. DFRI therefore records
the retrieval timestamp as both the release boundary and snapshot vintage; it never invents
historical availability dates. The shared Vintage Guard consequently excludes these snapshots
from any earlier as-of read. Source URLs in the lake omit API-key query parameters.

The nowcast uses a separate point-in-time Census lane. `make census-archive` discovers the
official dated MARTS release PDFs, parses each release's exact seasonally adjusted Table 1 total,
and writes `DELTA_RETAIL_SALES.M` as the advance target-month level minus the preliminary
prior-month level from that same PDF. The archive is continuous from January 2015 through its
latest listed release and needs no API key. PDF release timestamps are interpreted at the stated
8:30 a.m. Eastern boundary; file checksums, URLs, and retrieval timestamps remain attached to
each immutable row. Revised API history is never assigned a fictional historical release date.

On Windows, use `make.cmd census-archive`. Set `CENSUS_ARCHIVE_ARGS=--recheck-complete` to
refetch and byte-check every already stored dated artifact.

## Ragged-edge bridge model

The deterministic bridge model predicts each monthly first-print G.19 flow from the matching
H.8 weekly loan changes, the first-print MARTS retail flow when it was already public, and month
seasonality. Model code obtains every observation through the Vintage Guard. H.8 changes use
only dated Board release pages, choose the latest vintage available at the forecast timestamp,
and expose observed/expected Wednesday counts, coverage, and a coverage-paced monthly change.
A missing retail release or a month with no available H.8 week remains explicitly missing; the
model uses training-window mean imputation and availability indicators rather than inventing an
observation. Production version `bridge-ridge-v2-alpha10` uses an alpha-10 ridge penalty selected
after the first local ragged-edge run exposed extreme missing-retail leverage in v1. The v2
backtest improved primary MAE while returning finite, operationally meaningful live bands.

Each forecast carries its model version, training count, forecast timestamp, evidence-sensitive
input hash, point estimate, and 80/95% prediction bands. Historical evaluation uses only first
prints and strictly prior training months. The state-space candidate and the reproducible M2
model-selection report remain separate gates; the bridge is not yet claimed as the headline
model.

The mixed-frequency candidate retains each available H.8 Wednesday change as its own measurement
of a latent monthly G.19 flow in a statsmodels Kalman filter. The target, weekly H.8, and retail
measurements are scaled only from the strictly prior training window; unavailable weekly slots
are `NaN` observations handled by the filter. A seasonal AR(1) state transition advances months,
and the first-print G.19 target is absent for the forecast month. The candidate did not beat the
bridge on primary revolving-credit MAE, so it is not eligible to replace the bridge there under
the prescribed model-selection rule.

## Reproducible M2 backtest

[`reports/M2_BACKTEST.md`](reports/M2_BACKTEST.md) is the stable human report and
[`reports/m2_backtest.json`](reports/m2_backtest.json) is its canonical machine-readable
counterpart. Regenerate both from the append-only local lake with:

```sh
make backtest
```

On Windows, use `make.cmd backtest`. Override `BACKTEST_AS_OF`, `BACKTEST_OUTPUT`, or
`BACKTEST_MARKDOWN` to evaluate a different explicit boundary without silently changing the
committed report. The command reconstructs all five model versions, requires an identical
January 2018-to-latest period set, uses three-way acceleration signs, and applies the §6.2/§6.3
selection bars in code. It does not claim that the scoreboard is live.

## Immutable scoreboard ledgers

Predictions and grades are separate append-only Parquet ledgers. A prediction ID is a stable
SHA-256 identity over its model version, input hash, target series, and target period. The first
accepted job execution becomes the immutable `made_at`; a later retry with the same inputs is a
no-op that preserves that timestamp. The point and nested 80/95% bands are immutable under the
ID, and changed content raises an immutable-prediction error. A short-lived atomic lock
serializes writers and leaves an inspectable lock path when a writer cannot proceed.

Grades never rewrite prediction rows. Once the matching release-coherent first-print G.19 target
exists, the grading ledger records its exact dated Board URL, actual value, absolute error, and
release timestamp. Re-grading against the raw first-print target must reproduce every stored
grade exactly. First live publication metadata is a third append-only ledger: the initial
`published_at`, input `data_vintage`, and methodology version for a prediction survive every later
site rebuild.

Prediction points and interval bounds are canonicalized to nine decimal places in their declared
million-dollar unit before the first append. That boundary is one-thousandth of a dollar and
removes irrelevant cross-runner BLAS noise while the ledger still rejects any economically
meaningful content change under an existing prediction ID. Pre-boundary rows remain immutable;
retries compare their canonical form without rewriting them.

The idempotent local job commands are:

```sh
make scoreboard-predict
make scoreboard-grade
```

On Windows, use `make.cmd scoreboard-predict` and `make.cmd scoreboard-grade`. The current UTC
execution time is used by default; `SCOREBOARD_ARGS=--as-of <ISO timestamp>` supplies an explicit
test/replay boundary. The prediction job uses the latest stored dated H.8 release for its input
vintage and emits every unreleased target month through that release's latest Wednesday, while
`made_at` records the actual first execution. The grade job only appends when the matching
first-print target is available and then re-verifies every stored grade. Each attempt writes a
content-addressed ignored receipt under `.local/evidence/scoreboard_jobs`. These commands pass
locally; external scheduling and public hosting remain separate gates.

## Scoreboard feeds and static site

The deterministic publisher reads the append-only prediction and grade ledgers and creates one
deployable document root under `published/public`. It contains the stable `/v1/feeds` JSON, CSV,
and Parquet contracts; schema documentation; Home, Scoreboard, Methodology, and immutable
prediction-permalink pages; local assets; and a SHA-256 manifest. Feed rows carry the methodology
version, input-data vintage, publication timestamp, publication mode, and CC BY-NC 4.0 notice.
The server-rendered pages remain complete without JavaScript; the small script only enhances table
sorting.

An explicit data vintage is publication-blocking:

```sh
PUBLISH_ARGS="--data-vintage 2026-07-31T20:15:00+00:00" make publish-scoreboard
```

On Windows, set `PUBLISH_ARGS` before `make.cmd publish-scoreboard`. `--published-at` pins a
reproducible build time; otherwise the actual UTC execution time is used. `--publication-mode
live` removes the preview warning. The recovery-only `--minimum-made-at` boundary can exclude
known pre-public engineering rows without changing the ledger and reports the excluded count in
the feed and UI. A clean production ledger must not need that option.

Publishing always builds in a disposable sibling directory and atomically promotes a complete
artifact. A destination without a publisher manifest is treated as user-managed and is never
overwritten. Two identical local builds over the corrected four-row v2 preview produced the same
manifest hash, `3793b59199975166b89ada7be0355489b99c0ae32ef87fd7839f823d44414be7`,
and a 57,168-byte artifact. The eight earlier incorrectly timestamped smoke rows were explicitly
excluded and are not live-cycle evidence. Generated publication files remain ignored. The first
manual public bootstrap produced a separate live four-row ledger and deployed all 15
manifest-listed files with matching byte lengths and SHA-256 hashes; it is not live-cycle evidence.

The active default-branch clock definition is `.github/workflows/m2-scoreboard.yml`. It checks for
first-print grades on weekdays after the Board's 3:00 p.m. Eastern G.19 window and checks for new
H.8 input on weekdays after the 4:15 p.m. Eastern window, covering both ordinary Fridays and
holiday-shifted releases. Stable input identity makes non-release days no-ops. A single
non-cancelling concurrency gate protects the ledger, deployment occurs only after an append, and a
post-deployment receipt enforces the four-hour SLA. Disposable runners recover only from a
SHA-256-verified, deployment-accepted, allowlisted state artifact; changed state remains a
candidate until Pages succeeds. The one permitted empty-state bootstrap has completed and must not
be enabled again. See
[`ops/M2_SCOREBOARD.md`](ops/M2_SCOREBOARD.md) for activation, pause, retry, and recovery rules.

## Read-only API and publication monitoring

M4 defines exactly nine unauthenticated GET endpoints under `/v1`, generated from the immutable
Parquet/JSON publication directory. The committed contract is `docs/openapi-v1.json`; ETags,
cache headers, open GET CORS, and a fixed 60-requests/minute/IP boundary are enforced in the app.
The cold publication benchmark requires p95 below 300 ms.

Run it locally with:

```sh
uv run python -m dfri.api.app --publication-root published/public
```

The hourly `.github/workflows/m4-uptime.yml` job retains a JSON log for every attempt. It currently
checks the live Pages site, stable feeds, and nowcast freshness. The API portion remains explicitly
`BLOCKED_NOT_CONFIGURED` until a durable public FastAPI base URL is added as the repository variable
`DFRI_API_BASE_URL`; no static or vendor substitute is claimed as the API. See
[`ops/M4_PUBLICATION.md`](ops/M4_PUBLICATION.md) for the complete recovery contract.

## New York Fed HHDC history

The NY Fed history command discovers the current official Household Debt and Credit workbook,
verifies its report period and registered sheet contracts, and ingests quarterly balances,
total mortgage/auto originations, and transitions into 30+ and 90+ day delinquency from 2015:

```sh
make nyfed-history
```

The Windows equivalent is `make.cmd nyfed-history`; `NYFED_START=YYYY-MM-DD` changes the start
quarter. This source needs no API key. The current live Q1 2026 snapshot contains 945 rows across
21 series, and an unchanged second run appends nothing. Because the workbook is a revised
retrieval-time snapshot rather than a release-vintage archive, DFRI records retrieval time as
both release boundary and vintage and does not imply historical availability.

Source attribution: New York Fed Consumer Credit Panel / Equifax. DFRI derivatives remain
clearly labeled under the [NY Fed terms of use](https://www.newyorkfed.org/privacy/termsofuse.html);
the current workbook is discovered from the [HHDC page](https://www.newyorkfed.org/householdcredit/hhdc-iframe).

## Freshness precursor

The local read-only health precursor evaluates complete entity/series coverage and watermarks
against the checked-in release calendar, source-specific maximum ages, and explicit post-release
grace periods:

```sh
make health
```

The Windows equivalent is `make.cmd health`. Its secret-free JSON report uses exactly `GREEN`,
`STALE`, `BLOCKED`, and `OPTIONAL-DEGRADED`; it includes the source watermark, latest due event,
next expected event, and registered/observed entity counts. A historical `--as-of` timestamp
excludes future ingests. The current canonical lake is green across ten lanes: five macro sources,
17 SEC XBRL issuers, the required HTML filing fallback, five active Auto ABS trusts, one terminal
Auto ABS history, and three card 10-D trusts. This is the complete M1 `/v1/health` precursor. The
M4 FastAPI endpoint is implemented and verified over published data, but its durable public host
remains an open release gate.

## Live spot audit

The credential-backed quality gate runs every completed production parser in a disposable lake,
then uses a fixed seed to compare 20 canonical stored rows with exact live source identities:

```sh
make spot-audit
```

The Windows equivalent is `make.cmd spot-audit`. It reads `BEA_API_KEY` and `CENSUS_API_KEY`
from the process environment (or the ignored `.env` through the make target), rejects any
credential-bearing source URL, and writes its detailed receipt under ignored `.local/evidence/`.
Stdout contains only a compact summary. Each run covers Board G.19, Board H.8, BEA, Census, and
NY Fed. A changed source identity returns `BLOCKED`; an equal-identity value mismatch returns
`FAIL`. The current live gate passed 20 of 20 sampled rows with zero mismatches after the changed
Census response was appended as a new immutable snapshot.

## Repository boundaries

- `src/dfri/`: application and pipeline code
- `tests/`: unit, property, integration, and legally redistributable minimal fixtures
- `lake/`: append-only local data, ignored
- `published/`: versioned generated outputs, ignored until intentionally promoted
- `site/`: static templates and assets
- `docs/`: methodology, assumptions, and ADRs
- `ops/`: CI, scheduling, and runbooks
- `MILESTONE_REPORTS/`: stable acceptance reports only

Missing source data is recorded as `BLOCKED` with evidence. It is never fabricated or silently interpolated.
