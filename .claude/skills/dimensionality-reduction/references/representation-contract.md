# Representation contract

PCA summarizes variation in a numeric expression matrix. If `use_highly_variable=true`, an
existing `var["highly_variable"]` mask is an intrinsic input; otherwise all genes are eligible.

A nearest-neighbor graph consumes a chosen cell representation. The caller names that source:
`X_pca` is common for normalized expression, `X_scVI` for a trained scVI latent, and
`X_scimilarity` for SCimilarity embeddings. Building a graph does not imply that representation
is scientifically appropriate.

UMAP consumes a neighbor graph and provides a low-dimensional visualization of that graph.

Every operation writes a new H5AD and a new artifact-scoped identity. No tool requires the input
to be the single globally “current” file.
