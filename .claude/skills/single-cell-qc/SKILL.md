---
name: single-cell-qc
description: Calculate per-cell and per-gene single-cell QC evidence, or explicitly filter cells or genes as separate operations. Use to inspect library size, detected genes, mitochondrial fraction, and threshold effects.
---

# Single-Cell QC

QC measurement and filtering are intentionally separate.

- `calculate_single_cell_qc` adds count-derived metrics and threshold flags but removes nothing.
- `review_single_cell_qc` records the required visual interpretation and keep/filter decision.
- `filter_single_cells` removes cells only when `confirm_filtering=true`.
- `filter_single_cell_genes` removes genes only when `confirm_filtering=true`.

The tools require an H5AD containing finite nonnegative integer counts in the selected layer or
in `X`. Filtering creates a new cell-set or count identity and makes evidence tied to the old
artifact stale; historical artifacts remain intact.

Use `counts_layer="auto"` by default: it selects `layers["counts"]` when present and otherwise
validates `X`. Use `null` only to explicitly force `X`, and name a layer only when it is known to
exist. The calculation emits the standard QC suite: combined distributions/UMI knee, violins,
count/gene histograms, mitochondrial histogram, count/gene/mitochondrial scatters, and
ribosomal-versus-mitochondrial scatter. Inspect every returned figure before review. After UMAP,
also call `plot_qc_embedding` to localize these signals.

Thresholds are dataset- and assay-dependent. A PBMC default is not automatically appropriate for
nuclei, tumors, low-depth libraries, or large metabolically active cells. Prefer calculating and
reviewing flags before filtering.

A high flagged fraction is a question, not an automatic deletion. Compare threshold options,
distribution shape, QC-on-embedding localization, doublet evidence, and later cluster coherence.
Record why all cells are kept or which cells should be filtered. If filtering occurs, recalculate
and review QC on the retained artifact.

Read [references/qc-contract.md](references/qc-contract.md) for metric definitions and mutation
semantics.
