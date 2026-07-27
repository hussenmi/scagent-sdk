# Decision guide

## The two evidence axes

`gene_evidence` (primary, gene-level):

- `none`: no supported cross-sample population match carries a direct gene difference.
- `localized`: matched populations differ across samples, but no program recurs.
- `recurring_sample_associated`: a consistently directed program recurs across ≥2 distinct
  matched populations — the pattern suspicious for ambient RNA, sample-specific background, or a
  procedure/source effect.

`design_interpretation` (experimental design):

- `documented_technical_batch`: the batch is established as technical by metadata/documentation
  (`technical_batch_documented=true` with a basis) — never inferred from separation alone.
- `confounded_with_biology`: the batch is perfectly confounded with a supplied condition column.
- `orthogonal_but_not_known_technical`: a supplied condition is present and not confounded, but the
  batch is not documented technical.
- `unknown`: no design information resolves the cause.

## Recommendation (non-binding)

| gene_evidence | design | recommendation |
|---|---|---|
| none / localized | any | do_not_integrate_based_on_current_evidence |
| recurring | unknown / confounded_with_biology | cannot_determine_technical_vs_biological |
| recurring | orthogonal_but_not_known_technical | integration_optional_for_confirmed_replicates |
| recurring | documented_technical_batch | integration_supported |

## Deciding

- `keep_uncorrected`: effects are modest, biologically entangled, or not harmful to the analysis.
- `integrate`: justified unwanted variation with adequate cross-batch biological overlap. Requires
  an `integration_basis` (`documented_technical_batch` or `user_authorized_comparable_replicates`);
  if the recommendation does not support integration, an explicit `override_warning` is required —
  integration never proceeds silently against the evidence, and perfect biological confounding is
  never silently overridden.
- `separate`: batches are incompatible assays, tissues, species, or irreducibly confounded designs.
- `request_guidance`: design knowledge is insufficient to tell technical from biological.
- `not_applicable`: no meaningful batch unit is present.

## Recurrence is advisory, not replication

Recurrence is computed from **cell-level Wilcoxon** tests between matched regions — a
legacy-compatible advisory signal. Cells are not independent biological replicates, so a recurring
program is **not** evidence of sample-level replication, and the list may contain **low-expression
or compositional false positives**: with hundreds of cells, small random differences reach
`q <= 0.05`. Before treating a recurring program as real, read `recurring-programs.csv` together
with `direct-matched-region-degs.csv` and weigh the detection fractions (`pct_a`/`pct_b`), effect
size, and gene class. Sample-aware pseudobulk contrasts remain the appropriate tool for replicated
inference and are not implemented here.

## Authorization

`integrate` is validated at decision time and the result is persisted (`validated`,
`decision_policy_version`). A `documented_technical_batch` basis is only accepted when the
*evidence* was recorded with `technical_batch_documented=true` and a non-empty
`technical_batch_basis` — the claim cannot be asserted at decision time. Integration remains gated
by the floor on that validation, a matching evidence id, all four current identities, and an
explicit `override_warning` whenever the recommendation does not support integration.

Correction cannot recover a biological contrast perfectly confounded with batch. A non-confounded
condition column alone does not make sample-wide differences technical — donor and other biological
effects can remain. Sample-segregated clusters can be donor/patient-private biology (donor-specific
states in normal tissue, or malignant clones/CNVs in tumors); do not assume the tissue is a tumor
without dataset context. The advisory mixing and association metrics are representation- and
composition-dependent and are never optimization targets.
