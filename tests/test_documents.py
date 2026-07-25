import pytest

from meeting_scribe.storage.documents import UnsupportedDocumentError, extract_text


def test_extract_text_from_txt(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("hello world", encoding="utf-8")
    assert extract_text(path) == "hello world"


def test_extract_text_from_docx(tmp_path):
    docx = pytest.importorskip("docx")
    path = tmp_path / "doc.docx"
    document = docx.Document()
    document.add_paragraph("Meeting invite")
    document.add_paragraph("Friday at 3pm")
    document.save(path)

    text = extract_text(path)
    assert "Meeting invite" in text
    assert "Friday at 3pm" in text


def test_extract_text_from_pdf(tmp_path):
    pypdf = pytest.importorskip("pypdf")
    from pypdf import PdfWriter

    path = tmp_path / "doc.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    with open(path, "wb") as f:
        writer.write(f)

    # A blank page extracts to empty text, but this at least proves the PDF path doesn't blow up.
    assert extract_text(path) == ""


def test_unsupported_suffix_raises(tmp_path):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"not really a video")
    with pytest.raises(UnsupportedDocumentError):
        extract_text(path)
