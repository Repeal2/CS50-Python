"""Text extraction for context documents uploaded to a project.

These are the "here's a screenshot of the invite" / "here's the spec doc" files a user attaches to a
project (or a specific meeting) so the AI has more context when answering questions later. Everything is
reduced to plain text and handed to `Database.add_document`, which full-text-indexes it.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

SUPPORTED_SUFFIXES = {".txt", ".md", ".pdf", ".docx", ".png", ".jpg", ".jpeg", ".bmp", ".tiff"}


class UnsupportedDocumentError(ValueError):
    pass


def save_original_copy(path: Path, dest_dir: Path) -> Path:
    """Copies the uploaded file's original bytes into `dest_dir` under a synthetic, collision-free name
    (the real filename and extension are preserved separately in the documents table's `filename`
    column) so the original file is still around later — e.g. to hand off under its real name in a
    Copilot push package — even after the source path the user picked it from stops being reachable."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / f"{uuid.uuid4().hex}{path.suffix.lower()}"
    shutil.copyfile(path, dest_path)
    return dest_path


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
