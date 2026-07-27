---
name: cellbender-background-removal
description: Validate raw droplet matrices and remove ambient RNA with CellBender before standard single-cell QC and preprocessing. Use only for raw, unfiltered droplet data that includes empty droplets, or when the user explicitly requests CellBender/background correction; never use on filtered, normalized, or post-CellBender matrices.
---

# CellBender Background Removal

CellBender is optional preprocessing, not a routine step. It models ambient RNA from empty droplets,
so its input must be a raw droplet matrix containing both cells and many low-count/empty barcodes.

## Operating sequence

1. Call `validate_cellbender_input` directly on a candidate raw 10x H5. Validation computes and
   records the byte-level dataset identity. Read the UMI-rank plot and assessment. An unsuitable result is
   a refusal, not a warning to override. The reported minimum low-count-barcode requirement is
   only a suitability gate; it is never an estimate of cells or a value for `expected_cells`.
2. Call `remove_ambient_background` only when the validation is current and the biological goal
   warrants correction. Usually leave automatic `expected_cells` and `total_droplets_included`
   estimation in place on the first run.
3. Inspect the registered metrics, log warnings, CellBender PDF/report, count-comparison plot, and
   interpretation report. Process completion alone does not establish a good correction.
4. Continue with ordinary cell-level QC, doublet review, normalization, clustering, and cluster QC
   on the selected corrected output. CellBender replaces none of those checks.

## Scientific choices

- Use the default full model and FPR 0.01 unless the user/source provides a reason to change them.
- A larger FPR permits more background removal and more risk of true-signal removal. FPR 0 has a
  specific cohort/DE use case; do not select it casually.
- Only set expected cells or total droplets after reviewing the UMI-rank evidence or following an
  explicit source workflow.
- Never derive `expected_cells` by subtracting low-count barcodes or by reusing the validator's
  required-low-count threshold. The validator does not estimate a cell count.
- The broker requires a healthy GPU runtime. There is no silent CPU fallback.
- The tool records CellBender 0.3.2's fixed random seed (1234); it does not invent a seed flag the
  installed CLI does not expose.
- Non-Gene Expression feature types are left unchanged by default through CellBender's exclusion
  option and are reported in the run configuration.

## Output and lineage

Both full and filtered corrected matrices, posterior, metrics, barcodes, checkpoint, logs, plots,
and reports are registered when CellBender produces them. The source input remains untouched.
`selected_output=filtered` makes the inferred-cell matrix the active dataset; `full` selects the
corrected analyzed-droplet matrix explicitly. Either choice creates a new count representation and
invalidates downstream QC, representation, clustering, batch, annotation, and finalization facts.

If a run fails or times out before the broker limit, its logs and any checkpoint are committed
without changing the active dataset. Supply that registered checkpoint to a later run; the skill
copies it into new staging and never mutates the checkpoint source.

Read [references/method-and-contract.md](references/method-and-contract.md) before changing
parameters, interpreting warnings, or selecting an output.
