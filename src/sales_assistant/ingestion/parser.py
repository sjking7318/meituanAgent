from __future__ import annotations

from dataclasses import dataclass

PARSER_VERSION = "text-v1"

_SUPPORTED = {"text/plain", "text/markdown", "md", "txt", "markdown"}


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    text: str
    language: str | None


def _detect_language(text: str) -> str:
    han = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    return "zh" if han >= max(1, len(text) // 20) else "en"


class TextParser:
    """MVP parser for plain text / Markdown (rag-design.md FR-004).

    PDF/DOCX/PPTX/HTML and OCR are follow-up work; unsupported types raise so
    the caller can mark the version failed rather than silently mis-ingesting.
    """

    def supports(self, content_type: str) -> bool:
        return content_type.lower() in _SUPPORTED

    def parse(self, raw: str, *, content_type: str = "text/markdown") -> ParsedDocument:
        if not self.supports(content_type):
            raise ValueError(f"unsupported content type: {content_type}")
        # Normalise line endings and strip trailing whitespace per line.
        normalized = "\n".join(line.rstrip() for line in raw.replace("\r\n", "\n").split("\n"))
        return ParsedDocument(text=normalized.strip(), language=_detect_language(normalized))
