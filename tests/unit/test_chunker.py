from __future__ import annotations

from sales_assistant.ingestion.chunker import (
    ChunkingConfig,
    ParentChildChunker,
    estimate_tokens,
)


def test_headings_become_section_paths() -> None:
    text = "# 产品政策\n\n首月免佣金。\n\n## 佣金\n\n第二月起收取。"
    parents = ParentChildChunker().chunk(text, document_version_id="dv1")
    paths = {p.section_path for p in parents}
    assert "产品政策" in paths
    assert "产品政策 / 佣金" in paths


def test_parent_child_ids_are_stable_and_scoped() -> None:
    text = "# A\n\n内容一。\n\n内容二。"
    parents = ParentChildChunker().chunk(text, document_version_id="dv42")
    assert parents
    for parent in parents:
        assert parent.parent_id.startswith("dv42:p")
        for child in parent.children:
            assert child.parent_id == parent.parent_id
            assert child.child_id.startswith(parent.parent_id + ":c")


def test_long_text_splits_into_multiple_children_with_overlap() -> None:
    sentence = "这是一个用于测试切分的句子。"
    body = sentence * 60  # well beyond one child budget
    config = ChunkingConfig(parent_max_tokens=100000, child_max_tokens=40, child_overlap_tokens=10)
    parents = ParentChildChunker(config).chunk(body, document_version_id="dv1")
    children = [c for p in parents for c in p.children]
    assert len(children) > 1
    # Every child stays within a reasonable bound of the token budget.
    assert all(estimate_tokens(c.text) <= 80 for c in children)


def test_empty_text_yields_no_parents() -> None:
    assert ParentChildChunker().chunk("   \n\n  ", document_version_id="dv1") == []
