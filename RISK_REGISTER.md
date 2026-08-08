# DFRI risk register

This register contains active operational and source-continuity risks with explicit triggers,
failure boundaries, and recovery paths. Findings do not waive acceptance criteria; affected lanes
fail closed when source fidelity cannot be established.

## R-001 — Federal Reserve DDP and release-surface continuity

| Field | Current assessment |
|---|---|
| Status | MONITORING |
| Opened | 2026-08-08 |
| Owner | DFRI operations |
| Likelihood | DDP retirement is announced; removal of release-page XML or dated archives is not announced. |
| Impact | Low if only DDP retires; high if release-page XML disappears; critical if dated first-print archives disappear. |
| Current exposure | No `/datadownload/*` data dependency; DDP help is cited only as terms evidence. Release-page XML supports revised snapshots; dated G.19/H.8 pages and manifests support the production clock. |
| Early-warning signals | New Board transition notice; final retirement date; XML link removal; persistent non-200; manifest not advancing after a scheduled release; archive URL drift. |
| Detection | Scheduled ingest fails closed; live source verification checks pinned URLs, metadata, dates, units, and series coverage. Review the Board transition notice at least monthly through 2027. |
| Immediate response | Do not substitute FRED. Preserve the Git-backed public ledger and last verified site; pause only the source lane that cannot prove current or first-print fidelity. |
| Recovery | If release pages remain, no migration. If only XML retires, reconstruct 2015-forward snapshots from dated pages. If manifests retire, use annual indexes. If dated archives retire, promote minimal checksummed source snapshots/canonical first-print batches into Git before removal and prove fresh-clone recovery. |
| Cost envelope | <1 day for DDP-only retirement; 2–4 days for XML-to-archive reconstruction; 1–2 days for manifest discovery fallback; 3–5 days for a repository-backed first-print source mirror. |
| Evidence | [`DDP_RETIREMENT_RISK_REPORT.md`](DDP_RETIREMENT_RISK_REPORT.md) and the [Board transition notice](https://www.federalreserve.gov/data/data-download-fred-information.htm). |

### Accepted posture

No migration is performed while the Board's statistical release XML and dated archive surfaces
remain live. The risk is reviewed when the Board publishes transition details and before the
November 9, 2026 Build Your Package removal.
