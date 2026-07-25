"""Text extraction for context documents uploaded to a project.

These are the "here's a screenshot of the invite" / "here's the spec doc" files a user attaches to a
project (or a specific meeting) so the AI has more context when answering questions later. Everything is
reduced to plain text and handed to `Database.add_document`, which full-text-indexes it.
"""

from __future__ import annotations

from pathlib import Path

SUPPORTED_SUFFIXES = {".txt", ".md", ".pdf", ".docx", ".png", ".jpg", ".jpeg", ".bmp", ".tiff"}


class UnsupportedDocumentError(ValueError):
    pass


def extract_text(path: Path, *, tesseract_cmd: str | None = None) -> str:
    """Best-effort plain-text extraction. Raises UnsupportedDocumentError for unknown file types."""
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md"}:
        return path.read_text(encoding="utf-8", errors="replace")
    if suffix == ".pdf":
        return _extract_pdf(path)
    if suffix == ".docx":
        return _extract_docx(path)
    if suffix in {".png", ".jpg", ".jpeg", ".bmp", ".tiff"}:
        return _extract_image(path, tesseract_cmd=tesseract_cmd)
    raise UnsupportedDocumentError(
        f"Don't know how to extract text from {path.name!r} ({suffix}). "
        f"Supported: {sorted(SUPPORTED_SUFFIXES)}"
    )


def _extract_pdf(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n\n".join(pages).strip()


def _extract_docx(path: Path) -> str:
    import docx

    document = docx.Document(str(path))
    return "\n".join(p.text for p in document.paragraphs).strip()


def _extract_image(path: Path, *, tesseract_cmd: str | None) -> str:
    import pytesseract
    from PIL import Image

    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
    with Image.open(path) as image:
        return pytesseract.image_to_string(image).strip()
