---
name: visualize-single-cell
description: Produce standard single-cell figures — QC distributions, embedding scatters, group composition, label-agreement heatmaps, and marker dot plots — from an existing H5AD artifact, returning each figure for inspection. Use whenever a plot, figure, or visual summary of single-cell data or annotation results is wanted, instead of writing custom plotting code.
---

# Single-cell visualization

Plotting is a capability, not custom code. Use these tools rather than `analysis-workspace`
whenever one of them fits; hand-written matplotlib has no artifact contract, no layout rules, and
no guarantee the figure comes back for inspection.

Each tool reads one H5AD, writes the figure **and the table behind it**, and returns the figure as
model media so you see the pixels in the same turn. None of them mutates the dataset or the
scientific state.

| Question | Tool |
|---|---|
| Are these cells usable? Where are the QC thresholds? | `plot_qc_distributions` |
| Where do QC and doublet signals localize after embedding? | `plot_qc_embedding` |
| How is structure organized, and where do labels/genes fall on it? | `plot_embedding` |
| What is each cluster/sample made of? | `plot_group_composition` |
| Do two independent annotations agree? | `plot_label_agreement` |
| Which genes distinguish these groups? | `plot_marker_expression` |

## These tools are a floor, not a quota

Each call returns a *set* of figures, not one. `plot_qc_distributions` alone returns the UMI-rank
knee, log-scaled library-size and detected-gene histograms, mitochondrial and ribosomal percent,
jittered per-metric violins, the joint scatters, doublet evidence when the artifact carries it, and
per-group boxplots when you pass `group_key`. Returning that set is the *minimum* for a QC review —
it is what a reader needs before any threshold is defensible, not a complete answer to your
question.

So treat the standard set as the starting point and keep going:

- Pass `group_key` whenever a sample, donor, library, or cluster column exists. Pooled QC hides the
  one library that is failing, and "the pooled distribution looked fine" is not evidence about any
  individual sample.
- Plot the same annotation on more than one embedding when several exist (`X_umap` against
  `X_scimilarity` or `X_scVI`): agreement across representations is evidence, and a structure
  present in only one is a property of that embedding.
- Follow any figure that raises a question with the figure that answers it — a suspicious cluster
  in a QC overlay deserves its markers (`plot_marker_expression`) and its composition
  (`plot_group_composition`), not a verdict from the overlay alone.
- Reach for `analysis-workspace` when the scientific question genuinely has no tool here. Read
  [references/figure-legibility.md](references/figure-legibility.md) first and hold custom figures
  to the same standard: log axes for heavy-tailed metrics, `n` per group, no repeated colors.

Heavy-tailed metrics are drawn on log axes with log-spaced bins, because linear bins under a log
axis compress the low end into two or three bars and hide exactly the shape a threshold is read
from. Do not re-plot library size or detected genes on a linear axis.

## Choosing inputs

- `plot_embedding` requires an embedding to already exist in `obsm`. Reference-model embeddings
  such as `X_scimilarity` qualify — you do not need a UMAP pipeline to visualize structure. If no
  embedding exists, say so and offer to compute one; do not silently build a throwaway pipeline.
- Gene panels and marker dot plots normalize raw counts internally for display only and report
  `expression_normalized_internally`. Supply symbols that exist in `var_names`; convert identifiers
  first when the artifact carries Ensembl IDs.
- `plot_label_agreement` measures *exact string* agreement. Two reference vocabularies (SCimilarity
  reference labels versus a CellTypist model's classes) disagree on wording constantly, so read the
  heatmap structure, not just the headline percentage, and say which pairs are the same biology
  under different names.

## Reading the result

High cardinality is handled by design: composition switches from stacked bars to a heatmap past
twelve classes, and categories beyond the cap collapse into an explicit `other` rather than
producing an unreadable legend. Check `collapsed_*` in the returned facts before concluding that a
population is absent.

`plot_embedding` and `plot_qc_embedding` also return a **highlight grid** for each categorical
panel — one small panel per category, that category colored and every other cell grey, with its
cell count in the panel title. Read the grid, not just the overlay, whenever you need to locate a
specific cluster, judge whether a population is one blob or scattered fragments, or find a rare
`qc_flag_*` that the overlay renders as a handful of invisible pixels. Colors match between the two
figures, so a population can be tracked across them. `highlight_grids_skipped` in the facts says
which keys did not get one and why.

Mitochondrial and ribosomal percent come from `pct_counts_mt` / `pct_counts_ribo` when the artifact
already carries them, and are otherwise computed from the matrix; `mito_percent_source` and
`ribo_percent_source` say which. This matters on a gene-filtered artifact — recomputing after the
ribosomal genes have been dropped reports 0%, and the recorded column is the honest number.

Interpret the figure you were given. State what the plot shows, what it implies for the next
decision, and which artifact path holds it.

A figure that renders is not a figure that is readable. Before making a claim from one, confirm
every panel actually contains marks, that `n` is known per group, that no two categories share a
color, and that the data occupies the plot rather than a strip or a corner. If it does not, the
visual review is incomplete: regenerate a legible view or read the table behind it.

Read [references/figure-legibility.md](references/figure-legibility.md) before writing any custom
plotting code in `analysis-workspace`, and when deciding whether a hard-to-read figure needs to be
regenerated. It covers empty-panel and label-mismatch failures, heavy-tailed scales, overplotting,
aspect ratio, high-cardinality legends, and color.
