from __future__ import annotations

import re
from pathlib import Path

from scagent_sdk.capabilities.instructions import (
    instruction_sources,
    render_skill_instructions,
    strip_frontmatter,
)
from scagent_sdk.capabilities.registry import CapabilityRegistry, DiscoveredSkill

SKILLS_ROOT = Path(__file__).parents[2] / ".claude" / "skills"


def _skill(root: Path, name: str, body: str) -> DiscoveredSkill:
    package = root / name
    package.mkdir(parents=True, exist_ok=True)
    (package / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: One line.\n---\n\n{body}\n", encoding="utf-8"
    )
    return DiscoveredSkill(name=name, root=package, fingerprint="sha256:x", executable=True)


def test_frontmatter_is_stripped_but_body_is_preserved() -> None:
    text = "---\nname: demo\ndescription: d\n---\n\n# Demo\n\nGuidance here.\n"

    assert strip_frontmatter(text) == "# Demo\n\nGuidance here."
    assert strip_frontmatter("no frontmatter here") == "no frontmatter here"


def test_rendered_instructions_carry_body_and_absolute_directory(tmp_path: Path) -> None:
    skills = (
        _skill(tmp_path, "b-skill", "Second guidance."),
        _skill(tmp_path, "a-skill", "First guidance."),
    )

    block = render_skill_instructions(skills)

    assert block.index("### a-skill") < block.index("### b-skill")  # stable order
    assert "First guidance." in block and "Second guidance." in block
    assert f"Directory: `{tmp_path / 'a-skill'}`" in block
    assert "description: One line." not in block
    assert "you do not need to load it" in block


def test_budget_overflow_names_the_omitted_skills_instead_of_trimming_silently(
    tmp_path: Path,
) -> None:
    skills = (
        _skill(tmp_path, "small-skill", "short"),
        _skill(tmp_path, "huge-skill", "x" * 4000),
    )

    block = render_skill_instructions(skills, budget_bytes=1500)

    assert "### small-skill" in block
    assert "### huge-skill" not in block
    assert "load them with the `Skill` tool before use: huge-skill" in block


def test_unreadable_instructions_are_reported_not_skipped_quietly(tmp_path: Path) -> None:
    present = _skill(tmp_path, "present-skill", "guidance")
    missing = DiscoveredSkill(
        name="ghost-skill", root=tmp_path / "ghost-skill", fingerprint="sha256:y", executable=False
    )

    block = render_skill_instructions((present, missing))

    assert "### present-skill" in block
    assert "ghost-skill" in block.split("### present-skill")[-1]
    assert instruction_sources((present, missing)) == [
        {
            "skill": "present-skill",
            "path": str(present.root / "SKILL.md"),
            "fingerprint": "sha256:x",
        }
    ]


def test_every_project_skill_reaches_the_model_within_budget() -> None:
    skills = CapabilityRegistry(SKILLS_ROOT).skills()

    block = render_skill_instructions(skills)
    flat = re.sub(r"\s+", " ", block)

    assert len(skills) >= 21
    for skill in skills:
        assert f"### {skill.name}" in block
    assert "exceeded the inline budget" not in block
    # Substantive guidance, not just headings: a caveat the tool schema does not carry.
    assert "not a universal default for every tissue" in flat
