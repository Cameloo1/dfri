Exit code: 0
Wall time: 0.8 seconds
Output:
# DFRI — Expansion Specification v2.0
## Multi-stream borrowed-revenue attribution + interactive flow views

**Owner:** Camelon Systems (Wasif) · **Executor:** autonomous agent loop
**Supersedes nothing.** This document ADDS to `DFRI_BUILD_SPEC.md`. Every principle, constraint, and operating rule in that document remains in force. Where this document is silent, the original spec governs.

---

## 0. Why this exists

DFRI today answers one question well: *what share of a company's US consumer revenue is funded by new consumer credit?*

That question is real but narrow. Consumer credit is the **smallest** of the three debt streams that fund US company revenue:

| Stream | Approximate scale | Evidence quality available |
|---|---|---|
| Household credit (current DFRI) | ~$5T revolving + nonrevolving of ~$18T household debt | Mixed: Tier 1 for captives/PLCC, Tier 3 for most |
| Federal deficit spending | ~$36T debt, ~$2T annual deficit, ~1/3 of outlays deficit-financed | **Strongest** — recipients are named in public data |
| Nonfinancial business debt | ~$21T | **Weakest** — attribution to a seller's revenue is mostly unassignable |

The expansion generalizes DFRI from a single-stream consumer index into a **multi-stream borrowed-revenue index**, with the federal stream as the highest-value addition because USASpending names the company receiving the money.

The end-state artifact: for any covered company, a revenue decomposition — *estimated X% consumer-credit-funded, Y% federal-deficit-funded, Z% business-debt-funded, remainder unborrowed* — every component carrying its own band, tier mix, and provenance.

---

## 1. Non-negotiable principles (additions to Spec §1)

All original §1 principles apply unchanged. These are additional and equally binding.

1. **Source legality is checked BEFORE design work, not after.** Two sources have already been rejected on terms grounds (FRED, FINRA). For every source named in this document, the terms check is the first task, its result is recorded in the series registry with a terms URL and `verified_at`, and no ingest code is written until it passes. A source whose terms prohibit automated retrieval, storage, or redistribution of derivatives is excluded regardless of how valuable it would be. Report and continue on other tracks; never proceed on assumption.
2. **Streams are never silently summed.** Different streams fund different revenue dollars. Any aggregation across streams must be proven to have disjoint numerators against a common denominator, or must not be aggregated at all. A composite number whose components double-count is worse than publishing no composite.
3. **Observed receipt ≠ observed financing.** A federal award received by a company is Tier 1 observed. The *share of that award financed by deficit rather than taxation* is a fungibility assumption and is Tier 2 at best. This distinction must be preserved everywhere in code, storage, and display. Collapsing them would repeat exactly the error the tier system exists to prevent.
4. **Published DFR% values do not change.** DFR% keeps its current definition and denominator. New cross-stream measures are new metrics with new names, published alongside. Existing feeds, permalinks, and company page figures remain stable. Any change to an already-published number is a restatement requiring a changelog entry and is presumed a bug until proven otherwise.
5. **Interactivity never gates access.** Every flow view must be complete and readable as server-rendered static SVG at its own URL with JavaScript disabled. JS may only swap views without navigation and add highlight-on-hover. If a view cannot be understood without JS, it is not shippable.
6. **No milestone closes on a partial checklist.** Iterate until every acceptance criterion passes from a fresh clone via the documented make targets. A failing AC is a reason to keep working, not a reason to write a report with caveats.

---

## 2. Known hard truths (do not burn cycles fighting these)

- **Most of the current 50 covered companies receive near-zero federal awards.** Starbucks and Chipotle will show a federal stream of ~0. The federal stream is only interesting if the covered universe expands to include federally-exposed names (§6, P3 cohort). Expect and plan for this.
- **USASpending has a documented double-counting hazard**: prime awards and subawards must not be added together, and the same company appears at parent (`-P`), child (`-C`), and recognized-entity (`-R`) recipient levels. Match on UEI and roll up through `recipient_parent_uei`. Use prime awards only for Tier 1 attribution.
- **The USASpending paginated search endpoints cap at ~10,000 records**; some fields return NULL on `spending_by_award` regardless of the `fields` array. Use the bulk download endpoint for volume and read an example response before assuming a field is populated.
- **Federal fiscal years do not align with company fiscal quarters.** Period mapping is an explicit, documented assumption, not an implementation detail.
- **Federal money also reaches companies indirectly** (transfer payments funding consumer spending; Medicare paying providers who buy from suppliers). Only direct prime awards are Tier 1. Indirect paths are Tier 2/3 or out of scope — decide explicitly and document.
- **Business-debt-funded revenue may not be tractable at all.** A company borrowing does not make its own revenue borrowed; what matters is whether its *customers* borrowed. For B2B revenue this is mostly unassignable. This stream may honestly end as a context series rather than an attributed stream. That is an acceptable outcome (§6, M10).
- **The NY Fed Household Debt and Credit report is built on proprietary Equifax panel data and is published by a Reserve Bank, not the Board.** This is structurally the same risk profile as FRED. It is already ingested. Auditing it is a priority task (§6, M6).
- **Federal transfer payments to individuals have no UEI** (PII protection) and appear as aggregate records. They cannot be attributed to companies directly.

---

## 3. Architecture change — the stream dimension

### 3.1 The generalization

Today the attribution engine computes one thing. It becomes a **dispatcher over streams**, where each stream supplies its own flow source and attribution method but conforms to a common output contract.

```
                    ┌─────────────────────────────────────┐
                    │  STREAM REGISTRY                    │
                    │  id, name, flow_source,             │
                    │  attribution_method, denominator,   │
                    │  max_tier, active, methodology_ver  │
                    └──────────────┬──────────────────────┘
                                   │
        ┌──────────────────────────┼──────────────────────────┐
        ▼                          ▼                          ▼
┌────────────────┐      ┌────────────────────┐      ┌──────────────────┐
│ household_     │      │ federal_deficit    │      │ business_debt    │
│ credit         │      │                    │      │ (exploratory)    │
│                │      │ Awards (T1) ×      │      │                  │
│ flow × A × B   │      │ deficit share (T2) │      │ TBD / may be     │
│ (unchanged)    │      │ ÷ total revenue    │      │ context-only     │
└───────┬────────┘      └─────────┬──────────┘      └────────┬─────────┘
        └────────────────────────┬┴──────────────────────────┘
                                 ▼
              ┌──────────────────────────────────────┐
              │ COMMON OUTPUT CONTRACT               │
              │ (ticker, period, stream)             │
              │ → numerator band, denominator,       │
              │   tier shares, assumption_ids,       │
              │   evidence_refs, methodology_version │
              └──────────────────┬───────────────────┘
                                 ▼
              ┌──────────────────────────────────────┐
              │ COMPOSITION LAYER                     │
              │ disjointness proof → BFR% bands       │
              │ Monte Carlo across streams jointly    │
              └──────────────────────────────────────┘
```

### 3.2 The two metrics

- **DFR%** — unchanged. `household_credit` numerator ÷ estimated US consumer revenue. Existing definition, existing published values, existing feed fields. Do not touch.
- **BFR%** (Borrowed-Funded Revenue share) — **new**. Sum of all active stream numerators ÷ **total company revenue**. Published as a band with a per-stream decomposition and per-stream tier mix. This is the metric that supports cross-stream comparison, because every stream shares one denominator.

Both are published. DFR% remains the consumer-specific measure; BFR% is the composite. The homepage headline may present either or both, but must never present a number that mixes denominators.

### 3.3 Disjointness

Before any BFR% is published, the pipeline must assert that stream numerators do not overlap:
- `household_credit` numerator covers revenue from US consumers who financed purchases with new consumer credit.
- `federal_deficit` numerator covers revenue from direct federal prime awards.
- These are disjoint by construction *if and only if* federal award revenue is excluded from the consumer revenue denominator and from Matrix B allocation. **Verify this holds; if it does not, fix the allocation or do not publish BFR%.**
- Where disjointness cannot be proven for a company (e.g. a healthcare name with both consumer out-of-pocket and Medicare revenue routed through the same segment), flag that company `COMPOSITION_UNVERIFIED` and publish its streams separately without a composite.

### 3.4 Methodology versioning

This expansion bumps methodology to **2.0.0**. Rules:
- Existing predictions and grades are never re-versioned. They keep the version that produced them.
- Existing DFR% values must reproduce byte-identically under 2.0.0. If they do not, that is a release-blocking bug, not a restatement.
- The methodology page must present versions side by side with what changed.

---

## 4. Source contracts

**Universal rules (inherited from Spec §4):** runtime verification of title/units against the registry; record `source_url`, `release_date`, `vintage_date`, `ingested_at`, checksum; rate-limit with backoff; idempotent; every identifier below is a best-known value that MUST be verified against the live source before first use and pinned with `verified_at`.

**New universal rule:** every source gets a `terms_status` field in the registry — one of `PERMITTED`, `BLOCKED-LEGAL`, `UNVERIFIED` — plus `terms_url` and a one-line summary of what the terms permit regarding automated retrieval, storage, and redistribution of derivatives. Nothing with `UNVERIFIED` may be ingested.

### 4.1 Federal stream (M7)

| Source | Provides | Cadence | Expected status |
|---|---|---|---|
| **USASpending API** (`api.usaspending.gov`) | Prime award obligations by recipient UEI, with parent/child hierarchy, agency, NAICS, dates | Continuous | DATA Act open data, no auth |
| **USASpending bulk download** (`/api/v2/download/awards/`) | Volume retrieval past the ~10k search cap | On demand | Same |
| **Treasury Fiscal Data API** (`fiscaldata.treasury.gov`) | Monthly Treasury Statement: receipts, outlays, deficit; Debt to the Penny | Monthly / daily | Federal, public domain |
| **Fed Z.1** | Federal government debt levels for context | Quarterly | Board, public domain |

Deficit share of outlays derives from MTS: `deficit ÷ total outlays` for the matched period. This ratio is the Tier 2 assumption in §1.3 and must live in the assumption registry with a documented sensitivity note.

### 4.2 Household gap-fill (M9)

| Source | Provides | Cadence | Notes |
|---|---|---|---|
| **HMDA** (CFPB/FFIEC) | Loan-level mortgage applications and originations: amount, purpose, lender | Annual + quarterly | Verify terms and bulk access path |
| **FFIEC Call Reports** (CDR) | Bank loan balances by standardized category — separates auto from other consumer | Quarterly | Verify bulk download terms |
| **NCUA Call Reports** | Credit union loan balances — material share of auto lending | Quarterly | Federal agency |
| **Federal Student Aid Data Center** | Federal student loan portfolio and originations | Quarterly | Dept. of Education, public domain |
| **SBA 7(a) / 504 data** | Loan-level small business lending, borrower-named | Ongoing | Public domain |
| **Fed Z.1 L.101** | Household liabilities incl. mortgage, consumer credit, security credit | Quarterly | Board, public domain |
| BNPL | Affirm/Klarna/Block filings; CFPB BNPL market reports | Quarterly | Tier 1 only; no public aggregate exists |
| **Fannie/Freddie loan-level** | Mortgage performance detail | Monthly | **Likely blocked** — registration/agreement typically required. Check terms first; expect exclusion |

### 4.3 Business debt (M10, exploratory)

Fed Z.1 nonfinancial corporate debt; H.8 C&I loans; per-company debt and interest expense from existing SEC XBRL ingest. All Board or EDGAR — no new terms risk.

### 4.4 Priority audit — existing source

**NY Fed Household Debt and Credit.** Already ingested in M1. Equifax-derived, Reserve Bank publication. Audit its terms with the same rigor applied to FRED and FINRA. If prohibited, mark `BLOCKED-LEGAL`, purge derived rows, and report what breaks — do this before any expansion work, because it may already be a live exposure.

---

## 5. Data model additions

- `stream_registry(stream_id, display_name, flow_source, attribution_method, denominator_basis, max_tier_achievable, active, introduced_version, terms_status)`
- `stream_flow(stream_id, period, product, value, unit, release_date, vintage_date, source_url)` — the measured flow feeding each stream
- `federal_awards(period, ticker, recipient_uei, recipient_parent_uei, recipient_level, awarding_agency, naics, obligation_amount, award_type, accession_or_award_id, source_url, ingested_at)` — prime awards only; subawards stored separately and never summed with primes
- `uei_ticker_map(recipient_parent_uei, ticker, method, evidence_url, assumption_id, verified_at, review_status)` — every mapping is an assumption with an ID
- `stream_output(ticker, period, stream_id, numerator_low, numerator_mid, numerator_high, denominator, share_low, share_mid, share_high, tier1_share, tier2_share, tier3_share, assumption_ids, evidence_refs, methodology_version, data_vintage, published_at)` — append-only
- `bfr_output(ticker, period, bfr_low, bfr_mid, bfr_high, stream_decomposition, disjointness_status, methodology_version, data_vintage, published_at)` — append-only; `disjointness_status` ∈ `VERIFIED` / `COMPOSITION_UNVERIFIED`
- `flow_view(view_id, period, nodes, edges, tier_per_edge, value_per_edge, rendered_at)` — precomputed view data driving both static SVG and JS enhancement, so both read identical numbers

Existing tables (`predictions`, `grades`, `matrix_a`, `matrix_b`, `assumptions`, `dfri_output`) are unchanged in shape and content.

---

## 6. Milestones and acceptance criteria

**Sequencing:** M4 must close before M6 begins. M2's live-cycle clock continues running throughout and is never interrupted. Each milestone ends with `MILESTONE_REPORTS/M{n}.md` containing the AC checklist with pass/fail, evidence links, and open deviations.

**Universal ACs applying to every milestone below** (in addition to milestone-specific ones):
- Full test suite passes at or above prior coverage; report both numbers.
- `make replay` produces byte-identical published output.
- Cold-clone verification passes end to end via documented make targets.
- Prediction ledger, grades, and publication records byte-identical before and after.
- All existing URLs and permalinks resolve with no new redirects.
- Provenance link checker green.
- Page weight <500 KB, load <1 s on 4G, WCAG AA, no-JS functional, mobile-first — on every page including new ones.
- CI rule-compliance assertions (units present, tier badges present, bands never bare, tier never color-only) pass on all new surfaces.
- No fabricated, placeholder, or interpolated values anywhere. `BLOCKED` with evidence is the only acceptable substitute for a missing number.

---

### M6 — Multi-stream architecture refactor + source legality audit
*No new data. Pure refactor plus audit. The whole point is proving nothing changed.*

**AC6.1** NY Fed HHDC terms audit complete: `terms_status` recorded with terms URL and summary. If `BLOCKED-LEGAL`, derived rows purged, downstream impact reported, and any affected published values restated with a changelog entry.
**AC6.2** Every existing source in the registry has `terms_status`, `terms_url`, and a permissions summary. Zero sources remain `UNVERIFIED`.
**AC6.3** `stream_registry` exists; `household_credit` is registered as the first stream with its current attribution method, and all existing computation routes through the stream dispatcher.
**AC6.4** **Byte-identity proof**: every published DFR% low/mid/high, tier share, aggregate index value, and feed payload is byte-identical before and after the refactor. Any difference is a bug and blocks the milestone.
**AC6.5** `stream_output` populated for `household_credit` for all covered companies and periods, reproducing existing values exactly.
**AC6.6** BFR% scaffolding exists and is computable but is NOT published while only one stream is active (a one-stream BFR% is a denominator change dressed as a new metric).
**AC6.7** Disjointness checker implemented with unit tests covering the overlapping-revenue case, verified failing on a synthetic overlap fixture.
**AC6.8** Methodology 2.0.0 documented with a side-by-side version comparison; `docs/MODELING.md` updated to describe the stream architecture.
**AC6.9** Property tests: stream tier shares sum to 1±1e-9; bands ordered; no stream may claim a tier above its registered `max_tier_achievable`.

---

### M7 — Federal deficit-funded revenue stream

**AC7.1** USASpending and Treasury Fiscal Data terms verified `PERMITTED` and registered before any ingest code is written.
**AC7.2** USASpending client implemented with: UEI-based identity, parent/child rollup via `recipient_parent_uei`, prime awards only, bulk-download path for volume, documented handling of the ~10k pagination cap, and fixtures from real archived API responses.
**AC7.3** **Double-counting guard**: automated test proving primes and subawards are never summed, and that a company appearing at `-P`, `-C`, and `-R` levels is counted exactly once. Include a fixture with a known multi-level recipient.
**AC7.4** Treasury MTS ingested; deficit share of outlays computed per period and registered as an assumption with ID, source, value, and sensitivity note.
**AC7.5** **Tier separation enforced in code**: award receipt is Tier 1; deficit share is Tier 2. A test asserts that no federal stream output claims Tier 1 for the deficit-share component. This is the §1.3 principle and its violation blocks the milestone.
**AC7.6** `uei_ticker_map` populated for the full covered universe. Every mapping has an assumption ID, evidence URL, and review status. Machine-proposed mappings are marked unreviewed and excluded from publication until reviewed.
**AC7.7** Fiscal period mapping (federal FY → company fiscal quarter) documented as an explicit assumption with a stated method and sensitivity note.
**AC7.8** Covered universe expanded with a **P3 federal-exposed cohort** of at least 12 names selected by federal award volume among S&P 500 members (candidates to verify: LMT, RTX, NOC, GD, LHX, BA, HII, LDOS, HCA, UNH, CVS, MCK, CI, ELV). Selection criteria and the exclusion list are published.
**AC7.9** Federal stream published for the full universe: per-company numerator bands, tier mix, and provenance links to specific award records. Monte Carlo propagates award-mapping uncertainty and deficit-share uncertainty jointly.
**AC7.10** BFR% published where disjointness is `VERIFIED`; companies flagged `COMPOSITION_UNVERIFIED` publish streams separately with a visible explanation and no composite.
**AC7.11** Independent recompute: a standalone script sharing no code with the attribution engine reproduces three companies' federal stream figures within Monte Carlo tolerance.
**AC7.12** Company pages show the stream decomposition with per-stream tier badges and per-stream provenance. Feeds gain versioned stream fields; existing fields unchanged.
**AC7.13** Indirect federal paths (transfers, pass-through) explicitly documented as out of scope or as a stated Tier 3 assumption — no silent omission.

---

### M8 — Interactive flow views

**AC8.1** Three views implemented, each at its own URL, each fully server-rendered as static SVG:
  - **Stream view** — debt streams → spending categories / agencies → companies. Default view.
  - **Company view** — one company, all inbound flows across all streams, with its revenue decomposition.
  - **Tier view** — the same total flow reorganized by evidence tier, making visible how much of the whole picture is observed versus assumed.
**AC8.2** Encoding rules enforced and legend-documented on every view: **ribbon width = dollars, ribbon style = tier** (Tier 1 solid, Tier 2 dashed, Tier 3 finely dotted, decreasing opacity). Tier is never encoded by color alone.
**AC8.3** Node budget: default rendering of any view shows no more than ~14 nodes, using bundled "all others" nodes. Expansion reveals detail; it never starts expanded.
**AC8.4** **No-JS completeness**: with JavaScript disabled, each view URL renders its complete diagram and every other view is reachable by ordinary link. Verified by an automated test with scripting disabled.
**AC8.5** JS enhancement is limited to: swapping views without navigation, highlighting a path on hover/focus, and expanding a bundled node. It never fetches, never computes a displayed number, and never changes a value. All values come from `flow_view`, so static and enhanced rendering are provably identical — assert this in a test.
**AC8.6** Keyboard operable: every interactive element focusable, visible focus, logical order, and screen-reader labels on nodes and edges. Zero critical accessibility violations.
**AC8.7** **Revenue decomposition bar** implemented per company: total revenue split into consumer-credit-funded / federal-deficit-funded / business-debt-funded / unborrowed, with bands shown and each segment carrying its tier mix. This is the primary answer artifact; the Sankey is the mechanism.
**AC8.8** Every view carries the honesty caption: Tier 3 flows are proportional allocations, not observed transfers; the diagram is not a complete use-of-funds decomposition.
**AC8.9** Views degrade legibly at mobile width by reducing node count, never by introducing zoom or horizontal scroll.
**AC8.10** `UX_INVENTORY` before/after diff shows no user-visible information lost.

---

### M9 — Household stream gap-fill

**AC9.1** Terms verified and registered for every source in §4.2 before ingest. Sources that fail are marked `BLOCKED-LEGAL` with the specific prohibited clauses; expect Fannie/Freddie to fail and do not force it.
**AC9.2** Auto lending separated from student and other nonrevolving, using FFIEC + NCUA call report categories, with the separation method documented as an assumption.
**AC9.3** Mortgage and HELOC flows ingested (HMDA where permitted, Z.1 otherwise) and registered as household sub-products. Whether they enter attribution or remain context is an explicit, documented decision.
**AC9.4** Student loan flow ingested from Federal Student Aid; explicitly documented as largely non-attributable to covered company revenue, or attributed with a stated Tier 3 method.
**AC9.5** SBA loan-level data ingested; small business credit registered as a sub-stream or documented as out of scope with reasoning.
**AC9.6** BNPL Tier 1 evidence extended across all covered companies with named merchant relationships, sourced to specific filings.
**AC9.7** Matrix A extended to the new sub-products with evidence-linked weights; every new weight has an assumption ID and sensitivity note.
**AC9.8** Any change to previously published DFR% values is treated as a restatement: changelog entry, prior values preserved and retrievable, magnitude and cause explained.
**AC9.9** Assumption sensitivity report: top five assumptions by band width contribution, per stream.

---

### M10 — Business debt (exploratory)

**AC10.1** A written feasibility finding delivered BEFORE implementation, answering: can business-debt-funded revenue be attributed to a seller's revenue with any evidence above Tier 3? The honest answer may be no.
**AC10.2** If attribution is not defensible, the stream is registered as `context_only`, published as a macro chart beside the other streams, explicitly excluded from BFR%, and the reasoning is published on the methodology page. **This is a successful outcome, not a failure.**
**AC10.3** If attribution is defensible, full stream implementation to the same standard as M7, including disjointness proof against both existing streams.
**AC10.4** Per-company debt and interest expense from existing SEC XBRL published as company context regardless of the stream decision — it is already ingested and it is Tier 1.

---

## 7. Display requirements

Beyond the per-milestone ACs, the following govern what appears on the site.

- **The decomposition bar is the headline artifact**, not the Sankey. A reader should be able to answer "how much of this company's revenue is borrowed money, and how much do we actually know" in one glance, on mobile, without interaction.
- **Streams are visually distinguishable from tiers.** Stream is a category (which kind of borrowing); tier is a confidence (how well we know). Use different visual channels — do not let a reader confuse "federal" with "observed."
- **Near-zero streams are shown, not hidden.** A retail company's ~0% federal stream is information: it says the federal picture does not apply here. Suppressing it would misrepresent coverage.
- **The aggregate view must not imply the streams are one pot.** Where a composite is shown, its disjointness status is shown with it.
- **Every new number keeps the existing contract**: units on the page, tier badge, band never bare, provenance link to the specific source record.
- **No new jargon without inline definition at first use.** BFR% and stream names must each be defined where they first appear, not only in the methodology.

---

## 8. Regression protection

This expansion touches the attribution engine, the schema, the feeds, and every page. The refactor in M6 is the highest-risk change in the project's history because it rewrites the path that produces already-published numbers.

Required at **every** milestone boundary:
1. `UX_INVENTORY_BEFORE.md` / `UX_INVENTORY_AFTER.md` with a diff; every removal justified or reachable elsewhere.
2. Full `REGRESSION.md` suite re-run: ledger and feed byte-identity, URL/permalink integrity, provenance resolution, CI rule assertions, coverage, determinism replay, cold clone, immutability enforcement, page weight, accessibility.
3. Scheduled prediction and grading workflows verified unmodified and still firing. If any workflow file changed, state exactly what and why.
4. A `STREAM_INTEGRITY.md` report: disjointness status per company, any `COMPOSITION_UNVERIFIED` flags with reasons, and the double-counting guard results.

If any byte-identity or permalink check fails, **stop and report before proceeding**. Those are correctness and citation guarantees.

---

## 9. Risk register

| Risk | Mitigation |
|---|---|
| NY Fed HHDC turns out to be prohibited | Audit first (AC6.1); purge and restate if needed; Z.1 and call reports substitute for most of its role |
| USASpending recipient mapping is wrong for a major name | Every mapping is a reviewed assumption with evidence; unreviewed mappings never publish |
| Double-counting across recipient levels | Explicit guard test with a multi-level fixture (AC7.3) |
| Deficit-share assumption drives the whole federal result | Registered assumption with sensitivity analysis; band widens accordingly; never presented as Tier 1 |
| Business debt proves unattributable | Pre-accepted outcome; ships as context-only (AC10.2) |
| Refactor silently changes published DFR% | Byte-identity is a blocking AC (AC6.4) |
| Scope creep into a general macro dashboard | Streams must attribute to company revenue or be marked context-only; no unattached macro series |

---

## 10. Definition of done

- M6 through M9 all green; M10 resolved either way with its finding published.
- BFR% published with per-stream decomposition and disjointness status for every company where it is defensible.
- Three flow views live, no-JS complete, keyboard operable, with the decomposition bar on every company page.
- Methodology 2.0.0 published with version comparison; every new assumption in the registry with evidence and sensitivity.
- Every source carries a verified terms status; every rejection documented publicly in the source licensing section.
- `make bootstrap && make verify && make publish` works from a fresh clone.
- The M2 clock never stopped: predictions continued weekly and grades continued landing automatically throughout.
