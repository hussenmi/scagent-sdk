from __future__ import annotations

import json
from pathlib import Path

import pytest

from scagent_sdk.capabilities.skill_plugin import PLUGIN_NAME, materialize_skill_plugin


def _skills_root(tmp_path: Path, name: str = "skills") -> Path:
    root = tmp_path / name
    (root / "demo-skill").mkdir(parents=True)
    (root / "demo-skill" / "SKILL.md").write_text(
        "---\nname: demo-skill\n---\n\nGuidance.\n", encoding="utf-8"
    )
    return root


def test_plugin_root_publishes_the_skills_directory_to_the_cli(tmp_path: Path) -> None:
    skills = _skills_root(tmp_path)

    root = materialize_skill_plugin(skills, tmp_path / "plugin")

    manifest = json.loads((root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == PLUGIN_NAME
    assert (root / "skills").is_symlink()
    assert (root / "skills").resolve() == skills.resolve()
    assert (root / "skills" / "demo-skill" / "SKILL.md").is_file()


def test_materialization_is_idempotent_and_repoints_a_changed_skills_root(tmp_path: Path) -> None:
    first = _skills_root(tmp_path, "skills-a")
    second = _skills_root(tmp_path, "skills-b")
    destination = tmp_path / "plugin"

    materialize_skill_plugin(first, destination)
    materialize_skill_plugin(first, destination)
    root = materialize_skill_plugin(second, destination)

    assert (root / "skills").resolve() == second.resolve()


def test_missing_skills_root_and_occupied_destination_fail_loudly(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        materialize_skill_plugin(tmp_path / "absent", tmp_path / "plugin")

    skills = _skills_root(tmp_path)
    destination = tmp_path / "occupied"
    (destination / "skills").mkdir(parents=True)
    with pytest.raises(FileExistsError):
        materialize_skill_plugin(skills, destination)
