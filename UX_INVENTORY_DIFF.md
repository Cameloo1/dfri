# UX inventory diff

Status: **PASS — EVERY LOSS DISPOSITIONED**

- Before inventory: `UX_INVENTORY_BEFORE.md`
- After inventory: `UX_INVENTORY_AFTER.md`
- Routes: 62 before, 63 after
- Disclosure-expanded information lines: 4343 before, 4415 after
- Distinct information lines: 1336 before, 1358 after
- Distinct outbound targets: 62 before, 62 after

## Route changes

Missing routes (0):
- None

Added routes (1):
- /companies/

## Information-line changes

Missing distinct lines (4):
- 50 covered companies, alphabetically.
- Covered companies ranked by DFR% midpoint divided by their same-period pure-fungibility baseline.
- DFRI estimates the share of covered companies' U.S. consumer revenue funded by new consumer credit. It also predicts each month's change in U.S. consumer borrowing before the Federal Reserve publishes the official G.19 figure, records that forecast with a timestamp, and grades it against the first print.
- Full ledger

Added distinct lines (26):
- 2026-08-09 · publication · 1.1.1
- 50 covered companies, alphabetically by ticker.
- Across the same publication, covered-company midpoint estimates range from 1.45% at Booking Holdings to 18.67% at Carvana. The 3.36% headline is revenue-weighted, not a threshold or a typical-company score.
- Alphabetical directory of all 50 covered companies. Each record keeps the estimated DFR% band with its evidence tiers, assumptions, and source links.
- Browse all 50 company estimates →
- Companies
- Companies with evidence above the same-period pure-fungibility baseline, ranked by DFR% midpoint divided by that baseline.
- Companies · Prediction ledger · Evidence Lift
- Company directory and ledger path clarified
- Complete baseline-only group, retaining every rank, estimated DFR% band, and Tier 1/2/3 split.
- Current estimate
- DFRI publishes two linked records. It estimates the share of covered companies' U.S. consumer revenue funded by new consumer credit. It also predicts each month's change in U.S. consumer borrowing before the Federal Reserve publishes the official G.19 figure, records that forecast with a timestamp, and grades it against the first print.
- Each forecast is timestamped before the official G.19 release and kept as an immutable record. Grades use the first published figure, never a later revision.
- Every estimate separates directly observed financing links from category mappings and proportional allocation.
- Historical backtests compare the nowcast with explicit naive benchmarks; they are not live-grade results.
- How much is known
- Live grade record
- Live-grade statistics remain separate from the historical backtest. Read every prediction, grade, and the published first miss.
- Model record
- No company-specific financing evidence found; each estimate reflects proportional allocation. Their indistinguishability is the finding, so the full rows remain available here.
- Open the full ledger
- Prediction ledger
- Separated the homepage claims into an ordered reading path, promoted the immutable prediction ledger, grouped 38 indistinguishable baseline-only Evidence Lift rows behind a native disclosure, and added a complete alphabetical company directory without changing any estimate or feed.
- Show 38 baseline-only companies at 1.00x
- Skip to main content
- The complete alphabetical directory keeps all 50 company estimates, bands, evidence tiers, assumptions, and source links one step away.

The repeated baseline interpretation sentence appears 38 time(s) on the old homepage and 0 time(s) on the new homepage.

## Link-target changes

Missing normalized internal targets (1):
- /index.html#companies

Added normalized internal targets (67):
- /#main-content
- /#prediction-ledger
- /changelog/#main-content
- /changelog/#ux-information-architecture
- /companies
- /companies/#main-content
- /companies/abnb/#main-content
- /companies/amzn/#main-content
- /companies/azo/#main-content
- /companies/bby/#main-content
- /companies/bkng/#main-content
- /companies/casy/#main-content
- /companies/ccl/#main-content
- /companies/cmg/#main-content
- /companies/cost/#main-content
- /companies/cvna/#main-content
- /companies/dash/#main-content
- /companies/deck/#main-content
- /companies/dg/#main-content
- /companies/dltr/#main-content
- /companies/dpz/#main-content
- /companies/dri/#main-content
- /companies/ebay/#main-content
- /companies/el/#main-content
- /companies/expe/#main-content
- /companies/f/#main-content
- /companies/gm/#main-content
- /companies/grmn/#main-content
- /companies/has/#main-content
- /companies/hd/#main-content
- /companies/hlt/#main-content
- /companies/index.html
- /companies/kr/#main-content
- /companies/low/#main-content
- /companies/lulu/#main-content
- /companies/mar/#main-content
- /companies/mcd/#main-content
- /companies/mgm/#main-content
- /companies/nclh/#main-content
- /companies/nke/#main-content
- /companies/orly/#main-content
- /companies/pep/#main-content
- /companies/pg/#main-content
- /companies/rcl/#main-content
- /companies/rl/#main-content
- /companies/rost/#main-content
- /companies/sbux/#main-content
- /companies/tgt/#main-content
- /companies/tjx/#main-content
- /companies/tpr/#main-content
- /companies/tsco/#main-content
- /companies/tsla/#main-content
- /companies/ulta/#main-content
- /companies/wmt/#main-content
- /companies/wsm/#main-content
- /companies/wynn/#main-content
- /companies/yum/#main-content
- /methodology/#main-content
- /methodology/coverage/#main-content
- /methodology/sensitivity/#main-content
- /scoreboard/#main-content
- /scoreboard/predictions/prd_4613c5a7c6b9b652464e7c3230252f46a9fbf90f1b6d5c24b57244bba5a89966/#main-content
- /scoreboard/predictions/prd_6177c0c4e9f1c076686adb4cd5e4ead039b995aa246337288b9411c706e1a9dc/#main-content
- /scoreboard/predictions/prd_680ac2b8c3b5e47edcf306e2f1089e50d6bbfbaf50e83592563cb3e8ee5d66e0/#main-content
- /scoreboard/predictions/prd_89cff4ef13c82b4bccf6ded44e63c4d028bb1635da8c4890edbb9f7db44f86af/#main-content
- /scoreboard/predictions/prd_c589a3d29179066784b619fc64987dcc9929c5cedff496ff4d7ca3472d543e44/#main-content
- /scoreboard/predictions/prd_f180179664ad5f7d7df1c53740dc939220dbda78e29303b85e7d714454537d93/#main-content

Missing outbound targets (0):
- None

Added outbound targets (0):
- None

## Disposition of every reported loss

The machine diff reports no missing route and no changed outbound target. All 50 Evidence Lift row
lines compare exactly before and after when the native disclosure is expanded: 50 before, 50 after,
zero changed. The four missing distinct lines are copy replacements, not information losses:

1. `50 covered companies, alphabetically.` is now `50 covered companies, alphabetically by
   ticker.` on `/companies/`. The added phrase states the actual sort key.
2. `Covered companies ranked by DFR% midpoint divided by their same-period pure-fungibility
   baseline.` is replaced by the more precise split: the default table says it contains companies
   above baseline, while the native disclosure says it retains the complete baseline-only group,
   every rank, band, and Tier 1/2/3 split.
3. The original one-sentence project description is retained verbatim after the new orienting clause
   `DFRI publishes two linked records.` The diff treats the combined line as new because its prefix
   changed.
4. The link label `Full ledger` is now the literal action `Open the full ledger`; its destination
   remains `/scoreboard/`.

The old homepage repeated its baseline interpretation sentence 38 times. The new homepage replaces
those repetitions with one group-level explanation and retains all 38 data rows inside a native
`details` disclosure. This is the explicitly justified consolidation in `UX_PLAN.md`, not a loss of
access.

The sole missing internal *link target*, `/index.html#companies`, is the intentionally replaced nav
destination. It is not a missing URL: `/index.html` still returns HTTP 200 directly with no redirect,
and the `#companies` target remains in the document. The page now links onward to the complete
`/companies/` directory. Sixty-seven added internal targets comprise that directory, the prediction
ledger anchor, and first-focus skip targets on every rendered page.
