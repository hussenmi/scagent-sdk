---
name: scvi-integration
description: Train a scVI latent model from raw counts and an explicit batch covariate, saving the model, training history, and X_scVI representation. Use for representation learning or integration experiments; scientific adoption is a separate decision.
---

# scVI Latent Model

scVI is a variational generative model for single-cell count data. It models observed counts while
learning a lower-dimensional latent representation and can condition on an experimental batch
covariate.

`train_scvi_latent` requires raw counts in `layers["counts"]` and an observation column named
by `batch_key`. It validates those intrinsic inputs, trains the model, and saves:

- the model archive;
- the per-epoch loss history and a train/validation ELBO convergence figure;
- an H5AD with `obsm["X_scVI"]`.

`X_scVI` can be supplied explicitly to later representation and clustering operations.

## Epochs and convergence

Leave `max_epochs` unset. scVI's heuristic then sets the cap from the cell count, scaling epochs
*inversely* with it — `min(400, round(20000 / n_cells × 400))` — because more cells means more
gradient steps per epoch, so a large dataset needs fewer passes. 400 is the small-dataset ceiling,
not a default. Early stopping on the validation ELBO decides the actual run length; training uses
one device because scvi-tools cannot early-stop under multi-GPU DDP.

Review the attached convergence figure, then report the epoch count the way you would justify a PCA
elbow: state `n_cells`, the resolved cap, that scVI's heuristic chose it, and how training ended.
The result reports `epochs_trained`, `max_epochs`, `epoch_cap_source`, `early_stopped`, the final
and best ELBOs, and `overfitting_warning`.

- Early stopping fired: training reached the validation-ELBO plateau. Expected and good.
- Ran the full cap with early stopping enabled: the plateau was not reached; consider a higher cap.
- `overfitting_warning` set: the validation ELBO rose after its best epoch; prefer fewer epochs.

A converged model is not a validated one. Low loss says nothing about whether `batch_key` was a real
technical unit or whether biology was corrected away.

Evaluate batch evidence before adopting a corrected representation. Do not encode the biological
condition of interest as a nuisance batch merely to increase mixing.

Read [references/assumptions.md](references/assumptions.md).
