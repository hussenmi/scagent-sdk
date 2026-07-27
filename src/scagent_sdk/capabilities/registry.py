"""Discovery and safe entrypoint loading for project skill packages."""

from __future__ import annotations

import hashlib
import importlib.util
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml

from scagent_sdk.capabilities.manifest import CapabilityManifest, CapabilityTool
from scagent_sdk.errors import CapabilityManifestError

CapabilityHandler = Callable[..., Any]


def _skill_frontmatter_name(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise CapabilityManifestError(f"SKILL.md has no YAML frontmatter: {path}")
    try:
        _, frontmatter, _body = text.split("---", 2)
        data = yaml.safe_load(frontmatter)
    except (ValueError, yaml.YAMLError) as exc:
        raise CapabilityManifestError(f"invalid SKILL.md frontmatter in {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("name"), str):
        raise CapabilityManifestError(f"SKILL.md frontmatter needs a name: {path}")
    return str(data["name"])


def _directory_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        digest.update(str(relative).encode("utf-8") + b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


@dataclass(frozen=True)
class DiscoveredSkill:
    name: str
    root: Path
    fingerprint: str
    executable: bool


@dataclass(frozen=True)
class SkillPackage:
    root: Path
    instructions_path: Path
    manifest_path: Path
    manifest: CapabilityManifest

    @property
    def fingerprint(self) -> str:
        return _directory_fingerprint(self.root)

    def load_handler(self, tool: CapabilityTool) -> CapabilityHandler:
        return self._load_entrypoint(*tool.entrypoint_parts, entrypoint=tool.entrypoint)

    def load_readiness_probe(self) -> CapabilityHandler | None:
        readiness = self.manifest.readiness
        if readiness is None:
            return None
        return self._load_entrypoint(*readiness.entrypoint_parts, entrypoint=readiness.entrypoint)

    def _load_entrypoint(
        self, relative_path: Path, function_name: str, *, entrypoint: str
    ) -> CapabilityHandler:
        source = (self.root / relative_path).resolve()
        try:
            source.relative_to(self.root.resolve())
        except ValueError as exc:
            raise CapabilityManifestError(
                f"entrypoint escapes skill package: {entrypoint}"
            ) from exc
        if not source.is_file():
            raise CapabilityManifestError(f"entrypoint file does not exist: {source}")
        digest = hashlib.sha256(str(source).encode()).hexdigest()[:16]
        module_name = f"scagent_skill_{self.manifest.skill_id.replace('-', '_')}_{digest}"
        spec = importlib.util.spec_from_file_location(module_name, source)
        if spec is None or spec.loader is None:
            raise CapabilityManifestError(f"cannot load entrypoint module: {source}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        handler = getattr(module, function_name, None)
        if not callable(handler):
            raise CapabilityManifestError(
                f"entrypoint function is missing or not callable: {entrypoint}"
            )
        return cast(CapabilityHandler, handler)


class CapabilityRegistry:
    def __init__(self, skills_root: str | Path):
        self.skills_root = Path(skills_root).expanduser().resolve()

    def discover(self) -> tuple[SkillPackage, ...]:
        if not self.skills_root.is_dir():
            return ()
        packages: list[SkillPackage] = []
        seen: set[str] = set()
        for root in sorted(path for path in self.skills_root.iterdir() if path.is_dir()):
            instructions = root / "SKILL.md"
            manifest_path = root / "capability.yaml"
            if not manifest_path.is_file():
                continue
            if not instructions.is_file():
                raise CapabilityManifestError(f"executable skill is missing SKILL.md: {root}")
            manifest = CapabilityManifest.from_path(manifest_path)
            frontmatter_name = _skill_frontmatter_name(instructions)
            if frontmatter_name != manifest.skill_id or root.name != manifest.skill_id:
                raise CapabilityManifestError(
                    "skill directory, SKILL.md name, and capability skill.id must match: "
                    f"{root.name}, {frontmatter_name}, {manifest.skill_id}"
                )
            if manifest.skill_id in seen:
                raise CapabilityManifestError(f"duplicate skill id: {manifest.skill_id}")
            seen.add(manifest.skill_id)
            package = SkillPackage(root.resolve(), instructions, manifest_path, manifest)
            for tool in manifest.tools:
                package.load_handler(tool)
            package.load_readiness_probe()
            packages.append(package)
        return tuple(packages)

    def skills(self) -> tuple[DiscoveredSkill, ...]:
        if not self.skills_root.is_dir():
            return ()
        skills: list[DiscoveredSkill] = []
        for root in sorted(path for path in self.skills_root.iterdir() if path.is_dir()):
            instructions = root / "SKILL.md"
            if not instructions.is_file():
                continue
            name = _skill_frontmatter_name(instructions)
            if name != root.name:
                raise CapabilityManifestError(
                    f"skill directory and SKILL.md name must match: {root.name}, {name}"
                )
            skills.append(
                DiscoveredSkill(
                    name=name,
                    root=root.resolve(),
                    fingerprint=_directory_fingerprint(root),
                    executable=(root / "capability.yaml").is_file(),
                )
            )
        return tuple(skills)
