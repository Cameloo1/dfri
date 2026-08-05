# Fixtures

Fixtures must be minimal, real archived source responses with a source URL, retrieval timestamp, checksum, and redistribution basis. Synthetic fixture values may be used only as explicitly labeled test canaries and never as source evidence or published data.

The two complete `census/marts_adv*.pdf` fixtures span the legacy and current official
release-header/Table 1 layouts. They are retained intact because exercising PDF extraction itself is
part of the parser contract; each companion provenance file pins the exact bytes and source URL.
