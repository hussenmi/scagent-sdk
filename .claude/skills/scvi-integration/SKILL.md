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
- training history;
- an H5AD with `obsm["X_scVI"]`.

`X_scVI` can be supplied explicitly to later representation and clustering operations.

Evaluate batch evidence before adopting a corrected representation. Do not encode the biological
condition of interest as a nuisance batch merely to increase mixing.

Read [references/assumptions.md](references/assumptions.md).
