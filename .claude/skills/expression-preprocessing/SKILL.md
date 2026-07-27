---
name: expression-preprocessing
description: Normalize a single-cell count matrix or select highly variable genes as independent, composable operations. Use when a downstream method needs log-normalized expression or an HVG mask.
---

# Expression Preprocessing

`normalize_single_cell_expression` reads a validated count layer, writes total-count-normalized
log1p expression to `X`, and preserves the original counts layer.

`select_highly_variable_genes` computes an HVG Boolean mask on the current expression matrix (or
on a named layer) without subsetting genes. It can be run after normalization for `seurat`, or
directly from raw counts with a count-aware flavor such as `seurat_v3` when the installed Scanpy
stack supports it.

Each transformation is invoked explicitly when its output is required by the selected downstream
method.

See [references/matrix-semantics.md](references/matrix-semantics.md).
