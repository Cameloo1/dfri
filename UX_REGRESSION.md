# Information-architecture regression verification

Status: **BRANCH PASS — OWNER-AUTHORIZED MERGE AND DEPLOYMENT PENDING**

This report compares the deployed pre-change source at `c6c62b5` with the information-architecture
branch. It covers the complete inventory, data and ledger invariants, URLs, provenance, permanent
rendered-page rules, cold recovery, accessibility, mobile layout, and the observed visitor path.
The verified review surface is [PR #26](https://github.com/Cameloo1/dfri/pull/26).

## 1. Inventory and access preservation

The disclosure-expanded inventories and their machine diff are published as
`UX_INVENTORY_BEFORE.md`, `UX_INVENTORY_AFTER.md`, and `UX_INVENTORY_DIFF.md`.

| Invariant | Result |
| --- | --- |
| HTML routes | 62 before, 63 after |
| Missing routes | 0 |
| Added route | `/companies/` |
| Company pages | 50 before, 50 after |
| Prediction permalinks | 6 before, 6 after |
| Disclosure-expanded information lines | 4,343 before, 4,415 after |
| Evidence Lift rows | 50 before, 50 after, zero changed |
| Distinct outbound targets | 62 before, 62 after, zero changed |

The machine diff found four missing distinct text lines. Each is a literal replacement documented in
`UX_INVENTORY_DIFF.md`: the directory sort key is made explicit, the lift introduction now describes
the visible subset and expanded baseline group separately, the project description gains one
orienting prefix, and `Full ledger` becomes `Open the full ledger` without changing its destination.

The old homepage repeated the baseline interpretation 38 times. The new homepage states it once at
group level and preserves every rank, band, and tier split inside a native disclosure. With
JavaScript disabled, the disclosure begins collapsed, expands from its `summary`, and exposes all 38
rows.

The former nav target `/index.html#companies` is no longer a nav link, as planned. The URL still
returns HTTP 200 directly without a redirect, its `#companies` element remains present, and that
section links to `/companies/`.

## 2. Data, feed, and ledger invariants

No file under `state/ledgers/` changed. The repository-authoritative state remains six predictions,
two grades, and six publication records. Every required prediction field, grade field, record ID,
and original timestamp therefore remains byte-identical to the pre-change commit.

The canonical repository ledger hashes remain:

- manifest: `3eadf24738b1c329c0d9f2760ad4a88b037ca869a8637cdd58dfb9dd67e7fcf3`
- predictions: `4abb703726ee93e7f3734db2edc5a70abaa5f20462b5191f9936b50c913f921e`
- grades: `25103f7188d2f6a2bbd376b773b5bf6d4659783d915feac8b360109a02ef1d30`
- publication records: `52c220bb4935f45bef849d8c6177833f08b132973088889c892421a1a0c8ba58`

All 19 existing deterministic feed payloads compare byte-for-byte equal. The most scrutinized feed
hashes remain:

| Feed | SHA-256 |
| --- | --- |
| `v1/feeds/nowcast_predictions.json` | `b4fcae59977bbcb9ca8cb02290863b761d85907c90309c5bfbe34eb3a2464788` |
| `v1/feeds/scoreboard.json` | `1733758743138c12c32c45de7e652baf29cef0a5f813d7f98fa30f40aa82bc2e` |
| `v1/feeds/dfri_companies.json` | `58a380e74b107810ef405e64f72d1d0dc6affdfc05ad49a9643becff64816e36` |
| `v2/feeds/dfri_companies.json` | `993d39883a781f4fa5235481af8e4f2bf773cdeb49a58ee79217628d8be82cad` |
| `v1/feeds/assumptions.json` | `d5e5fdb59274474e4b22417b84362bcfd6edfd86db08075c53fcdbeafa9bffef` |

All 50 company DFR% low/mid/high values and Tier 1/2/3 shares are feed-identical. The
revenue-weighted aggregate midpoint remains `3.353285003290871`. The new 3.36% comparison anchor is
a view-only selection of the already-published lowest and highest company midpoints; it adds no feed
field, data source, or attribution computation.

## 3. URLs, provenance, and workflows

The pre-change deterministic route set had 60 pages; the guarded build has all 60 plus
`companies/index.html`. The accepted-state inventory separately covers all 63 current routes: seven
primary pages, 50 company pages, and six prediction permalinks. No route disappeared and no redirect
was introduced.

The live provenance checker followed all 58 registered Tier 1 and source URLs. All 58 returned HTTP
200. The complete rendered inventory independently found the same 62 outbound targets before and
after, with no missing or added target.

No file under `.github/workflows/` changed. The scheduled prediction and grading jobs therefore keep
their existing cron, append-only state, and deployment contracts. Their contract tests are included
in the full suite.

## 4. Permanent rule checks

The publication gate still blocks an unlabelled number, a bare modeled point, a missing interval,
a missing tier badge, a color-only tier distinction, misuse of `measured`, loss of `estimated`, and
loss of the disclaimer or license. This pass adds permanent checks for:

- the complete 12-row evidence-supported and 38-row baseline partition;
- the complete 50-entry company directory;
- one main landmark and a working first-focus skip link;
- correct `aria-current` state and no false current state;
- heading hierarchy, link names, and accessible SVG names on every page;
- actual keyboard traversal order and visible focus outlines on every focusable item;
- native no-JavaScript expansion of all 38 baseline rows; and
- 390 px, JavaScript-disabled horizontal-overflow and SVG-viewport checks on every page.

The browser receipt covers 61 deterministic pages and reports zero critical axe violations, zero
semantic failures, zero keyboard failures, and zero mobile-layout failures. The recorded worst
finding is `none`.

## 5. Functional verification and recovery

| Check | Pre-change | Current branch |
| --- | ---: | ---: |
| Full tests | 417 passed | 422 passed |
| Statement/branch coverage | 85.28% | 85.30% |
| Deterministic replay tests | 3 passed | 3 passed |

Two consecutive replay runs reproduced the prior canonical tree hash
`4c04d976f6a9cd480a09a71ffc8ae53aa152b091a50c8a7943e6cebe004a0325`.

The focused adversarial run passed three explicit proofs: an attempted prediction edit is rejected,
an unchanged repository-ledger candidate is a no-op without rewritten bytes, and a fresh runtime
restores byte-identically from Git without an artifact.

A fresh clone of the pushed branch, with no local state, artifact, virtual environment, or
dependency cache, passed the documented `make bootstrap`, `make verify`, `make publish`, and
`make site-quality` Windows targets. The real runner evidence remains available from
[PR #26 checks](https://github.com/Cameloo1/dfri/pull/26/checks).

## 6. Accessibility and budgets

| Gate | Result |
| --- | ---: |
| Heaviest page | `methodology/index.html` |
| Heaviest page including shared assets | 75,688 bytes |
| Maximum estimated 4G first load | 528.44 ms |
| Minimum checked text contrast | 5.736:1 |
| JavaScript-enabled pages audited | 61 |
| JavaScript-disabled pages audited | 61 |
| 390 px no-overflow pages | 61 |
| Critical accessibility violations | 0 |

Every page remains below 500 KB and below the one-second estimated 4G budget. At 390 px every
document width equals the 390 px viewport and every accessible SVG remains inside it.

## 7. Observed primary path

The rendered path was walked at 1,280 px and 390 px rather than inferred from templates:

1. A cold arrival exposes one H1, a two-record plain-language description, then the 3.36% estimated
   band and its same-publication company range. At 390 px the number, units, tier badge, provenance,
   and band are visible without horizontal scrolling.
2. The immutable prediction ledger is the second numbered section. It states the timestamp and
   first-print rule, shows graded and pending counts, and links to the full scoreboard.
3. Tier definitions follow the claim and ledger under `How much is known`; they no longer occupy the
   opening figure.
4. The Evidence Lift table exposes the 12 differentiated rows. Its native summary announces the 38
   baseline-only rows before expansion.
5. `Companies` in primary navigation opens a real alphabetical-by-ticker directory. Carvana was
   reached in one further activation; its page retained the 15.14%–22.39% band, Tier 1/2/3 split,
   SEC filing link, assumption IDs, and sensitivity record.
6. The first Tab after reload focuses a visibly outlined `Skip to main content` link. Activating it
   moves both the URL fragment and focus to the main landmark.

These observations prove order, access, semantics, and traceability. They do not prove the untested
behavioral hypotheses in `UX_PLAN.md`: whether a new reader understands DFRI faster, perceives the
ledger as more credible, or scans the lift distribution more accurately still requires reader
testing. No engagement, urgency, or conversion claim is made.

## 8. Manual and publication boundaries

Human inspection was used only for text collision and hierarchy at desktop and mobile widths; it
found no collision or clipping. All data, route, link, rules, no-JavaScript, keyboard, overflow,
contrast, weight, determinism, and immutability checks above are automated.

Relaybase discovery was unavailable during local inspection, so the documented direct-port fallback
was used. The process was command-line verified before each stop, and the port was proven closed
afterward. No runtime manifest or local path was committed.

The PR is ready and CI-green, but merging into public `main` was not performed because that external
mutation requires explicit owner approval in the current execution environment. Consequently the
existing public Pages site still shows the pre-change presentation; post-merge deployment and final
live 63-route verification remain the only unexecuted steps.
