# scVI assumptions

scVI models raw counts with learned latent variables. Preserve integer-like counts, encode real
batch units, and avoid correcting away the biological condition. Training produces `X_scVI` and
model/training artifacts. Representation adoption is a separate scientific decision.

Run comparative diagnostics before promoting `X_scVI` as the analysis representation. Mixing alone
is not success: assess biological conservation, design confounding, training history, and whether
known states remain separable.
