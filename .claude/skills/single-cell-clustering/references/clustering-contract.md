# Clustering and group-ranking contract

Leiden partitions a neighbor graph. Resolution controls granularity but does not have a universal
correct value. A clustering identity binds the graph, labels, key, resolution, and seed.

A graph derived from PCA, scVI, SCimilarity, or another explicit representation can be used for
Leiden when scientifically justified.

Ranked genes compare each group with a reference population using the requested Scanpy method.
The input matrix should match the inferential goal; log-normalized expression is typical for
Wilcoxon ranking. A ranked list is evidence, not a finalized cell-type label.

The two tools are intentionally independent. Ranking can consume any existing group key, including
labels created outside this SDK.
