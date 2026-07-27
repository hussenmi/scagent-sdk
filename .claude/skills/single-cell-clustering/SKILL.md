---
name: single-cell-clustering
description: Cluster cells from an existing neighbor graph or rank genes for an existing grouping as separate operations. Use when the scientific question requires discrete groups or group-wise differential expression.
---

# Single-Cell Clustering

`cluster_single_cells` runs Leiden on an explicitly named neighbor graph and writes labels to an
explicit, non-conflicting observation key.

`rank_single_cell_groups` ranks genes for any existing categorical grouping, including clusters,
conditions, or another supplied group key.

Leiden requires a neighbor graph. Gene ranking requires a grouping and an appropriate expression
matrix.

Read [references/clustering-contract.md](references/clustering-contract.md).
