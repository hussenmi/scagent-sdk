# Adjudication

Resolve labels cluster by cluster. Prefer convergent marker and reference evidence. When methods disagree, inspect DEGs, prediction confidence/support, tissue context, and likely out-of-domain behavior. Generalize upward instead of inventing precision. Preserve mixed, doublet-like, cycling, and low-quality states as qualified labels when justified.

## DEGs are primary; every final label carries an independent DEG label

`finalize_analysis` requires a complete `deg_labels` map written independently from the cluster
DEG/marker evidence, plus `evidence_summaries`, per-cluster `confidence`, and a justification for
every cluster whose final label differs from its DEG label. The final label is not a place to
launder a reference prediction that the DEGs do not support.

## Confidence must track evidence, not conviction

- When positive and negative markers conflict, or reference methods disagree with the DEGs, set
  `confidence` to `medium` or `low` and say why in the evidence summary. Do not report `high`
  confidence over contradictory evidence.
- Generalize upward (a broader lineage) rather than asserting an unsupported subtype.

## Look-alikes and probabilistic evidence

- **pDC versus plasma.** A `GZMB`-high cluster is not "plasma" without immunoglobulin/secretory
  markers (`MZB1`, `JCHAIN`, `SDC1`, `XBP1`, `IGHG1`). Call plasmacytoid dendritic cells only with
  their defining program (`LILRA4`, `IL3RA`, `CLEC4C`, `IRF7`/`TCF4`). See the marker skill's
  evidence standard for the full discriminator.
- **Scrublet is probabilistic.** Predicted-doublet enrichment is evidence about barcodes for review,
  not a cell-type identity; never finalize a cluster as "doublet" on a Scrublet call alone, and do
  not describe Scrublet calls with more certainty than a probabilistic detector warrants.
