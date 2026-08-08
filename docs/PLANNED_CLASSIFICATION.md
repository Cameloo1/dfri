# Planned classification layer

**Milestone:** M6 — LLM-assisted Matrix B classification expansion

**Status:** NOT SCHEDULED

**Start date:** None

**Nature of this document:** specification only. No provider, model dependency, model call,
pipeline integration, or new methodology is authorized by this milestone definition.

## Purpose

The current Matrix B is hand-built for roughly 50 companies. A future expansion may reach a scale
where manually finding and normalizing every company segment disclosure is not sustainable. M6 may
use an LLM for one bounded task: extract a disclosed company segment and propose one or more labels
from DFRI's controlled spending-category taxonomy.

The model is an intake assistant. It is not an estimator and is never a publication authority.

## Hard boundary

The layer is limited to classification and extraction:

```text
specific filing excerpt
    -> frozen machine proposal
    -> human review
    -> versioned classification assumption
    -> deterministic Matrix B construction under separately documented rules
```

A model may propose a category label and the filing text that supports it. It may not:

- produce a published numeric estimate;
- enter the consumer-credit nowcast;
- set, suggest, normalize, or revise a Matrix A or Matrix B weight;
- determine a company's revenue denominator or evidence tier;
- publish an assumption before review;
- run during `make replay`, `make publish`, CI, or a scheduled scoreboard job.

Models propose labels. Reviewed labels become assumptions. Any numeric weight remains a separately
traceable deterministic calculation or human-authored assumption under the existing methodology
contracts.

## Proposal and assumption records

Every machine proposal must be frozen as canonical, versioned data before review. A proposal record
must contain at least:

- stable `proposal_id` and proposed `assumption_id`;
- company ticker and CIK;
- filing form, filing date, accession number, accession-linked SEC URL, and source checksum;
- exact normalized segment label, bounded evidence excerpt, and excerpt hash;
- proposed controlled spending-category label or labels;
- provider, model identifier, model version, and inference configuration;
- prompt template version and canonical prompt hash;
- canonical response hash and proposal timestamp;
- review status: `PENDING`, `ACCEPTED`, `REJECTED`, or `SUPERSEDED`;
- reviewer role, review timestamp, and review reason when no longer pending;
- superseding assumption ID when a corrected label replaces the proposal.

Every frozen proposal enters the Assumption Registry immediately as a normal, inactive,
versioned classification assumption with its own ID. The registry must retain the filing/accession
evidence, machine origin, model and prompt versions, frozen artifact hash, and review status.
Acceptance changes whether that assumption may become an active Matrix B input; it does not
create or erase the record. `PENDING` and `REJECTED` assumptions remain in the registry
permanently but are never active and cannot be compiled into Matrix B.

## Determinism and storage

Model calls are not reproducible and are therefore outside the publication build:

1. A separate acquisition command creates a canonical proposal artifact from a checksummed filing
   excerpt.
2. The complete normalized proposal is frozen and committed as a data artifact after privacy and
   source-legality checks.
3. Review changes status by appending a review record; it does not edit or delete the proposal.
4. The deterministic build reads only frozen `ACCEPTED` assumptions.
5. CI and public builds have no model credential, provider SDK, or provider network path.

Given the same Git commit and `AS_OF`, `make replay` must remain byte-identical and must succeed with
network access to every model provider blocked. A changed model or prompt creates a new version and
new proposal artifacts; it never regenerates an existing artifact in place.

## Human review gate

Every proposed mapping requires human review before publication. The model's confidence, if any, is
metadata only and cannot waive review.

Owner review is defined as follows:

- The owner reviews 100% of the first 100 proposals and 100% of the first two expansion batches.
- After both batches pass, every proposal still receives a human review, and the owner audits a
  deterministic stratified sample of 20% of each batch, with a minimum of 25 proposals.
- The owner additionally reviews 100% of multi-label, out-of-taxonomy, low-evidence, corrected, and
  rejected proposals.
- If no delegated reviewer role exists, owner review remains 100%.

A disagreement means the owner selects a different category set, finds that the cited excerpt does
not support the proposal, or finds an accession/source mismatch. Expansion halts when:

- owner-audit disagreement exceeds 5% in any batch or rolling 50 audited proposals;
- any proposal cites the wrong filing, accession, or company; or
- any unreviewed proposal reaches an active registry or published Matrix B artifact.

Resume requires a written root-cause note, a new prompt/model version where applicable, a newly
frozen proposal batch, and a fresh holdout evaluation. Failed proposals are not silently dropped.
A rejection remains in the append-only registry with `REJECTED`, the proposed label, reviewer role,
reason, and timestamp. A corrected label receives a new assumption ID that names the rejected or
superseded record.

## Evaluation before trust

Before the layer may contribute an accepted assumption for any company, it must pass a frozen,
hand-labeled holdout set that was not used in prompts, examples, taxonomy development, or provider
fine-tuning.

The holdout must contain at least 200 segment-disclosure examples across at least 40 issuers and
cover every category proposed for the expansion. Two human labels are reconciled into one
adjudicated reference label per example before scoring.

Required pass thresholds:

- exact category-set accuracy of at least **90%**;
- macro-averaged category F1 of at least **0.90**;
- filing/accession/evidence-link precision of **100%**; and
- zero out-of-taxonomy labels accepted as valid categories.

Results must include per-category precision, recall, F1, exact-set accuracy, confusion pairs,
abstention rate, and error examples. The holdout artifact, scoring code, model/prompt versions, and
canonical evaluation receipt are committed. A model or prompt version change invalidates the prior
pass for new proposals and requires re-evaluation.

## Public disclosure

Machine assistance is not hidden. Before the first machine-proposed mapping can publish, the
methodology page and versioned feeds must expose:

- that the mapping was machine-proposed;
- its classification assumption ID;
- the exact filing and accession evidence;
- provider/model and prompt version;
- frozen proposal artifact hash;
- final review status and reviewer role;
- whether the machine label was accepted unchanged, corrected, or rejected; and
- the deterministic or separately registered rule that produced any eventual Matrix B weight.

The public page must distinguish “machine-proposed, human-verified” from hand-mapped assumptions.
Neither designation changes evidence tier by itself.

## Source and provider legality

Legality is a pre-implementation gate, not a cleanup task. Before selecting a provider or sending
one byte:

- verify and record the provider terms URL, version/date, permitted use, retention policy, training
  policy, automated-access rules, and derivative-output rights;
- verify that the source terms allow the exact filing excerpt to be transmitted to that provider;
- send only the minimum evidence excerpt needed for classification, never credentials, private lake
  data, borrower/loan-level data, or unpublished control documents;
- prohibit provider training or retention when required by the source or DFRI privacy posture; and
- mark the milestone `BLOCKED` if provider and source terms cannot both support the workflow.

No provider is approved by this specification. Verification must use then-current primary terms
before implementation.

## Explicit non-goals

M6 does not authorize:

- an LLM or other new model in the nowcast;
- model-generated commentary, summaries, recommendations, or written analysis on the site;
- model-authored numbers, priors, bands, weights, denominators, tiers, or scores;
- automated acceptance based on confidence;
- live provider calls from CI, replay, publication, scheduled jobs, or page requests;
- replacement of accession-linked evidence with model output; or
- retroactive rewriting of any published methodology or mapping.

## Acceptance criteria

1. **AC-M6-01 — Scope boundary:** architecture tests prove that the model lane can emit only frozen
   extraction/classification proposals and cannot emit numeric methodology fields.
2. **AC-M6-02 — Legal gate:** current provider and source terms are verified with URLs, dates, and
   conditions before the first model call; any conflict produces `BLOCKED`.
3. **AC-M6-03 — Frozen inputs:** every proposal binds one minimal filing excerpt to an accession,
   checksum, model version, prompt version, and canonical artifact hash.
4. **AC-M6-04 — Registry provenance:** every proposal has a stable assumption ID and append-only
   review status; accepted, rejected, corrected, and superseded outcomes remain inspectable.
5. **AC-M6-05 — Publication gate:** a build containing any active unreviewed classification fails
   closed, and no `PENDING` or `REJECTED` label can enter Matrix B.
6. **AC-M6-06 — Deterministic replay:** a fresh clone reproduces byte-identical output with provider
   access blocked and no model credential configured.
7. **AC-M6-07 — Holdout quality:** the frozen holdout meets 90% exact category-set accuracy, 0.90
   macro-F1, 100% evidence-link precision, and zero accepted out-of-taxonomy labels.
8. **AC-M6-08 — Owner review:** the defined 100%-then-20% owner audit is reproducible; a greater
   than 5% disagreement rate or any provenance mismatch halts expansion.
9. **AC-M6-09 — Rejection history:** rejected and corrected proposals remain in the append-only
   registry with reasons and supersession links rather than disappearing.
10. **AC-M6-10 — Public disclosure:** methodology and feeds identify every machine-proposed mapping,
    its evidence, model/prompt versions, artifact hash, review outcome, and weight derivation.
11. **AC-M6-11 — Non-goal enforcement:** tests prove that no provider dependency or call exists in
    nowcast, replay, publish, scheduled workflow, or request-time paths.
12. **AC-M6-12 — Cold milestone proof:** all M6 criteria pass from a fresh clone through documented
    make targets and a milestone report is written before the layer is described as active.

Dependencies: completed M5 mapping contracts, an explicit owner scheduling decision, a verified
provider/source legal contract, and a frozen controlled spending-category taxonomy.
