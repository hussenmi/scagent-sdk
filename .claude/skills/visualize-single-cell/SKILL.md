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

Interpret the figure you were given. State what the plot shows, what it implies for the next
decision, and which artifact path holds it.
