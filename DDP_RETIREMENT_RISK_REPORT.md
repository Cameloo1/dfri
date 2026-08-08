# Federal Reserve DDP retirement risk

**Status:** MONITORING — the live clock is not blocked

**Verified:** 2026-08-08

**Decision:** Do not migrate yet. The repository has no direct Data Download Program (DDP)
application dependency, and its Board XML URLs are the statistical-release-page files that the
Board says will remain available during the transition.

## Board announcement

The Board's July 16, 2026 notice establishes the following timeline:

- The DDP **Build Your Package** option is scheduled for removal during the week of
  **November 9, 2026**.
- The Board plans additional DDP changes later in 2026 and in 2027, including removal of
  preformatted packages and eventual DDP retirement.
- The Board has **not announced a final DDP retirement date**. It says more details and timelines
  will be provided in advance.
- The Board says historical data will remain available as XML files on statistical release pages.

Primary evidence:

- [Board DDP/FRED transition notice](https://www.federalreserve.gov/data/data-download-fred-information.htm)
- [G.19 statistical release page](https://www.federalreserve.gov/releases/g19/)
- [H.8 statistical release page](https://www.federalreserve.gov/releases/h8/)

The G.19 and H.8 release pages currently link their XML downloads directly at:

- `https://www.federalreserve.gov/releases/g19/data/FRB_g19_xml.zip`
- `https://www.federalreserve.gov/releases/h8/data/FRB_h8_xml.zip`

Both returned HTTP 200 on 2026-08-08 and carried August 7, 2026 update timestamps. These are the
exact URLs pinned in the client. They are under `/releases/`, not `/datadownload/`.

## Exact DFRI dependency map

| Board surface | DFRI consumers | Scheduled clock dependency | DDP retirement exposure |
|---|---|---:|---|
| `/datadownload/*` package builder, preformatted packages, and download application | No data retrieval path. The source-contract registry cites DDP help only as automation-policy evidence. | No | Direct retirement has no effect. |
| `/releases/{g19,h8}/data/FRB_*_xml.zip` release-page XML | `FederalReserveBoardClient.fetch_release`, `board-snapshot`, live source verification, and the disposable 20-row spot audit | No | The files are historically described as DDP SDMX packages, but the Board now designates them as the release-page XML continuity path and says historical XML will remain. |
| `/releases/{g19,h8}/releaseDates.json` | Archive discovery for `board-backfill` and `board-targets` | Yes | Separate `/releases/` endpoint. The DDP notice does not announce its removal. |
| `/releases/{g19,h8}/YYYYMMDD/` dated HTML releases | First-print G.19/H.8 observations, release-coherent G.19 targets, backtests, predictions, grades, and quarterly attribution flow inputs | Yes | Separate dated-release archive. No retirement notice was found for it. |
| `/releases/{g19,h8}/YYYY.htm` annual archive indexes | Archive-depth verification and an available discovery fallback | No | Separate release archive. |

The public scheduler refreshes `board-backfill` and `board-targets`; it does **not** run
`board-snapshot`. Prediction and grading then read the append-only rows built from dated releases.
The quarterly refresh also uses those archive-derived G.19 target flows plus EDGAR. Therefore:

- Build Your Package removal does not affect the clock.
- Removal of DDP preformatted packages does not affect the clock.
- Eventual DDP application retirement does not affect the clock if the Board keeps the release
  pages, release-date manifests, and release-page XML files available.

Relevant implementation boundaries:

- [`src/dfri/ingest/board.py`](src/dfri/ingest/board.py)
- [`src/dfri/ingest/board_backfill.py`](src/dfri/ingest/board_backfill.py)
- [`src/dfri/ingest/board_targets.py`](src/dfri/ingest/board_targets.py)
- [`src/dfri/ingest/board_snapshot.py`](src/dfri/ingest/board_snapshot.py)
- [`.github/workflows/m2-scoreboard.yml`](.github/workflows/m2-scoreboard.yml)

## Archive separation and persistence

The public evidence supports a technical separation, not an internal organizational claim:

- DDP is served under `/datadownload/`.
- Current XML, release-date manifests, annual indexes, and dated releases are served under
  `/releases/` and linked from the G.19/H.8 statistical release pages.
- The Board explicitly commits to keeping historical XML on statistical release pages during the
  transition.

The Board does not state who administers the dated HTML archives internally and does not publish a
permanence guarantee for `releaseDates.json` or every `/YYYYMMDD/` page. No Board notice announcing
their retirement was found. They should be treated as an independently addressed, currently
supported publication surface, but monitored rather than assumed permanent.

On 2026-08-08, the live manifests exposed **140 G.19 releases from 2015-01-08 through
2026-08-07** and **606 H.8 releases from 2015-01-02 through 2026-08-07**.

## Other machine-readable Board paths

The Board currently provides these non-DDP-application paths usable by DFRI:

1. The full-history SDMX/XML ZIPs linked from each statistical release page. These are the current
   one-request source for revised G.19 and H.8 histories and registered series metadata.
2. Dated release HTML pages plus the JSON release-date manifests. They are already parsed under
   strict table, date, unit, checksum, and series-coverage contracts and provide point-in-time
   first prints.
3. Annual HTML release indexes, which can replace the JSON manifest for discovery if necessary.

Current-release HTML is an additional machine-parseable publication fallback, while PDF remains a
document fallback. RSS announces releases but does not carry the complete values. The Board
identifies FRED as the only replacement API; DFRI cannot use that path under the standing terms
decision. No separate Board JSON, REST, or SDMX API for G.19/H.8 was found beyond the release-page
XML files.

## Are dated archives alone sufficient?

**Yes for DFRI's 2015-forward operational data and first-print vintages, with one qualification.**

- The latest dated page contains the newest G.19/H.8 observations needed by the clock.
- Iterating the dated pages reconstructs the 2015-forward histories used by the nowcast and
  backtest.
- Each dated page preserves the release-coherent first print needed for grading.
- The existing production parsers and scheduled workflow already use this route.

The qualification is that dated pages are not a one-request, byte-equivalent replacement for the
full revised SDMX snapshot. If the release-page XML ZIPs disappeared while dated pages remained,
prediction and grading would continue, but `board-snapshot`, live registry verification, and the
Board portion of the spot audit would require a small migration to archive reconstruction.

## Contingency plan

### Trigger and monitoring

Reassess immediately if the Board announces a date for release-page XML or dated-archive removal,
an XML URL disappears from a release page, an endpoint returns a persistent non-200 response, or a
release manifest stops advancing after the published release window. Keep the ingest fail-closed;
never substitute FRED or revised values for first prints.

### Scenario A — DDP retires; release pages remain

**Action:** None to the production ingest. Rename stale internal “DDP snapshot” descriptions after
retirement and re-run live verification.

**Cost:** Less than one engineering day for documentation and a cold verification run.

**Breakage:** None expected. This is the outcome the Board's current notice describes.

### Scenario B — release-page XML ZIPs retire; dated archives remain

**Action:** Keep the scheduled `board-backfill` and `board-targets` paths unchanged. Replace
`fetch_release` snapshot assembly with a 2015-forward reconstruction over dated pages; point live
registry and spot-audit checks at the newest dated release and verify the full reconstructed
coverage. Preserve source URLs and checksums per dated page rather than synthesizing one snapshot
identity.

**Cost:** Approximately 2–4 engineering days for the adapter, fixtures, contract tests, and a
746-page cold live verification at the current archive size.

**Breakage before migration:** `board-snapshot`, the Board portion of `live-smoke`, and the Board
portion of `spot-audit`. Prediction, grading, backtesting, and quarterly flow refresh remain live.

### Scenario C — JSON manifests retire; dated pages and annual indexes remain

**Action:** Switch discovery to the already implemented annual-index parser, compare its result to
the last accepted manifest, and fail on gaps or duplicates.

**Cost:** Approximately 1–2 engineering days including workflow and live regression evidence.

**Breakage before migration:** Scheduled refresh fails closed at discovery; existing ledgers and
published pages remain intact.

### Scenario D — dated archives retire

**Action:** Stop new predictions and grades until point-in-time fidelity is restored. Before any
announced removal date, promote checksummed, minimal source snapshots or canonical parsed
first-print batches into version control so fresh-clone recovery no longer depends on remote archive
retention. Continue prospectively from each current Board release page. Do not use FRED.

**Cost:** Approximately 3–5 engineering days plus repository-size review and a byte-identity cold
recovery exercise.

**Breakage:** Historical first-print reconstruction, backtest reproduction, and grading evidence
retrieval break without a repository mirror. Existing Git-backed predictions and grades remain
permanent, but the clock must pause rather than publish unverifiable numbers.

### Scenario E — all Board release publication paths retire

**Action:** Mark the Board lanes BLOCKED, freeze prediction and grading publication, retain the
immutable ledger/site, and seek a new Board-hosted public-domain path. This is a genuine source
hard-blocker.

## Conclusion

DDP retirement is a **monitored naming and endpoint-continuity risk, not a current single point of
failure for the public clock**. The project already uses the Board's release-page XML continuity
path for revised snapshots and dated release archives for every clock-critical first print. No
migration is warranted before the Board publishes a final retirement plan or changes those release
surfaces.
