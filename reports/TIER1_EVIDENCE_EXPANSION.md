# Tier 1 evidence expansion — 2026-08-10

## Result

The filing sweep moves one of the 38 baseline-only companies, TJX, to an evidence-supported
estimate. The other 37 remain baseline-only because no reviewed provider filing supplied both a
covered-company relationship and a defensible program amount. Named or provider-wide activity was
not converted into a company weight.

Methodology 1.2.1 adds `A-T1-TJX-SYF-001`. Two dated Synchrony Card Issuance Trust filings
report TJX Dual Card receivables of $2.228 billion at 2025-10-31 and $2.357 billion at 2026-01-31.
The observed $128.2 million adjacent-quarter increase is an independent scale check for the registered
0.4% / 0.8% / 1.5% share of national quarterly revolving-credit flow. Because the trust dates do not
exactly match the 2026-Q1 G.19 window, the mapping retains a deliberately wide band.

## Restatement

| Output | Methodology 1.2.0 | Methodology 1.2.1 | Change |
|---|---:|---:|---:|
| TJX estimated DFR% band | 1.35% / 1.56% / 1.82% | 2.29% / 2.75% / 3.32% | +1.19 pp midpoint |
| TJX Tier 1 share | 0.0% | 42.7% | +42.7 pp |
| TJX Evidence Lift | 1.00x baseline-only | 1.76x evidence-supported | +0.76x |
| Revenue-weighted aggregate DFR% | 3.27% / 3.54% / 3.81% | 3.29% / 3.56% / 3.84% | +0.02 pp midpoint |

The 49 unaffected company result objects are byte-identical between 1.2.0 and 1.2.1. This is enforced
by an additive-draw-stream regression test: a newly appended assumption receives an identity-keyed
Monte Carlo stream and cannot perturb an unrelated company through random-number iteration order.
The complete 1.2.0 registries remain committed and loadable.

## Provider findings

- Synchrony: program-level trust balances support the new TJX link and independently strengthen the
  existing Amazon and Lowe's links.
- Bread Financial: Ulta is identified as a program contributing at least 10% of Bread's 2025 total net
  interest and non-interest income. Ulta was already evidence-supported; no weight changed.
- Affirm: Amazon represented 22% of Affirm's $36.7 billion fiscal-2025 GMV. Amazon was already
  evidence-supported; no weight changed because adding a second overlapping Amazon lane would
  double count financing.
- Capital One and historical Discover: the current filings provide aggregate card and network
  measures but no active covered-company program amount. Capital One's former Walmart program had
  terminated, so it was not carried forward as current evidence.
- Klarna: the filing reports provider-wide GMV and payment-option mix but no covered-company program
  amount usable for a numerical mapping.
- Block / Afterpay: the filing explains BNPL mechanics and merchant-fee accounting but does not name a
  covered merchant with a program-level amount.

The machine-readable review ledger is
`src/dfri/attribution/tier1_evidence_review_v1.json`. Only rows marked
`APPROVED_NUMERIC_MAPPING` may activate a new Matrix lane. The new TJX assumption also carries
`review_status=APPROVED`; registry validation rejects a new assumption with any other status.
