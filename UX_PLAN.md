# DFRI information-architecture plan

Status: **ROUND-TWO AND OWNER-APPROVED OPENING-SPREAD FOLLOW-UP IMPLEMENTED ON PR #26**

This plan governs the second frontend overhaul. It follows the Laws of UX process, but the laws
are treated as explanatory heuristics rather than proof. Accessibility, honest presentation,
auditability, and the Section 8.3 constraints override every heuristic.

The deployed baseline was inspected before this plan was written. The complete no-JavaScript
inventory is frozen in `UX_INVENTORY_BEFORE.md` at commit `5f2fbfd`. It covers 62 HTML routes,
148 distinct internal link targets, 62 distinct outbound link targets, and every visible line in
DOM reading order.

## Friction observed before naming principles

These statements describe what the rendered site asks a reader to do. They intentionally avoid
UX-law labels.

1. A cold visitor receives two project claims in one opening paragraph, then sees the aggregate
   DFR% and a monthly nowcast side by side. The aggregate card immediately adds three tier shares,
   three tier definitions, an uncertainty band, two dollar amounts, and provenance. The first
   screen therefore requires the reader to understand the estimate, the forecast, and the evidence
   taxonomy at the same time.
2. The immutable ledger is the project's strongest proof, but the homepage presents it as a later
   record-count cell. The live scoreboard itself is much clearer: it leads with live calibration,
   publishes the first miss plainly, and links every row to an immutable record. That proof is not
   represented with comparable prominence on the landing path.
3. The Evidence Lift table renders all 50 companies. Rows 13–50 repeat the same 1.00x lift, the
   same 0.0%/53.0%/47.0% tier mix, and the same interpretation. The 12 differentiated rows are a
   small portion of a long table, so the finding that 38 companies are indistinguishable at the
   baseline is visually harder to see than any individual baseline row.
4. After the 50-row lift table, the homepage renders the same 50 companies again in an
   alphabetical link index. Both lists are valid, but putting both complete lists on one page makes
   the page longer without providing 50 additional company destinations.
5. The headline 3.36% is shown with its uncertainty band and dollar numerator/denominator, but not
   alongside the range of company estimates in the same publication. A first-time reader can see
   uncertainty but cannot tell where the aggregate sits relative to the covered companies.
6. “Companies” is styled and positioned as a primary destination, but it links to a late homepage
   anchor. The other comparable navigation items open dedicated pages. This mismatch is only
   discovered after activation.
7. The document has correct header, navigation, main, and footer landmarks, but there is no skip
   link, the main landmark has no target ID, and no primary-navigation link exposes
   `aria-current="page"`. Keyboard and screen-reader users must traverse the masthead on every page
   and receive no programmatic current-location cue.

The owner-verified six frictions are accepted as product evidence. Items 1, 2, 6, and 7 were also
reproduced in the deployed DOM; items 3 and 4 were confirmed by the complete rendered inventory.

## Principles selected for the largest risks

Only five catalog principles are used:

- **Cognitive Load** explains why the estimate, forecast, and tier taxonomy should be introduced in
  a sequence rather than in one compound first-screen component.
- **Selective Attention** explains why 38 visually identical baseline rows conceal the 12 rows
  carrying differentiated evidence.
- **Jakob's Law** explains the navigation mismatch: a primary nav label that resembles the other
  page destinations is expected to open a page.
- **Working Memory** supports carrying an honest comparison context with the headline figure rather
  than asking readers to remember company values from a later section.
- **Serial Position Effect** supports placing the immutable prediction record immediately after the
  headline estimate because it is a core claim, not a tertiary statistic.

Hick's Law, Fitts's Law, the Von Restorff Effect, and the Aesthetic-Usability Effect are not used.
There is no observed decision-time problem, undersized-target problem, or need for ornamental
contrast. Invoking those principles would not justify a product change here.

## Material change 1 — stage the homepage claims

- **User evidence:** The first screen combines the index, the latest forecast, tier shares, tier
  definitions, provenance, and two different time periods before the reader has an ordered mental
  model.
- **Relevant principle:** Cognitive Load.
- **Hypothesis:** *Untested behavioral hypothesis:* a literal two-sentence orientation followed by
  one claim per section will let a cold visitor distinguish “estimated company revenue share” from
  “timestamped monthly credit forecast” without reading the methodology first.
- **Product decision:** Keep the approved typography and all content, but reorganize the homepage
  into this literal reading order: `Debt-funded revenue` orientation; `Current estimate`;
  `Prediction ledger`; `Model record`; `How much is known`; `Credit flow`; `Evidence Lift`;
  `Companies`. Move the complete tier definition block out of the aggregate figure and into “How
  much is known.” Do not add a tagline, decorative hero, icon, or marketing copy.
- **Counter-risk:** Separating related evidence can make the 3.36% figure less traceable.
- **Verification:** The index figure must retain its period, DFR% unit, full band, Tier 1–3 badge,
  tier shares, numerator/denominator, and methodology link in one figure boundary. The tier
  definitions must remain on the homepage and preserve their exact meaning. The after-inventory
  must account for every moved line.

## Material change 2 — make the ledger visible as proof

- **User evidence:** The homepage's ledger proof is a small count below the first screen, while the
  scoreboard exposes live grades, calibration, the published miss, and immutable permalinks.
- **Relevant principle:** Serial Position Effect.
- **Hypothesis:** *Untested behavioral hypothesis:* placing a dedicated ledger section immediately
  after the current index will make the project's falsifiable record discoverable before readers
  reach attribution detail.
- **Product decision:** Give `Prediction ledger` its own second homepage section. Lead with the
  existing prediction/graded/pending counts, keep the latest prediction's full interval and
  immutable-record link, and surface the already-computed live calibration summary with a direct
  link to the full scoreboard. No prediction, grade, status, or statistic is recomputed or copied
  into a new feed.
- **Counter-risk:** Duplicating the full scoreboard would recreate the homepage overload.
- **Verification:** The homepage uses only the existing summary, latest-record, and live-calibration
  view objects; the complete row ledger remains only on `/scoreboard/`. Every displayed statistic
  carries its unit and link context. The public ledger and feed hashes must remain byte-identical.

### Owner follow-up — use the prediction rail for its evidence

- **User evidence:** In the rendered wide layout, the latest-prediction figure ends well before the
  current-estimate column. The resulting empty area sits directly above the ledger-count and
  historical-backtest figures, even though those figures explain the prediction record. The owner
  identified this relationship in the rendered page and requested that the three figures occupy
  that unused prediction-side area.
- **Relevant principles:** Cognitive Load and Gestalt Proximity. Proximity is used narrowly here to
  keep evidence beside the claim it qualifies, not to imply that unlike evidence types are the
  same measurement.
- **Hypothesis:** *Untested behavioral hypothesis:* a prediction-side evidence rail will let a
  reader connect the latest forecast with the public record and its historical validation without
  scanning into a separate lower grid.
- **Product decision:** At wide viewports, render the current DFR estimate in the left column and a
  prediction rail in the right. The rail contains the latest prediction and interval first, the
  current ledger count second, then the existing historical MAE and interval-coverage figures in
  two equal cells. Keep live-grade calibration as a full-width block below the opening spread.
  At narrow viewports, retain the semantic reading order: estimate, prediction, ledger count,
  backtest MAE, backtest coverage, then live calibration. Use CSS Grid without fixed heights,
  absolute positioning, or JavaScript layout behavior.
- **Counter-risk:** The ledger count is current public state, while MAE and coverage are historical
  backtest results. A visually undifferentiated cluster could make all three appear to describe the
  latest forecast or live grades.
- **Verification:** Use explicit `Live ledger` and `Historical backtest` labels separated by
  hairline rules. Preserve the figures' units and adjacent record/method links. Assert that each
  value appears once, that source order matches the narrow-screen visual order, and that the
  live-calibration block remains visibly and semantically separate. Inspect 390, 768, 1280, and
  1440 pixel layouts; rerun the no-JavaScript, keyboard, screen-reader, overflow, page-weight,
  inventory, feed, ledger, replay, and immutable-state gates.

## Material change 3 — publish the baseline as a group finding

- **User evidence:** Twelve companies differ from baseline; 38 rows are identical at 1.00x with the
  same evidence mix and repeated explanation.
- **Relevant principle:** Selective Attention.
- **Hypothesis:** *Untested behavioral hypothesis:* showing the 12 differentiated rows first and
  naming the remaining 38 as one expandable baseline group will make both findings faster to scan
  without reducing access.
- **Product decision:** Keep the 12 evidence-supported rows in the visible ranked table. Render the
  38 baseline-only rows inside a native HTML `<details>` element whose summary states the count and
  1.00x result. Keep every baseline company, rank, estimated DFR% band, Tier 1/2/3 split, status,
  and company link in the static HTML. State the baseline interpretation once directly above the
  baseline table instead of repeating it 38 times.
- **Counter-risk:** A closed disclosure can be mistaken for missing data, and some readers may not
  know it is expandable.
- **Verification:** The summary uses literal copy such as “Show 38 baseline-only companies at
  1.00x”; it is keyboard focusable, exposes native expanded/collapsed state, works without
  JavaScript, and has a visible focus indicator. Automated tests must prove a 12/38 partition, all
  50 unique tickers in the HTML, all bands and tier badges retained, and the one shared explanation
  present. Screen-reader inspection must confirm the summary is announced as an expandable control.

## Material change 4 — make Companies a real destination

- **User evidence:** The homepage contains the same 50 company destinations twice, and the primary
  `Companies` nav item unexpectedly stays on the homepage.
- **Relevant principle:** Jakob's Law.
- **Hypothesis:** *Untested behavioral hypothesis:* a dedicated alphabetical directory will better
  match navigation expectations and make a specific company findable without forcing readers past
  the full Evidence Lift ranking.
- **Product decision:** Add `/companies/` as a first-class no-JavaScript page containing the complete
  alphabetical 50-company index with each estimated band. Point `Companies` in primary navigation
  to that page on every surface. Replace the homepage's second 50-company list with a short
  `Companies` section linking to the directory. Preserve `/#companies` as a valid anchor so existing
  fragment links still land on an explanatory company section. Do not redirect or move any of the
  50 company permalinks.
- **Counter-risk:** Moving the alphabetical list can make it one activation farther from the
  homepage and can accidentally orphan existing links.
- **Verification:** `/companies/` returns 200 directly; all 50 current company URLs remain 200 with
  zero redirects; `/#companies` still resolves to a present element; keyboard order reaches the
  Companies nav link, directory entries in alphabetical order, then the chosen company. The after
  inventory must map every removed homepage company line to `/companies/`.

## Material change 5 — anchor the aggregate to published company context

- **User evidence:** 3.36% has an uncertainty band but no same-publication high/low context.
- **Relevant principle:** Working Memory.
- **Hypothesis:** *Untested behavioral hypothesis:* a concise same-period comparison will prevent
  readers from interpreting 3.36% as a threshold, score, or typical-company value.
- **Product decision:** Add one sentence beside the existing aggregate explaining that it is
  revenue-weighted and stating the lowest and highest already-published company midpoint estimates
  for the same period. This is a descriptive selection from the existing 50 company outputs, not a
  benchmark, index input, model feature, feed field, or methodology change. Name it a range, not
  “normal,” “high,” “low,” “good,” or “bad.”
- **Counter-risk:** A range can be mistaken for a normative benchmark or can drift from company
  pages.
- **Verification:** Derive both endpoints from the already-computed `CompanyEstimate` objects at
  render time; tests must prove they equal the minimum and maximum existing midpoints and that the
  corresponding company pages show the same values. No source, feed schema, model, attribution
  weight, or published numeric estimate changes.

## Material change 6 — preserve semantic location and bypass

- **User evidence:** The live DOM has no skip link, no main target ID, and no `aria-current` value in
  primary navigation.
- **Relevant principle:** None. This decision is governed directly by accessibility and semantic
  hierarchy requirements, which outrank the heuristic catalog.
- **Hypothesis:** No behavioral claim is needed; the current page and a main-content bypass are
  explicit interface states.
- **Product decision:** Add a first-focus skip link to `#main-content`, assign that ID to the main
  landmark, and render `aria-current="page"` on the matching primary page destination. Keep the
  link visually hidden until focused and use the existing ink focus language.
- **Counter-risk:** Incorrect active-page state would be worse than no state, and a hidden skip link
  can become unreachable or clipped.
- **Verification:** Assert exactly one main landmark, one valid skip target, one current nav item on
  Scoreboard/Companies/Methodology/Changelog pages, no false current item on prediction records or
  JSON, and a visible first-focus skip link at mobile and desktop widths.

## Primary and alternate paths to verify

Primary cold-visitor path:

1. Arrive at `/` with no prior context and JavaScript disabled.
2. State in plain language what DFRI estimates and what it predicts.
3. Read the current DFR% as a band, place it within the covered-company range, and identify that it
   is revenue-weighted.
4. Find the immutable prediction ledger, its graded/pending state, and the route to full records.
5. Read how Tier 1/2/3 affect how much is known.
6. Open `Companies`, find a named company alphabetically, and reach its evidence page.

Alternate expert paths:

- Open `/scoreboard/` or a cited prediction permalink directly and retain the same URL, grade,
  intervals, provenance, and navigation context.
- Open `/#companies` from an existing citation and reach the directory link without a redirect.
- Expand all 38 baseline companies using only keyboard controls and no JavaScript.

## Implementation boundaries

Expected durable changes are limited to:

- homepage, base, and new company-directory templates;
- the static publisher's page contexts and route output;
- CSS needed for the new information groups, disclosure, skip link, and current-page state;
- tests and permanent quality checks for route preservation, content partition, semantic
  navigation, no-JavaScript disclosure, keyboard focus, and inventory preservation;
- `DESIGN.md` only where its navigation and page-coverage rules must describe the new current
  architecture; the type scale, palette, chart system, figure rules, and editorial ledger visual
  system remain unchanged;
- changelog copy describing presentation and information architecture only.

Explicit non-goals: no new data source, market data, model, attribution computation, company
estimate, feed field, schema version, URL redirect, tracker, cookie, remote asset, icon system,
engagement mechanic, urgency device, or dark pattern.

## Verification gate

No implementation is complete until all of the following pass:

1. Generate `UX_INVENTORY_AFTER.md` with the same capture contract and a machine-produced diff.
   Every removed line or link must map to its new location or to the explicit 38-to-one repeated-copy
   consolidation above.
2. Re-run every invariant in `REGRESSION.md`: prediction/grade/publication fields, three canonical
   ledger hashes, all published feed bytes, every company DFR% and tier share, aggregate value,
   route and permalink integrity, provenance links, rendered-number rules, full tests and coverage,
   deterministic replay, cold-clone verification, and adversarial immutability.
3. Run JavaScript-enabled and disabled audits over every HTML page, including the new directory and
   every permalink. Enforce less than 500 KB and less than 1 second estimated 4G load per page,
   WCAG AA contrast, zero critical accessibility violations, and no horizontal overflow at mobile
   widths.
4. Programmatically inspect keyboard order, first-focus skip behavior, visible focus styles, native
   disclosure semantics, heading outline, landmarks, link names, `aria-current`, and accessible SVG
   names on every page. Record the worst finding, including `none` only if the evidence supports it.
5. Walk the primary path in the rendered site at desktop and mobile widths. Report observed
   behavior and remaining uncertainty; do not claim the untested behavioral hypotheses are proven.
