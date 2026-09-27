from meeting_scribe.screen.capture import ScreenTextEvent
from meeting_scribe.transcription.engine import TranscriptLine
from meeting_scribe.transcription.speaker_names import caption_names, name_speakers


def _caption(name, first_seen, settled, text="..."):
    return ScreenTextEvent(timestamp_seconds=settled, text=text, speaker=name, started_seconds=first_seen)


# Priya talks from about 10 s to 30 s, Tom from about 40 s to 55 s; captions lag the audio slightly.
CAPTIONS = [
    _caption("Priya Shah", 11.0, 20.0),
    _caption("Priya Shah", 20.5, 31.0),
    _caption("Tom Jones", 41.0, 56.0),
    ScreenTextEvent(timestamp_seconds=60.0, text="Slide 4"),  # no badge: not a caption
]


def test_cloud_labels_take_the_name_the_captions_give_while_they_speak():
    system = [
        TranscriptLine(10.0, "system", "Let's look at the budget.", speaker="SPEAKER_00"),
        TranscriptLine(22.0, "system", "It's up on last quarter.", speaker="SPEAKER_00"),
        TranscriptLine(40.0, "system", "Sounds good to me.", speaker="SPEAKER_01"),
        TranscriptLine(70.0, "system", "One more thing.", speaker="SPEAKER_02"),  # nobody captioned then
    ]

    named, used = name_speakers([], system, CAPTIONS)

    assert [line.speaker for line in named] == ["Priya Shah", "Priya Shah", "Tom Jones", "SPEAKER_02"]
    assert used == ["Priya Shah", "Tom Jones"]


def test_each_line_of_this_pcs_transcript_is_named_on_its_own():
    system = [
        TranscriptLine(12.0, "system", "Let's look at the budget."),
        TranscriptLine(42.0, "system", "Sounds good to me."),
        TranscriptLine(65.0, "system", "Anyone else?"),  # after the last caption
    ]

    named, _used = name_speakers([], system, CAPTIONS)

    assert [line.speaker for line in named] == ["Priya Shah", "Tom Jones", None]  # None renders as "Others"


def test_a_line_the_captions_split_between_two_people_keeps_its_label():
    # Captioned time within the line's span: 6 s each.
    captions = [_caption("Priya Shah", 12.0, 16.0), _caption("Tom Jones", 18.0, 22.0)]

    named, used = name_speakers([], [TranscriptLine(10.0, "system", "Overlapping talk.")], captions)

    assert named[0].speaker is None and used == []


def test_the_person_recording_is_never_given_to_the_other_side():
    # The recorder's own captions line up with the microphone track, not system audio.
    captions = [_caption("Me Myself", 1.0, 9.0), _caption("Priya Shah", 11.0, 20.0)]
    mic = [TranscriptLine(0.5, "mic", "Hi everyone, thanks for joining.")]
    system = [
        TranscriptLine(5.0, "system", "Mm-hm.", speaker="SPEAKER_00"),  # someone murmuring while I talk
        TranscriptLine(10.0, "system", "Hi.", speaker="SPEAKER_01"),
    ]

    named, used = name_speakers(mic, system, captions)

    assert "Me Myself" not in used
    assert [line.speaker for line in named] == ["SPEAKER_00", "Priya Shah"]


def test_a_name_goes_to_one_cloud_label_at_most():
    system = [
        TranscriptLine(10.0, "system", "First half.", speaker="SPEAKER_00"),
        TranscriptLine(20.0, "system", "Second half.", speaker="SPEAKER_03"),
    ]

    named, _used = name_speakers([], system, [_caption("Priya Shah", 11.0, 31.0)])

    assert sorted(line.speaker for line in named).count("Priya Shah") == 1


def test_nothing_changes_without_captions():
    system = [TranscriptLine(1.0, "system", "Hello.", speaker="SPEAKER_00")]

    assert name_speakers([], system, [ScreenTextEvent(5.0, "Slide 1")]) == (system, [])


def test_caption_names_lists_everyone_once_in_order():
    assert caption_names(CAPTIONS) == ["Priya Shah", "Tom Jones"]
