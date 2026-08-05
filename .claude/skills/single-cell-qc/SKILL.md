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

## Gene filtering

`filter_single_cell_genes` removes two classes of gene, and keeps a third:

- **Low-detection genes**, on a dataset-size-aware threshold by default: keep genes detected in at
  least `min_cell_fraction` of cells (default 0.02 = 2%), capped at `max_cells` absolute cells
  (default 100) so a large dataset does not delete rare-cell-type markers. The fraction governs
  small datasets; the cap takes over on big ones. Set `min_cells` for an exact absolute threshold
  (e.g. strict source replication), or `min_cell_fraction=0` to keep all genes.
- **Ribosomal genes** (`remove_ribosomal`, default true) — RPS/RPL and the mitochondrial-ribosomal
  MRPS/MRPL — so they cannot dominate variance and drive the embedding or marker interpretation.
  Set `remove_ribosomal=false` to keep them.
- **Mitochondrial genes are kept** by default. Mitochondrial content is a cell-level QC concern
  (`max_pct_mito`), not a feature to strip; `remove_mitochondrial=true` is available but rarely
  appropriate.

Ordering matters. The per-cell `pct_counts_mt` and `pct_counts_ribo` percentages are computed on
the full matrix **before** removal and preserved in `obs`, so the ribosomal QC, embedding, and
cluster-QC figures keep reading the correct values after ribosomal genes are gone. Do the QC
calculation and review first; do **not** re-run `calculate_single_cell_qc` on the ribosomal-filtered
artifact expecting a ribosomal percentage — those genes are gone, so a recomputed `pct_counts_ribo`
would read ~0. The preserved obs column is the authoritative one.

Because gene filtering preserves the cell set, it **carries the current cell-QC review and any
complete doublet evidence forward**, re-stamped to the new gene-filtered matrix — you do not need to
re-review cell QC after it, and finalization is not blocked by it. (Only removing *cells* — via
`filter_single_cells` or cluster cleanup — invalidates the cell-QC review and doublet evidence,
because the cell set genuinely changed.)

The tools require an H5AD containing finite nonnegative integer counts in the selected layer or
in `X`. Filtering creates a new cell-set or count identity and makes evidence tied to the old
artifact stale; historical artifacts remain intact.

Use `counts_layer="auto"` by default: it selects `layers["counts"]` when present and otherwise
validates `X`. Use `null` only to explicitly force `X`, and name a layer only when it is known to
exist. The calculation emits the standard QC suite: combined distributions/UMI knee, per-metric
violins with the cells jittered over them, log-scaled count/gene histograms, mitochondrial
histogram, count/gene/mitochondrial scatters, ribosomal-versus-mitochondrial scatter, and — when
the artifact already carries doublet scores — a doublet violin and histogram. Inspect every
returned figure before review. After UMAP, also call `plot_qc_embedding` to localize these
signals, and use `visualize-single-cell` for anything this suite does not cover: per-sample QC
splits are the usual next step, since a pooled distribution hides the one failing library.

Library size and detected genes are drawn on log axes with log-spaced bins. Read thresholds off
those; a linear axis compresses the low tail into a few bars and hides the shape you are judging.

Thresholds are dataset- and assay-dependent. A PBMC default is not automatically appropriate for
nuclei, tumors, low-depth libraries, or large metabolically active cells. Prefer calculating and
reviewing flags before filtering.

A high flagged fraction is a question, not an automatic deletion. Compare threshold options,
distribution shape, QC-on-embedding localization, doublet evidence, and later cluster coherence.
Record why all cells are kept or which cells should be filtered. If filtering occurs, recalculate
and review QC on the retained artifact.

`review_single_cell_qc` needs each required figure named in `reviewed_artifacts`; the file name on
its own is enough, so `qc_scatter.png` matches. What matters is that you looked at it — say what
each one showed. A figure the runtime reports as *not attached* is not reviewed: open it with
`inspect-media` first.

Read [references/qc-contract.md](references/qc-contract.md) for metric definitions and mutation
semantics.
