"""The model must be told what it can run instead of investigating the host for it.

A capability the base instructions never name, or a prerequisite the model can only find by
searching the filesystem, shows up in a session as slow flailing rather than as a clean answer.
Both are contract failures, not style preferences.
"""

from __future__ import annotations

from pathlib import Path

from scagent_sdk.capabilities.registry import CapabilityRegistry

ROOT = Path(__file__).parents[2]
SKILLS_ROOT = ROOT / ".claude" / "skills"
BASE_PROMPT = ROOT / "configs" / "models" / "prompts" / "base.md"
# Skills whose tools cannot run without a host asset this project never downloads implicitly.
ASSET_DEPENDENT_SKILLS = {"celltypist-annotation", "scimilarity-annotation"}


def test_base_instructions_name_every_executable_skill() -> None:
    prompt = BASE_PROMPT.read_text(encoding="utf-8")
    packages = CapabilityRegistry(SKILLS_ROOT).discover()

    unnamed = sorted(
        package.manifest.skill_id
        for package in packages
        if package.manifest.skill_id not in prompt
    )

    assert unnamed == []


def test_asset_dependent_skills_declare_a_readiness_probe() -> None:
    packages = CapabilityRegistry(SKILLS_ROOT).discover()

    declared = {
        package.manifest.skill_id
        for package in packages
        if package.manifest.readiness is not None
    }

    assert ASSET_DEPENDENT_SKILLS <= declared


def test_base_instructions_forbid_discovering_capabilities_by_searching_the_host() -> None:
    prompt = BASE_PROMPT.read_text(encoding="utf-8")

    assert "Never search the filesystem" in prompt
    assert "local-prerequisite inventory" in prompt
