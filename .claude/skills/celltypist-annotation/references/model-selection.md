# Model selection and input contract

Use a model trained on the same organism and a sufficiently compatible tissue. Broad models reduce false precision; specialized models may resolve subtypes but can force out-of-domain cells into known labels. Report the model filename, confidence distribution, and disagreement with markers.

CellTypist consumes gene-symbol expression. The skill selects the best-overlapping source from
`var_names` and common symbol columns, aligns casing to the model vocabulary, and normalizes raw
counts internally. Human examples include `CD3D`, `MS4A1`, and `LYZ`; conventional mouse examples
include `Cd3d`, `Ms4a1`, and `Lyz`. Ensembl-indexed inputs need a symbol column or prior mapping.

Per-cell inference is independent of clusters. Majority or cluster consensus is a downstream
summary and must not be used as a prerequisite for running the classifier.
