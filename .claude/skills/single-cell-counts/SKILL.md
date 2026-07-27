---
name: single-cell-counts
description: Resolve and materialize a validated raw-count matrix from H5AD or 10x input. Use whenever a method needs raw counts or when an input has several possible count representations.
---

# Single-Cell Count Matrix

`materialize_count_matrix` chooses a raw-count source, validates it, and saves a non-overwriting
H5AD with that matrix in `layers["counts"]`, preserving the input observations and variables.

The input can be an H5AD, a 10x H5 file, or a 10x Matrix Market directory. For H5AD, the tool
inspects `X`, every layer, and an alignable `.raw`. `auto` uses count-like `X`, otherwise the sole
count-like alternative, and refuses ambiguity. A valid count matrix is finite, nonnegative, and
integer-valued. Explicit `X`, `raw`, or `layer` selection is available when the scientist knows
which representation is authoritative.

The output records count, cell-set, and dataset-revision identities derived from the actual
matrix and names. Those identities describe the artifact; they are not proof that another tool
ran first.

Read [references/count-contract.md](references/count-contract.md) for source-selection and
lineage details.
