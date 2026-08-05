# scVI assumptions

scVI models raw counts with learned latent variables. Preserve integer-like counts, encode real
batch units, and avoid correcting away the biological condition. Training produces `X_scVI` and
model/training artifacts. Representation adoption is a separate scientific decision.

Run comparative diagnostics before promoting `X_scVI` as the analysis representation. Mixing alone
is not success: assess biological conservation, design confounding, training history, and whether
known states remain separable.

## Training length

The epoch cap is not a tuning knob to raise for better results. It exists because scVI's objective
is optimized by stochastic gradient descent, so the informative quantity is the number of gradient
steps, not passes over the data: a dataset with ten times the cells takes ten times as many steps
per epoch and reaches the same optimization progress in roughly a tenth of the epochs. That is why
the cap scales inversely with cell count and why a small dataset needs the 400-epoch ceiling.

Early stopping monitors the held-out ELBO, so it detects the point past which further training
improves the fit to the training cells without improving the model. A run that hits the cap while
early stopping is enabled has not shown that point, so its representation may be undertrained; a run
whose validation ELBO rises after its best epoch has passed it. Both are visible in the saved
curve, and neither is decidable from the final loss value alone.

Seed and epoch count are part of the representation's identity: two runs that stopped at different
epochs produce different latent spaces, and downstream neighbor graphs, clusters, and evidence are
bound to whichever one was used.
