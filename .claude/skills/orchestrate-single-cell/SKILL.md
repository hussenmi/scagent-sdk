---
name: orchestrate-single-cell
description: Orchestrate an evidence-driven single-cell RNA-seq analysis across focused skills while preserving scientific state, provenance, and resumability. Use for complete or multi-step analyses, deciding what should happen next, resuming prior work, coordinating inspection, QC, preprocessing, clustering, batch investigation, integration, annotation, differential expression, pathways, visualization, and reporting, or explaining why a scientific floor blocks finalization.
---

# Orchestrate Single-Cell

Drive the analysis toward the user's biological goal without turning the workflow into a fixed pipeline. Consult the durable session facts and artifacts, then select the smallest focused skill that can resolve the next scientific uncertainty.

## Comprehensive end-to-end default

When the user asks to analyze a regular raw or minimally processed dataset end to end, treat
“complete” as a comprehensive evidence standard, not merely a final H5AD. This is a default
playbook, not a runtime DAG: the user may change parameters, omit inapplicable branches, or begin
from an already processed artifact, and the observed data may require replanning.

1. Inspect and describe the input, establish byte identity, resolve raw counts, and convert gene
   identifiers before QC when symbols are available.
2. Calculate cell QC with `counts_layer="auto"`. Inspect every returned standard figure, evaluate
   doublets when raw counts permit it, and call `review_single_cell_qc` with a concrete keep/filter
   rationale. If cells or genes are removed, recalculate and review QC on the retained artifact.
3. Normalize, select HVGs, compute PCA, inspect the PCA variance figure, build neighbors, compute
   UMAP, and call `plot_qc_embedding`. Explain where quality signals localize; distributions alone
   do not show whether a signal is a coherent population.
4. Unless the user specifies another grid, compare Leiden resolutions **2.0, 1.5, and 1.0** using
   distinct keys such as `leiden_res_2_0`. At each resolution, run `evaluate_cluster_qc`, inspect
   its per-cluster metric boxplots, cluster/QC UMAP, and every per-cluster covariance heatmap, then
   call `review_cluster_qc`. Do not carry an unresolved remove/merge/split/recluster disposition
   into annotation. These resolutions are overridable defaults, not hardcoded requirements; add or
   change them when cluster sizes, stability, or biology justify it.
5. Select the final resolution from stability, DEG identity, covariance coherence, separation, and
   interpretability—not because it was run last. Make that selected clustering the current
   clustering, then investigate batch structure when meaningful batch metadata exists and record
   an explicit decision. Record `not_applicable` when no defensible batch unit exists.
6. For annotation, use SCimilarity early when it helps establish broad tissue/context, inspect the
   complete readiness inventory of cached CellTypist models, and choose the closest organism/tissue
   model rather than a generic immune default. When both are suitable, run and summarize both and
   visualize their agreement. Generate cluster DEGs and marker programs; **DEGs are the primary
   decision basis**, while references and curated marker resources such as Cytopus corroborate or
   challenge the call. Query the reference atlas or literature for genuinely ambiguous clusters.
7. Call `review_annotation_evidence`, leaving ambiguous clusters unresolved until the evidence is
   adequate. Finalize only then. The final report must reconstruct the full committed workflow,
   parameters, QC decisions, cluster reviews, batch decision, annotation disagreements, caveats,
   and deliverables.

For a targeted question—one plot, one reference query, an already finalized object—use only the
capabilities needed for that question. Do not force the comprehensive playbook onto unrelated work.

## Operating loop

1. Establish the relevant artifact and current processing state. Never assume a resumed file is unchanged.
2. State the immediate scientific question and why the selected capability answers it.
3. Prefer deterministic capability tools for computation and validation. Use model reasoning to choose parameters, compare evidence, and interpret results.
4. Inspect the committed facts and artifacts after every material action. Do not infer success from narration. If a figure is unreadable because of crowded legends or labels, treat visual review as incomplete and use its table or regenerate a legible view before making a visual claim.
5. Replan when evidence contradicts the initial path. Avoid running integration, fine-grained annotation, or destructive filtering merely because those steps are common.
6. Conclude with actual results, decisions, caveats, and artifact paths—not a list of tools used.

## Scientific floors

- Require a current raw-droplet suitability attestation before ambient-background removal.
- Generate doublet evidence from verified raw counts, normally per biological library. Treat predicted calls and cluster enrichment as probabilistic review evidence, never as cell-type truth or automatic permission to remove cells.
- Require cluster DEGs plus independent reference/marker evidence before final labels. DEGs are primary; reference-model predictions are hypotheses. When programs conflict, lower confidence, generalize the label, or leave it unresolved instead of forcing a subtype.
- Require an evidence-bound visual QC decision and a resolved cluster-QC visual review before final publication.
- Invalidate downstream evidence when filtering, representation, or clustering identity changes.
- Register every saved dataset, table, figure, and report with provenance.

Floors belong only on consequential decisions or mutations, not on ordinary computation. Every
focused tool is directly callable when its own intrinsic inputs are present. SCimilarity and
CellTypist require raw counts, compatible genes, and a matching reference model.
If a needed capability is unavailable, say exactly what is missing and preserve the session;
never fabricate a result or claim completion.

Read [references/workflow-decisions.md](references/workflow-decisions.md) when choosing between optional branches or resuming a partially completed analysis.
