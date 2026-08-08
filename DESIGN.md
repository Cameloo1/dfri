# DFRI Visual System

Status: approved implementation contract for the presentation-only redesign.

The interface should read as a public financial record: editorial, compact, and calm. Its distinctive device is the ledger. Predictions, grades, methodology versions, evidence, and publication metadata are set in visible rows separated by rules so the append-only behavior is legible before it is explained.

This document changes presentation only. Published content, values, tiers, provenance, feed contracts, page URLs, permalinks, and acceptance criteria remain unchanged.

## Design principles

1. **A public record, not a product pitch.** Lead with dated facts and measured claims. Avoid decorative hero treatments, dashboard chrome, marketing cards, icons, shadows, gradients, and ornamental imagery.
2. **Typography establishes hierarchy.** Serif display type gives headlines editorial authority; a neutral sans-serif carries prose; a tabular monospace carries every figure, range, percentage, date, version, identifier, and unit-bearing value.
3. **Rules reveal append-only structure.** Horizontal rules, row numbers, timestamps, and aligned metadata create the ledger. Sections are open on the paper rather than enclosed in floating boxes.
4. **Verification owns the accent.** Green is reserved for graded or verified states. Links, navigation, pending states, tiers, and decoration remain ink or grayscale so green always has semantic meaning.
5. **Evidence travels with the number.** A primary figure must be visually joined to its units, uncertainty band, tier label, and provenance link. On narrow screens these stack without separating the figure from its evidence.
6. **No hidden meaning.** Charts and tables are complete when JavaScript is absent. Texture is paired with explicit tier text. Every SVG has a title and readable labels.

## Typography

No font files or remote font requests are permitted.

- Display serif: `Iowan Old Style`, `Palatino Linotype`, `Book Antiqua`, `Palatino`, `Georgia`, serif.
- Body grotesque: `Inter`, `Arial`, `Helvetica Neue`, system-ui, sans-serif. Inter is a fallback name only; the site does not fetch it.
- Ledger numerals: `SFMono-Regular`, `Cascadia Mono`, `Roboto Mono`, `Consolas`, monospace, with `font-variant-numeric: tabular-nums lining-nums`.

Mobile-first type scale:

| Token | Minimum | Fluid maximum | Use |
|---|---:|---:|---|
| `--step--1` | 0.75rem | 0.78rem | labels, badges, footnotes |
| `--step-0` | 0.94rem | 1rem | body and table text |
| `--step-1` | 1.08rem | 1.25rem | ledes and compact subheads |
| `--step-2` | 1.45rem | 2rem | section headings |
| `--step-3` | 2rem | 3.15rem | page headlines |
| `--step-4` | 2.65rem | 4.75rem | primary figures only |

Headlines use tight leading between 0.96 and 1.08 and modest negative tracking. Body copy uses 1.55–1.7 leading. Uppercase is limited to short ledger labels with generous tracking. Figures never use proportional numerals.

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
- Every page begins with an eyebrow, headline, and lede aligned to the same editorial measure. No vague taglines are introduced.
- Major sections begin with a numbered ledger marker and a full-width rule. Existing section order and content remain intact.
- `.card` remains as a compatibility class in templates but renders as an open ledger entry: square corners, no shadow, no floating surface, and rule-based separation.
- Summary metrics use a responsive ledger grid. Each cell has a top rule, label, figure, units, and supporting metadata. Cells do not resemble product cards.
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

## Component rules

### Figure lock-up

Required visual order: ledger label; primary figure; explicit unit; full interval; tier treatment; provenance or immutable-record link. A figure may never appear without its band where a band exists.

### Status

Status labels are square-ended ledger stamps, not pills. `GRADED` uses verified green. Pending, baseline-only, evidence-supported, and superseded states use ink and grayscale unless the state itself represents completed verification.

### Links

Links use currentColor, an underline, and an increased underline offset. They do not use the verified accent. Hover strengthens the underline; focus uses the global double outline.

### Navigation

Navigation wraps on small screens and remains plain anchor markup. The primary five destinations and all existing hrefs remain unchanged.

### Responsive behavior

- Start with a single-column ledger.
- At 48rem, summary records may span a 12-column editorial grid.
- Primary figures and their evidence stay in the same grid item.
- Tables scroll horizontally without clipping content or relying on scripts.
- Tap targets are at least 44px where navigation or sorting controls are interactive.

### Progressive enhancement

JavaScript may only replace existing table-heading text with sorting buttons and reorder already-rendered rows. No content, navigation, chart, provenance, or status depends on scripting.

## Page coverage

The system applies to every generated surface:

- Homepage and company index
- Evidence Lift ranking
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
