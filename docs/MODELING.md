# DFRI modeling architecture

**Scope:** implemented behavior in the 2026-08-10 deployment candidate; public scheduling remains
subject to the separate deployment approval gate

**Current headline model:** `bridge-ridge-v2-alpha10`

This document describes the statistical machinery that exists in the code. It does not describe a
planned model as if it were running. The source of truth is the implementation under
[`src/dfri/nowcast/`](../src/dfri/nowcast/) and
[`src/dfri/attribution/`](../src/dfri/attribution/), together with the canonical
[M2 backtest](../reports/M2_BACKTEST.md).

## Architecture at a glance

DFRI has three distinct calculation systems:

1. A small monthly nowcast system estimates first-print changes in revolving and nonrevolving
   consumer credit. This is the only part that estimates statistical model parameters from
   historical observations.
2. A second monthly clock predicts Treasury MTS federal deficit and total outlays. It selects an
   honest benchmark separately for each series and adds empirical intervals from prior
   out-of-sample errors; it does not reuse or blend G.19 calibration statistics.
3. A deterministic attribution system applies documented debt-product, spending-category, and
   company mappings. It performs no learning. Monte Carlo sampling propagates registered
   uncertainty through those mappings.

The currently deployed prediction ledger contains only `bridge-ridge-v2-alpha10`. The deployment
candidate adds `mts-benchmark-empirical-v1:mts-naive-seasonal-v1` for deficit and
`mts-benchmark-empirical-v1:mts-ar2-ols-v1` for outlays without changing a G.19 record.

## Build order and version surfaces

The original public baseline commit landed the nowcast modules together, so Git does not preserve a
finer implementation chronology inside that commit. The surviving execution and deviation ledgers
record the model-family order as baselines, ridge bridge, then state-space candidate. The ridge
bridge also had a pre-public v1 predecessor that was superseded before the public clock started.

| Order | Model | Implemented version string | Where it appears publicly |
|---:|---|---|---|
| 1 | Random-walk baseline | `naive-random-walk-v1` | Canonical backtest only |
| 1 | Seasonal-naive baseline | `naive-seasonal-v1` | Canonical backtest only |
| 1 | Autoregression baseline | `ar2-ols-v1` | Canonical backtest and live-grade naive comparison |
| 2 | Ragged-edge ridge bridge | `bridge-ridge-v2-alpha10` | Canonical backtest and every public prediction record |
| 3 | Mixed-frequency state-space candidate | `mixed-frequency-kalman-v1-sm0.14.6` | Canonical backtest only |
| 4 | MTS random-walk benchmark | `mts-naive-random-walk-v1` | MTS point-in-time backtest |
| 4 | MTS seasonal benchmark | `mts-naive-seasonal-v1` | MTS backtest; selected for deficit |
| 4 | MTS AR(2) benchmark | `mts-ar2-ols-v1` | MTS backtest; selected for outlays |
| 4 | MTS empirical-band forecast | `mts-benchmark-empirical-v1:<selected>` | Deployment-candidate prediction records |

`bridge-ridge-v1` is retired history, not a current runnable model. Its first local ragged-edge run
used effectively unregularized ridge behavior (`alpha=1e-6`) and produced extreme leverage and
multi-trillion-dollar intervals when retail was missing and H.8 was partial. No v1 row was
published. The current implementation labels bridge forecasts as v2 and defaults to fixed
`alpha=10`; it does not perform an alpha search during fitting.

## Nowcast targets and feature boundary

The two targets are monthly changes in first-print G.19 levels, in millions of U.S. dollars:

- `DELTA_DTCTLR.M`: revolving consumer credit;
- `DELTA_DTCTLN.M`: nonrevolving consumer credit.

Each target is computed from one dated G.19 release: the preliminary target-month level minus the
revised prior-month level in that same release. This is release-coherent and avoids comparing levels
from two differently revised releases.

The bridge and state-space models share point-in-time feature construction:

- Revolving uses H.8 series `B1247NCBA`; nonrevolving uses `B3248NCBA`.
- Each available in-month Wednesday contributes the change from its preceding Wednesday.
- `h8_coverage` is observed Wednesdays divided by all Wednesdays in the target month.
- `h8_paced_change` is the sum of observed weekly changes divided by coverage. It is missing if no
  target-month Wednesday is available.
- `DELTA_RETAIL_SALES.M` is the release-coherent Census MARTS first-print retail flow. It is present
  only when its dated release existed at the forecast boundary.
- The feature hash binds the target, as-of time, exact H.8 evidence, retail evidence, release times,
  and source checksums.

Historical feature rows use the final H.8 release available immediately before each target's G.19
first print. A live prediction uses the latest common H.8 release available to both target series.

## Models

### 1. Random walk

Form:

```text
forecast(t) = actual(t - 1)
```

There are no fitted parameters. The input is the continuous sequence of strictly earlier
first-print target flows. The model emits a point forecast, target identity, training-observation
count, and evidence-sensitive input hash. It does not emit an interval.

Implementation: [`baselines.py`](../src/dfri/nowcast/baselines.py), version
`naive-random-walk-v1`.

### 2. Seasonal naive

Form:

```text
forecast(t) = actual(t - 12 months)
```

There are no fitted parameters. A forecast exists only when the same calendar month one year
earlier is present. Under the committed 2018-forward comparison window, every evaluated month has
the required lag. It emits the same point-only record fields as the random walk and no interval.

Implementation: [`baselines.py`](../src/dfri/nowcast/baselines.py), version
`naive-seasonal-v1`.

### 3. AR(2) by ordinary least squares

Form:

```text
y(t) = beta0 + beta1 * y(t - 1) + beta2 * y(t - 2) + error(t)
```

The coefficients are re-estimated for each forecast with `numpy.linalg.lstsq` over the expanding
window. The production backtest requires at least 24 prior monthly observations, although the
low-level function's mathematical minimum is four. A rank-deficient design fails closed. The model
emits a point forecast, training count, and input hash; it does not estimate public 80/95 bands.

Implementation: [`baselines.py`](../src/dfri/nowcast/baselines.py), version `ar2-ols-v1`.

### 4. Ragged-edge ridge bridge

The bridge regresses the monthly first-print target on a 16-column design:

```text
intercept
+ standardized H.8 coverage-paced change
+ H.8 coverage
+ standardized retail first-print flow
+ retail-present indicator
+ February-through-December month indicators
```

Available H.8 and retail values are standardized with means and population standard deviations
estimated from the prior training window. A missing H.8 paced value or retail value is set to its
training mean, which becomes zero after standardization. H.8 coverage remains explicit; retail has
a separate availability indicator. Standard deviations below `1e-12` are replaced by 1.0.

The fitted coefficients are:

```text
beta = inverse(X'X + alpha * D) * X'y
```

where `alpha=10`, the intercept's penalty is zero, and all other diagonal penalty entries are 10.
At least 36 prior months are required. This alpha is fixed configuration, not a fitted parameter or
an automated hyperparameter search result.

The interval calculation uses in-sample ridge residuals and forecast leverage:

```text
residual_variance = residual_sum_of_squares / max(n - rank(X), 1)
prediction_variance = residual_variance * (1 + x' * inverse(X'X + alpha*D) * x)
```

The model applies normal quantiles 1.2815516 and 1.959964 to produce symmetric nominal 80% and 95%
prediction intervals. These are model-based normal-theory intervals; they are not conformal or
empirical-quantile intervals.

It emits model version, target, target period, timestamp, point, nested 80/95 bands, training count,
and an input hash covering all target and feature evidence. The scheduled job replaces the feature
boundary timestamp with the actual first accepted execution time before append.

Implementation: [`bridge.py`](../src/dfri/nowcast/bridge.py), current version
`bridge-ridge-v2-alpha10`.

### 5. Mixed-frequency state-space candidate

This is a one-state linear Gaussian Kalman filter. The latent state is standardized monthly G.19
flow:

```text
x(t) = month_intercept(t) + phi * x(t - 1) + state_error(t)
```

The transition intercept, lag coefficient, and February-through-December effects are estimated by
ridge regression with penalty `1e-6`; the intercept is unpenalized. `phi` is clipped to
`[-0.98, 0.98]`. State variance is the mean squared transition residual with a `1e-6` floor.

Each monthly observation vector has seven channels:

1. the standardized G.19 target;
2. through 6. up to five separately observed standardized H.8 Wednesday changes;
7. standardized first-print retail flow.

The H.8 and retail intercepts/loadings are estimated by separate ridge regressions against the
standardized training target. Measurement variances are residual mean squares with the same floor.
The forecast month's G.19 target is missing; unreleased H.8 slots and retail are also `NaN`, which
the Kalman filter handles as missing observations. Parameters are estimated with deterministic
linear systems before filtering; the code does not run statsmodels maximum-likelihood optimization.

The point is the final filtered state transformed back into target units. Its interval combines the
filtered state covariance with a small target-measurement variance and applies the same normal 80%
and 95% quantiles as the bridge. At least 36 prior months and at least 24 available retail
observations are required.

The statsmodels version is embedded in the model version. With the current lock it is
`mixed-frequency-kalman-v1-sm0.14.6`. The candidate is evaluated but not written to the public
prediction ledger.

Implementation: [`state_space.py`](../src/dfri/nowcast/state_space.py).

### 6. Treasury MTS benchmark clock

The MTS clock targets `MTS:DEFICIT.M` and `MTS:OUTLAYS.M`, both in millions of U.S. dollars.
Each target is read from Table 1 of one dated Monthly Treasury Statement issue, so the value and
its release boundary come from the same first-print document. The Vintage Guard admits only issue
PDFs whose release timestamp is pinned in the official calendar; historical September issues whose
calendar lists no exact date are omitted rather than interpolated.

The clock fits only three prescribed benchmarks: last value, same month one year earlier, and an
expanding-window AR(2) by ordinary least squares. It selects the lowest point-in-time MAE separately
for each series. No bridge or state-space candidate is implemented because none has first proved it
beats the benchmark hierarchy.

Live 80% and 95% bands use the 80th and 95th percentiles of strictly prior absolute out-of-sample
errors, with at least 24 errors required. Backtest coverage is likewise expanding-window: the
current observation never enters the width used to judge itself. The committed 2018–2026 report
selects seasonal naive for deficit (MAE 130,896.7; 83.3%/100.0% interval coverage; 76.2% acceleration
sign accuracy) and AR(2) for outlays (MAE 90,750.6; 86.2%/100.0% coverage; 62.3% sign accuracy).
Outlays misses the inherited improvement, 80%-coverage, and sign-accuracy bars; that gap is logged
instead of being hidden behind an unproved model.

## Benchmark hierarchy and headline selection

The canonical comparison is [`m2-point-in-time-backtest-v1`](../reports/m2_backtest.json), fixed at
an as-of boundary of `2026-08-04T23:59:00Z`. It contains 101 expanding-window forecasts from
2018-01-31 through 2026-05-31. Training data starts in January 2015 and every forecast uses only
strictly earlier targets and release-available features.

The hierarchy implemented in code is:

1. Evaluate random walk, seasonal naive, and AR(2); the lowest primary-target MAE is the best naive
   benchmark.
2. Compare bridge and state-space MAE on the primary revolving target only; the lower-MAE model is
   the headline candidate.
3. Apply the acceptance bars to that headline candidate. Nonrevolving results are a secondary
   diagnostic and cannot change the headline selection.

Current primary-target results:

| Model | MAE | Improvement vs best naive | 80% coverage | 95% coverage | Acceleration sign accuracy |
|---|---:|---:|---:|---:|---:|
| `naive-random-walk-v1` | 9,186.139 | — | — | — | 0.0% |
| `naive-seasonal-v1` | 8,836.634 | — | — | — | 71.3% |
| `ar2-ols-v1` | 7,486.797 | best naive | — | — | 73.3% |
| `bridge-ridge-v2-alpha10` | 4,632.858 | 38.1% | 71.3% | 93.1% | 87.1% |
| `mixed-frequency-kalman-v1-sm0.14.6` | 5,073.118 | 32.2% | 63.4% | 85.1% | 86.1% |

Acceleration sign is a three-way comparison: the sign of `forecast(t) - actual(t-1)` must equal
the sign of `actual(t) - actual(t-1)`. It is not simply the sign of the forecast level.

The bridge wins the bridge-versus-state-space MAE comparison and beats the best naive AR(2) by
38.1%, so `bridge-ridge-v2-alpha10` is the current headline and production model.

### Implemented acceptance-gate divergence

The report field `coverage80_within_5pp` is mislabeled relative to its implementation. The code
accepts 80% coverage from 70% through 90%, which is a plus-or-minus 10 percentage-point window.
Therefore the observed 71.3% passes the implemented gate but would fail a literal plus-or-minus
5-point requirement of 75% through 85%. The 95% gate is implemented as 90% through 100%, which is
the literal plus-or-minus 5-point window around 95%. This document records the divergence; it does
not change the backtest or selection pipeline.

## Point-in-time correctness

[`VintageGuard`](../src/dfri/lake/guard.py) is the sole model-facing series-read boundary. It
requires a timezone-aware release timestamp and returns only rows with `release_date <= as_of`.
Model packages are prevented by an AST boundary test from importing the direct lake reader or store.
A poisoned-future canary proves that a visibly extreme future value disappears when read through
the guard.

Additional contracts make the time boundary meaningful:

- first-print target histories must be monthly-continuous, have one dated G.19 source per month,
  and have strictly increasing release times;
- expanding-window models use only `targets[:index]` and `features[:index]`;
- a training feature must predate the target release it is paired with;
- H.8 selection keeps the latest vintage that existed at the historical forecast boundary;
- unreleased retail and future H.8 weeks stay missing;
- source URLs, release times, checksums, feature hashes, and model versions enter forecast identity.
- MTS history accepts only dated Fiscal Data issue PDFs, exact schedule timestamps, verified Table 1
  identifiers and units, and does not fill the explicit September calendar gaps.

This boundary matters more to DFRI's claim than adding a more sophisticated estimator. Revised
data, later weekly observations, or a retail release that was not public yet can make a historical
forecast look much better than a forecast that could actually have been issued. DFRI's claim is
that a prediction was written down using information then available and later graded against the
first print. A complex model with lookahead would invalidate that claim; a simple model behind a
provable vintage boundary can support it.

## Attribution engine: mapping and uncertainty, not learning

The attribution engine performs no learning and fits no parameters to historical company outcomes.
Its current deployment-candidate methodology bundle is `1.2.1`; immutable 1.2.0 inputs remain
loadable for the restatement comparison.

Matrix A is a hand-built, evidence-linked mapping from debt products to spending categories. Each
row carries a tier and a low/mid/high prior. Every current Matrix A row references exactly one
assumption ID, and validation requires the row's prior to equal the Assumption Registry prior.

Matrix B is a hand-built, evidence-linked mapping from spending categories to covered companies.
Rows carry weights, a method, and evidence references. Uncertain Matrix B rows must reference one
registered assumption whose prior matches the row. Fixed Matrix B rows are also permitted: direct
links can be 1.0 and normalized category allocations can be deterministic consumer-revenue
midpoints.

The Assumption Registry is the source of truth for judgmental and uncertain priors used by either
matrix and for company consumer-revenue-share priors. It stores the assumption ID, statement,
low/mid/high prior, tier, evidence URL and snippet, sensitivity note, version, and active state.
New numerical mappings also require `review_status=APPROVED`; the 1.2.1 validator rejects any new
active assumption without it.

There is one implementation qualification to the shorthand “the registry is the source of truth
for both matrices”: fixed Matrix B weights do not have an `A-` assumption ID. For those rows, the
versioned Matrix B JSON itself is the numeric source of truth and its evidence references explain
the deterministic derivation. The validator requires assumption IDs only when a Matrix B range is
uncertain.

The Monte Carlo step is uncertainty propagation, not fitting:

- default execution uses 20,000 draws and fixed seed `20260804`;
- every non-degenerate registered prior is sampled from its documented triangular distribution;
- a prior with equal low/mid/high values becomes a constant array;
- each contribution is `debt flow * Matrix A weight * Matrix B weight`;
- each company denominator is quarterly consolidated revenue times its sampled U.S.-consumer
  revenue share;
- company and revenue-weighted aggregate bands are the 10th, 50th, and 90th percentiles;
- tier shares and assumption-to-output correlations are derived from the same frozen draws.

Nothing in this process updates a weight from prediction error or learns a company classification
from examples.

## What is not used

DFRI currently uses no neural network, gradient-boosted tree, learned representation, embedding,
large language model, or model-generated narrative. It performs no training on historical data
beyond parameter estimation in the AR(2), ridge bridge, and mixed-frequency state-space models
named above. Random-walk and seasonal forecasts have no fitted parameters, and attribution Monte
Carlo is sampling from documented distributions rather than training.

There are three reasons for this boundary:

1. **Auditability:** every published number must be traceable to dated observations, explicit
   formulas, mappings, assumptions, and a stable model version.
2. **Small target sample:** the monthly first-print series provides only a modest expanding history;
   the committed comparison has 137 target observations and 101 evaluated forecasts per target.
3. **Interpretable benchmarking:** the project needs a clear comparison against random-walk,
   seasonal, and AR(2) alternatives, not an opaque performance claim that cannot be diagnosed.

## How to describe DFRI accurately

DFRI is **an evidence pipeline with a statistical nowcast attached**. The evidence pipeline—source
verification, vintages, provenance, assumptions, immutable predictions, and first-print grades—is
the main system. The statistical component is a small, interpretable monthly nowcast with explicit
naive benchmarks.

Calling DFRI a machine learning system would overstate what it is today. It has fitted statistical
models, but no learned classification layer, representation model, or autonomous model-authored
output in the published pipeline.
