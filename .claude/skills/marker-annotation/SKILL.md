---
name: marker-annotation
description: Generate group-wise differential-expression evidence and score transparent curated marker programs for a supplied grouping and expression matrix.
---

# Marker Annotation Evidence

Call `evaluate_marker_evidence` for a supplied grouping column and appropriate expression matrix.
This produces candidates and competing evidence for later label adjudication.

Inspect statistically supported positive DEGs, expression fractions, log-fold changes, marker
coverage, shared-marker ambiguity, and specificity-weighted overlap. Reject candidates driven by
ambient RNA, generic stress/cell-cycle programs, or a single gene. An `insufficient` overlap is not
a negative label; it means the supplied marker programs do not support that candidate. Generalize
labels when subtype evidence is weak. Add tissue-specific marker sets through `marker_sets` when
the built-in broad programs are insufficient, and cite their source in the analysis rationale.

Explain the DEG analysis before interpreting it: name the grouping, state that each cluster is
compared against the other cells in the current analysis, and explain that positive DEGs are genes
enriched in that cluster rather than a cell-type label by themselves.

Then give the user a complete cluster-by-cluster evidence-to-decision account. Cover every current
cluster, preferably in a readable table with:

- cluster and cell count;
- the most informative positive DEGs and, where useful, their effect sizes or expression
  fractions;
- the lineage or identity program those genes support;
- important expected markers that are absent, contradictory programs, or plausible alternatives;
- agreement or disagreement with each current independent reference method;
- the DEG-derived label, final proposed label, confidence, and the reason for any difference.

Introduce shared lineage patterns once and keep individual rows focused, but never omit a cluster
because the dataset has many clusters. Thorough does not mean listing every ranked gene: select the
genes that discriminate the chosen identity from realistic alternatives. For ambiguous clusters,
say what is unresolved and what additional evidence would separate the alternatives. The user
must be able to trace every proposed label back to its DEG comparison and corroborating or
conflicting evidence before annotation review is marked resolved.

Separate look-alike programs on their full positive and negative evidence. A `GZMB`-high cluster is
not a plasma cell without immunoglobulin/secretory markers (`MZB1`, `JCHAIN`, `SDC1`, `XBP1`,
`IGHG1`); consider plasmacytoid dendritic cells when their defining program (`LILRA4`, `IL3RA`,
`CLEC4C`, `IRF7`/`TCF4`) is present. Predicted-doublet enrichment is probabilistic barcode evidence
for review, never a cell-type label.

Use at least one independent reference method when an appropriate model exists, then finalize through the gated finalization capability.

For a comprehensive analysis, use both CellTypist and SCimilarity when appropriate models are
ready, visualize their agreement, and call `review_annotation_evidence`. If only one method is
compatible, record a specific waiver. DEGs are the primary label hypothesis; reference labels,
curated programs, Cytopus-covered marker knowledge, and atlas queries corroborate or challenge it.
For human data, keep `use_cytopus=true` unless the package is unavailable or the user explicitly
opts out. The capability records whether the knowledge base loaded and which programs contributed;
Cytopus augments marker hypotheses but does not replace cluster DEGs as the primary evidence.
Do not mark the evidence resolved while any cluster remains ambiguous.

Read [references/evidence-standard.md](references/evidence-standard.md) for label-strength expectations and the pDC-versus-plasma discriminator.
