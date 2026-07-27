---
name: finalize-analysis
description: Adjudicate explicit cluster-to-cell-type labels, verify complete cluster coverage, save a final annotated AnnData and evidence report, and mark a session finalized only when dataset identity, current cluster QC, batch decision, and current annotation evidence floors pass. Use only after reviewing competing evidence and uncertainty.
---

# Finalize Analysis

Call `finalize_analysis` with a complete cluster-to-label mapping and a per-cluster rationale. Use the broadest label supported by the evidence and include uncertainty in labels or rationales when needed.

The runtime gate denies this tool unless the input identity exists, cell QC has a current resolved
visual review, cluster QC matches the current clustering and has a resolved visual review, a batch
decision is recorded, and DEG-primary annotation evidence has been reviewed with no unresolved
clusters. These conditions are necessary, not sufficient: inspect the actual evidence before
calling.

The capability refuses missing or extra clusters, writes labels to a new column, preserves all source observations, and produces a final dataset plus report. Never overwrite a user-provided annotation column.

The report is reconstructed from durable state and committed capability provenance. It includes
the workflow/parameters, QC and cluster-review decisions, batch evidence, annotation agreement,
per-cluster labels, automatically surfaced caveats, and artifact guidance. It also writes an exact
ordered capability-call recipe into the session `code/` view.

Provide `deg_labels` written independently from the DEG/marker evidence, `evidence_summaries`,
per-cluster `confidence`, and an override justification for every cluster whose final label differs
from its DEG label. Keep confidence honest: when evidence conflicts, choose `medium`/`low` and
generalize upward instead of asserting a subtype. Do not finalize a cluster as "doublet" on a
Scrublet call, and do not call a `GZMB`-high cluster "plasma" without immunoglobulin/secretory
markers (consider pDC instead).

Read [references/adjudication.md](references/adjudication.md) for disagreement and uncertainty handling.
