import pytest

from meeting_scribe.storage.documents import UnsupportedDocumentError, extract_text, save_original_copy


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


def test_save_original_copy_preserves_bytes_and_extension(tmp_path):
    source = tmp_path / "invite.pdf"
    source.write_bytes(b"%PDF-1.4 fake contents")
    dest_dir = tmp_path / "vault"

    saved = save_original_copy(source, dest_dir)

    assert saved.parent == dest_dir
    assert saved.suffix == ".pdf"
    assert saved.read_bytes() == b"%PDF-1.4 fake contents"


def test_save_original_copy_creates_the_dest_dir_and_avoids_name_collisions(tmp_path):
    source = tmp_path / "notes.txt"
    source.write_text("first", encoding="utf-8")
    dest_dir = tmp_path / "vault" / "nested"

    first = save_original_copy(source, dest_dir)
    second = save_original_copy(source, dest_dir)

    assert dest_dir.is_dir()
    assert first != second
    assert first.read_text(encoding="utf-8") == "first"
    assert second.read_text(encoding="utf-8") == "first"
