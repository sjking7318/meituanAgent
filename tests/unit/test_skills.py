from __future__ import annotations

from pathlib import Path

import pytest

from sales_assistant.domain import SkillError
from sales_assistant.infrastructure.skills import FileSystemSkillLibrary, build_skill_library


def _make_skill(root: Path, name: str, description: str, body: str) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return d


def test_catalog_only_exposes_name_and_description(tmp_path: Path) -> None:
    _make_skill(tmp_path, "alpha", "第一个技能", "# 正文\n很长很长的正文内容" * 50)
    _make_skill(tmp_path, "beta", "第二个技能", "另一段正文")
    library = FileSystemSkillLibrary(tmp_path)

    catalog = library.catalog()
    names = {m.name for m in catalog}
    assert names == {"alpha", "beta"}
    # Level-1 must not carry the body — only name + description.
    for m in catalog:
        assert not hasattr(m, "instructions")
        assert m.description


def test_load_returns_body_and_resources(tmp_path: Path) -> None:
    d = _make_skill(tmp_path, "alpha", "第一个技能", "# 操作说明\n第一步做这个。")
    (d / "template.md").write_text("模板内容", encoding="utf-8")
    library = FileSystemSkillLibrary(tmp_path)

    loaded = library.load("alpha")
    assert loaded.name == "alpha"
    assert "操作说明" in loaded.instructions
    assert loaded.resources == ("template.md",)


def test_read_resource_returns_content(tmp_path: Path) -> None:
    d = _make_skill(tmp_path, "alpha", "技能", "正文")
    (d / "template.md").write_text("模板内容X", encoding="utf-8")
    library = FileSystemSkillLibrary(tmp_path)
    assert library.read_resource("alpha", "template.md") == "模板内容X"


def test_read_resource_blocks_path_traversal(tmp_path: Path) -> None:
    _make_skill(tmp_path, "alpha", "技能", "正文")
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    library = FileSystemSkillLibrary(tmp_path)
    with pytest.raises(SkillError):
        library.read_resource("alpha", "../secret.txt")


def test_load_unknown_skill_raises(tmp_path: Path) -> None:
    library = FileSystemSkillLibrary(tmp_path)
    with pytest.raises(SkillError):
        library.load("nope")


def test_invalid_skill_name_rejected(tmp_path: Path) -> None:
    library = FileSystemSkillLibrary(tmp_path)
    with pytest.raises(SkillError):
        library.load("../etc")


def test_malformed_skill_skipped_in_catalog(tmp_path: Path) -> None:
    good = tmp_path / "good"
    good.mkdir()
    (good / "SKILL.md").write_text(
        "---\nname: good\ndescription: 可用\n---\n正文", encoding="utf-8"
    )
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "SKILL.md").write_text("no frontmatter here", encoding="utf-8")
    library = FileSystemSkillLibrary(tmp_path)
    assert {m.name for m in library.catalog()} == {"good"}


def test_repo_skills_are_discoverable() -> None:
    # The bundled skills/ content directory should expose the two examples.
    library = build_skill_library()
    names = {m.name for m in library.catalog()}
    assert {"visit-planning", "visit-analysis"} <= names
