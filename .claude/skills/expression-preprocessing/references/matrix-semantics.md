# Matrix semantics

Total-count normalization scales each cell to a target library size and `log1p` compresses the
dynamic range. The output is useful for visualization and rank-based differential expression; it
is not raw counts.

The source count matrix is preserved in the selected layer. Count models must continue to read
that layer rather than normalized `X`.

Highly variable genes are a feature-selection aid. The tool records `var["highly_variable"]` but
does not discard non-HVG genes. The number and flavor are analysis parameters, not universal
quality thresholds.

SCimilarity aligns raw counts to its model gene order and applies its own normalization.
CellTypist similarly normalizes count-derived expression internally. scVI models raw counts.
Those tools can therefore operate without this preprocessing skill.
