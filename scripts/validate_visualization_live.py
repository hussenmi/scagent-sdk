"""Live validation of visualize-single-cell through the real environment broker.

Builds one artifact carrying both reference annotations and the SCimilarity embedding, then
exercises every figure tool on it. This is live-compute evidence for the figure contract, not a
biological claim about the labels.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from scagent_sdk.capabilities.assembly import CapabilityAssembler
from scagent_sdk.capabilities.registry import CapabilityRegistry
from scagent_sdk.execution.broker import EnvironmentBroker
from scagent_sdk.execution.profile import EnvironmentRegistry
from scagent_sdk.session import AnalysisSession

ROOT = Path(__file__).resolve().parents[1]
SOURCE_SESSION = ROOT / "sessions" / "run_20260724T184855Z_e7113a" / "artifacts" / "capabilities"
SCIMILARITY = SOURCE_SESSION / "74c12fc5-66ce-497d-b673-05e8c19b2c6b" / "scimilarity-annotated.h5ad"
CELLTYPIST = SOURCE_SESSION / "e01af7e3-2d76-46a4-afb3-8361907c0959" / "celltypist-annotated.h5ad"
MARKERS = ["EPCAM", "PTPRC", "CD3D", "CD8A", "MS4A1", "LYZ", "COL1A1", "PECAM1"]


def _combined(destination: Path) -> Path:
    """Merge both annotation columns onto the SCimilarity artifact via the compute environment."""

    program = f"""
import anndata as ad
import scanpy as sc

sci = sc.read_h5ad({str(SCIMILARITY)!r})
ct = sc.read_h5ad({str(CELLTYPIST)!r})
shared = sci.obs_names.intersection(ct.obs_names)
sci = sci[shared].copy()
for column in ("celltypist_prediction", "celltypist_prediction_confidence"):
    sci.obs[column] = ct[shared].obs[column].to_numpy()
sci.write_h5ad({str(destination)!r}, compression="gzip")
print(sci.n_obs, sci.n_vars, list(sci.obsm))
"""
    registry = EnvironmentRegistry.from_path(ROOT / "configs" / "environments" / "iris.toml")
    profile = registry.resolve("gpu-singlecell")
    import subprocess

    completed = subprocess.run(
        [str(profile.python), "-c", program],
        capture_output=True,
        text=True,
        check=False,
        env=profile.build_environment(),
    )
    if completed.returncode != 0:
        raise SystemExit(f"failed to build the combined artifact:\n{completed.stderr}")
    print(f"combined artifact: {completed.stdout.strip()}")
    return destination


async def _run(session: AnalysisSession, assembler: CapabilityAssembler) -> int:
    packages = CapabilityRegistry(ROOT / ".claude" / "skills").discover()
    package = next(p for p in packages if p.manifest.skill_id == "visualize-single-cell")
    combined = _combined(session.directory / "combined.h5ad")
    calls = [
        ("plot_qc_distributions", {"path": str(combined), "group_key": "celltypist_prediction"}),
        (
            "plot_embedding",
            {
                "path": str(combined),
                "embedding_key": "X_scimilarity",
                "color_keys": [
                    "scimilarity_prediction",
                    "celltypist_prediction",
                    "EPCAM",
                    "PTPRC",
                ],
            },
        ),
        (
            "plot_group_composition",
            {
                "path": str(combined),
                "group_key": "celltypist_prediction",
                "class_key": "scimilarity_prediction",
            },
        ),
        (
            "plot_label_agreement",
            {
                "path": str(combined),
                "first_key": "celltypist_prediction",
                "second_key": "scimilarity_prediction",
            },
        ),
        (
            "plot_marker_expression",
            {
                "path": str(combined),
                "group_key": "celltypist_prediction",
                "genes": MARKERS,
            },
        ),
    ]
    failures = 0
    for name, arguments in calls:
        tool = next(item for item in package.manifest.tools if item.name == name)
        response = await assembler.executor.execute(package, tool, arguments)
        if response.get("is_error"):
            failures += 1
            print(f"FAIL {name}: {response.get('error_summary') or response.get('content')}")
            continue
        structured = response.get("structuredContent") or {}
        images = [item for item in response.get("content", []) if item.get("type") == "image"]
        sizes = [len(item["data"]) for item in images]
        print(f"PASS {name}: {structured.get('summary')}")
        print(f"     files={[f['name'] for f in structured.get('files', [])]} base64={sizes}")
    return failures


def main() -> int:
    session = AnalysisSession.create(ROOT / "sessions", title="visualization live validation")
    broker = EnvironmentBroker(
        EnvironmentRegistry.from_path(ROOT / "configs" / "environments" / "iris.toml")
    )
    assembler = CapabilityAssembler(
        CapabilityRegistry(ROOT / ".claude" / "skills"), session, environment_broker=broker
    )
    print(f"session: {session.session_id}")
    failures = asyncio.run(_run(session, assembler))
    print(json.dumps({"session": session.session_id, "failures": failures}))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
