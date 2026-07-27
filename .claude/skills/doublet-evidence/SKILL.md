---
name: doublet-evidence
description: Generate and review identity-bound Scrublet doublet evidence from verified raw counts, normally per biological library. Use before final cell-set cleanup when doublet calls are missing or stale; do not use score cutoffs invented outside Scrublet, and do not remove cells without an explicit review decision.
---

# Doublet Evidence and Review

Scrublet predicts transcriptomes that resemble simulated multiplets. Its calls are evidence, not
ground truth: heterotypic doublets are easier to detect than homotypic doublets, high-RNA singlets
can score highly, and the expected rate depends on loading and capture design.

## Evidence sequence

1. Start from an H5AD with integer-valued raw counts in `layers["counts"]` or `X`. Evaluation
   derives portable identities from the artifact. CellBender, when chosen, belongs before this
   step. Never run Scrublet on log-normalized or scaled values.
2. Name the biological library column explicitly. Scrublet must simulate doublets independently
   within each captured library. If the dataset genuinely contains one library, pass
   `batch_key=null` and explicitly confirm unstratified execution.
3. Call `evaluate_doublet_evidence`. Inspect the per-library rates, observed-versus-simulated score
   distributions, UMAP overlay when available, warnings, and exact `predicted_doublet` calls.
4. Keep flagged cells in place while reviewing their cluster distribution and marker/QC context.
   Do not invent a score threshold and substitute it for Scrublet's predicted call.
5. Call `review_doublet_evidence` with a written rationale. Review is deliberately state-bound
   because it authorizes a consequential decision. `retain_for_cluster_review` is the normal
   first decision. `remove_predicted` requires explicit confirmation, removes only
   `predicted_doublet == true`, creates a new cell set, and invalidates all downstream evidence.

## Interpretation rules

- Report the exact library key and number of libraries used.
- A zero or unusually high predicted rate is a warning to inspect, not permission to tune until a
  preferred percentage appears.
- Never treat the configured expected rate as a target that observed calls must equal.
- Failed or undersized libraries fail the evidence run; they are never silently written as zero
  scores or singlets.
- If calls concentrate in a coherent biological cluster, review marker identity and library/QC
  context before removal. Doublet enrichment alone is not proof that the cluster is artifactual.
- Removing calls changes the cell set. Re-run preparation, embedding, clustering, cluster QC,
  batch investigation, and annotation on the filtered raw-count artifact.

Read [references/scrublet-and-state-contract.md](references/scrublet-and-state-contract.md) before
changing parameters or removing calls.
