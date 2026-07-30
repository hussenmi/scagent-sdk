# Figure legibility

Load this when you are about to write plotting code in `analysis-workspace.run_analysis_code`, or
when a figure you just received back is hard to read and you need to decide whether to fix it or
accept it.

The capability tools in this skill already apply these rules. This file exists for the figures the
tools do not cover — and for reviewing any figure, including theirs, before you make a claim from
it.

## The first check: is the figure showing the data?

Before judging aesthetics, confirm the plot contains what you asked it to contain. Three failures
in real sessions here, all of which produced a figure that *looked* finished:

**Empty panels from a label mismatch.** A faceted plot was built by filtering cells with
colloquial names — `"CD8+ T cells"`, `"CD4+ T cells"` — against a column holding Cell Ontology
labels such as `CD8-positive, alpha-beta T cell`. Every filter matched zero cells. Matplotlib drew
four axes frames with autoscaled `±0.05` limits and no marks, which reads as "boxes rendered but
too small to see" rather than "no data". The per-facet `n=0` annotation was the only evidence.

Never construct a category filter from a name you typed. Take the exact strings from
`value_counts()` on the actual column, assert every requested group is present, and fail loudly
when one is not:

```python
present = set(adata.obs[key].astype(str).unique())
missing = [g for g in requested if g not in present]
if missing:
    raise ValueError(f"absent from {key}: {missing}. present: {sorted(present)[:20]}")
```

**A subset that is empty for a legitimate reason** still must not render as a blank frame. Annotate
the axes with `no cells` text, or drop the facet and say in your reply which facets were dropped.

**Silent truncation.** If you cap categories, sample cells, or clip a range, the count that was
dropped belongs in the figure (as an `other (n)` legend entry or a caption) and in your reply. A
figure that quietly shows 30 of 61 cell types is a false statement about the data.

Always print `n` per group next to the figure. An `n=262` box plot and an `n=52,788` box plot look
identical and mean very different things.

## Scale: heavy tails are the default in single-cell data

Mahalanobis distances, library sizes, UMI counts, doublet scores, and gene counts are all
long-tailed. Plotted on linear axes with autoscaled limits, a handful of extreme cells set the
range and 99% of the data compresses into a few percent of the canvas — a figure that is
technically correct and visually empty.

A real example: a 250,000-cell NN-distance vs. Mahalanobis scatter had a y-limit of ~310 because of
a few outliers, so the entire population sat in the bottom 5% of the plot as an invisible smear.

- Set limits from robust percentiles, not from the extremes:
  `axis.set_ylim(0, np.percentile(values, 99.5))`, and state how many points fell outside.
- Or use `set_yscale("log")` when the values are positive and span orders of magnitude.
- Never silently drop the outliers to make the plot look nice. Clip the *view*, report the count.
- Sharing a y-axis across facets is right when comparing magnitudes and wrong when the facets
  differ by orders of magnitude. Decide deliberately, and say which you chose.

## Overplotting: 250,000 points is not a scatter plot

Beyond roughly 50,000 points, individual markers stop carrying information — the visual result is
determined by draw order and alpha, not by density.

- Prefer `hexbin`, a 2D histogram, or a KDE for raw density.
- If you must scatter, use `s=1..3`, `alpha=0.1..0.3`, `linewidths=0`, and **shuffle the row order**
  so the last-plotted category does not paint over the others. Every embedding tool here permutes
  rows with a seeded RNG for exactly this reason.
- For a categorical overlay on a dense cloud, plot a grey density background and draw only the
  highlighted group in color.

## Aspect ratio and panel proportion

An embedding is a 2D geometry. A UMAP or latent-space panel squeezed into a tall narrow box
misrepresents cluster shape and separation, which is the thing you are reading it for.

- Keep an embedding panel's data area close to square (`axis.set_box_aspect(1)`).
- Reserve legend space in the **figure width**, not by letting the legend eat the axes. Under
  `layout="constrained"` a legend anchored outside the axes shrinks the axes to fit; if the figure
  was not sized for it, the plot becomes a sliver while the legend fills the canvas.
- Sanity-check the result: if the drawn data region occupies less than about a third of the figure
  width, the layout is wrong regardless of how correct the data is.

## Legends and high-cardinality vocabularies

Reference-model annotation vocabularies are the hard case. SCimilarity and CellTypist emit Cell
Ontology names that routinely run 50–60 characters — `effector memory CD8-positive, alpha-beta T
cell, terminally differentiated` — and a single dataset can carry 60+ of them.

- Past ~12 entries a scatter legend stops being readable. Past ~30 it is decoration.
- Legend width scales with the **longest label**, not with the entry count. Budget for it.
- Above the readable limit, change strategy rather than shrinking the font: collapse to the top-N
  plus an explicit `other (n)`, switch to a heatmap or dot plot, or label the largest groups
  directly on the plot near their centroids.
- Prefer a shortened display label (`CD8+ Tem, term. diff.`) with the exact ontology string kept in
  the accompanying table. Never abbreviate in a way that merges two distinct labels.
- Do not place legend or annotation boxes inside the data region. Overlapping annotation boxes that
  occlude each other and the densest part of the cloud are worse than no legend.

## Choosing the form, before choosing anything else

Pick the mark from the job the data has to do — magnitude, identity, polarity, distribution,
change over time. Sometimes the answer is not a chart: three numbers with an `n` are a table, and
a single comparison is a sentence. Color is the *last* decision, not the first.

**Never use two y-axes.** Two measures on different scales go in two panels, small multiples, or
indexed to a common baseline. A dual-axis plot lets the author choose the apparent correlation.

## Color

- **Categorical**: use a qualitative palette and **never let colors repeat**. A cyclic index into a
  20-color map with 30 categories gives two different cell types the same color — that is a wrong
  figure, not an ugly one. If the number of categories exceeds the palette, collapse the tail into
  `other` rather than wrapping.
- **Color follows the entity, not its rank.** Assign hues by a stable property of the label, never
  by its position in a count-sorted list. Otherwise filtering cells, changing `max_categories`, or
  re-running at a different resolution silently repaints the survivors — and "NK cells were blue
  in the first UMAP and are orange in the second" makes two figures of the same data impossible to
  compare. Cross-figure comparison is the normal activity here, so stability matters more than
  having the largest population take the first color.
- **Identity must not rest on color alone.** Past a dozen categories, adjacent hues stop being
  separable no matter how good the palette. Direct-label the largest groups on the plot, or
  reduce the number of categories.
- **Sequential** (expression, QC magnitude, counts): `viridis` / `magma`. Perceptually uniform and
  colorblind-safe.
- **Diverging** (log fold change, z-score, correlation, anything with a meaningful zero): `RdBu_r`
  or `coolwarm`, and center the norm on zero (`TwoSlopeNorm(vcenter=0)`). The midpoint must be
  neutral — a diverging map with a *hue* at the middle, or with an off-center midpoint, invents
  structure that is not in the data.
- Never use `jet` or `rainbow`.
- Grey is reserved for `other`, `unassigned`, and background density. Do not also use it for a real
  category.
- Reserve red/green pairs for something other than the primary categorical contrast.
- **Colorblind-safety is computable — do not eyeball it.** The standard check is pairwise
  perceptual distance in a uniform space (OKLab) under simulated deuteranopia/protanopia, with a
  separation floor for adjacent pairs. We have no validator wired up yet, so prefer the
  known-safe maps above (`viridis`, `magma`, `RdBu_r`) and the curated qualitative sets rather
  than hand-picking hex values.

## Text and annotation

Labels, values, and legends stay in plain ink — black or grey. A colored marker beside the text
carries the identity; coloring the *text* to encode series is harder to read and fails outright
when annotation boxes overlap. Annotation boxes belong outside the data region.

## Labeling

- Axis labels state the quantity and its unit or transform: `NN distance (cosine)`,
  `log1p(total counts)` — not `x` or `value`.
- If you rescale for display (`NN Distance × 1000`), say so in the axis label, and keep the
  untransformed values in the table.
- Titles state what the panel shows, not the variable name that produced it.
- Give the figure a caption or a returned fact stating `n`, the filter applied, and the artifact it
  came from.
- Tick labels that would overlap must be rotated, thinned, or the plot transposed to horizontal.

## Before you claim anything from a figure

Look at the pixels you got back. Then confirm:

1. Every panel contains marks, and any empty one is explained.
2. `n` is known for each group and is stated.
3. The data occupies most of the plot area rather than a corner or a strip.
4. No two categories share a color; the legend is readable at rendered size.
5. Nothing is occluded — by a legend, an annotation box, or overplotting.
6. What you are about to say is visible in the figure, not inferred from what you expected.

If any of these fails, the visual review is **incomplete**. Regenerate a legible view or read the
underlying table instead. Do not describe a figure you cannot actually read.
