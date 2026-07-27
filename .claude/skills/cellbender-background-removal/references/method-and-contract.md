# CellBender method and state contract

## Appropriate input

`remove-background` requires an unfiltered droplet matrix containing empty droplets. This skill's
first version deliberately supports Cell Ranger-style 10x H5 only so input structure, sparse
encoding, feature types, barcodes, and UMI ranks can be checked deterministically.

Suitability requires:

- a valid `/matrix` group with `data`, `indices`, `indptr`, `shape`, `barcodes`, and `features`;
- nonnegative integer counts and at least one Gene Expression feature;
- enough barcodes for droplet inference;
- a material low-count tail (at least 100 barcodes and at least 1% of all barcodes with at most 10
  UMIs); and
- no CellBender latent/metadata groups or filtered-file naming evidence.

The low-count criterion is intentionally conservative. A rare raw library with unusually high
ambient counts may be refused even though an expert CellBender run could be possible. Do not bypass
the refusal through renamed files or custom code; inspect the library and revise the validated
contract deliberately if such a case must be supported.

Validation is standalone: it accepts the candidate file directly, computes a full byte-level
fingerprint, and records that dataset identity with the suitability result. The expensive removal
step remains gated on the matching fingerprint and suitability attestation so a changed or
different file cannot reuse an earlier approval.

`required_low_count_barcodes` is the minimum number of barcodes at or below 10 UMIs needed to pass
this input-suitability gate. It is **not** a knee estimate, candidate-cell count, or
`expected_cells` recommendation. The validator deliberately does not infer the number of cells.

## Parameter interpretation

The locked CellBender 0.3.2 CLI sets a fixed seed of 1234 internally. Its relevant defaults are a
full ambient-plus-swapping model, 150 epochs, low-count threshold 5, MCKP estimator, and FPR 0.01.
The first run normally leaves expected cells and total droplets unset so CellBender estimates its
priors from the UMI curve.

- `expected_cells`: a rough estimate within about a factor of two; set only with source or UMI-curve
  evidence.
- `total_droplets_included`: must contain plausible cells while excluding surely empty tail
  droplets. It must be larger than expected cells when both are supplied.
- `fpr`: the target probability of erroneously removing a true count. Increasing it removes more
  background and risks more biological signal.
- `exclude_feature_types`: excluded feature types are copied through unchanged. The skill
  automatically adds every detected type other than `Gene Expression`.
- `selected_output=filtered`: use CellBender's inferred-cell barcode set for downstream analysis.
- `selected_output=full`: use the corrected analyzed-droplet matrix and defer barcode filtering.

## Output review

Read the following together:

- input UMI-rank plot and validation JSON;
- CellBender PDF/HTML when produced;
- metrics CSV, especially expected/found cells, counts removed, target FPR, and convergence fields;
- the CellBender log for failed training, retry, NaN, convergence, prior, or report-generation
  messages;
- raw-versus-corrected barcode-rank comparison;
- filtered/full output shapes and exact hashes.

Missing optional HTML is a warning if the PDF, metrics, logs, and H5 outputs are intact. A zero exit
code and readable output prove execution and container integrity, not biological improvement. Do
not claim successful decontamination without reviewing the evidence, and do not interpret a large
removed fraction as automatically better.

## Failure and checkpoint behavior

The inner timeout is lower than the environment broker timeout. A caught timeout or nonzero exit
returns a committed failed-run envelope with logs, command/config, lineage, and any `ckpt.tar.gz`.
The active dataset and all existing downstream evidence remain unchanged. Retrying with
`checkpoint_path` copies the checkpoint to the new execution staging directory before launch.

## Identity and invalidation

Input validation is keyed to the current dataset fingerprint. Changing the dataset fingerprint
makes the suitability floor fail even when the path string is unchanged.

On success, the selected output receives:

- a full `scagent-dataset-v1` content fingerprint;
- a dataset-revision identity derived from parent and child fingerprints plus the selected output;
- a barcode-set identity derived from the exact ordered barcodes;
- a count-representation identity derived from lineage, CellBender parameters, and output bytes.

The state patch deletes current cell QC, batch evidence, embedding representation, clustering,
cluster QC, annotation evidence, and finalization. Historical artifacts are not deleted; they remain
auditable but stale for the new count representation.
