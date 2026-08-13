# Auto-allocation restatement report

Status: **CANDIDATE — NOT PUBLISHED**

Prepared: 2026-08-09

Prior methodology: 1.1.1

Candidate methodology: 1.2.0

This is the required pre-publication movement report. It does not authorize a push, deployment, or
public restatement. Methodology 1.1.1 remains byte-reproducible from its versioned registries and
source hash `29c35c118d3380c7b37d6de3084ea613fccfd1b186c5fe5a9deca53da12aee3b`.
The candidate 1.2.0 source hash is
`6e5bf9bdf66c0cc0c2b784a7d7ff6350bea09a065d65a402146fa1a2ebec58e7`.

## Replacement evidence

The active 1.1.1 assumption `A-T2-NONREV-AUTO-001` (8% / 12% / 18%) is retired in
1.2.0. Its replacement is `A-T2-NONREV-AUTO-002` (10% / 16.5903581813124% / 23%).

| Evidence lane | 2026-Q1 result | Scope and interpretation |
|---|---:|---|
| FFIEC Schedule RC-C Part I item 6 | 63.098018% automobile share of directly reported bank automobile plus other-consumer loans | Direct U.S. bank regulatory balances; excludes credit unions. |
| NCUA 5300 Call Reports | 76.810623% automobile share of the derived credit-union nonrevolving-consumer residual | Direct credit-union vehicle balances; residual denominator removes real-estate, commercial, card, and lease balances. |
| FFIEC + NCUA regulated-lender reconciliation | 68.902001% automobile share after combining the bank and credit-union balances in common dollar units | Reconciles the two direct-reporting populations; it is a regulated-lender cross-check, not a national covered-company allocation. |
| Federal Reserve Board G.19 | 41.024967% motor-vehicle share of national nonrevolving balances | Broad national scale anchor; includes non-auto nonrevolving products outside the bank/credit-union direct comparison. |
| Six-trust SEC Auto ABS sample | 40.439662% covered-sponsor share of original pool amount | Independent securitization sample; covered sponsors are Ford and two GM trusts. It does not include Tesla or Carvana trusts. |

The FFIEC and NCUA balances reconcile to a combined 68.902001% regulated-lender auto share after
converting FFIEC thousands to dollars. The candidate midpoint is the Board national motor-vehicle
share multiplied by the covered-sponsor share of the six-trust ABS sample. The leave-one-trust-out allocation range is 11.646519% to
22.003288%. The registered 10% to 23% band encloses that range and is deliberately wider because
the four source lanes have different scopes and the trust sample omits Tesla and Carvana. The
method does not select the FFIEC or NCUA level and mislabel it as a national covered-company share.

## Published-value movement if approved

All values are estimated DFR% of U.S. consumer revenue. Bands are the complete 80% low/high range;
changes are percentage points in the midpoint.

| Company | 1.1.1 estimated band (mid) | 1.2.0 candidate estimated band (mid) | Midpoint change |
|---|---:|---:|---:|
| Carvana (CVNA) | 15.137127%–22.385413% (18.672152%) | 16.605996%–24.084823% (20.258700%) | +1.586547 pp |
| Ford (F) | 14.857868%–20.487062% (17.483240%) | 16.182676%–22.260809% (19.052356%) | +1.569116 pp |
| General Motors (GM) | 15.757759%–21.191214% (18.306818%) | 17.101586%–22.994678% (19.857972%) | +1.551154 pp |
| Tesla (TSLA) | 4.962120%–9.210960% (6.602237%) | 6.097942%–11.401803% (8.162502%) | +1.560264 pp |

The revenue-weighted 50-company headline moves from an estimated
3.110711%–3.615374% (midpoint 3.358728%) to an estimated
3.267153%–3.810511% (midpoint 3.535847%), a **+0.177119 percentage-point** midpoint
restatement. No company coverage, company revenue denominator, measured consumer-credit flow, or
nowcast output changes.

## Preservation and publication boundary

- The 1.1.1 assumption, matrices, company inputs, flow inputs, source hash, and reproducible values
  remain in the repository and the methodology comparison page.
- The 1.2.0 output receives a new methodology version and source hash. It never overwrites 1.1.1.
- The changelog entry describes the source retirement and restatement without softening the
  movement.
- No public feed or Pages deployment has been changed by this candidate. Deployment requires a
  separate owner approval after the complete verification gates pass.
