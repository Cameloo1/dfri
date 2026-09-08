# DFRI visual system

This document describes the implemented research interface. Deployment is a separate approval and
verification step. The redesign replaces the compact ledger presentation; published data, evidence,
uncertainty, URLs, and immutable history retain their existing contracts.

## Reading order

The homepage starts with the question DFRI answers, a company exploration action, and the current
revenue-weighted estimate. Its midpoint remains joined to the estimated 80% band, period, units,
evidence mix, and provenance. A separate forecast panel keeps consumer-credit and fiscal predictions
separate from company attribution. Historical backtests and live calibration remain explicitly distinct.

The remaining homepage sections explain evidence tiers, show credit flow, compare Evidence Lift,
and link to the complete company directory. Baseline-only companies remain a single native disclosure
with every original row. Evidence Lift is not a risk, quality, or investment score.

## Typography and color

Newsreader provides expressive page and section headings. IBM Plex Sans provides body text,
navigation, tables, and tabular headline figures. IBM Plex Mono is reserved for identifiers and
version metadata. The existing self-hosted WOFF2 files, licenses, and hash-pinned provenance remain
unchanged. No remote fonts or scripts are loaded.

Body text starts at 16 pixels, with generous line spacing and limited prose measure. Headings use a
fluid scale from mobile to desktop. Chart labels use responsive HTML text beside the SVG, avoiding
illegible labels scaled down inside a wide viewBox.

| Role | Color |
| --- | --- |
| Page | `#f7f8fc` |
| Surface | `#ffffff` |
| Primary text | `#17233b` |
| Secondary text | `#57647a` |
| Navigation and data accent | `#254cdb` |
| Light accent surface | `#e8edff` |
| Structural rule | `#d8deea` |
| Graded state only | `#00654c` |

Blue identifies links, active navigation, primary actions, and estimated bands. Green remains
exclusive to completed grading. Evidence tiers retain text labels and distinct solid, hatched, and
dotted treatments; their meaning never depends on color alone.

## Components and responsive behavior

- The shared masthead has a compact DFRI mark, wrapped anchor navigation, and a visible current-page
  state. The skip link is the first keyboard stop. Navigation remains usable without JavaScript.
- Company directory cards retain ticker, full name, and complete estimated band. Optional local
  search accepts names or tickers, exposes an empty state, and restores all entries with Clear.
  Without JavaScript, all companies remain visible and search controls remain hidden.
- Company pages group the estimate, evidence composition, and Evidence Lift. In-page links lead to
  history, evidence, assumptions, and sensitivity. Narrow screens show history as readable records;
  the SVG and complete table remain available in the publication.
- Data panels use white surfaces, restrained borders, and modest corners. The homepage's primary
  estimate uses the blue accent surface with high-contrast light text.
- Native disclosures hold repeated chart tables, historical diagnostics, detailed calibration, and
  long reference registries. They retain every original value and source destination. Essential
  estimates, bands, units, limitations, source-fallback warnings, and status remain visible.
- Tables retain real table semantics. Wide tables scroll within their container; a mobile cue explains
  that more columns are available. Forecast controls expose ascending or descending state and keep
  unreleased values last. Numeric columns compare numbers, including negative amounts.
- The server-rendered credit-flow chart retains linear ribbon widths, explicit evidence-tier styles,
  the nine-node readability cap, and its complete data table. Desktop layouts pair explanation with
  the diagram; mobile layouts stack them.
- Automation status remains above the main content with its status-record link and checked date.
  The strip summarizes the state; the full machine-readable status retains
  every lane and timestamp. The footer holds licensing, corrections, publication metadata, and citation.
- Controls have visible focus outlines. Reduced-motion preferences disable smooth scrolling and
  transitions. Print styles simplify surfaces and expose disclosure contents.

## Regression contract

Run `make.cmd verify` and `make.cmd publish` (or their Make equivalents). The publication gate checks
frozen-input determinism, metadata, local font integrity, estimated bands, tier semantics, contrast,
and weight. The browser gate covers every generated page, keyboard navigation, numeric sorting,
company search, expanded disclosures, and no-JavaScript access. Responsive checks cover 320, 390,
768, 1024, and 1440 pixel widths with reference tables expanded.

A redesign must preserve every published feed byte when inputs are unchanged, every data-table row,
every immutable route, and every evidence destination. Review screenshots of the homepage, directory,
company records with and without observed evidence, forecasts, methodology, version comparison,
coverage, roadmap, corrections, and changelog. Local screenshots and audit output are not public reports.
