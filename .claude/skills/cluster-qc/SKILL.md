---
name: cluster-qc
description: Adjudicate a supplied clustering with three independent axes — metric QC, DEG identity, and covariance/coherence (plus technical Moran's I) — and optionally remove clusters on which all three converge as junk within a bounded fraction.
---

# Cluster QC

Call `evaluate_cluster_qc` with a compatible clustered AnnData, a grouping column, and `X_pca` for
separation evidence. It evaluates three independent axes per cluster and synthesizes one decision:

- **Metric QC** — robust per-cluster severity from mitochondrial/ribosomal fraction, library size,
  detected genes, doublet enrichment, tiny size, and negative silhouette. No single signal
  decides removal.
- **DEG identity** — whether the cluster carries a discriminating cell-identity program, or only
  nuisance/broad markers (`junk_markers`).
- **Covariance/coherence** — within-cluster gene-gene correlation structure with a saved heatmap
  per eligible cluster. Technical Moran's I for mitochondrial fraction and library size localizes
  technical pockets and is never cell-type evidence.

A cluster can be auto-removed **only** when metric QC is adverse, its DEGs are junk, and its covariance
is unstructured/weak — and the total removal stays below `auto_remove_max_fraction`. A missing or
inconclusive axis never counts as agreement; conflicts (`junk_markers_but_structured`,
`identity_without_structure`, `conflicting`, `inconclusive`) are flagged for review and kept. Set
`auto_remove_convergent=false` is the default inspect/report-only behavior. Set it true explicitly
only when mutation is intended.

Review every warning, especially tiny clusters, negative silhouette, weak markers, high-MT
clusters, and Scrublet enrichment. Doublet enrichment nominates review; it never deletes a cluster
or proves a coherent state is artifactual. `attested` means the checks ran for the current
identities — not that every cluster is biologically valid.

The capability saves a per-cluster metric boxplot, a cluster/QC UMAP, and one covariance heatmap
for every eligible cluster. Inspect all of them, including heatmaps not attached inline, then call
`review_cluster_qc`. The review must cover every returned visual artifact and every cluster marked
for review. A `keep` rationale resolves a warning; remove/merge/split/recluster/defer remains an
open action and blocks final publication until a new current clustering is evaluated.

For a comprehensive end-to-end run, evaluate resolutions 2.0, 1.5, and 1.0 by default, using
distinct cluster keys and the full compute-plus-review cycle at each resolution. This is guidance,
not hardcoding: users and data can change the grid. Select the final resolution from stability,
coherence, identity DEGs, separation, and interpretability.

When a removal is applied, the capability issues fresh dataset/cell-set/count identities from
preserved raw counts and invalidates downstream evidence: re-prepare, recluster, and re-run this
capability on the cleaned cells. A held-for-review removal (at or above the fraction bound) removes
nothing until you decide. Old attestations cannot satisfy the current-clustering floor.

Read [references/interpretation.md](references/interpretation.md) before merging, keeping, or
removing a suspicious cluster.
