# Evidence standard

A defensible cluster label usually has multiple coherent positive markers, compatible absent/negative markers, plausible tissue context, and agreement with at least one independent reference or ontology source. Doublet-like mixed programs and low-quality stress programs should be reported as uncertainty, not forced into a precise label.

Marker-set coverage is length-normalized, and markers shared by multiple programs are down-weighted
in the specificity score. Neither score is a label assignment. Review the underlying DEG table,
effect sizes, adjusted p-values, expression fractions, competing candidates, doublet localization,
and tissue context. Finalization must preserve an independently written DEG-derived label for every
cluster and make any override explicit.

## Discriminating look-alike programs

Some programs share individual genes and must be separated on their full positive and negative
evidence, not on a single shared marker:

- **Plasmacytoid dendritic cells (pDC) versus plasma cells.** Both can express `GZMB`, and `GZMB`
  is intentionally shared across the pDC and cytotoxic programs so it can never carry a call on its
  own. A pDC needs its own program — `LILRA4`, `IL3RA` (CD123), `CLEC4C` (BDCA2), and the
  `IRF7`/`TCF4` transcriptional program — and should lack the plasma secretory program. A plasma
  cell needs immunoglobulin and secretory evidence — `MZB1`, `JCHAIN`, `SDC1` (CD138), `XBP1`,
  `IGHG1` — and should lack `LILRA4`/`IL3RA`/`CLEC4C`. Do not label a `GZMB`-high cluster "plasma"
  without immunoglobulin/secretory markers; consider pDC when the pDC-defining genes are present.
- **Cytotoxic lymphocytes** share `GZMB` with pDC but are positive for `NKG7`, `GNLY`, `PRF1`, and
  `CTSW`, and negative for the pDC innate program.

When positive and negative evidence conflict, lower confidence and generalize upward (for example
"dendritic cell" or "mononuclear phagocyte") rather than asserting an unsupported subtype. Doublet
scores and predicted-doublet calls are probabilistic evidence about barcodes, never a cell-type
identity: an enriched Scrublet signal supports review, not a "doublet" label.
