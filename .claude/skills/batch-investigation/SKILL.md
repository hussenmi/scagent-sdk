---
name: batch-investigation
description: Produce gene-first batch evidence (sample-enriched regions, within-sample identity DEGs, cross-sample population matches, direct comparisons, recurring programs, and design confounding) with a two-axis verdict, then record an explicit keep/integrate/separate/request-guidance/not-applicable decision as a separate step. Use before integration and before finalization whenever samples, donors, libraries, lanes, or batches may affect the representation.
---

# Batch Investigation

This is a two-step, evidence-before-decision capability.

1. **`investigate_batch`** accepts any H5AD with expression values, cluster labels, and a
   meaningful batch column, then produces evidence and records **no** decision. It derives
   portable identities from the artifact. This region-comparison method uses the supplied
   clustering and is **gene-first**:
   - finds **sample-enriched cluster regions** by enrichment over each batch's dataset-wide
     frequency (not raw purity — a region that is 42% of one sample when that sample is 9% of the
     data is caught);
   - runs a **within-sample identity DEG** for each region (that cluster vs the rest of its OWN
     batch, holding batch constant) — the primary evidence for what the population is;
   - **matches the same population across batches** by shared identity genes (a candidate match,
     not proof);
   - **compares matched regions directly** and reports genes higher on each side;
   - flags a **recurring** sample-associated program (higher in the same batch across ≥2 distinct
     populations);
   - cross-tabs supplied `condition_keys` against the batch key for **confounding**.

   Composition, Cramér's V, neighborhood mixing, and per-batch QC are kept only as **advisory
   context**. The verdict is two independent axes — `gene_evidence` (`none`/`localized`/
   `recurring_sample_associated`) × `design_interpretation` (`unknown`/`confounded_with_biology`/
   `orthogonal_but_not_known_technical`/`documented_technical_batch`) — and a **non-binding**
   recommendation. Only a recurring program **and** a documented technical batch yields
   `integration_supported`. Set `technical_batch_documented=true` only with a real
   `technical_batch_basis` — never infer it from separation. If there is no meaningful batch
   variable, pass `batch_key=null` to record `not_applicable`.

2. **`decide_batch_handling`** consumes the current `evidence_id` after you inspect the evidence and
   records the decision. This consequential decision step is deliberately state-bound.
   Non-integration decisions are always allowed. `integrate` requires an
   explicit `integration_basis` and never proceeds silently against the evidence: if the
   recommendation does not support it you must pass an `override_warning`.

A matched identity plus a direct gene list does **not** prove a technical batch effect; cell-level
q-values rank separation and are not sample-level replication. diffxpy runs in an isolated runtime,
so `prefer_diffxpy` degrades visibly to the in-environment Wilcoxon test rather than running in the
wrong environment. After an `integrate` decision, integration issues a new representation and
clustering that require fresh cluster QC and a fresh batch evaluation before finalization.

Read [references/decision-guide.md](references/decision-guide.md) for confounding and integration
cautions.
