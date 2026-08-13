# DFRI screen-reader traversal

Verified on 2026-08-10 against the deterministic local publication candidate.

## What was tested

Windows Narrator was run in Chrome with scan mode enabled. The manual sequence used next-heading,
next-table, and next-item traversal on three named pages:

1. the homepage;
2. the General Motors company page; and
3. the current nonrevolving-credit prediction permalink.

The Narrator session was paired with a browser accessibility-tree inspection so the spoken path
could be checked against the exposed headings, table names, row headers, units, tiers, and links.
The disposable session evidence is retained under the ignored local evidence boundary; it is not a
published artifact.

## Observed primary path

- The skip link reaches the main landmark before the site navigation and automation-status frame.
- The homepage exposes one H1, then literal section headings for the current estimate, prediction
  ledger, evidence depth, credit flow, Evidence Lift, and companies.
- The revenue-weighted DFR% image is followed by a named table containing the period, 80% band,
  midpoint, Tier 1/2/3 mix, units, and methodology link.
- The current prediction image is followed by a named table containing the point, both intervals,
  units, status, and immutable-record link.
- The flow image is followed by a table in which each rendered ribbon is one source-to-destination
  row with its exact amount and evidence tier.
- A company page exposes separate named tables for the current DFR% band, the evidence-tier revenue
  decomposition, and every versioned historical band. The evidence and filing links follow those
  tables in document order.
- A prediction permalink exposes both uncertainty intervals before the immutable record fields and
  its first-print source links.

## Remaining awkwardness

- The screen reader encounters an image summary and then a complete table, so important figures are
  intentionally repeated. Removing either would reduce access; the duplication is the cost of
  keeping the visual and text paths equivalent.
- The credit-flow equivalent has 18 ribbon rows in the current publication. It is complete and
  auditable, but slower to traverse than the nine-node visual summary.
- The company history table has six columns. Row and column headers are exposed correctly, but a
  narrow visual viewport still needs the table wrapper even though screen-reader traversal is
  linear.
- The automation-status iframe precedes the main content. The skip link prevents it from blocking
  the primary task, but readers who do not use the skip link hear operational status before the
  headline estimate.
- Narrator speech is not machine-recorded by the repository. The actual screen-reader traversal
  remains a manual release-boundary check; CI permanently checks the same semantic associations,
  table contracts, keyboard order, no-JavaScript content, and axe results.

## Automated companion evidence

The 63-page no-JavaScript and axe pass reported zero critical violations, zero semantic failures,
zero keyboard failures, and zero mobile-layout failures. The static quality gate rejects any
required chart whose associated table, units, tier context, or provenance is missing.
