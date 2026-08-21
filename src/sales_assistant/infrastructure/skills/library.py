from __future__ import annotations

import re
from pathlib import Path

import structlog

from sales_assistant.domain import LoadedSkill, SkillError, SkillManifest

logger = structlog.get_logger()

_SKILL_FILE = "SKILL.md"
_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
# Minimal frontmatter fields we care about (avoid a YAML dependency).
_KEY = re.compile(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    match = _FRONTMATTER.match(text)
    if match is None:
        raise SkillError("SKILL.md missing YAML frontmatter")
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        kv = _KEY.match(line)
        if kv:
            value = kv.group(2).strip().strip("'\"")
            meta[kv.group(1).strip().lower()] = value
    return meta, match.group(2).strip()


class FileSystemSkillLibrary:
    """Progressive-disclosure skill store backed by the filesystem.

    Layout mirrors Claude Code: ``<root>/<skill-name>/SKILL.md`` where the file
    has YAML frontmatter (``name``/``description``) plus a Markdown body. Extra
    files in the skill folder are level-3 resources, read only on demand.

    Levels:
      1. :meth:`catalog` — name + description for every skill (cheap, always on).
      2. :meth:`load` — one skill's body (instructions), on demand.
      3. :meth:`read_resource` — a single referenced file, only when needed.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def catalog(self) -> list[SkillManifest]:
        manifests: list[SkillManifest] = []
        if not self._root.is_dir():
            return manifests
        for skill_dir in sorted(self._root.iterdir()):
            skill_file = skill_dir / _SKILL_FILE
            if not skill_file.is_file():
                continue
            try:
                meta, _ = _parse_frontmatter(skill_file.read_text(encoding="utf-8"))
                name = meta.get("name", skill_dir.name)
                description = meta.get("description", "")
                if not description:
                    raise SkillError("missing description")
                manifests.append(SkillManifest(name=name, description=description))
            except Exception:
                # A malformed skill must not break the catalog for the others.
                logger.warning("skill_manifest_skipped", skill=skill_dir.name)
        return manifests

    def load(self, name: str) -> LoadedSkill:
        skill_file = self._skill_dir(name) / _SKILL_FILE
        if not skill_file.is_file():
            raise SkillError(f"skill not found: {name}")
        meta, body = _parse_frontmatter(skill_file.read_text(encoding="utf-8"))
        resources = tuple(
            sorted(
                p.name
                for p in skill_file.parent.iterdir()
                if p.is_file() and p.name != _SKILL_FILE
            )
        )
        return LoadedSkill(
            name=meta.get("name", name),
            description=meta.get("description", ""),
            instructions=body,
            resources=resources,
        )

    def read_resource(self, name: str, resource: str) -> str:
        skill_dir = self._skill_dir(name)
        target = (skill_dir / resource).resolve()
        # Path-traversal guard: resource must stay inside the skill folder.
        if not str(target).startswith(str(skill_dir.resolve()) + "/"):
            raise SkillError(f"illegal resource path: {resource}")
        if not target.is_file():
            raise SkillError(f"resource not found: {resource}")
        return target.read_text(encoding="utf-8")

    def _skill_dir(self, name: str) -> Path:
        if not _NAME_RE.match(name):
            raise SkillError(f"invalid skill name: {name}")
        return self._root / name


def build_skill_library(root: Path | None = None) -> FileSystemSkillLibrary:
    """Default library rooted at the ``skills/`` content directory.

    Resolution order (first existing wins), so it works both for a local editable
    checkout and a container where the app runs from the project root:
      1. explicit ``root`` argument;
      2. ``<cwd>/skills`` (Docker WORKDIR=/app, skills copied alongside);
      3. repo root inferred from this file's location.
    """
    if root is not None:
        return FileSystemSkillLibrary(root)
    cwd_skills = Path.cwd() / "skills"
    if cwd_skills.is_dir():
        return FileSystemSkillLibrary(cwd_skills)
    # src/sales_assistant/infrastructure/skills/library.py -> repo root / skills
    return FileSystemSkillLibrary(Path(__file__).resolve().parents[4] / "skills")
