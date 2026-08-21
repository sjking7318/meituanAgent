from __future__ import annotations

import re
from dataclasses import dataclass, field

CHUNKER_VERSION = "pc-v1"

# Approximate CJK-friendly token estimate: ~1.6 chars/token for mixed zh/en.
_CHARS_PER_TOKEN = 1.6


def estimate_tokens(text: str) -> int:
    return max(1, round(len(text) / _CHARS_PER_TOKEN))


@dataclass(frozen=True, slots=True)
class ChildChunk:
    child_id: str
    parent_id: str
    text: str
    section_path: str
    page: int | None = None


@dataclass(frozen=True, slots=True)
class ParentChunk:
    parent_id: str
    text: str
    section_path: str
    children: list[ChildChunk] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ChunkingConfig:
    parent_max_tokens: int = 1500
    child_max_tokens: int = 320
    child_overlap_tokens: int = 64


_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


@dataclass(slots=True)
class _Section:
    path: str
    lines: list[str] = field(default_factory=list)


def _split_sections(text: str) -> list[_Section]:
    """Split Markdown/plaintext into sections by heading hierarchy.

    section_path reflects the heading breadcrumb (e.g. "产品政策 / 佣金").
    Text without headings becomes a single root section.
    """
    sections: list[_Section] = []
    stack: list[tuple[int, str]] = []  # (heading level, title)
    current = _Section(path="")

    for line in text.splitlines():
        match = _HEADING.match(line.strip())
        if match is None:
            current.lines.append(line)
            continue
        # Close current section before starting a new heading.
        if current.lines or current.path:
            sections.append(current)
        level = len(match.group(1))
        title = match.group(2).strip()
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        path = " / ".join(t for _, t in stack)
        current = _Section(path=path)

    if current.lines or current.path:
        sections.append(current)
    if not sections:
        sections.append(_Section(path=""))
    return sections


def _pack_paragraphs(text: str, max_tokens: int) -> list[str]:
    """Greedily pack paragraphs into blocks under a token budget."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    blocks: list[str] = []
    buf: list[str] = []
    buf_tokens = 0
    for para in paragraphs:
        para_tokens = estimate_tokens(para)
        if buf and buf_tokens + para_tokens > max_tokens:
            blocks.append("\n\n".join(buf))
            buf, buf_tokens = [], 0
        buf.append(para)
        buf_tokens += para_tokens
    if buf:
        blocks.append("\n\n".join(buf))
    return blocks


def _split_child(text: str, max_tokens: int, overlap_tokens: int) -> list[str]:
    """Sentence-aware child splitting with token overlap inside a parent."""
    if estimate_tokens(text) <= max_tokens:
        return [text]
    sentences = re.split(r"(?<=[。！？.!?\n])", text)
    sentences = [s for s in sentences if s.strip()]
    children: list[str] = []
    buf: list[str] = []
    buf_tokens = 0
    for sentence in sentences:
        stoks = estimate_tokens(sentence)
        if buf and buf_tokens + stoks > max_tokens:
            children.append("".join(buf).strip())
            # Carry tail sentences as overlap.
            overlap: list[str] = []
            otoks = 0
            for prev in reversed(buf):
                ptoks = estimate_tokens(prev)
                if otoks + ptoks > overlap_tokens:
                    break
                overlap.insert(0, prev)
                otoks += ptoks
            buf = list(overlap)
            buf_tokens = otoks
        buf.append(sentence)
        buf_tokens += stoks
    if buf:
        children.append("".join(buf).strip())
    return [c for c in children if c]


class ParentChildChunker:
    """Parent-Child chunker (rag-design.md 2). Parents give answer context,
    children are the high-precision recall units with in-section overlap."""

    def __init__(self, config: ChunkingConfig | None = None) -> None:
        self._config = config or ChunkingConfig()

    def chunk(self, text: str, *, document_version_id: str) -> list[ParentChunk]:
        cfg = self._config
        parents: list[ParentChunk] = []
        parent_index = 0
        for section in _split_sections(text):
            body = "\n".join(section.lines).strip()
            if not body:
                continue
            for block in _pack_paragraphs(body, cfg.parent_max_tokens):
                parent_id = f"{document_version_id}:p{parent_index}"
                children: list[ChildChunk] = []
                child_texts = _split_child(block, cfg.child_max_tokens, cfg.child_overlap_tokens)
                for child_index, child_text in enumerate(child_texts):
                    children.append(
                        ChildChunk(
                            child_id=f"{parent_id}:c{child_index}",
                            parent_id=parent_id,
                            text=child_text,
                            section_path=section.path,
                        )
                    )
                parents.append(
                    ParentChunk(
                        parent_id=parent_id,
                        text=block,
                        section_path=section.path,
                        children=children,
                    )
                )
                parent_index += 1
        return parents
