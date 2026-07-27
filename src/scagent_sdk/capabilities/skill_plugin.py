"""Expose project skills to the model without importing the project's settings.

Skill instructions (`SKILL.md`) are the scientific half of a capability: when to use it, how to
read its output, what its limits are. They only reach the model if the runtime offers the `Skill`
tool *and* the CLI can discover the skills.

The obvious route — `setting_sources: ["project"]` — also loads this repository's `CLAUDE.md` and
`AGENTS.md`, which are instructions for coding agents ("run pytest", "do not commit"), into a
scientific session's context. A local plugin root loads skills alone, so discovery stays
deny-by-default and the scientific agent never inherits development instructions.
"""

from __future__ import annotations

import json
from pathlib import Path

PLUGIN_NAME = "scagent-science"
PLUGIN_VERSION = "0.1.0"


def materialize_skill_plugin(skills_root: str | Path, destination: str | Path) -> Path:
    """Create or refresh a local plugin root that publishes `skills_root` to the CLI.

    Idempotent: an existing root is repointed rather than rebuilt, so repeated assembly and a
    changed `--skills-root` both converge on the current skills directory.
    """

    skills = Path(skills_root).expanduser().resolve()
    if not skills.is_dir():
        raise FileNotFoundError(f"skills root does not exist: {skills}")
    root = Path(destination).expanduser().resolve()
    manifest_dir = root / ".claude-plugin"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": PLUGIN_NAME,
                "version": PLUGIN_VERSION,
                "description": "scagent-sdk scientific skill instructions",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    link = root / "skills"
    if link.is_symlink():
        if link.readlink() == skills:
            return root
        link.unlink()
    elif link.exists():
        raise FileExistsError(f"skill plugin path is occupied by a real directory: {link}")
    link.symlink_to(skills, target_is_directory=True)
    return root
