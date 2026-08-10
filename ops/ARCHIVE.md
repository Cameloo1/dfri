# Immutable ledger archive

The repository is the primary durable and tamper-evident home for predictions, grades, and first
publication records. A deterministic release archive supplies an independent recovery object.

## Package and proof

`make archive-round-trip` (or `make.cmd archive-round-trip`) verifies the repository ledger,
packages only the allowlisted public ledger, canonical manifest, changelog, license, citation, and
archive metadata, builds the archive twice, compares bytes, extracts into a disposable directory,
and reruns the repository-ledger semantic checks. The output and receipt remain ignored under
`.local/archive/`.

The archive workflow repeats that proof for every GitHub Release and attaches the exact tarball to
the release. `CITATION.cff` and `.zenodo.json` are ready for Zenodo's GitHub-release integration.
The intended offsite cadence is every release containing a new grade, restatement, fallback
activation, or methodology version, and at least quarterly while the project is active.

## DOI gate

A real Zenodo deposit requires either a Zenodo account connected to GitHub or a Zenodo API token
with deposit permissions. Neither exists under the current no-connected-account instruction. The
archive registry therefore has status `PENDING_CREDENTIAL` and contains no placeholder DOI. The
site renders no “cite this” DOI block until an owner-authorized deposit is downloaded, verified
against the local archive SHA-256, and the returned concept/version DOI is recorded as `VERIFIED`.

When authorized, enable the public repository in Zenodo, publish a reviewed GitHub Release, wait
for the Zenodo record, download its archive, run `python -m dfri.ops.archive verify`, compare the
SHA-256 to the release asset, and only then update `archive_registry_v1.json`. A failed comparison
leaves the registry pending and the citation block absent.
