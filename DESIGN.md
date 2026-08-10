# DFRI Visual System

Status: approved implementation contract, revised 2026-08-08 for typographic inversion and the
tier-encoded credit-flow view.

The interface should read as a public financial record: editorial, compact, and calm. Its distinctive device is the ledger. Predictions, grades, methodology versions, evidence, and publication metadata are set in visible rows separated by rules so the append-only behavior is legible before it is explained.

This document changes presentation only. Published content, values, tiers, provenance, feed contracts, page URLs, permalinks, and acceptance criteria remain unchanged.

## Design principles

1. **A public record, not a product pitch.** Lead with dated facts and measured claims. Avoid decorative hero treatments, dashboard chrome, marketing cards, icons, shadows, gradients, and ornamental imagery.
2. **The number is the display type.** Headings are quiet, short labels. A neutral sans-serif and
   restrained serif establish the editorial voice, while large tabular monospace figures carry the
   page. Hierarchy comes from weight, rules, and spacing rather than oversized headings.
3. **Rules reveal append-only structure.** Horizontal rules, row numbers, timestamps, and aligned metadata create the ledger. Sections are open on the paper rather than enclosed in floating boxes.
4. **Verification owns the accent.** Green is reserved for graded or verified states. Links, navigation, pending states, tiers, and decoration remain ink or grayscale so green always has semantic meaning.
5. **Evidence travels with the number.** A primary figure must be visually joined to its units, uncertainty band, tier label, and provenance link. On narrow screens these stack without separating the figure from its evidence.
6. **No hidden meaning.** Charts and tables are complete when JavaScript is absent. Texture is paired with explicit tier text. Every SVG has a title and readable labels.

## Typography

No font files or remote font requests are permitted.

- Display serif: `Iowan Old Style`, `Palatino Linotype`, `Book Antiqua`, `Palatino`, `Georgia`, serif.
- Body grotesque: `Inter`, `Arial`, `Helvetica Neue`, system-ui, sans-serif. Inter is a fallback name only; the site does not fetch it.
- Ledger numerals: `SFMono-Regular`, `Cascadia Mono`, `Roboto Mono`, `Consolas`, monospace, with `font-variant-numeric: tabular-nums lining-nums`.

Mobile-first type scale. Heading tokens are deliberately compressed; only figure tokens may become
display-sized:

| Token | Minimum | Fluid maximum | Use |
|---|---:|---:|---|
| `--step--1` | 0.75rem | 0.78rem | labels, badges, footnotes |
| `--step-0` | 0.94rem | 0.98rem | body and table text |
| `--step-1` | 1rem | 1.08rem | ledes and compact subheads |
| `--step-2` | 1.08rem | 1.2rem | section headings |
| `--step-3` | 1.3rem | 1.65rem | page headings |
| `--figure-small` | 1.7rem | 2.25rem | secondary headline figures |
| `--figure-large` | 2.8rem | 5.25rem | index, prediction, and DFR% figures only |

Headings use 1.05–1.2 leading and modest tracking. Body copy uses 1.5–1.65 leading. Heading and
section copy must be short and literal. Page-level tagline or kicker text above the H1 is prohibited.
The small label above a headline figure uses sentence case, states the period and units, and never
uses forced uppercase. Figures never use proportional numerals.

## Palette and contrast

| Role | Value | Required use | Contrast on paper |
|---|---|---|---:|
| Paper | `#f4f0e7` | page background | — |
| Raised paper | `#faf7f0` | sticky table headings and print-safe inset rows | — |
| Ink | `#181816` | primary text, rules, links | 15.63:1 |
| Muted ink | `#5d5a52` | secondary text | 6.05:1 |
| Verified green | `#006b50` | graded and verified state only | 5.74:1 |
| Rule | `#b7b0a4` | structural dividers, never text | — |
| Quiet fill | `#e6e0d6` | pending rows and chart ranges | ink 13.55:1 |

All listed text pairs exceed WCAG AA for normal text. Paper text on verified green is 5.74:1. Focus indication uses a 3px double ink outline with offset and does not consume the verification accent.

## Ledger composition

- The masthead is bounded by a strong top rule and a double bottom rule. The product name, organization, navigation, methodology version, and publication vintage read like edition metadata.
- Every page begins with a short literal H1 and, only when needed, one compact explanatory paragraph.
  There is no eyebrow, tagline, or kicker above the H1, and real data appears in the first screen.
- Major sections begin with a numbered ledger marker and a full-width rule. Reordering may clarify
  the reading path, but content remains reachable and its evidence context stays intact.
- The homepage stages its claims in a fixed reading order: current estimate, immutable prediction
  ledger, historical model record, evidence tiers, credit flow, Evidence Lift, then the company
  directory route. Relocation never separates a figure from its units, band, tier badge, or
  provenance.
- `.card` remains as a compatibility class in templates but renders as an open ledger entry: square corners, no shadow, no floating surface, and rule-based separation.
- Summary metrics use a responsive ledger grid. Each figure lock-up has a hairline top rule, a
  sentence-case period-and-unit label, a tabular figure, and one joined evidence line containing
  units, tier badge where applicable, and provenance link. Cells do not resemble product cards.
- Tables use strong header and closing rules, thin row rules, tabular figures, left-aligned labels, and right-aligned numeric columns where markup permits. Horizontal scrolling remains available on small screens.
- Company lists become ruled index entries. Ticker, company, and full estimated band remain visible together.
- Prediction permalinks use a record-header treatment: prediction identity and timestamp first, full band second, evidence and grade as a definition ledger.
- The footer is publication colophon text bounded by a double rule.

## Tier language

Tier meaning is never encoded by color alone:

- Tier 1 — Observed: solid ink field with paper text and the heaviest border.
- Tier 2 — Category-mapped: grayscale diagonal hatch with an ink border.
- Tier 3 — Fungible: dotted grayscale texture with an ink border.
- Mixed Tier 1–3: plain paper with a double ink border.

Every treatment retains its explicit `Tier 1`, `Tier 2`, `Tier 3`, or `Tier 1–3` text. Tier stacks use the same patterns and remain accompanied by text labels and an accessible description.

## Charts

All charts remain server-rendered inline SVG and must be complete without hover or JavaScript.

- Use a thin ink baseline and a quiet-fill rectangle or substantial line segment for the uncertainty range.
- Use a vertical ink rule for the midpoint; do not reduce a band to a bare point or conventional error-bar whiskers.
- Label low, midpoint, and high values directly below or beside the corresponding marks. Do not add legends when direct labels suffice.
- Use monospace SVG text with tabular numerals and explicit units in the surrounding figure caption.
- Graded actuals may use verified green because they are verified observations. Forecast bands, midpoints, and historical ranges remain grayscale.
- Historical company bands remain one directly labeled row per append-only version, ordered by effective date.

### Credit-flow view

The current-period attribution flow is a server-rendered inline SVG and is complete without
JavaScript. It is a presentation of already-published midpoint inputs and mappings, not a new
estimate.

- Ribbon width is linear in estimated midpoint millions of U.S. dollars. The caption states the
  period, unit, and that the view begins with the portion represented by published attribution
  lanes rather than claiming a complete use-of-funds decomposition.
- Ribbon style encodes the Matrix A evidence tier without relying on color: Tier 1 is solid at full
  opacity, Tier 2 is dashed at 0.72 opacity, and Tier 3 is finely dotted at 0.46 opacity.
- A visible legend says “width = estimated dollars” and “style = how much is known,” with direct
  Tier 1/2/3 labels.
- The static view contains no more than nine nodes: two credit products; the largest spending
  category; tier-preserving remainder-category groups where needed; the two highest-lift
  companies; and one “all other covered companies” node. This lower cap is the mobile legibility
  budget; it is deliberately stricter than the roughly-12-node product requirement.
- Omitted categories are never combined across tiers. The tier of every ribbon therefore remains
  unambiguous after aggregation.
- The mobile view uses a vertical flow with direct labels. If 12 nodes do not remain readable at
  320 CSS pixels, the generator reduces named category or company nodes before considering any
  interaction. Scroll and zoom are not remedies.
- Optional expansion may reveal frozen detail already present in the page, but it is enhancement
  only. The default SVG and its adjacent accessible data table remain complete with scripting
  disabled.
- The caption states: “Tier 3 flows are proportional allocations, not observed transfers.”
- The homepage keeps its prediction/index ledger first. The flow appears later as an additive,
  independently labeled section and is repeated with fuller explanation on Methodology.

## Component rules

### Figure lock-up

Required visual order: sentence-case period-and-unit label; hairline rule; primary figure; joined
evidence line with explicit unit, tier treatment where applicable, and provenance or
immutable-record link; full interval. A figure may never appear without its band where a band
exists. The figure—not the surrounding heading—is the largest type in the component.

### Status

Status labels are square-ended ledger stamps, not pills. `GRADED` uses verified green. Pending, baseline-only, evidence-supported, and superseded states use ink and grayscale unless the state itself represents completed verification.

### Links

Links use currentColor, an underline, and an increased underline offset. They do not use the verified accent. Hover strengthens the underline; focus uses the global double outline.

### Navigation

Navigation wraps on small screens and remains plain anchor markup. Page-shaped labels open real
pages: `Companies` targets the complete `/companies/` directory, while the historical
`/#companies` fragment remains present as a compatibility landing point. Existing company pages,
methodology pages, scoreboard routes, and immutable prediction permalinks do not move. The current
primary page uses `aria-current="page"`, and the first keyboard stop is a visible skip link to the
main landmark.

### Responsive behavior

- Start with a single-column ledger.
- At 48rem, summary records may span a 12-column editorial grid.
- At 68rem, the homepage opening becomes a two-column spread: the current DFR estimate remains in
  the left column and the prediction, live ledger count, and explicitly labeled historical
  backtest evidence form the right rail. Live-grade calibration remains full-width below both.
- The prediction rail uses normal document flow and CSS Grid only. At narrower widths its source
  and visual order are estimate, prediction, live ledger count, historical MAE, historical
  coverage, then live-grade calibration.
- Primary figures and their evidence stay in the same grid item.
- Tables scroll horizontally without clipping content or relying on scripts.
- Tap targets are at least 44px where navigation or sorting controls are interactive.

### Progressive enhancement

JavaScript may only replace existing table-heading text with sorting buttons, reorder
already-rendered rows, or reveal already-rendered flow detail. No content, navigation, chart,
provenance, status, flow value, or default flow label depends on scripting.

Evidence Lift uses native HTML disclosure for the 38 baseline-only rows. The summary states the
row count and 1.00x result; every company, rank, band, tier split, status, and link remains in the
server-rendered document and can be expanded without JavaScript. The shared baseline
interpretation is stated once instead of repeated per row.

## Page coverage

The system applies to every generated surface:

- Homepage and dedicated alphabetical company index
- Evidence Lift ranking
- Current-period tier-encoded credit-flow view on the homepage and Methodology
- Scoreboard
- Individual immutable prediction permalinks
- All 50 company pages and company history charts
- Methodology and Assumption Registry
- Methodology version comparison
- Coverage and exclusion ledger
- Changelog
- Preview banner and publication footer

## Performance and accessibility budget

- No remote fonts, images, trackers, cookies, or third-party scripts.
- Shared CSS and enhancement JavaScript remain the only page assets.
- Every generated page plus shared assets must remain below 500 KB.
- Estimated first load on the existing 4G model must remain below 1 second.
- Minimum tested text contrast is 4.5:1.
- Every SVG retains `role="img"` and a non-empty `<title>`.
- Every existing number retains its tier badge and provenance link where the publishing contract supplies them.
- Full content, links, tables, bands, and permalinks must pass with scripting disabled.
