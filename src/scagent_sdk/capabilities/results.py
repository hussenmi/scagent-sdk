"""Deterministic capability execution inputs and result envelopes."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scagent_sdk.capabilities.documentation import ArtifactDocumentation
from scagent_sdk.contracts._json import ensure_jsonable, require_mapping
from scagent_sdk.errors import CapabilityExecutionError

RESULT_SCHEMA_VERSION = 1
INLINE_RESULT_LIMIT_BYTES = 48 * 1024
# A capability must be able to show the model every figure it just produced. A per-cluster
# covariance pass legitimately renders one heatmap per cluster, and a count cap of 8 meant the
# model was *required* to review figures it was never shown — it had to reopen them one at a
# time after a floor blocked it, and wrote its review from recall rather than from pixels. The
# byte budgets below are the limit that should bind: a full 28-cluster heatmap set measures
# ~2.8 MiB, comfortably inside them. This ceiling only stops a runaway skill.
MODEL_MEDIA_LIMIT = 64
# Per-image and per-result byte budgets for pixels sent to the model. These are limits on the
# *transport*, not on the science: an oversized figure is downscaled to fit and, only if that still
# fails, left unattached with the model told which file to open. Exceeding them must never fail an
# execution. Doing so cost a real session its cluster QC -- a 2.1 MiB UMAP made evaluate_cluster_qc
# raise after every axis had been computed and written, so review_cluster_qc could never be reached,
# and finalization stayed floor-blocked on evidence the run had already produced twice.
MODEL_MEDIA_LIMIT_BYTES = 2 * 1024 * 1024
MODEL_MEDIA_TOTAL_BYTES = 8 * 1024 * 1024
MODEL_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})
# Longest edge a downscale pass may keep. Figures are rendered at 150 dpi, so an oversized one is
# oversized in pixels; halving the longest edge quarters the byte count while leaving panel layout,
# cluster separation, and axis labels legible.
MODEL_MEDIA_DOWNSCALE_EDGES = (2000, 1400, 1000)


def capability_artifact_directory_name(tool_name: str, execution_id: str) -> str:
    """Stable, readable canonical directory name for one capability execution."""

    action = "".join(
        character if character.isalnum() else "-"
        for character in tool_name.casefold()
    )
    action = "-".join(part for part in action.split("-") if part)
    return f"{action or 'capability'}--{execution_id}"


@dataclass(frozen=True)
class CapabilityContext:
    scientific_session_id: str
    session_dir: Path
    staging_dir: Path
    skill_id: str
    tool_name: str
    execution_id: str
    state_revision: int
    state_facts: dict[str, Any]
    # Read-only view of the artifact lineage. Supplied so a tool can describe the available
    # versions or validate a switch target; only the executor ever mutates it.
    state_lineage: dict[str, Any] = field(default_factory=dict)
    # Read-only index of the session's user turns ({turn_id, sequence, user_prompt}), oldest
    # first. Supplied so a decision that is only legitimate once a human has been consulted can
    # verify the consultation against the event log instead of trusting the model's account of
    # it. The runtime states who spoke and when; which decisions require that is the skill's
    # business, so no consultation policy lives here.
    user_turns: tuple[dict[str, Any], ...] = ()

    @property
    def artifact_relative_dir(self) -> str:
        return (
            "artifacts/capabilities/"
            + capability_artifact_directory_name(self.tool_name, self.execution_id)
        )

    def artifact_relative_path(self, relative_path: str) -> str:
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("capability artifact path must stay inside its execution directory")
        return str(Path(self.artifact_relative_dir) / relative)

    def artifact_absolute_path(self, relative_path: str) -> Path:
        return self.session_dir / self.artifact_relative_path(relative_path)


@dataclass(frozen=True)
class ProducedArtifact:
    """A file a capability staged for commit.

    ``name`` is the stable identifier other code selects on ("embedding", "cluster-qc-metrics");
    it must not drift, because floors, reports, and figure requirements match it. ``title``
    describes what this particular file shows ("umap by leiden_int") and is what the human review
    surface names the projection after. Keeping them apart is what lets a name stay matchable
    while the browsable filename says which embedding, clustering, or sample it belongs to.
    """

    name: str
    relative_path: str
    media_type: str = "application/octet-stream"
    title: str = ""
    # Canonical artifacts include both scientific outputs and supporting implementation records.
    # The latter stay committed and inspectable for provenance, but should not be advertised in
    # the human-facing code/figures/reports/tables/data review surface.
    include_in_output_view: bool = True

    @classmethod
    def from_dict(cls, value: Any) -> ProducedArtifact:
        data = require_mapping(value, name="artifact")
        try:
            include_in_output_view = data.get("include_in_output_view", True)
            if not isinstance(include_in_output_view, bool):
                raise CapabilityExecutionError(
                    "artifact include_in_output_view must be true or false"
                )
            artifact = cls(
                name=str(data["name"]),
                relative_path=str(data["relative_path"]),
                media_type=str(data.get("media_type", "application/octet-stream")),
                title=str(data.get("title", "")),
                include_in_output_view=include_in_output_view,
            )
        except KeyError as exc:
            raise CapabilityExecutionError(
                f"artifact is missing required field: {exc.args[0]}"
            ) from exc
        if not artifact.name.strip() or not artifact.relative_path.strip():
            raise CapabilityExecutionError("artifact name and relative_path must not be empty")
        if Path(artifact.relative_path).is_absolute():
            raise CapabilityExecutionError("artifact relative_path must be relative")
        return artifact


@dataclass(frozen=True)
class ModelMedia:
    """A staged artifact that should also be attached to the model tool result."""

    name: str
    relative_path: str
    media_type: str

    @classmethod
    def from_dict(cls, value: Any) -> ModelMedia:
        data = require_mapping(value, name="model_media")
        try:
            media = cls(
                name=str(data["name"]),
                relative_path=str(data["relative_path"]),
                media_type=str(data["media_type"]),
            )
        except KeyError as exc:
            raise CapabilityExecutionError(
                f"model_media is missing required field: {exc.args[0]}"
            ) from exc
        if not media.name.strip() or not media.relative_path.strip():
            raise CapabilityExecutionError("model_media name and relative_path must not be empty")
        if Path(media.relative_path).is_absolute():
            raise CapabilityExecutionError("model_media relative_path must be relative")
        if media.media_type not in MODEL_IMAGE_TYPES:
            raise CapabilityExecutionError(
                f"unsupported model image media type: {media.media_type}"
            )
        return media


@dataclass(frozen=True)
class CapabilityResult:
    summary: str
    details: Any = None
    facts_patch: dict[str, Any] = field(default_factory=dict)
    decisions_patch: dict[str, Any] = field(default_factory=dict)
    artifacts: tuple[ProducedArtifact, ...] = ()
    model_media: tuple[ModelMedia, ...] = ()
    documentation: ArtifactDocumentation | None = None
    schema_version: int = RESULT_SCHEMA_VERSION

    @classmethod
    def from_value(cls, value: Any) -> CapabilityResult:
        data = require_mapping(value, name="capability result")
        try:
            result = cls(
                schema_version=int(data.get("schema_version", RESULT_SCHEMA_VERSION)),
                summary=str(data["summary"]),
                details=data.get("details"),
                facts_patch=require_mapping(data.get("facts_patch", {}), name="facts_patch"),
                decisions_patch=require_mapping(
                    data.get("decisions_patch", {}), name="decisions_patch"
                ),
                artifacts=tuple(
                    ProducedArtifact.from_dict(item) for item in data.get("artifacts", [])
                ),
                model_media=tuple(
                    ModelMedia.from_dict(item) for item in data.get("model_media", [])
                ),
                documentation=(
                    ArtifactDocumentation.from_dict(data["documentation"])
                    if data.get("documentation") is not None
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CapabilityExecutionError(f"invalid capability result: {exc}") from exc
        if result.schema_version != RESULT_SCHEMA_VERSION:
            raise CapabilityExecutionError(
                f"unsupported capability result schema: {result.schema_version}"
            )
        if not result.summary.strip():
            raise CapabilityExecutionError("capability result summary must not be empty")
        ensure_jsonable(result.details, name="details")
        ensure_jsonable(result.facts_patch, name="facts_patch")
        ensure_jsonable(result.decisions_patch, name="decisions_patch")
        if len(result.model_media) > MODEL_MEDIA_LIMIT:
            raise CapabilityExecutionError(
                f"model_media may contain at most {MODEL_MEDIA_LIMIT} images"
            )
        return result
