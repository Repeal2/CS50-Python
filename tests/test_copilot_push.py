import json

from meeting_scribe.ai.copilot_push import ReferenceDocument, TextReferenceDocument, push_meeting_package


def test_push_meeting_package_writes_transcripts_named_by_meeting_code(tmp_path):
    inbox_dir = tmp_path / "Inbox"

    push_meeting_package(
        meeting_code="20260728-1030",
        audio_transcript_text="[00:01] You: let's get started",
        screen_transcript_text="[00:02] Screen: Slide: Agenda",
        reference_documents=[],
        inbox_dir=inbox_dir,
    )

    audio = inbox_dir / "20260728-1030_transcript-audio.txt"
    screen = inbox_dir / "20260728-1030_transcript-screen.txt"
    assert audio.read_text(encoding="utf-8") == "[00:01] You: let's get started"
    assert screen.read_text(encoding="utf-8") == "[00:02] Screen: Slide: Agenda"


def test_push_meeting_package_handles_empty_transcripts(tmp_path):
    inbox_dir = tmp_path / "Inbox"

    push_meeting_package(
        meeting_code="20260728-1030",
        audio_transcript_text="",
        screen_transcript_text="",
        reference_documents=[],
        inbox_dir=inbox_dir,
    )

    assert "no audio transcript" in (inbox_dir / "20260728-1030_transcript-audio.txt").read_text()
    assert "no on-screen text" in (inbox_dir / "20260728-1030_transcript-screen.txt").read_text()


def test_push_meeting_package_copies_reference_documents_with_meeting_code_prefix(tmp_path):
    inbox_dir = tmp_path / "Inbox"
    doc_path = tmp_path / "Q3 Budget Proposal.pdf"
    doc_path.write_bytes(b"fake pdf bytes")

    push_meeting_package(
        meeting_code="20260728-1030",
        audio_transcript_text="hello",
        screen_transcript_text="",
        reference_documents=[
            ReferenceDocument(original_filename="Q3 Budget Proposal.pdf", source_path=doc_path)
        ],
        inbox_dir=inbox_dir,
    )

    saved = inbox_dir / "20260728-1030_Q3 Budget Proposal.pdf"
    assert saved.read_bytes() == b"fake pdf bytes"


def test_push_meeting_package_dedupes_reference_documents_with_the_same_original_filename(tmp_path):
    inbox_dir = tmp_path / "Inbox"
    first_path = tmp_path / "a" / "notes.docx"
    second_path = tmp_path / "b" / "notes.docx"
    first_path.parent.mkdir()
    second_path.parent.mkdir()
    first_path.write_bytes(b"first")
    second_path.write_bytes(b"second")

    manifest_path = push_meeting_package(
        meeting_code="20260728-1030",
        audio_transcript_text="",
        screen_transcript_text="",
        reference_documents=[
            ReferenceDocument(original_filename="notes.docx", source_path=first_path),
            ReferenceDocument(original_filename="notes.docx", source_path=second_path),
        ],
        inbox_dir=inbox_dir,
    )

    assert (inbox_dir / "20260728-1030_notes.docx").read_bytes() == b"first"
    assert (inbox_dir / "20260728-1030_notes-2.docx").read_bytes() == b"second"

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    saved_names = {doc["saved_filename"] for doc in manifest["files"]["reference_docs"]}
    assert saved_names == {"20260728-1030_notes.docx", "20260728-1030_notes-2.docx"}


def test_push_meeting_package_skips_reference_documents_whose_source_file_is_missing(tmp_path):
    inbox_dir = tmp_path / "Inbox"

    manifest_path = push_meeting_package(
        meeting_code="20260728-1030",
        audio_transcript_text="",
        screen_transcript_text="",
        reference_documents=[
            ReferenceDocument(original_filename="gone.txt", source_path=tmp_path / "gone.txt")
        ],
        inbox_dir=inbox_dir,
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["files"]["reference_docs"] == []
    assert manifest["reference_count"] == 0
    assert not (inbox_dir / "20260728-1030_gone.txt").exists()


def test_push_meeting_package_writes_a_well_formed_manifest_last(tmp_path):
    inbox_dir = tmp_path / "Inbox"
    doc_path = tmp_path / "invite.pdf"
    doc_path.write_bytes(b"invite bytes")

    manifest_path = push_meeting_package(
        meeting_code="20260728-1030",
        audio_transcript_text="hello",
        screen_transcript_text="world",
        reference_documents=[ReferenceDocument(original_filename="invite.pdf", source_path=doc_path)],
        inbox_dir=inbox_dir,
    )

    assert manifest_path.name == "20260728-1030_done.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["meetingID"] == "20260728-1030"
    assert manifest["files"]["transcript_audio"] == "20260728-1030_transcript-audio.txt"
    assert manifest["files"]["transcript_screen"] == "20260728-1030_transcript-screen.txt"
    assert manifest["files"]["reference_docs"] == [
        {"original_filename": "invite.pdf", "saved_filename": "20260728-1030_invite.pdf"}
    ]
    assert manifest["reference_count"] == 1
    assert manifest["timestamp_completed"].endswith("Z")


def test_push_meeting_package_writes_manual_notes_and_attendees_as_reference_docs(tmp_path):
    inbox_dir = tmp_path / "Inbox"

    manifest_path = push_meeting_package(
        meeting_code="20260728-1030",
        audio_transcript_text="hello",
        screen_transcript_text="",
        reference_documents=[],
        text_reference_documents=[
            TextReferenceDocument(original_filename="meeting-notes.txt", content="Follow up with legal."),
            TextReferenceDocument(original_filename="attendees.txt", content="John Smith\nJane Doe"),
        ],
        inbox_dir=inbox_dir,
    )

    notes_path = inbox_dir / "20260728-1030_meeting-notes.txt"
    attendees_path = inbox_dir / "20260728-1030_attendees.txt"
    assert notes_path.read_text(encoding="utf-8") == "Follow up with legal."
    assert attendees_path.read_text(encoding="utf-8") == "John Smith\nJane Doe"

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["reference_count"] == 2
    assert manifest["files"]["reference_docs"] == [
        {"original_filename": "meeting-notes.txt", "saved_filename": "20260728-1030_meeting-notes.txt"},
        {"original_filename": "attendees.txt", "saved_filename": "20260728-1030_attendees.txt"},
    ]


def test_push_meeting_package_dedupes_a_text_reference_doc_against_an_uploaded_one(tmp_path):
    inbox_dir = tmp_path / "Inbox"
    uploaded_path = tmp_path / "uploaded-notes.txt"
    uploaded_path.write_bytes(b"uploaded copy")

    manifest_path = push_meeting_package(
        meeting_code="20260728-1030",
        audio_transcript_text="",
        screen_transcript_text="",
        reference_documents=[
            ReferenceDocument(original_filename="meeting-notes.txt", source_path=uploaded_path)
        ],
        text_reference_documents=[
            TextReferenceDocument(original_filename="meeting-notes.txt", content="typed during the meeting")
        ],
        inbox_dir=inbox_dir,
    )

    # Text reference docs are written first, so the typed notes keep the plain name and the uploaded
    # file (which happens to share that original filename) gets the "-2" suffix.
    assert (inbox_dir / "20260728-1030_meeting-notes.txt").read_text(encoding="utf-8") == (
        "typed during the meeting"
    )
    assert (inbox_dir / "20260728-1030_meeting-notes-2.txt").read_bytes() == b"uploaded copy"

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["reference_count"] == 2


def test_push_meeting_package_creates_the_inbox_directory(tmp_path):
    inbox_dir = tmp_path / "nested" / "Inbox"

    push_meeting_package(
        meeting_code="20260728-1030",
        audio_transcript_text="hello",
        screen_transcript_text="",
        reference_documents=[],
        inbox_dir=inbox_dir,
    )

    assert inbox_dir.is_dir()


def test_push_meeting_package_reference_docs_is_empty_array_with_no_documents(tmp_path):
    inbox_dir = tmp_path / "Inbox"

    manifest_path = push_meeting_package(
        meeting_code="20260728-1030",
        audio_transcript_text="hello",
        screen_transcript_text="",
        reference_documents=[],
        inbox_dir=inbox_dir,
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["files"]["reference_docs"] == []
    assert manifest["reference_count"] == 0
