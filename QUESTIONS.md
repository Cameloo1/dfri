# DFRI Review Questions

Non-blocking questions accumulate here for the Day-14 review. None currently block M0 execution.

| ID | Date | Area | Question | Build impact | Current action |
|---|---|---|---|---|---|
| Q-001 | 2026-08-04 | GitHub/public hosting | **RESOLVED:** Which Camelon Systems GitHub repository and public hostname should receive DFRI? | The canonical public destinations are `Cameloo1/dfri` and `https://cameloo1.github.io/dfri/`. | Keep the full local history in the primary workspace and publish only inspected filtered-lane commits. The active clock and manual bootstrap are verified; do not count the bootstrap as a live cycle. |
| Q-002 | 2026-08-05 | Public API hosting | Which owner-approved existing infrastructure should host the read-only FastAPI process? | M4 cannot close and M5 cannot start through its prescribed dependency gate until the nine `/v1` endpoints are durably public. GitHub Pages cannot execute FastAPI, and the owner prohibited introducing an unapproved vendor. | Keep the implementation, OpenAPI contract, local SLO evidence, and partial uptime lane ready; do not select a hosting vendor or claim a live API without owner direction. |
