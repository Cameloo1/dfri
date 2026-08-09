# Redesign regression verification

Status: **PASS**

This report compares the pre-redesign commit `badb0a865d986e0ab80c08cf7eb0aff1cdb73ae1`
with the merged redesign at `f668f8825a9f1d2b6416c3c96aaba5517f0c6778`, then reruns the
same invariants after the permanent guards in `f947018`. The redesign changes rendered HTML and
CSS bytes, so the site publication manifest changes as expected. The public ledgers, feeds, and
published numbers do not.

## 1. Data and ledger invariants

The comparison read the repository-authoritative Parquet batches, selected every required field,
and compared normalized values rather than relying on page text.

| Invariant | Result |
| --- | --- |
| Six predictions: ID, `made_at`, point, 80%/95% bounds, `model_version`, `inputs_hash` | Byte/value-identical |
| Two grades: `actual_first_print`, `abs_error`, `graded_at` | Byte/value-identical |
| Six publication records | Byte/value-identical |
| 50 companies: DFR% low/mid/high and Tier 1/2/3 shares | Value-identical |
| Revenue-weighted aggregate midpoint | Identical at `3.353285003290871` |
| Feed schemas | Byte-identical; the redesign added no schema field |

The repository ledger manifest hash remained
`3eadf24738b1c329c0d9f2760ad4a88b037ca869a8637cdd58dfb9dd67e7fcf3`. Its three canonical
table hashes remained:

- predictions: `4abb703726ee93e7f3734db2edc5a70abaa5f20462b5191f9936b50c913f921e`
- grades: `25103f7188d2f6a2bbd376b773b5bf6d4659783d915feac8b360109a02ef1d30`
- publication records: `52c220bb4935f45bef849d8c6177833f08b132973088889c892421a1a0c8ba58`

The compared feed payloads also remained byte-identical:

| Feed | SHA-256 |
| --- | --- |
| `v1/feeds/nowcast_predictions.json` | `b4fcae59977bbcb9ca8cb02290863b761d85907c90309c5bfbe34eb3a2464788` |
| `v1/feeds/scoreboard.json` | `1733758743138c12c32c45de7e652baf29cef0a5f813d7f98fa30f40aa82bc2e` |
| `v1/feeds/dfri_companies.json` | `58a380e74b107810ef405e64f72d1d0dc6affdfc05ad49a9643becff64816e36` |
| `v2/feeds/dfri_companies.json` | `993d39883a781f4fa5235481af8e4f2bf773cdeb49a58ee79217628d8be82cad` |
| `v1/feeds/assumptions.json` | `d5e5fdb59274474e4b22417b84362bcfd6edfd86db08075c53fcdbeafa9bffef` |

No difference requiring explanation was found.

## 2. URLs, redirects, and provenance

- The before/after publication inventories each contained 60 generated HTML pages. No baseline
  page or file disappeared, no page-level `href` was removed, and both internal-link inventories
  resolved without failure.
- Neither publication contained a meta refresh.
- A live audit covered all 62 public routes: six primary pages, 50 company pages, and all six
  repository-ledger prediction permalinks. Every route returned HTTP 200 directly and the redirect
  count was zero.
- The provenance checker followed 58 source and Tier 1 evidence URLs. All 58 returned HTTP 200;
  there were no 404s or changed targets.

## 3. Permanent rendered-page rules

Every headline figure now declares a compact `data-figure` contract. The static quality gate walks
every rendered HTML file and enforces the contract for units, interval bands, evidence tiers,
provenance, and the word `estimated`. It also blocks:

- a headline number outside a figure contract;
- a modeled point without its visible range;
- a tiered figure without a tier badge;
- Tier 1/2/3 styling that loses its non-color border, texture, dash, or dot distinction;
- `measured` outside an explicit Tier 1 context; and
- removal of the site-wide disclaimer, CC BY-NC 4.0 notice, commercial-license reservation, or
  contact path.

Mutation tests prove each failure mode. CI now runs `make site-quality` immediately after
`make publish`; the existing publication step continues to run the browser accessibility and
no-JavaScript route audit. The page templates keep the unit, interval, tier badge, and provenance
link inside the same figure boundary.

## 4. Non-visual functionality

| Check | Pre-redesign | Guarded redesign |
| --- | ---: | ---: |
| Full tests | 405 passed | 417 passed |
| Statement/branch coverage | 85.05% | 85.28% |
| Deterministic replay tests | 3 passed | 3 passed |

Two consecutive `make replay` executions produced the identical tree hash
`4c04d976f6a9cd480a09a71ffc8ae53aa152b091a50c8a7943e6cebe004a0325`.

The focused adversarial checks passed: an attempted prediction edit was rejected, an unchanged
candidate merge was a no-op without rewriting repository bytes, and a fresh runtime restored from
Git matched the snapshot. A separate fresh clone of the pushed branch, with no local state or
Actions artifact, passed `make bootstrap`, `make verify`, `make publish`, and `make site-quality`.
Its Git-only restore and resnapshot each contained 10 files and 14 rows and reproduced the ledger
manifest hash above.

The scheduled prediction/grading workflow was not modified by the redesign or this guard. Its cron
and append-only state contract remain covered by the workflow tests. The only workflow change is
to `.github/workflows/ci.yml`: it adds the blocking rendered-page quality step after publication.

## 5. Accessibility and budgets

The deterministic publication audit covered 60 pages, including all 50 company pages and every
frozen prediction permalink. The live audit separately covered all 62 deployed routes and all six
current permalinks. Both JavaScript-enabled and JavaScript-disabled passes succeeded; the live
audit reported 62 of 62 substantive no-JavaScript pages, zero redirects, and zero critical axe
violations.

The static budget gate checked every generated page. The heaviest page was
`methodology/index.html` at 74,231 bytes including shared assets, with an estimated 4G load of
521.155 ms. The complete deterministic publication was 1,196,596 bytes, below its existing
1,200,000-byte aggregate guard. The worst checked text contrast ratio was `5.736:1`, above WCAG
AA's `4.5:1` minimum.

The credit-flow SVG was also inspected at 360 px and 1280 px viewports. At 360 px, the document
used 345 px, the SVG used 313 px, and there was no horizontal overflow or zoom. The static diagram
retained nine nodes, Tier 1 solid ribbons, Tier 2 dashed ribbons, Tier 3 dotted ribbons, the legend,
and the Tier 3 proportional-allocation disclaimer.

## Manual-only checks

Human visual inspection was still required for text collision and ribbon-label legibility in the
static flow diagram at mobile and desktop widths; it passed. All correctness, URL, provenance,
content-rule, no-JavaScript, accessibility, contrast, and weight checks above were automated.
