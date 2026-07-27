# SCimilarity method and interpretation

## Model type

SCimilarity learns an expression embedding in which transcriptionally similar cells are close.
The encoder checkpoint converts a model-ordered expression vector to a latent vector.
`CellAnnotation` uses a reference nearest-neighbor index and reference labels to transfer a label
to each query cell through metric learning and reference lookup. `CellQuery` uses a separate,
much larger cell-search index to return the reference *cells* themselves with their metadata.

## Model directory

A usable embedding model contains at least:

- `encoder.ckpt` — trained encoder weights;
- `gene_order.tsv` — exact ordered feature vocabulary;
- hyperparameter/layer metadata used to reconstruct the encoder.

kNN annotation additionally needs the matching indexed reference assets and labels. Releases may
store those as index files or model-specific reference structures; validate the directory with the
installed SCimilarity version rather than searching only for a `.pt` filename. Standard
checkpoints use `.ckpt`.

Atlas queries need `cellsearch/full_kNN.bin` and the `cellsearch/cell_metadata` store, which are a
separate multi-gigabyte download. Their absence is reported by the readiness probe and refused by
the query tool with the two paths named; it never blocks annotation.

## Input contract

- cells × genes AnnData;
- raw, finite, nonnegative integer counts from a selected layer or `X`;
- enough symbols overlapping `gene_order.tsv` (default minimum 5,000);
- an explicitly declared organism, verified against the model vocabularies.

Clustering, PCA, HVGs, a graph, UMAP, and prior SDK state are unnecessary.

`align_dataset` reorders query genes to the model vocabulary and fills missing model genes with
zeros. The skill chooses between `var_names` and known symbol columns by measured overlap and
aligns case to the model. `lognorm_counts` then applies SCimilarity's training-compatible
normalization before the encoder runs.

## Organism verification

Overlap counting cannot detect a species mismatch. Roughly 15,600 symbols are shared between the
human and mouse vocabularies once case is folded, so mouse input clears a 5,000-gene threshold
against the human reference and produces confident, wrong labels. Case is no help either: some
pipelines uppercase mouse symbols.

The check therefore scores only the *organism-specific* part of each vocabulary — the case-folded
set difference between the two configured models, about 12,600 human-only and 6,000 mouse-only
symbols. Real human colorectal data matches ~2,300 human-only against ~20 mouse-only symbols;
mouse input of any casing matches thousands of mouse-only against zero human-only. A declaration
is contradicted when counter-organism hits exceed declared hits by more than 2×, given at least 50
organism-specific hits in total. Below that the verdict is `unverified` rather than a guess, which
is the correct outcome for Ensembl-only identifiers — the overlap threshold catches those instead.

`allow_species_mismatch=true` converts the refusal into a recorded caveat for deliberate
cross-species exploration. The verdict is stored in the run report and in session facts either way.

## Output contract

Per-cell inference preserves the input observations and adds the SCimilarity embedding, predicted
reference label, minimum neighbor distance, and the two vote margins. The run report records the
exact model path, lightweight model fingerprint, count source, name source, overlap count,
embedding shape, prediction diversity, distance distribution, vote-margin summaries, the
constrained-annotation block when one ran, and the organism verdict.

## Voting, confidence, and constrained annotation

`get_predictions_knn` votes over `knn_k` reference neighbors (default 50) and, alongside the
label, computes `vs2nd` and `vsAll` — the winner's share of the top-two votes and of all votes —
plus their inverse-distance-weighted twins when `weighting=true`. These are the model's own measure
of how contested a call was and are surfaced as `obs` columns; a dataset where a fifth of cells sit
below 0.5 on `vs_all` is one whose per-cell labels should not be read individually.

Constrained annotation safelists an explicit set of reference labels. Mechanically,
`safelist_celltypes` marks every *other* label deleted in the annotation index, so the labels must
exist verbatim in the model's ~700-label set — they are validated first, with near-miss
suggestions, because a single typo would otherwise leave the reference empty. Both the constrained
and unconstrained predictions come from one embedding pass, and the report gives their agreement,
the number of reassigned cells, and any safelisted type that matched no cells. Low agreement is
information about the safelist or the sample, not proof that either is wrong.

Cluster consensus is a separate aggregation. A high support fraction means cells in that supplied
group received similar reference labels; it does not prove the group is correct or biologically
homogeneous.

## Reference atlas query

A query is either a centroid over selected cells (`query_mode="centroid"`) or one search per
selected cell (`query_mode="cells"`, capped at `max_query_cells`, default 10). The centroid path
matches SCimilarity's centroid and cluster-centroid searches; the per-cell path matches its
single-cell search tutorials, where one cell of interest is used to ask what it resembles across
the atlas. Per-cell cost is linear in cells queried, which is why the cap is small and exceeding
it is an error rather than a subsample: which cells were queried must be reproducible from the
recorded inputs.

Each query embeds its vector and retrieves the `k` nearest reference cells. Reported per query:

- **composition** — the top values of whichever reference metadata columns the release provides,
  with counts and fractions. Column names are measured, not assumed; the current human and mouse
  releases carry `celltype_name`, `tissue`, `tissue_general`, `disease`, `study`, `sample`, and a
  `prediction` column, and absent columns are listed in the report.
- **neighbor distances** — minimum, median, mean, maximum over the retrieved cells. Same caveat as
  per-cell distances: model-specific, not calibrated.
- **coherence** — the query is k-means split into up to 10 sub-centroids; each sub-centroid's 100
  nearest reference cells are searched, and coherence is the mean number of those that also fall
  within the whole-query centroid's top `k`. High coherence means the population lands in one
  region of the reference; low coherence means the centroid is averaging over cells that match
  different reference neighborhoods, which is evidence of a mixed population. Because the
  comparison set is the top `k`, the number only means something relative to the `k` used, and it
  is not computed below 20 query cells or for per-cell queries.
- **sample enrichment** — via `compile_sample_metadata`, the studies and samples the hits came
  from with each hit count as a fraction of that sample's total cells. Ranked by that fraction so
  a small, densely matched sample is not buried under a large one contributing more raw hits.
- **reference background** — the reference-wide disease and tissue composition of the leading
  matched cell type, with the query fraction over the reference fraction as an enrichment ratio.
  This is the step that turns composition into a claim: the SCimilarity tutorials do it by hand,
  showing that a myofibroblast query was ~10% healthy where the reference myofibroblasts are >50%
  healthy. A value absent from the background gets no ratio rather than an infinite one.

Study exclusion (`exclude_studies`) is applied after retrieval and before every summary. When the
query data is itself part of the reference, its own study dominates the neighbors and the result
is circular; the tutorials filter the query study out for exactly this reason. A query whose
neighbors are entirely excluded is reported with a warning rather than as a weak match.

Measured cost against the 42.9M-cell human atlas on Iris, all at k=100. Loading the index takes
82-88 s and peaks near 84 GiB resident — the 46.9 GiB neighbor index plus the full reference
metadata table, which SCimilarity loads eagerly. After that, per query: **55 s** for a centroid
with coherence, **6.2 s** for a centroid without it (identical composition and distances), and
**~17 s** per cell in per-cell mode. Reference-background comparison adds 4-6 s for the whole call.
Every search joins its hits against the 43M-row metadata frame, which is what dominates all three
numbers; coherence is expensive because it issues one extra such search per sub-centroid. Load,
search, and background times are reported with every run, so the cost stays observable rather than
folklore.

Bounding: all requested groups share one index load. A grouping with more groups than
`max_queries` is refused rather than subset. The neighbor artifact is capped at 200,000 rows,
keeping the nearest rows per query and flagging that it was capped. The model-visible result
carries at most 25 queries and the top 5 values per column; the full report is always written as
an artifact.

## Limitations and failures

- Wrong species is refused on vocabulary evidence, not on overlap, which cannot see it.
- Poor symbol mapping can silently degrade embeddings, so low overlap is refused.
- Normalized/scaled input is refused because the model owns normalization.
- Rare or unseen cell states can be forced to the nearest known label; inspect distance and label
  diversity.
- Distances are relative to one model/index and should not be compared as universal probabilities.
- A centroid over a heterogeneous population describes a point no real cell occupies; low
  coherence is the signal for that, and the fix is to query finer groups.
- A model trained outside the tissue or perturbation may collapse meaningful novel states.
- Embedding can be scientifically useful even when cluster aggregation is irrelevant.
