"""Standalone scVI latent-model training across an isolated environment boundary."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

PROGRESS_FILENAME = ".capability-progress.jsonl"

# scVI's own epoch heuristic: epochs scale *inversely* with cell count, because more cells means
# more gradient steps per epoch, so a large dataset needs far fewer passes. 400 is the ceiling for
# small datasets, not a default: ~20k cells trains to 400, ~200k cells to ~40. Reproduced here as a
# fallback so the cap is still cell-aware if a future scvi-tools release moves or renames its
# helper, and so the formula itself stays unit-testable in the control plane.
EPOCH_CEILING = 400
EPOCH_REFERENCE_CELLS = 20_000
# A validation ELBO that rises this much above its best value by the final epoch is overfitting.
OVERFITTING_RELATIVE_RISE = 0.02


def _identity(kind: str, value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"{kind}:sha256:{hashlib.sha256(encoded).hexdigest()}"


def heuristic_epoch_cap(n_cells: int) -> int:
    """Cell-count epoch cap: ``min(400, round(20000 / n_cells * 400))``, at least one epoch."""

    if n_cells <= 0:
        return EPOCH_CEILING
    scaled = round((EPOCH_REFERENCE_CELLS / n_cells) * EPOCH_CEILING)
    return max(1, min(EPOCH_CEILING, int(scaled)))


def resolve_epoch_cap(
    n_cells: int,
    requested: Any,
    heuristic: Callable[[int], Any] | None = None,
) -> tuple[int, str]:
    """Decide the epoch upper bound and record where it came from.

    An explicit ``requested`` value always wins. Otherwise scVI's own
    ``get_max_epochs_heuristic`` sets the cap from the cell count, with the local formula as a
    fallback. The cap is only an upper bound: early stopping governs how long training runs.
    """

    if requested is not None:
        return max(1, int(requested)), "explicit"
    if heuristic is not None:
        try:
            return max(1, int(heuristic(n_cells))), "scvi_cell_count_heuristic"
        except (TypeError, ValueError, AttributeError):
            pass
    return heuristic_epoch_cap(n_cells), "cell_count_formula"


def convergence_verdict(
    train_curve: list[float],
    validation_curve: list[float],
    *,
    max_epochs: int,
    early_stopping: bool,
) -> dict[str, Any]:
    """Summarize how training ended from the per-epoch ELBO curves.

    Returns the epochs actually run against the cap, the final and best losses, and two honest
    flags rather than a verdict on scientific quality: ``overfitting_warning`` when the validation
    ELBO rose meaningfully after its best epoch, and ``hit_cap`` when training used the whole cap
    (with early stopping enabled that means it had not yet plateaued, so a higher cap may help).
    ``tail_relative_improvement`` is descriptive context — how much the training ELBO still moved
    over the last quarter of training — not a pass/fail threshold. Judge convergence from the
    saved curve, not from one number.
    """

    epochs_trained = len(train_curve) or (len(validation_curve) or None)
    verdict: dict[str, Any] = {
        "max_epochs": int(max_epochs),
        "early_stopping_enabled": bool(early_stopping),
        "epochs_trained": epochs_trained,
        "early_stopped": bool(epochs_trained is not None and epochs_trained < max_epochs),
        "hit_cap": bool(epochs_trained is not None and epochs_trained >= max_epochs),
        "final_elbo_train": float(train_curve[-1]) if train_curve else None,
        "final_elbo_validation": float(validation_curve[-1]) if validation_curve else None,
        "best_val_epoch": None,
        "best_elbo_validation": None,
        "tail_relative_improvement": None,
        "overfitting_warning": None,
    }
    if len(train_curve) >= 4:
        tail_start = max(1, (len(train_curve) * 3) // 4) - 1
        start_value = float(train_curve[tail_start])
        end_value = float(train_curve[-1])
        if start_value != 0:
            verdict["tail_relative_improvement"] = (start_value - end_value) / abs(start_value)
    if validation_curve:
        best_index = min(range(len(validation_curve)), key=lambda i: validation_curve[i])
        best_value = float(validation_curve[best_index])
        last_value = float(validation_curve[-1])
        verdict["best_val_epoch"] = int(best_index)
        verdict["best_elbo_validation"] = best_value
        rose = (
            best_index < len(validation_curve) - 1
            and best_value != 0
            and (last_value - best_value) / abs(best_value) > OVERFITTING_RELATIVE_RISE
        )
        if rose:
            verdict["overfitting_warning"] = (
                f"validation ELBO was best at epoch {best_index} but rose "
                f"{(last_value - best_value) / abs(best_value) * 100:.1f}% by the final epoch "
                f"(epoch {len(validation_curve) - 1}) — possible overfitting; prefer fewer epochs"
            )
    return verdict


def epoch_narrative(n_cells: int, verdict: dict[str, Any], cap_source: str) -> str:
    """One sentence a reader can check: why this many epochs, and how training ended."""

    cap = verdict["max_epochs"]
    if cap_source == "explicit":
        reason = f"an explicitly requested cap of {cap}"
    else:
        reason = (
            f"scVI's cell-count heuristic, which set the cap to {cap} epochs for {n_cells:,} "
            f"cells (epochs scale inversely with cell count; {EPOCH_CEILING} is the "
            "small-dataset ceiling, not a default)"
        )
    trained = verdict["epochs_trained"]
    if verdict["early_stopped"]:
        ending = f"early stopping halted training at {trained} epoch(s)"
    elif verdict["early_stopping_enabled"]:
        ending = (
            f"training ran the full {cap}-epoch cap without early stopping firing, so the "
            "validation ELBO had not plateaued by scVI's criterion — consider a higher cap"
        )
    else:
        ending = f"early stopping was disabled, so training ran the full {cap}-epoch cap"
    return f"Trained under {reason}; {ending}."


def _write_progress(
    staging_dir: Path,
    message: str,
    *,
    current: int | None = None,
    total: int | None = None,
) -> None:
    """Append one small progress event for the parent runtime."""

    path = staging_dir / PROGRESS_FILENAME
    payload: dict[str, Any] = {"message": message}
    if current is not None:
        payload["current"] = current
    if total is not None:
        payload["total"] = total
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def _training_callback(staging_dir: Path, max_epochs: int) -> Any:
    try:
        from lightning.pytorch.callbacks import Callback
    except ImportError:
        from pytorch_lightning.callbacks import Callback

    class VisibleTrainingProgress(Callback):
        def on_train_start(self, trainer: Any, _module: Any) -> None:
            _write_progress(staging_dir, f"epoch 0/{max_epochs}", current=0, total=max_epochs)

        def on_train_epoch_end(self, trainer: Any, _module: Any) -> None:
            current = min(int(trainer.current_epoch) + 1, max_epochs)
            metric_text = ""
            metrics = getattr(trainer, "callback_metrics", {})
            for name in (
                "elbo_train",
                "train_loss",
                "validation_loss",
                "reconstruction_loss_train",
            ):
                value = metrics.get(name) if hasattr(metrics, "get") else None
                if value is None:
                    continue
                try:
                    metric_text = f" · {name} {float(value):,.3f}"
                except (TypeError, ValueError):
                    pass
                break
            _write_progress(
                staging_dir,
                f"epoch {current}/{max_epochs}{metric_text}",
                current=current,
                total=max_epochs,
            )

        def on_train_end(self, trainer: Any, _module: Any) -> None:
            completed = min(int(trainer.current_epoch) + 1, max_epochs)
            _write_progress(
                staging_dir,
                f"training complete after {completed} epoch(s)",
                current=completed,
                total=max_epochs,
            )

    return VisibleTrainingProgress()


def _history_curves(history: Any) -> tuple[list[float], list[float], Any]:
    """Return ``(train_elbo, validation_elbo, merged_frame)`` from ``model.history``."""

    frames = dict(history or {})

    def _series(*keys: str) -> list[float]:
        for key in keys:
            frame = frames.get(key)
            if frame is None or len(frame) == 0:
                continue
            return [float(value) for value in frame[frame.columns[0]].tolist()]
        return []

    train_curve = _series("elbo_train", "train_loss_epoch")
    validation_curve = _series("elbo_validation", "validation_loss")
    merged = None
    for key, frame in frames.items():
        column = frame.rename(columns={frame.columns[0]: key})
        merged = column if merged is None else merged.join(column, how="outer")
    return train_curve, validation_curve, merged


def _plot_convergence(
    train_curve: list[float],
    validation_curve: list[float],
    verdict: dict[str, Any],
    path: Path,
) -> None:
    """Render the train/validation ELBO curves so convergence is visible, not inferred."""

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure, axis = plt.subplots(figsize=(7, 5))
    if train_curve:
        axis.plot(range(len(train_curve)), train_curve, label="train ELBO", color="#1f77b4")
    if validation_curve:
        axis.plot(
            range(len(validation_curve)),
            validation_curve,
            label="validation ELBO",
            color="#d62728",
        )
        best = verdict.get("best_val_epoch")
        if best is not None:
            axis.axvline(
                best,
                color="gray",
                linestyle="--",
                linewidth=1,
                label=f"best validation epoch {best}",
            )
    axis.set_xlabel("Epoch")
    axis.set_ylabel("ELBO loss (lower is better)")
    if verdict["early_stopped"]:
        title = f"scVI convergence — early-stopped at {verdict['epochs_trained']} epochs"
    else:
        title = f"scVI convergence — ran the full {verdict['max_epochs']}-epoch cap"
    axis.set_title(title)
    axis.legend(fontsize=9)
    figure.tight_layout()
    figure.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(figure)


def _documentation(
    *,
    batch_key: str,
    n_cells: int,
    n_latent: int,
    n_layers: int,
    cap_source: str,
    verdict: dict[str, Any],
    narrative: str,
    has_plot: bool,
) -> dict[str, Any]:
    findings = [narrative]
    if verdict["final_elbo_validation"] is not None:
        findings.append(
            "Final validation ELBO (the held-out reconstruction + regularization loss, lower is "
            f"better): {verdict['final_elbo_validation']:,.1f}; best was "
            f"{verdict['best_elbo_validation']:,.1f} at epoch {verdict['best_val_epoch']}."
        )
    if verdict["overfitting_warning"]:
        findings.append(verdict["overfitting_warning"])
    files = [
        {
            "filename": "scvi-latent.h5ad",
            "purpose": (
                f"The input cells with a {n_latent}-dimensional scVI representation added as "
                "obsm['X_scVI']. No neighbor graph, UMAP, or clustering was created."
            ),
            "how_to_read": (
                "X_scVI replaces PCA as the coordinate space for later neighbor/UMAP/clustering "
                "steps only if the batch evidence justifies integration."
            ),
        },
        {
            "filename": "scvi-model.zip",
            "purpose": "The trained scVI model, so the same latent space can be reloaded exactly.",
        },
        {
            "filename": "scvi-training-history.csv",
            "purpose": "Every loss scVI logged, one row per epoch.",
            "how_to_read": (
                "elbo_train and elbo_validation are the training and held-out losses; a flat tail "
                "means training converged, and a rising elbo_validation means overfitting."
            ),
        },
    ]
    if has_plot:
        files.append(
            {
                "filename": "scvi-training-loss.png",
                "purpose": "The train and validation ELBO curves with the best validation epoch.",
                "how_to_read": (
                    "Both curves should fall and then flatten. A validation curve that turns "
                    "upward is overfitting; a curve still falling steeply at the last epoch means "
                    "training stopped before convergence."
                ),
            }
        )
    return {
        "name": "scvi-training",
        "title": "scVI latent representation training",
        "overview": (
            "scVI is a generative model of raw counts that learns a low-dimensional latent "
            "representation while conditioning on a stated experimental batch covariate. This "
            "step trained the model and saved that representation; adopting it as the analysis "
            "representation is a separate scientific decision."
        ),
        "what_was_done": [
            f"Trained scVI on raw counts for {n_cells:,} cells with batch covariate "
            f"'{batch_key}' ({n_layers} hidden layer(s), {n_latent} latent dimensions).",
            narrative,
            "Saved the per-epoch loss history and convergence curve so training can be judged "
            "from the measurements rather than from the progress display.",
        ],
        "findings": findings,
        "parameters": {
            "batch_key": batch_key,
            "n_latent": n_latent,
            "n_layers": n_layers,
            "max_epochs": verdict["max_epochs"],
            "epoch_cap_source": cap_source,
            "early_stopping": verdict["early_stopping_enabled"],
            "epochs_trained": verdict["epochs_trained"],
        },
        "caveats": [
            "A converged model is not a correct one: low loss says nothing about whether the "
            "batch covariate was a real technical unit or whether biology was corrected away.",
            "Mixing improvement alone does not justify adopting X_scVI. Compare like-for-like "
            "latent spaces and check that known states remain separable.",
        ],
    }


def run(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    _write_progress(context.staging_dir, "loading counts")
    import scanpy as sc
    import scvi

    path = Path(str(arguments["path"])).expanduser().resolve()
    batch_key = str(arguments["batch_key"])
    n_latent = int(arguments.get("n_latent", 30))
    n_layers = int(arguments.get("n_layers", 2))
    requested_epochs = arguments.get("max_epochs")
    early_stopping = bool(arguments.get("early_stopping", True))
    seed = int(arguments.get("random_seed", 0))
    adata = sc.read_h5ad(path)
    if batch_key not in adata.obs:
        raise ValueError(f"batch key {batch_key!r} is absent")
    if "counts" not in adata.layers:
        raise ValueError("scVI requires raw counts in layers['counts']")
    old = adata.uns.get("scagent_sdk", {})
    cell_set_id = old.get("cell_set_id") or _identity(
        "cells", sorted(map(str, adata.obs_names))
    )

    try:
        from scvi.model._utils import get_max_epochs_heuristic
    except ImportError:
        get_max_epochs_heuristic = None  # type: ignore[assignment]
    max_epochs, cap_source = resolve_epoch_cap(
        int(adata.n_obs), requested_epochs, get_max_epochs_heuristic
    )

    scvi.settings.seed = seed
    scvi.model.SCVI.setup_anndata(adata, layer="counts", batch_key=batch_key)
    model = scvi.model.SCVI(adata, n_latent=n_latent, n_layers=n_layers)
    callback = _training_callback(context.staging_dir, max_epochs)
    _write_progress(
        context.staging_dir,
        f"starting epoch 0/{max_epochs} ({cap_source.replace('_', ' ')} cap)",
        current=0,
        total=max_epochs,
    )
    model.train(
        max_epochs=max_epochs,
        accelerator="auto",
        # One device deliberately: scvi-tools cannot early-stop under multi-GPU DDP, so a
        # multi-device run would silently train the full cap instead of stopping at convergence.
        devices=1,
        early_stopping=early_stopping,
        enable_progress_bar=False,
        callbacks=[callback],
    )

    _write_progress(context.staging_dir, "reading the training history")
    train_curve, validation_curve, merged_history = _history_curves(model.history)
    verdict = convergence_verdict(
        train_curve,
        validation_curve,
        max_epochs=max_epochs,
        early_stopping=early_stopping,
    )
    narrative = epoch_narrative(int(adata.n_obs), verdict, cap_source)

    _write_progress(context.staging_dir, "computing the latent representation")
    adata.obsm["X_scVI"] = model.get_latent_representation()
    representation_id = _identity(
        "representation",
        {
            "cell_set_id": cell_set_id,
            "method": "scvi",
            "batch_key": batch_key,
            "n_latent": n_latent,
            "n_layers": n_layers,
            "max_epochs": max_epochs,
            "early_stopping": early_stopping,
            # Two runs that stopped at different epochs are different representations, so the
            # epochs actually trained belong in the identity, not just the requested cap.
            "epochs_trained": verdict["epochs_trained"],
            "seed": seed,
        },
    )
    training_facts = {
        "max_epochs": max_epochs,
        "epoch_cap_source": cap_source,
        "early_stopping": early_stopping,
        "epochs_trained": verdict["epochs_trained"],
        "early_stopped": verdict["early_stopped"],
    }
    new_metadata = {
        **old,
        "cell_set_id": cell_set_id,
        "representation_id": representation_id,
        "representation_key": "X_scVI",
        "scvi": {
            "method": "scvi",
            "batch_key": batch_key,
            "n_latent": n_latent,
            "n_layers": n_layers,
            **training_facts,
        },
    }
    new_metadata.pop("clustering_id", None)
    adata.uns["scagent_sdk"] = new_metadata
    _write_progress(context.staging_dir, "saving the integrated representation")
    adata.write_h5ad(context.staging_dir / "scvi-latent.h5ad", compression="gzip")
    _write_progress(context.staging_dir, "saving the trained model")
    model_dir = context.staging_dir / "scvi-model"
    model.save(model_dir, overwrite=True)
    shutil.make_archive(str(context.staging_dir / "scvi-model"), "zip", model_dir)
    shutil.rmtree(model_dir)

    if merged_history is not None:
        merged_history.to_csv(
            context.staging_dir / "scvi-training-history.csv", index_label="epoch"
        )
    else:
        (context.staging_dir / "scvi-training-history.csv").write_text("\n", encoding="utf-8")
    artifacts: list[dict[str, Any]] = [
        {
            "name": "scvi-latent-anndata",
            "relative_path": "scvi-latent.h5ad",
            "media_type": "application/x-hdf5",
        },
        {
            "name": "scvi-model",
            "relative_path": "scvi-model.zip",
            "media_type": "application/zip",
        },
        {
            "name": "scvi-training-history",
            "relative_path": "scvi-training-history.csv",
            "media_type": "text/csv",
        },
    ]
    model_media: list[dict[str, Any]] = []
    if train_curve or validation_curve:
        _plot_convergence(
            train_curve,
            validation_curve,
            verdict,
            context.staging_dir / "scvi-training-loss.png",
        )
        figure = {
            "name": "scvi-training-loss",
            "relative_path": "scvi-training-loss.png",
            "media_type": "image/png",
            "title": "scVI train and validation ELBO by epoch",
        }
        artifacts.append(figure)
        model_media.append(
            {key: figure[key] for key in ("name", "relative_path", "media_type")}
        )

    details = {
        "batch_key": batch_key,
        "n_cells": int(adata.n_obs),
        "n_latent": n_latent,
        "n_layers": n_layers,
        "representation_id": representation_id,
        "representation_key": "X_scVI",
        "training": {**training_facts, **verdict, "epoch_narrative": narrative},
    }
    warning = verdict["overfitting_warning"]
    return {
        "summary": (
            f"Trained scVI and saved a {n_latent}-dimensional X_scVI representation for "
            f"{adata.n_obs:,} cells; no graph, UMAP, or clusters were created. {narrative}"
            + (f" {warning}." if warning else "")
            + (
                " Review the attached convergence curve before reporting the representation."
                if model_media
                else ""
            )
        ),
        "details": details,
        "facts_patch": {
            "analysis": {
                "representation": {
                    "id": representation_id,
                    "method": "scvi",
                    "batch_key": batch_key,
                    "key": "X_scVI",
                    "training": training_facts,
                },
                "clustering": None,
            },
            "cluster_qc": None,
            "batch": None,
            "annotation": None,
            "finalization": None,
        },
        "artifacts": artifacts,
        "model_media": model_media,
        "documentation": _documentation(
            batch_key=batch_key,
            n_cells=int(adata.n_obs),
            n_latent=n_latent,
            n_layers=n_layers,
            cap_source=cap_source,
            verdict=verdict,
            narrative=narrative,
            has_plot=bool(model_media),
        ),
    }
