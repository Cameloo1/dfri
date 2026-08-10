# DFRI first-visit path report

Verified on 2026-08-10 against the deterministic local publication candidate. This is an observed
walkthrough, not a behavioral claim about all readers.

## Path walked

1. **Arrive cold on the homepage.** The H1 says “Debt-funded revenue.” The first paragraph states
   the claim: DFRI estimates the share of covered companies' U.S. consumer revenue funded by new
   consumer credit, while the ledger records forecasts before official releases and grades them
   against first prints.
2. **Understand the headline.** The current-estimate section gives the revenue-weighted midpoint,
   the ordered 80% band, units, company count, evidence-tier mix, and methodology link. A same-period
   company range prevents the aggregate from reading like an unexplained score.
3. **Understand how much is known.** The “How much is known” section defines observed,
   category-mapped, and fungible evidence before the flow and company ranking. The text-equivalent
   chart tables now keep those definitions usable without interpreting SVG geometry.
4. **Look for credibility.** The prediction-ledger section explains timestamping, first-print
   grading, immutable records, and the difference between live grades and historical backtests.
   The Scoreboard navigation item resolves to a dedicated page rather than an on-page anchor.
5. **Find a company.** Companies opens the 50-row alphabetical directory. General Motors was found
   and opened in two navigations from the homepage. Its page presents the DFR% band, evidence-tier
   decomposition, version history, filing evidence, assumptions, and sensitivity in that order.
6. **Find scope and correction boundaries.** Roadmap is now a primary navigation destination.
   Corrections and roadmap links also appear in every page footer.

## Breaks found and cheap fixes made

- The prior homepage flow had no text-equivalent ledger. It now publishes every exact ribbon as a
  source, destination, amount, and tier row.
- Company current/history bands and the evidence decomposition relied on image semantics. Each now
  has a visible, programmatically associated table with units and provenance.
- A first-time reader could not distinguish deliberate exclusions from unfinished work. The new
  roadmap-and-boundaries page separates current publication, planned milestones, and exclusions,
  including source-term rejections and the use-versus-redistribution rule.
- There was no site-wide route for reporting a company-estimate error. The new corrections policy
  names the contact path, acknowledgement target, verification process, append-only correction
  behavior, and retrieval path for prior values.
- The company chart caption used an awkward possessive for company names. It now reads “the current
  estimated DFR% band for [company].”

## Remaining uncertainty and friction

- The local candidate contains four pending records and no live grades, so its rendered Scoreboard
  cannot demonstrate the already-published first-grade callout. That is a publication-state
  boundary, not a copy problem. This candidate must not be deployed over a live Git-backed ledger
  until the ledger-state regression and promotion gates prove the accepted live grades survive.
- The status frame reports incomplete automation in preview mode because local builds do not carry
  live workflow receipts. The preview banner states that boundary, but it delays the credibility
  story for a reader evaluating the local candidate.
- The complete credit-flow table is long. Bundling its rows would make the screen-reader version
  easier to scan but would stop it being equivalent to every rendered ribbon.
- “DFR%” still requires either the opening sentence or methodology for a full conceptual model. The
  first paragraph is sufficient to state the claim, but a reader seeking the exact numerator and
  denominator must continue into the current-estimate note or methodology.

## Inventory regression disposition

A like-for-like build of the pre-polish commit and the final candidate found **zero missing URLs,
internal-link targets, or outbound-link targets**. The final inventory adds the corrections and
roadmap routes; the Companies route also appears as an inventory addition because the permanent
inventory tool now includes that pre-existing directory in its route set.

Six old flow-summary lines no longer occur byte-for-byte. They were not removed from the
publication: four category-to-company summary rows were replaced by 12 exact category-to-company
ribbon rows, and the two summary headings were replaced by the explicit source, destination,
estimated amount, and evidence-tier table contract. The same category totals remain visible in the
SVG and are exactly reconstructable from the table: Tier 1 $7,196M, general retail $4,620M, other
Tier 2 $4,479M, and Tier 3 $4,240M. This is a granularity increase required for an equivalent
screen-reader representation, not a data removal.

The older committed UX inventory is not a like-for-like state baseline: it contains six
predictions and two grades, while the deterministic seed candidate contains four pending records
and no grades. That separate candidate-state mismatch is why the older inventory reports two
missing prediction permalinks and many changed values. It remains a deployment stop-gate as stated
above; no record was altered to make the inventory pass.

## Outcome

The claim is first understandable in the homepage opening paragraph, evidence depth becomes legible
in the current-estimate and tier sections, and a named company is reachable through a conventional
directory path. Credibility is structurally attached to the prediction ledger, provenance links,
and append-only correction policy. The remaining live-grade gap in this local candidate is kept
explicit rather than treated as a successful live-state verification.
