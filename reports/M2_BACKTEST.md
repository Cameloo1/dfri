# M2 Point-in-Time Backtest

Backtest version: `m2-point-in-time-backtest-v1`  
As of: `2026-08-04T23:59:00+00:00`  
Window: `2018-01-01` through the latest gradeable first print  
Report hash: `8c0611137452d7fcdf1aa4509d7a2ef877726ec9448a914e0fc9f0974a56c8c6`

This report uses dated Federal Reserve Board G.19 and H.8 release archives plus dated
Census MARTS releases. Every model input is selected through the Vintage Guard at the
historical forecast timestamp; grades use release-coherent first-print G.19 flows.

## Results

### `DELTA_DTCTLR.M`

| Model | n | MAE | RMSE | 80% coverage | 95% coverage | Acceleration sign |
|---|---:|---:|---:|---:|---:|---:|
| ar2-ols-v1 | 101 | 7,486.797 | 11,645.149 | — | — | 73.3% |
| bridge-ridge-v2-alpha10 | 101 | 4,632.858 | 7,948.380 | 71.3% | 93.1% | 87.1% |
| mixed-frequency-kalman-v1-sm0.14.6 | 101 | 5,073.118 | 7,718.131 | 63.4% | 85.1% | 86.1% |
| naive-random-walk-v1 | 101 | 9,186.139 | 11,721.597 | — | — | 0.0% |
| naive-seasonal-v1 | 101 | 8,836.634 | 13,908.865 | — | — | 71.3% |

First target vintage: [https://www.federalreserve.gov/releases/g19/20150306/](https://www.federalreserve.gov/releases/g19/20150306/)  
Last target vintage: [https://www.federalreserve.gov/releases/g19/20260708/](https://www.federalreserve.gov/releases/g19/20260708/)

### `DELTA_DTCTLN.M`

| Model | n | MAE | RMSE | 80% coverage | 95% coverage | Acceleration sign |
|---|---:|---:|---:|---:|---:|---:|
| ar2-ols-v1 | 101 | 4,905.739 | 7,278.094 | — | — | 68.3% |
| bridge-ridge-v2-alpha10 | 101 | 5,301.607 | 9,665.799 | 78.2% | 93.1% | 71.3% |
| mixed-frequency-kalman-v1-sm0.14.6 | 101 | 5,228.420 | 10,042.794 | 67.3% | 83.2% | 73.3% |
| naive-random-walk-v1 | 101 | 5,858.416 | 8,461.766 | — | — | 1.0% |
| naive-seasonal-v1 | 101 | 6,223.762 | 9,685.684 | — | — | 72.3% |

First target vintage: [https://www.federalreserve.gov/releases/g19/20150306/](https://www.federalreserve.gov/releases/g19/20150306/)  
Last target vintage: [https://www.federalreserve.gov/releases/g19/20260708/](https://www.federalreserve.gov/releases/g19/20260708/)

## Primary headline decision

Selected model: `bridge-ridge-v2-alpha10` for `DELTA_DTCTLR.M`.
State-space eligible under §6.2: `false`.
Best naive comparator: `ar2-ols-v1`.
MAE improvement versus best naive: `38.1%`.

| §6.3 bar | Result |
|---|---|
| `mae_at_least_10pct_better_than_best_naive` | PASS |
| `coverage80_within_5pp` | PASS |
| `coverage95_within_5pp` | PASS |
| `acceleration_sign_accuracy_at_least_55pct` | PASS |

Overall primary bar decision: `PASS`.

The nonrevolving target is a secondary diagnostic and does not change the primary M2
acceptance decision. No live-scoreboard or two-cycle claim is made by this report.
