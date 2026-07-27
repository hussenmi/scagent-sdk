# Scrublet method and state contract

## Scientific role

Scrublet simulates doublets by combining observed raw-count profiles, embeds observed and simulated
profiles together, and calls cells from their doublet-neighbor scores. It is most informative for
heterotypic multiplets. Homotypic doublets can resemble singlets and remain undetected; conversely,
high-RNA or transitional singlets can be flagged.

The locked implementation is `rapids-singlecell` 0.15.2 in the GPU-required `gpu-singlecell`
profile. Its installed API performs raw-count filtering, normalization, variable-gene selection,
simulation, GPU PCA/neighbors, automatic thresholding, and independent execution by `batch_key`.
The skill does not silently fall back to CPU or to another implementation.

## Input contract

- Input may be any H5AD with valid raw counts; it need not already be the current prepared artifact.
- Existing provenance identities are preserved when present. Otherwise the evaluator derives
  portable dataset, cell-set, count-matrix, and count-representation identities from the artifact.
- `layers['counts']` is preferred and must be finite, nonnegative, integer-valued raw counts. `X`
  is accepted only when it independently satisfies that contract.
- Existing `doublet_score` or `predicted_doublet` columns are never overwritten unless explicitly
  requested.
- A non-null library key must exist, contain no missing values, and leave every library at or above
  `min_cells_per_library`. A failed or undersized library fails the whole evidence result.
- Null `batch_key` requires `confirm_unstratified=true`; it is not an automatic fallback.

## Parameters and calls

Defaults preserve the proven legacy choices: expected doublet rate 0.06, simulated-to-observed
ratio 2.0, 30 principal components, and random seed 0. The expected rate is a prior, not an
observed-rate target. `n_prin_comps` is conservatively bounded by the smallest library and feature
count and the actual value is recorded.

By default Scrublet selects its own threshold and writes `predicted_doublet`. Downstream reporting
and filtering use this Boolean call exactly. A user-supplied threshold remains explicit provenance;
the model must not invent a score cutoff in prose or custom code.

## Evidence review

Review together:

- per-library sizes, predicted counts/rates, score median, 95th percentile, maximum, and threshold;
- observed and simulated score distributions;
- doublet-score and predicted-call localization on the existing UMAP;
- cluster and sample enrichment in current cluster QC;
- library size, detected genes, mitochondrial fraction, and marker identity for flagged regions.

Zero calls, rates much higher than the prior, missing simulated scores, or threshold failures are
warnings or blockers. Do not tune parameters simply to force the expected percentage.

## State transitions

Evidence adds observation columns while preserving counts, cells, representation, and clustering.
The annotated H5AD is recorded as the current evidence artifact, and cluster QC/finalization become
stale so the new doublet signal is reviewed. This state update follows evidence generation.

`retain_for_cluster_review`, `keep_all`, and `request_guidance` only record a decision. They do not
change the active dataset or cell set.

`remove_predicted` is a separate, explicitly confirmed mutation. It subsets exactly
`predicted_doublet == false`, restores raw counts to `X`, emits a new full-file fingerprint,
cell-set identity, count-representation identity, and dataset-revision identity, and clears current
QC, representation, clustering, batch, annotation, and finalization facts. The original annotated
artifact remains immutable evidence.
