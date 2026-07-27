---
name: dimensionality-reduction
description: Compute PCA, a nearest-neighbor graph, or UMAP as three independent transformations on an H5AD artifact. Use the operation whose output is required by the scientific task.
---

# Dimensionality Reduction

- `compute_single_cell_pca` creates a PCA representation from `X`, optionally restricted to an
  existing HVG mask, and returns a variance/cumulative-variance figure for inspection.
- `build_single_cell_neighbors` creates a neighbor graph from an explicit representation such as
  `X_pca`, `X_scVI`, or `X_scimilarity`.
- `compute_single_cell_umap` creates a visualization from an existing neighbor graph.

Each tool validates its algorithm's inputs: PCA consumes an expression matrix, graph construction
consumes a named cell representation, and UMAP consumes a neighbor graph.

The conventional UMAP key is always `X_umap`; downstream visualization, cluster QC, and
finalization share this contract. After UMAP in a comprehensive run, call `plot_qc_embedding`
before clustering and interpret localized high-MT, low-library, low-gene, or doublet regions.

See [references/representation-contract.md](references/representation-contract.md).
