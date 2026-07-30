from __future__ import annotations

from pathlib import Path

import pytest

from scagent_sdk.capabilities.manifest import CapabilityManifest
from scagent_sdk.capabilities.registry import CapabilityRegistry
from scagent_sdk.errors import CapabilityManifestError


def test_registry_discovers_and_loads_executable_project_skill() -> None:
    root = Path(__file__).parents[2] / ".claude" / "skills"

    packages = CapabilityRegistry(root).discover()

    package_by_id = {package.manifest.skill_id: package for package in packages}
    assert set(package_by_id) == {
        "analysis-versions",
        "analysis-workspace",
        "batch-investigation",
        "cellbender-background-removal",
        "celltypist-annotation",
        "cluster-qc",
        "doublet-evidence",
        "dimensionality-reduction",
        "expression-preprocessing",
        "finalize-analysis",
        "inspect-dataset",
        "inspect-media",
        "marker-annotation",
        "research-literature",
        "research-web",
        "scimilarity-annotation",
        "single-cell-clustering",
        "single-cell-counts",
        "single-cell-qc",
        "visualize-single-cell",
        "scvi-integration",
    }
    package = package_by_id["inspect-dataset"]
    tool = package.manifest.tools[0]
    assert tool.name == "inspect_dataset"
    assert callable(package.load_handler(tool))
    skills = CapabilityRegistry(root).skills()
    assert len(skills) == 22
    assert {skill.name for skill in skills if not skill.executable} == {"orchestrate-single-cell"}


def test_manifest_rejects_escaping_entrypoint(tmp_path: Path) -> None:
    manifest_path = tmp_path / "capability.yaml"
    manifest_path.write_text(
        """\
schema_version: 1
skill:
  id: unsafe-skill
  version: "1"
  description: unsafe
tools:
  - name: escape
    description: escape
    entrypoint: ../outside.py:run
    input_schema: {type: object}
""",
        encoding="utf-8",
    )
    CapabilityManifest.from_path(manifest_path)
    skill = tmp_path / "unsafe-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: unsafe-skill\ndescription: unsafe\n---\n", encoding="utf-8"
    )
    (skill / "capability.yaml").write_text(manifest_path.read_text(), encoding="utf-8")

    with pytest.raises(CapabilityManifestError, match="escapes skill package"):
        CapabilityRegistry(tmp_path).discover()
