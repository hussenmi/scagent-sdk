# Deterministic scientific-engine restoration specifications

Status: proposal for Claude review; not implemented  
Date: 2026-07-23  
Scope: restore three legacy evidence engines inside the current `scagent-sdk` skill and state
architecture without restoring the legacy global DAG, giant prompt, or runtime dependency.

## Contents

1. [Decisions already made](#decisions-already-made)
2. [Three-axis cluster QC](#restoration-1--three-axis-cluster-qc-with-convergent-auto-removal)
3. [Gene-first batch evidence](#restoration-2--gene-first-batch-evidence-and-a-post-evidence-decision)
4. [Deterministic annotation validation](#restoration-3--deterministic-annotation-proposal-validation)
5. [Cross-cutting constraints](#cross-cutting-implementation-constraints)
6. [Claude review checklist](#claude-review-checklist)

## Decisions already made

- Preserve the current flat skill catalog and capability discovery.
- Keep scientific implementation beside the focused skill.
- Bind every evidence object to current cell-set, count-representation, representation, and
  clustering identities as applicable.
- Use floors only for consequential state validity. Floors must not prescribe a universal analysis
  sequence.
- Cluster QC **may auto-remove clusters in the same call** when metric QC, DEG identity, and
  covariance/coherence all converge on removal and the total removal is below a bounded fraction.
- Keep conflicts and inconclusive cases. A missing evidence axis can never be treated as agreement.
- PanglaoDB remains optional. Cytopus is local deterministic evidence when it has coverage, not a
  mandatory universal reference.
- Never import, invoke, subclass, or require legacy `scagent` at runtime. Port requirements and
  validate new implementations independently.

## Validation vocabulary

Each restoration must report four validation levels separately:

1. deterministic contract tests;
2. real brokered compute and artifact inspection;
3. model-behavior evaluations;
4. biological validation across datasets/designs.

Passing a lower level must never be described as satisfying a higher one.

---

## Restoration 1 — three-axis cluster QC with convergent auto-removal

### Current implementation and exact gap

Current package: `.claude/skills/cluster-qc/`  
Current tool: `evaluate_cluster_qc` → `scripts/evaluate.py:run`  
Current version: `0.3.0`

The current script validates current cell/clustering identities and computes:

- cluster sizes;
- PCA silhouette summaries;
- top-marker summary statistics;
- cluster medians for available QC covariates;
- Scrublet predicted-call enrichment;
- a combined table, UMAP, report, and `cluster_qc.status=attested` fact.

It does not currently classify metric severity, distinguish identity-bearing from nuisance-only
DEGs, measure within-cluster gene-gene structure, render correlation heatmaps, calculate technical
Moran's I, synthesize conflicts, or issue a new cell set after convergent cleanup.

### Proposed capability manifest delta

Keep one tool. Bump the skill to `0.4.0` and change its description to state that it performs
metric, DEG-identity, and covariance/coherence adjudication and may remove a bounded convergent
cleanup set.

Proposed additions to the existing `evaluate_cluster_qc.input_schema.properties`:

```yaml
n_structure_genes:
  type: integer
  minimum: 20
  maximum: 500
  default: 150
  description: Non-nuisance informative genes used for within-cluster correlation structure.
min_cells_for_structure:
  type: integer
  minimum: 5
  default: 15
moran_min_cells:
  type: integer
  minimum: 10
  default: 40
corr_threshold:
  type: number
  exclusiveMinimum: 0
  maximum: 1
  default: 0.3
max_heatmaps:
  type: [integer, "null"]
  minimum: 1
  default: null
  description: Null renders every eligible cluster; an integer bounds pathological cluster counts.
metric_warning_z:
  type: number
  exclusiveMinimum: 0
  default: 2.0
metric_extreme_z:
  type: number
  exclusiveMinimum: 0
  default: 3.0
auto_remove_convergent:
  type: boolean
  default: true
auto_remove_max_fraction:
  type: number
  exclusiveMinimum: 0
  maximum: 0.5
  default: 0.2
  description: A convergent removal at or above this fraction is held for review.
```

Retain the current size, silhouette, doublet-enrichment, and seed parameters. Reject unknown
arguments. Record every effective parameter and threshold in the result and report.

### Input and identity contract

Before computing evidence:

- Require a registered current prepared/integrated H5AD path, not an arbitrary H5AD.
- Match `cell_set_id`, `count_representation_id`, `representation_id`, and `clustering_id` between
  H5AD provenance and durable state.
- Require the requested `cluster_key` to be the key recorded for the current clustering.
- Require finite log-normalized expression for DEG/correlation analysis and a current neighbor
  graph for Moran's I. If a graph is absent, Moran is explicitly unavailable; it is never filled
  with zero.
- Preserve `layers['counts']` as the source for any cell-set mutation. Refuse auto-removal when a
  validated raw-count representation cannot be recovered.

### Axis A — metric QC

Retain current per-cluster summaries and add a deterministic severity classification:

- available signals: mitochondrial fraction, ribosomal fraction, total counts, detected genes,
  mean doublet score, predicted-doublet rate, tiny-cluster status, and silhouette;
- compare cluster medians with the across-cell distribution using robust center/scale; record the
  raw statistic, robust z-score, direction, absolute guard if one applies, and missingness;
- classify `clean`, `ambiguous`, or `obvious` from independent adverse signals;
- do not let size, silhouette, mitochondrial signal, or doublet enrichment alone produce a removal;
- record which signals nominated the cluster and which signals were unavailable.

Initial compatibility policy:

- `ambiguous`: one moderate adverse signal at `metric_warning_z`;
- `obvious`: at least two independent moderate signals, or one extreme signal at
  `metric_extreme_z` plus a compatible absolute-quality guard;
- doublet enrichment remains `rate >= max(doublet_rate_floor, global_rate * enrichment_ratio)`;
- tissue/data-type absolute guards belong in the skill reference and must remain configurable or
  context-derived rather than hard-coded as universal biology.

Implement the classifier as pure helpers and test it without Scanpy.

### Axis B — DEG identity

Compute one-vs-rest positive DEGs for every current cluster, once per call, using the current
log-normalized expression and current cluster key.

For every cluster record:

- top positive DEGs with score, log fold change, adjusted p-value, target/reference expression
  fractions, and expression-fraction difference where available;
- significant positive DEG count;
- nuisance genes: mitochondrial, ribosomal, MALAT1-like, and other explicitly versioned generic
  technical patterns;
- broad/context genes: generic activation, stress, cell-cycle, interferon, or broadly shared
  programs, stored as a versioned list/reference rather than hidden code prose;
- discriminating non-nuisance genes;
- verdict: `identity_supported`, `junk_markers`, or `inconclusive`;
- exact reasons and the gene-set/reference fingerprint used.

Initial deterministic rule:

- `identity_supported`: multiple significant positive non-nuisance genes, including at least two
  discriminating genes with nontrivial expression-fraction separation;
- `junk_markers`: significant evidence is absent/very thin and the supported top program is
  dominated by nuisance/broad genes;
- otherwise `inconclusive`.

Do not assign a cell type here. This axis asks whether a coherent cluster-specific biological
program exists, not what the program should be called.

### Axis C — covariance/coherence

For every cluster with at least `min_cells_for_structure` cells:

1. Select up to `n_structure_genes` expressed, variable, non-nuisance genes. Prefer current HVGs;
   fall back visibly to expressed non-mitochondrial genes when too few HVGs remain.
2. Remove zero-variance genes and record the selected genes and fallback strategy.
3. Compute the within-cluster gene-gene Pearson correlation matrix.
4. Record mean absolute off-diagonal correlation, fraction of pairs with
   `abs(correlation) >= corr_threshold`, PC1 variance support, eigenspectrum preview, hierarchical
   modules, module sizes, and linkage status.
5. Render one clustered correlation heatmap per eligible cluster unless explicitly bounded.
6. Classify the structure using versioned compatibility thresholds:

   - `unstructured`: mean absolute correlation `< 0.08` and high-correlation-pair fraction `< 0.05`;
   - `weak`: mean absolute correlation `< 0.12`;
   - `moderate`: mean absolute correlation `< 0.18`;
   - `strong`: otherwise;
   - `inconclusive`: insufficient cells/genes or failed computation.

These thresholds are legacy-compatibility starting points, not validated universal constants.
Record them in artifacts and evaluate sensitivity on multiple datasets before calling them mature.

### Technical Moran's I

Using the current `obsp['connectivities']` graph:

- compute local and global Moran's I for mitochondrial fraction and log library size when the
  covariates exist;
- summarize local Moran's I within each cluster;
- pair localization with the cluster's direction relative to the global distribution;
- report high-MT and low-library technical pockets separately;
- never use Moran's I as cell-type evidence or as a substitute for missing DEG/covariance evidence;
- emit explicit skip reasons for missing graph/covariate or clusters below `moran_min_cells`.

The first implementation may use the legacy compatibility signal `Moran > 0.3` plus directional
z-score magnitude `> 0.5`, but these must remain recorded versioned policy, not invisible magic.

### Deterministic synthesis and removal policy

Produce one decision row per current cluster with all three axes and a reason list.

| Metric axis | DEG axis | Covariance axis | Synthesis | Action |
|---|---|---|---|---|
| `ambiguous` or `obvious` | `junk_markers` | `unstructured` or `weak` | `confirmed_junk` | removal candidate |
| `clean` | `junk_markers` | `unstructured` or `weak` | `unstructured_junk_markers` | review/keep |
| any | `identity_supported` | `moderate` or `strong` | `structured_identity` | keep |
| adverse | `junk_markers` | `moderate` or `strong` | `junk_markers_but_structured` | review/keep |
| adverse | `identity_supported` | `unstructured` or `weak` | `identity_without_structure` | review/keep |
| any | `inconclusive` | any, or any missing required axis | `inconclusive`/`conflicting` | review/keep |

Only `confirmed_junk` enters the automatic removal set. A missing or inconclusive covariance axis
does **not** count as agreement, even if metric QC and DEGs look poor.

When `auto_remove_convergent=true` and the union of confirmed-junk clusters removes more than zero
but strictly less than `auto_remove_max_fraction` of current cells:

- subset exactly those cluster labels;
- use the existing immutable input artifact as the pre-cleanup checkpoint;
- restore validated raw counts into `X` and `layers['counts']` in a new filtered H5AD;
- remove stale cluster labels, embeddings, graphs, rank-gene results, and source `.raw`;
- issue new dataset, dataset-revision, cell-set, and count-representation identities;
- set current representation and clustering to null;
- invalidate cell/cluster QC, doublet evidence/review, batch, annotation, and finalization facts;
- register removed cluster IDs, cell barcodes/count, removal fraction, parent identities, evidence
  ID, parameters, and full output fingerprint;
- return `cleanup_applied`, not `attested`, and tell the model to prepare/recluster and rerun QC.

At or above the fraction bound, or when auto-removal is disabled, preserve all cells and register a
`cleanup_held_for_review` decision with the exact candidate set and fraction. Do not partially
remove a subset merely to stay below the limit.

When no cleanup is applied, write `cluster_qc.status=attested` only if metric, DEG, and covariance
evidence completed for every eligible cluster and every skip is explicit. Bind the attestation to
the current cell-set, count, representation, and clustering identities plus an evidence ID.

Strengthen `current_cluster_qc` accordingly: require the restored evidence schema version, current
cell/count/representation/clustering identities, `status=attested`, and the evidence ID/artifact.
Old clustering-only attestations fail closed for new annotation/finalization and explain that
cluster QC must be rerun.

### Required artifacts

- `cluster-qc-decision-table.csv`
- `cluster-metric-evidence.csv`
- `cluster-deg-identity.csv`
- `cluster-structure-evidence.csv`
- `cluster-qc-evidence.json`
- `cluster-qc-report.md`
- cluster/QC UMAP and metric distribution figure
- `cluster-structure/cluster_<id>_correlation.png` for each rendered cluster
- when removal occurs: `cluster-qc-filtered-raw-counts.h5ad` and `removed-cells.csv`

Every visual claim must cite a registered figure. Every heatmap path must name its cluster and
evidence ID.

### Tests and live acceptance

Add pure tests for metric classification, nuisance/identity DEG classification, structure
thresholds, synthesis, fraction boundary (`<`, not `<=`), and no-removal with a missing axis.

Add capability tests for:

- strict schema and parameter bounds;
- current/stale cell/count/representation/clustering identities;
- raw-count availability before mutation;
- one heatmap per eligible cluster and bounded heatmaps;
- all-three-agree removal, conflict preservation, high-impact hold, and disabled auto-removal;
- exact new lineage/invalidation after cleanup;
- no current-QC attestation after cell-set mutation;
- two-phase executor recovery for a large multi-artifact result.

Live acceptance requires at least:

- a clean/coherent dataset where no cluster is removed;
- a deterministic injected/synthetic junk cluster where all three axes converge and bounded
  removal/reprocessing occurs;
- a metric-poor but structured/identity-bearing cluster that is preserved;
- artifact inspection of representative coherent and unstructured heatmaps;
- a model eval that reports the axes and does not call every flagged cluster junk.

---

## Restoration 2 — gene-first batch evidence and a post-evidence decision

### Current implementation and exact gap

Current package: `.claude/skills/batch-investigation/`  
Current tool: `investigate_batch` → `scripts/investigate.py:run`  
Current version: `0.3.0`

The current call computes composition, Cramér's V, per-batch QC, PCA-neighbor same-batch fraction,
normalized batch entropy, and figures. It also requires `decision` and `rationale` in that same
call. Therefore the model supplies a decision before it can inspect the evidence just generated.
The computed signals are advisory and cannot explain which genes drive sample differences.

### Proposed capability shape

Keep one skill package but expose two tools. Bump to `0.4.0`.

#### Tool 1: `investigate_batch`

This tool generates evidence and does not record the final handling decision.

Proposed manifest contract:

```yaml
- name: investigate_batch
  entrypoint: scripts/investigate.py:run_evidence
  environment: gpu-singlecell
  activity_label: Investigating batch evidence
  input_schema:
    type: object
    properties:
      path: {type: string, description: Current prepared or integrated H5AD path.}
      batch_key: {type: [string, "null"]}
      cluster_key: {type: string, default: leiden}
      condition_keys:
        type: array
        uniqueItems: true
        items: {type: string, minLength: 1}
        default: []
      technical_batch_documented: {type: boolean, default: false}
      technical_batch_basis: {type: [string, "null"], default: null}
      min_cells_per_region: {type: integer, minimum: 10, default: 30}
      min_enrichment: {type: number, minimum: 1, default: 2.0}
      n_identity_genes: {type: integer, minimum: 10, maximum: 100, default: 25}
      max_candidate_pairs: {type: integer, minimum: 1, maximum: 100, default: 30}
      n_neighbors_for_mixing: {type: integer, minimum: 2, maximum: 200, default: 30}
      max_cells_for_mixing: {type: integer, minimum: 100, default: 20000}
      random_seed: {type: integer, default: 0}
    required: [path, batch_key]
    additionalProperties: false
```

`technical_batch_documented=true` requires a nonempty basis that names user-provided metadata,
study documentation, or another registered source. The tool may validate and store that claim; it
must never infer a technical batch merely from separation.

For `batch_key=null`, create a current `not_applicable` evidence object tied to current identities;
do not use an identity-free exception in the floor evaluator.

#### Tool 2: `decide_batch_handling`

This tool consumes a current evidence ID after evidence inspection.

```yaml
- name: decide_batch_handling
  entrypoint: scripts/investigate.py:run_decision
  environment: current
  activity_label: Recording batch decision
  floors: [current_batch_evidence]
  input_schema:
    type: object
    properties:
      evidence_id: {type: string, minLength: 1}
      decision:
        type: string
        enum: [keep_uncorrected, integrate, separate, request_guidance, not_applicable]
      rationale: {type: string, minLength: 1}
      integration_basis:
        type: [string, "null"]
        enum: [documented_technical_batch, user_authorized_comparable_replicates, null]
      override_warning: {type: [string, "null"], default: null}
    required: [evidence_id, decision, rationale]
    additionalProperties: false
```

This is not global tool-order coupling. It is a local evidence/decision boundary: integration and
finalization require a current decision, but the model may reach that state through any valid
evidence route and may choose not to integrate.

### Gene-first evidence contract

Retain the current advisory evidence and add these deterministic stages:

1. **Sample-enriched regions.** For each cluster × batch level, compare its fraction within the
   cluster with that batch's global frequency. Nominate regions by enrichment over baseline and
   minimum cells, not by a universal purity cutoff.
2. **Within-sample identity DEGs.** For each nominated region, compare that cluster's cells with all
   other cells from the same batch level. This holds batch constant and estimates population
   identity. Store full gene/effect/q-value/expression-fraction evidence.
3. **Cross-sample population matching.** Match regions from different batch levels using overlap of
   top non-nuisance identity genes, with profile similarity as candidate-ranking context. Record
   matched and rejected candidates and the deterministic threshold.
4. **Direct matched-region comparison.** For supported matches, compare the regions directly and
   report genes higher on each side. Keep technical/stress/ribosomal/mitochondrial genes here;
   these may be the sample-associated signal. Treat this as secondary because batch is no longer
   held constant.
5. **Recurrence.** Identify consistently directed sample-associated genes/programs across at least
   two distinct matched populations. Include a region-wide scan over all enriched regions so the
   recurring/localized verdict does not depend only on model-nominated pairs.
6. **Design/confounding axis.** Cross-tab each supplied condition/design column against the batch
   key; record missingness, level mapping, one-to-one/perfect confounding, association, and whether
   the batch was explicitly documented as technical.

Use current log-normalized expression for rank-based cell-level DE and record the matrix identity.
The first restoration should use in-environment Scanpy Wilcoxon for bounded execution. Every
report must state that cell-level q-values rank separation and do not establish biological
replication. A later sample-aware DEG/pseudobulk skill may supply stronger replicated contrasts;
do not hide diffxpy fallback or dynamically run it in the wrong environment.

### Evidence axes and deterministic recommendation

Record `gene_evidence` as exactly one of:

- `none`: no supported cross-sample population matches/direct gene evidence;
- `localized`: matched populations differ, but programs do not recur;
- `recurring_sample_associated`: a consistently directed program recurs across at least two
  distinct populations.

Record `design_interpretation` as exactly one of:

- `unknown`;
- `confounded_with_biology`;
- `orthogonal_but_not_known_technical`;
- `documented_technical_batch`.

Produce a deterministic recommendation, but do not write the user/model decision in the evidence
call:

| Gene evidence | Design interpretation | Recommendation |
|---|---|---|
| `none` | any | do not integrate based on current evidence |
| `localized` | any | do not apply dataset-wide integration based on current evidence |
| recurring | unknown/confounded | cannot determine; request guidance or preserve biology |
| recurring | orthogonal but not known technical | integration optional only for confirmed comparable replicates |
| recurring | documented technical | integration supported |

`decide_batch_handling` validates the submitted decision against this matrix. It may record a
different user-owned decision only with an explicit `override_warning` preserved in facts/report.
`integrate` requires either a documented technical basis or explicit user authorization that the
levels are comparable replicates; perfect biological confounding must never be silently overridden.

### State and floor contract

Refactor the current `batch` fact:

```json
{
  "batch": {
    "evidence": {
      "status": "complete",
      "evidence_id": "batch-evidence:sha256:...",
      "cell_set_id": "...",
      "count_representation_id": "...",
      "representation_id": "...",
      "clustering_id": "...",
      "batch_key": "sample",
      "gene_evidence": "recurring_sample_associated",
      "design_interpretation": "unknown",
      "recommendation": "cannot_determine_technical_vs_biological",
      "artifact_path": "..."
    },
    "decision": null
  }
}
```

Add `current_batch_evidence`: require a complete/not-applicable evidence object whose cell, count,
representation, and clustering identities match current analysis.

Update `batch_decision`: require a decision that references the current evidence ID and the same
identities. Remove the current identity-free `not_applicable` exemption.

Update `integration_authorized`: require `decision=integrate`, a current evidence ID, and a valid
integration basis. Existing legacy-shaped batch facts fail closed and require re-investigation.

Any cell/count/representation/clustering change invalidates both evidence and decision. scVI's new
representation/clustering therefore requires post-integration batch evaluation before finalization,
but does not require another integration decision unless another integration is requested.

### Required artifacts

- current composition counts/proportions and readable figures;
- `sample-enriched-regions.csv`;
- `within-sample-identity-degs.csv`;
- `population-matches.csv` including rejected candidates/reasons;
- `direct-matched-region-degs.csv`;
- `recurring-programs.csv` with direction, batches, and distinct populations;
- `design-confounding.csv`;
- `batch-evidence.json`;
- deterministic `batch-investigation.md` with gene/design axes and limitations;
- later decision artifact `batch-decision.md` linked to the evidence ID.

### Tests and live acceptance

Add pure tests for enrichment-over-baseline, nuisance filtering only during identity matching,
within-sample comparison construction, match thresholds, recurrence across distinct populations,
direction consistency, design enums, verdict matrix, and decision/override rules.

Add capability/floor tests for strict schemas, current/stale identities, evidence-before-decision,
identity-bound not-applicable, decision replacement, integration authorization, and invalidation.

Live acceptance requires:

- a multi-sample dataset with a known localized population difference;
- a synthetic or documented recurring technical program across at least two populations;
- a perfectly confounded biological design that never authorizes integration;
- repeated runs showing the same recurrence verdict regardless of candidate-pair ordering;
- artifact inspection and model evals that distinguish sample-wide from technical.

---

## Restoration 3 — deterministic annotation proposal validation

### Current implementation and exact gap

Current packages:

- `.claude/skills/marker-annotation/` produces DEG summaries and broad marker-program candidates;
- CellTypist and SCimilarity write current-clustering reference evidence;
- `.claude/skills/finalize-analysis/` validates exact map coverage, non-overwrite, independent
  `deg_labels`, confidence enum, and override strings, then writes final artifacts.

Current finalization verifies structure but not the biological content of the submitted evidence.
It does not deterministically confirm supporting genes against current DEGs, classify nuisance or
broad genes, score Cytopus coverage/competitors, assign evidence tiers, cap confidence from
conflicts/QC, or verify optional external-evidence call history.

### Proposed package and manifest shape

Do not move all annotation science into `finalize.py`. Keep `finalize.py` focused on validation,
publication, and envelope assembly; add `scripts/adjudicate.py` inside the same
`finalize-analysis` skill package for the deterministic proposal validator.

Bump `finalize-analysis` to `0.4.0` and expose two tools.

#### Tool 1: `validate_annotation_proposal`

```yaml
- name: validate_annotation_proposal
  entrypoint: scripts/adjudicate.py:run
  environment: gpu-singlecell
  activity_label: Validating annotation evidence
  floors: [dataset_identity, current_cluster_qc, batch_decision, current_annotation_evidence]
  input_schema:
    type: object
    properties:
      path: {type: string, description: Current prepared or integrated H5AD path.}
      cluster_key: {type: string, default: leiden}
      labels:
        type: object
        additionalProperties: {type: string, minLength: 1}
      deg_labels:
        type: object
        additionalProperties: {type: string, minLength: 1}
      supporting_genes:
        type: object
        additionalProperties:
          type: array
          minItems: 1
          uniqueItems: true
          items: {type: string, minLength: 1}
      competing_labels:
        type: object
        default: {}
        additionalProperties:
          type: array
          uniqueItems: true
          items: {type: string, minLength: 1}
      rationales:
        type: object
        additionalProperties: {type: string, minLength: 1}
      evidence_summaries:
        type: object
        additionalProperties: {type: string, minLength: 1}
      confidence:
        type: object
        additionalProperties: {type: string, enum: [high, medium, low]}
      overrides:
        type: object
        default: {}
        additionalProperties: {type: string, minLength: 1}
      external_evidence_refs:
        type: object
        default: {}
        description: Registered artifact/evidence IDs only; free-text claims are not call history.
        additionalProperties:
          type: array
          uniqueItems: true
          items: {type: string, minLength: 1}
    required: [path, labels, deg_labels, supporting_genes, rationales, evidence_summaries, confidence]
    additionalProperties: false
```

#### Tool 2: `finalize_analysis`

Retain the current finalization fields and add required:

```yaml
validation_id: {type: string, minLength: 1}
proposal_fingerprint: {type: string, minLength: 1}
```

Add floor `current_annotation_validation`. Finalization must recompute the proposal fingerprint and
rerun the pure validator against the current evidence; it may not trust a stale state flag.

### Proposal and evidence contract

Require exact current-cluster coverage for labels, DEG labels, supporting genes, rationales,
evidence summaries, and confidence. Unknown/extra clusters fail.

For each cluster, the validator reads rather than trusts:

- current marker evidence and its DEG artifact/fingerprint;
- current CellTypist and/or SCimilarity evidence, including confidence and blockers;
- current cluster-QC decision row and any confidence-relevant warnings;
- current doublet evidence/review when available;
- the versioned local Cytopus marker resource when the proposed/competing labels map to covered
  terms;
- registered optional external evidence only when its ID and query history are present.

Every proposal receives a fingerprint over exact labels, DEG labels, genes, confidence,
rationales, evidence references, current scientific identities, and source-evidence fingerprints.

### DEG classification and gene validation

For every current cluster, classify significant positive DEGs into:

- `discriminating_degs`: label/competitor-separating non-nuisance genes;
- `broad_context_degs`: real but insufficiently specific programs such as broad antigen
  presentation, generic myeloid, activation, interferon, stress, or cell cycle;
- `nuisance_degs`: mitochondrial, ribosomal, MALAT1-like, hemoglobin when contextually technical,
  and explicitly versioned generic nuisance patterns.

Validate every submitted supporting gene against the current cluster DEG table. Record genes that
are absent, non-significant, broad, nuisance, or discriminating. Require at least one
discriminating non-nuisance supporting DEG; stronger tiers require multiple coherent genes.

Do not use marker-dictionary overlap alone to manufacture `deg_labels`. `deg_labels` remain the
model's independent interpretation of current DEGs, but the validator checks that the cited genes
actually support the statement.

### Cytopus and optional external evidence

Bundle a versioned, licensed local Cytopus-derived marker resource or a deterministic adapter with
the skill. Record resource version/fingerprint and label mapping.

For covered labels:

- score the proposed label and supplied competitors on current discriminating DEGs;
- report marker overlap, best label, runner-up, margin, coverage, and uncovered labels;
- treat thin margin or conflict as uncertainty;
- never fail merely because Cytopus has no coverage for a tissue/label.

PanglaoDB remains optional. If a proposal claims external support, validate the claim against the
registered query/fetch artifact history and compatible label/gene results. Absence of a PanglaoDB
call never reduces confidence by itself. A free-text statement that a query happened is not
evidence.

### Evidence tiers and confidence ceilings

Assign exactly one transparent tier per cluster:

- `reference_consensus_plus_deg`;
- `cytopus_plus_deg`;
- `reference_partial_plus_deg`;
- `deg_primary`;
- `external_adjudicated` when optional external evidence was actually registered and compatible.

Record the rule that selected the tier. Initial confidence ceilings:

| Condition | Maximum confidence |
|---|---|
| Multiple discriminating DEGs, no meaningful contradiction, and reference consensus or clear Cytopus margin | high |
| Strong DEG basis with only partial reference support, a thin Cytopus margin, or a justified broader parent label | medium |
| DEG-primary with unresolved competitors, reference conflict, structure-QC conflict, or doublet enrichment | medium |
| Supporting evidence dominated by broad/nuisance genes, serious QC caveat, or unresolved mixed program | low |

Confidence caps only lower requested confidence. They never inflate it. The validator returns
`requested_confidence`, `maximum_confidence`, and `effective_confidence` plus reasons.

When `labels[cluster] != deg_labels[cluster]`, require an override that names verified
discriminating DEGs and addresses negative/competing evidence. A nonempty generic string is not
sufficient. Unsupported precision becomes a validation blocker; the validator may recommend a
broader parent label but must not silently invent one.

### Validation output and state floor

Emit one row per cluster containing:

- proposed/final and independent DEG label;
- verified/rejected supporting genes with reasons;
- discriminating, broad, and nuisance DEG previews;
- reference-method labels/support/conflicts;
- Cytopus coverage, winner, runner-up, margin, and resource fingerprint;
- optional external evidence IDs and compatibility;
- evidence tier;
- requested/maximum/effective confidence;
- override status;
- QC/doublet caveats;
- blockers, warnings, and suggested remediation.

Write `annotation.validation`:

```json
{
  "status": "pass",
  "validation_id": "annotation-validation:sha256:...",
  "proposal_fingerprint": "sha256:...",
  "cell_set_id": "...",
  "count_representation_id": "...",
  "representation_id": "...",
  "clustering_id": "...",
  "marker_evidence_fingerprint": "...",
  "reference_evidence_ids": ["..."],
  "artifact_path": "..."
}
```

Add `current_annotation_validation`: require `status=pass`, current identities, current marker and
reference evidence IDs/fingerprints, and a proposal fingerprint. Any underlying evidence or
identity change makes it stale.

`finalize_analysis` requires this floor, matches `validation_id`, recomputes the proposal
fingerprint, reruns pure validation, uses `effective_confidence`, and records the full validation
lineage in the final H5AD/report. A failed validation does not mutate data.

### Required artifacts

- `annotation-proposal.json`
- `annotation-validation.csv`
- `annotation-validation.json`
- `annotation-validation.md`
- versioned Cytopus resource/mapping fingerprint in the report
- final artifacts already produced by `finalize_analysis`, augmented with validation/tier columns

### Tests and live acceptance

Add pure tests for DEG classification, supporting-gene verification, label mapping, Cytopus
coverage/margin, evidence tiers, confidence caps, meaningful overrides, proposal fingerprinting,
and external-call-history verification.

Add capability/floor tests for exact coverage, stale marker/reference/QC/identity evidence,
validation pass/fail, tampered proposal after validation, non-overwrite, and final artifact lineage.

Live/model acceptance requires:

- pDC versus plasma, cytotoxic versus pDC, and broad-versus-subtype cases;
- a label outside Cytopus coverage that remains valid through DEG-primary evidence;
- reference consensus that contradicts discriminating DEGs and does not win silently;
- a QC/doublet-conflicted cluster whose confidence is capped;
- an optional external claim with and without real registered call history;
- repeated model evals showing generalize-upward behavior and complete cluster coverage.

---

## Cross-cutting implementation constraints

### Keep code modular inside the skills

Recommended files:

```text
.claude/skills/cluster-qc/scripts/
├── evaluate.py              # orchestration and result envelope
├── metric_evidence.py       # pure severity helpers
├── deg_identity.py          # DEG classification
├── structure_evidence.py    # correlation/Moran/heatmaps
└── cleanup.py               # bounded lineage mutation

.claude/skills/batch-investigation/scripts/
├── investigate.py           # run_evidence/run_decision envelopes
├── gene_evidence.py         # enriched regions, identity matches, recurrence
└── design_evidence.py       # confounding and verdict matrix

.claude/skills/finalize-analysis/scripts/
├── adjudicate.py            # proposal validator
└── finalize.py              # publication plus validation recheck
```

Do not create a central biology module under `src/scagent_sdk`. Generic identity/fingerprint and
artifact-commit primitives may remain platform code; scientific rules remain in the skill package.

### Existing-session compatibility

- Old cluster-QC attestations without all-axis evidence do not satisfy the restored current-QC
  contract for a new finalization. Historical artifacts remain valid history.
- Old batch facts fail the new current-batch-evidence/decision floors and require re-investigation.
- Previously finalized artifacts remain historical. A new finalization requires a current proposal
  validation.
- Use explicit evidence schema versions inside facts so failures explain remediation rather than
  silently accepting weaker legacy shapes.

### Suggested implementation order

1. Add shared test fixtures for current scientific identities and brokered skill contexts.
2. Restore cluster-QC evidence and bounded convergent cleanup.
3. Refactor batch into evidence then decision; add gene-first evidence.
4. Add annotation proposal validation and the finalization floor.
5. Run deterministic baselines after each slice.
6. Run one bounded live validation and artifact inspection per slice.
7. Run model-behavior evals separately.
8. Update `docs/current-state.md`, `docs/skill-catalog.md`, `docs/scientific-parity.md`, and
   `docs/roadmap.md` only with achieved validation levels.

## Claude review checklist

Before implementation, Claude should report:

1. Any mismatch between this proposal and the current scripts/manifests/fact shapes.
2. Any manifest-schema feature proposed here that the registry does not support.
3. Whether the cleanup lineage can reuse the proven doublet-filtering pattern without importing
   scientific code across skill packages.
4. Whether the cluster evidence thresholds should be compatibility defaults or replaced before the
   first implementation, with evidence.
5. The smallest reusable DEG/design foundation needed by batch without building a new monolith.
6. How current CellTypist/SCimilarity facts expose enough detail for deterministic annotation
   validation and what schema strengthening is required.
7. Exact floor/reducer changes and stale-session behavior.
8. A file-by-file plan, test matrix, live datasets/synthetic fixtures, and bounded compute budget.

Claude should audit first and stop before editing. The implementation should begin only after the
review resolves contract ambiguities.

---

# Claude review and resolutions (2026-07-23)

Audited against the live code, not the prose: `floors/evaluator.py`, every touched `capability.yaml`,
`prepare-single-cell/scripts/prepare.py`, `doublet-evidence/scripts/doublets.py`,
`capabilities/executor.py`, `execution/broker.py`, `execution/profile.py`, `configs/environments/iris.toml`,
`celltypist-annotation/scripts/run_celltypist.py`, `scimilarity-annotation/scripts/run_scimilarity.py`,
and legacy `scagent/batch/{scvi,diffxpy}.py` + `annotation/cytopus_markers.py`.

## Code-verified fact base (the contract the specs must bind to)

- **Identity vocabulary is real and complete.** `prepare.py` mints and records `dataset_revision_id`,
  `cell_set_id`, `count_representation_id`, `count_matrix_id`, **`representation_id`**, and
  `clustering_id` into both `adata.uns["scagent_sdk"]` and `facts.analysis.*`. `facts.analysis`
  carries nodes `dataset_revision / cell_set / count_representation / representation / clustering`,
  each `{id, ...}`.
- **Floors read a subset.** `evaluator.py` reads `analysis.clustering.id` (`_clustering_id`),
  `analysis.cell_set.id`, `analysis.count_representation.id`. `analysis.representation.id` **exists
  but no floor reads it yet.** Unknown floor names fail closed (`evaluator.py:140`).
- **Mutation/lineage template** (mirror this for cluster cleanup): `doublets.py` `run_review`
  removal mints an `observation-revision` id from `{parent dataset_revision_id, doublet_evidence_id}`,
  writes a new H5AD, and returns a `facts_patch` that nulls `cluster_qc / finalization`. Skills
  never write state directly.
- **Commit contract is two-phase.** A skill returns `facts_patch` + `artifacts`; `executor._stage_result`
  stages them under `runtime/capabilities/pending/<execution_id>`; the PostToolUse `commit()`
  atomically renames pending→final and emits `capability.result_committed` with the facts. Crash
  recovery replays pending. Cleanup mutation must therefore be expressed purely as a returned
  `facts_patch` + new artifacts, never as an in-place state write.
- **Broker executes every tool as a subprocess** into the runtime interpreter via a `worker.py`
  bootstrap (`broker.execute`), passing `context.state_facts` in the payload. This means the
  agent↔compute boundary is crossed uniformly for all science — the CUDA-context collision the
  legacy hand-fixed with `_scvi_worker.py` is solved structurally.

## Corrections to the earlier informal audit

- **`representation_id` DOES exist** (`prepare.py:295,340,405`). The earlier claim that it was not a
  first-class identity was wrong. The specs' pervasive `representation_id` matching is therefore
  supportable with the data already in `facts.analysis.representation.id`; the only work is teaching
  the relevant floors to read it (parallel to `_clustering_id`).

## Resolutions to the checklist (1–8)

1. **Mismatches.** Versions confirmed (`cluster-qc 0.3.0`, `batch 0.3.0`, `finalize 0.3.0`,
   `marker 0.2.0`). Finalize floors match the spec. The only substantive fact-shape gap is that
   floors do not yet read `representation`; add `analysis.representation.id` reads where the specs
   require representation currency.
2. **Unsupported manifest features.** `environment: current` (proposed for `decide_batch_handling`)
   **does not exist** — `iris.toml` defines only `gpu-singlecell/scvi/celltypist/scimilarity/cellbender/diffxpy`.
   Resolution: run pure decision/recording tools in `gpu-singlecell` (trivial cost, no new runtime),
   unless/until a control-plane execution path is added. Multi-tool skills and per-tool `floors: [...]`
   are supported (precedent: `doublet-evidence` has `evaluate`/`review`).
3. **Cleanup reuse.** The doublet lineage logic is skill-local in `doublets.py` and must not be
   cross-imported. Resolution: reimplement the mutation template in `cluster-qc/scripts/cleanup.py`;
   optionally promote only the generic id/fingerprint + `facts_patch` invalidation shape into a small
   platform helper under `src/scagent_sdk` (allowed by the cross-cutting section), keeping all
   scientific rules in-skill.
4. **Thresholds.** Keep every numeric threshold (correlation 0.08/0.12/0.18, Moran 0.3, z 2.0/3.0,
   enrichment 2.0) as recorded *compatibility* defaults with provenance in artifacts; do not present
   them as validated. They reach the repo's "biological generality" bar only after a multi-dataset
   sweep, which is out of scope for first landing and must be tracked as an explicit open risk.
5. **Batch DEG foundation.** Default the within-sample identity DEG to in-environment Scanpy
   Wilcoxon; make diffxpy an **opt-in cross-check that degrades gracefully** (mirror legacy
   `diffxpy_available()`), not fail-closed. This is the one place the SDK's fail-closed default is
   wrong. No new DEG monolith; a `gene_evidence.py` helper inside the batch skill is sufficient.
6. **Reference facts suffice, with one strengthening.** `run_celltypist.py`/`run_scimilarity.py`
   already register per-cluster `top_label` + `support_fraction` (+ CellTypist `median_confidence`)
   under `facts.annotation.evidence.{celltypist,scimilarity}`, bound to `clustering_id`. Gap: they
   bind to `clustering_id` only. To let the annotation validator fingerprint source evidence and
   bind multi-identity, add `cell_set_id`/`count_representation_id` + an evidence fingerprint to
   those facts.
7. **Floor/reducer changes.** Add branches to `evaluator.py` for `current_batch_evidence` and
   `current_annotation_validation`; extend `current_cluster_qc` to require the restored evidence
   schema/version; teach the batch/annotation floors to read `analysis.representation.id`. Legacy-shaped
   facts fail closed with a remediation message (schema-version gated), never silently accepted.
8. **Order.** Implement Restoration 1 first (no blocking decisions remain); then 2; then 3. Baseline
   after each slice; one bounded live run per slice; model evals separate.

## Environment coordination — findings and decisions

The SDK already mirrors the legacy model (one shared runtime + isolated envs only for genuine
conflicts) and is cleaner (declarative broker, uniform subprocess boundary, centralized GPU
gating). **Do not redesign.** Close operational gaps:

- **CONFIRMED DEFECT — no GPU pinning.** `profile.py:_SAFE_INHERIT` passes `CUDA_VISIBLE_DEVICES`
  through unchanged; `broker._validate_profile` gates on `min_gpu_memory_mb` at probe time, but
  `broker.execute` never selects and pins the idlest device. On a shared node this lets scVI's
  `devices="auto"` collide with the GPU hosting the agent's own vLLM — exactly what legacy
  `_select_gpu_device()` (NVML, most-free-memory) prevented. **Fix location:** in `broker.execute`,
  re-probe device free memory and set `CUDA_VISIBLE_DEVICES=<best physical index>` in the subprocess
  `environment` before `subprocess.run`, centralizing the legacy per-script logic for all GPU tools.
  Small, high-value, and independent of the three restorations.
- **scVI diagnostics regressed** (out of restoration scope, tracked): the SDK hardcodes
  `max_epochs=200` with no convergence/overfitting diagnostics; legacy used the cell-count heuristic
  `min(400, round(20000/n_obs*400))` + loss curve + `overfitting_warning`. Restore when scVI is next
  touched.
- `_preload_nvrtc_builtins` is likely obviated by Pixi/conda activation (LD_LIBRARY_PATH handled);
  do not port pre-emptively — only if a GPU dlopen error appears.

## Cytopus — findings and decision

Legacy `cytopus_markers.py` has **zero coupling to `scagent`** (imports only the third-party
`cytopus` package), so its resolver/lineage/`adjudicate` logic is freely reproducible. The
"use it for immune cells" behavior is **coverage-based auto-scoping, per cluster**, not a tissue
flag: `resolve_label` (synonym + fuzzy) → `markers_for` (curated set unioned with lineage ancestors)
→ `covered()` true only for immune/tumor labels; non-immune labels (platelet/erythroid/MAIT/most
epithelial) return `covered=False` and defer. `adjudicate` scores candidate + competitors + a
default immune panel by marker∩DEG overlap and returns `needs_external_fallback`.

**Decision:** vendor a **versioned JSON snapshot** of the Cytopus KB `identities` dict into the
`finalize-analysis` skill and carry the synonym/lineage/resolver/adjudicate logic in-skill (no
runtime `cytopus` dependency; `cytopus` is not even installed in the SDK env today). The `covered()`
gate is the immune-scoping mechanism — no separate tissue switch. **Blocking sub-task:** confirm the
Cytopus KB license permits redistributing the gene sets before snapshotting.

## Remaining decisions needing a user nod

- **Cytopus license** for vendoring the gene-set snapshot (Restoration 3 only).
- Nothing blocks **Restoration 1**: `representation_id` is available, the cleanup/lineage template is
  known, auto-removal on convergent evidence is confirmed intended (report-only == `auto_remove_convergent=false`).

## Restoration 1 readiness

Ready to implement with no open contract questions: three-axis evidence + bounded convergent
auto-cleanup + `representation`-aware `current_cluster_qc` strengthening + pure/capability tests,
reusing the doublet mutation template in a new `cleanup.py`. The GPU-pin fix can land alongside as an
independent, small platform change.
