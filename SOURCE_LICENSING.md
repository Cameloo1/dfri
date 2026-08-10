# Source licensing and continuity

Verified through 2026-08-09. This is the public record for source identity, automation,
storage, derivative use, redistribution, and fallback status. The machine-readable source
contracts remain in `src/dfri/ingest/source_contracts.json`; assumption-specific active and
fallback sources remain in `src/dfri/attribution/source_registry_v1.json`.

## Two separate permission gates

Permission to retrieve or use a source does not imply permission to publish its content or a
derived compilation under DFRI's feed license. Every source must independently pass:

1. **Use:** automated access, local storage, reproducible transformation, and the intended
   analysis are permitted.
2. **Redistribution:** the published aggregate or derivative may be redistributed under the
   stated terms without imposing incompatible restrictions.

Failure of either gate marks that source non-permitted for the affected publication lane. The
pipeline must use a registered permitted fallback with a wider band or publish `BLOCKED`; it must
not silently retain a disallowed source.

## Methodology 1.2 auto-allocation sources

| Source | Verified contract | Pinned current fields | Role |
|---|---|---|---|
| FFIEC Call Reports, accessed through the FDIC public financials API and FFIEC bulk surface | The [Data.gov catalog](https://catalog.data.gov/dataset/ffiec-call-reports) identifies the dataset as public and public-domain. The [FFIEC 051 page](https://www.ffiec.gov/resources/reporting-forms/ffiec051) identifies the June 2026 form and December 2025 instructions as current. | Schedule RC-C Part I item 6: `RCONB538` credit cards, `RCONB539` other revolving, `RCONK137` automobile, `RCONK207` other consumer. FDIC normalized fields: `LNCRCD`, residual other revolving within `LNCONORP`, `LNAUTO`, `LNCONOTH`. | Primary bank auto/non-auto evidence. |
| NCUA 5300 Call Reports | The [quarterly page](https://ncua.gov/analysis/credit-union-corporate-call-report-data/quarterly-data) publishes final ZIP CSV files suitable for database import. NCUA states that its federal material is [public domain unless otherwise noted](https://ncua.gov/support-services/guaranteed-notes-program). | `ACCT_370` used vehicle, `ACCT_385` new vehicle, `ACCT_396` credit cards, `ACCT_002` leases, `ACCT_025B1` total loans/leases, `ACCT_RL0047` consumer real estate, and `ACCT_400A1`/`ACCT_400B1` commercial balances. | Independent credit-union cross-check and first fallback. |
| Federal Reserve Board G.19 | Board website information is [public domain unless otherwise indicated](https://www.federalreserve.gov/disclaimer.htm). The dated [May 7, 2026 G.19 release](https://www.federalreserve.gov/releases/g19/20260507/) supplies the March 2026 first-print motor-vehicle and nonrevolving balances. | Motor vehicle and national nonrevolving balances, both billions of U.S. dollars. | National scale anchor and independent fallback. |
| SEC Auto ABS-EE | The SEC documents programmatic EDGAR access and a ten-request-per-second fair-access ceiling in its [developer resources](https://www.sec.gov/about/developer-resources). DFRI retains only curated trust-level aggregates in the public method. | Earliest curated original-pool amount for six registered auto trusts; covered sponsors are Ford and the two registered GM trusts. | Independent level/direction cross-check and fallback. |

The registered prior is 10.0% / 16.5903581813124% / 23.0%. Its midpoint is the Board
motor-vehicle share of national nonrevolving balances multiplied by the covered-sponsor share of
the six-trust ABS sample. The band encloses the leave-one-trust-out range and is wider because the
FFIEC, NCUA, national-balance, and securitization samples do not have identical scopes. DFRI does
not pick one incompatible scope and describe it as consensus.

## Retired NY Fed Household Debt and Credit input

Methodology 1.1.1 cited the New York Fed Household Debt and Credit material for
`A-T2-NONREV-AUTO-001`. The [New York Fed Terms of Use](https://www.newyorkfed.org/privacy/termsofuse.html)
permit automated access, downloading, storage, distribution, modification, and derivative works,
but condition redistribution on preserving the same permissions and not imposing more restrictive
terms. DFRI's blanket CC BY-NC 4.0 feed license is more restrictive because it excludes commercial
reuse. That makes retrieval and analysis permitted while making the existing blanket feed-license
posture incompatible for redistributed New York Fed content or derivatives.

Methodology 1.2 therefore retires the NY Fed source from the active auto-allocation assumption.
The separately versioned 1.1.1 record remains reproducible and retrievable as methodology history;
it is not rewritten or represented as an active 1.2 source. Any preserved New York Fed content
continues to be subject to the New York Fed terms rather than being relicensed by DFRI.

## Criticality and automatic degradation

Criticality policy `midpoint-dependency-v1` computes, for every assumption, the greater of:

- its share of the midpoint attributed numerator; and
- its share of the midpoint covered-company denominator.

An assumption is `CRITICAL` at 5% or more. `reports/ASSUMPTION_CRITICALITY.json` is generated from
the registries and is blocking if stale. A critical assumption without an independent registered
fallback is a build warning. Methodology 1.2 currently has 11 critical assumptions and no such
warning. The five critical issuer-denominator assumptions use Census MARTS category evidence as a
weaker independent fallback to issuer filings; the band widens by 1.25x, the site renders a
source-degradation notice, and the v2 company-feed metadata identifies the active source,
effective band, and reason.

Source selection is a versioned input, not a hidden network-time decision. If a source registry
row becomes unavailable or non-permitted, the deterministic build selects the first available,
permitted fallback from a different independent group. It preserves the midpoint, widens the
registered band, and renders a site-wide source note. If no registered source remains, the build
fails with `BLOCKED`.
