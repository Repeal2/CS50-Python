from meeting_scribe.screen.capture import (
    ScreenWatcher,
    _looks_like_speaker_badge,
    _looks_like_ui_noise,
    _new_lines,
    _ocr_region_once,
)
from meeting_scribe.screen.region_picker import RegionTarget, WindowRegionTarget


class FakeShot:
    def __init__(self, payload: bytes):
        self.size = (2, 2)
        self.rgb = payload


class FakeSct:
    def __init__(self, frames: list[bytes]):
        self._frames = iter(frames)

    def grab(self, monitor):
        return FakeShot(next(self._frames))


class FakeImage:
    def __init__(self, data: bytes):
        self._data = data

    @staticmethod
    def frombytes(mode, size, data):
        return FakeImage(data)

    def tobytes(self):
        return self._data


class FakePytesseract:
    def __init__(self, texts: list[str]):
        self._texts = iter(texts)

    def image_to_string(self, image):
        return next(self._texts)


def _watcher(**kwargs):
    """A ScreenWatcher with the settle-check's real-time wait disabled, so tests run instantly. Each
    stable capture still consumes two frames from FakeSct (see _grab_stable_frame)."""
    kwargs.setdefault("interval_seconds", 0.01)
    kwargs.setdefault("settle_seconds", 0.0)
    watcher = ScreenWatcher(**kwargs)
    watcher._started_at = 0.0
    return watcher


def test_dedup_skips_unchanged_frames_and_repeated_text():
    events = []
    watcher = _watcher(on_text=events.append)

    # Every grab returns the same bytes: both settle-check pairs are stable, and the frame is unchanged
    # from one capture to the next, so only the first capture should ever reach OCR.
    sct = FakeSct([b"frame-a"] * 4)
    tess = FakePytesseract(["Slide 1"])

    # 1st call: new frame, new text -> line becomes pending (not yet emitted).
    watcher._capture_once(sct, region=None, Image=FakeImage, pytesseract=tess)
    # 2nd call: identical frame hash -> OCR skipped entirely, pending line carries over untouched.
    watcher._capture_once(sct, region=None, Image=FakeImage, pytesseract=tess)

    assert events == []
    assert watcher._pending_lines == ["Slide 1"]


def test_a_line_is_emitted_once_a_later_capture_confirms_it_stopped_growing():
    events = []
    watcher = _watcher(on_text=events.append)

    sct = FakeSct([b"frame-a", b"frame-a", b"frame-b", b"frame-b"])
    tess = FakePytesseract(["Slide 1", "Slide 2"])

    watcher._capture_once(sct, region=None, Image=FakeImage, pytesseract=tess)  # "Slide 1" -> pending
    watcher._capture_once(sct, region=None, Image=FakeImage, pytesseract=tess)  # doesn't grow -> settles

    assert [e.text for e in events] == ["Slide 1"]
    assert watcher._pending_lines == ["Slide 2"]


def test_a_growing_caption_line_is_emitted_once_in_its_final_form():
    """Reproduces the real-world bug: Teams-style live captions render word by word, so naive exact-match
    dedup treats every partial state as a brand new line. Growth-matching should collapse all of them into
    one event carrying only the final wording."""
    events = []
    watcher = _watcher(on_text=events.append)

    # Each capture's settle-check needs a stable (matching) pair, and the screen visibly changes every
    # cycle (the caption is still being typed out), so each pair differs from the one before it.
    frames = []
    for i in range(4):
        frames.extend([f"frame-{i}".encode(), f"frame-{i}".encode()])
    sct = FakeSct(frames)
    tess = FakePytesseract(
        [
            "We we we",
            "We we we discussed the trolley choice we're still discussing",
            "We we we discussed the trolley choice we're still discussing the trolley choice.",
            "Next thing entirely.",
        ]
    )

    for _ in range(4):
        watcher._capture_once(sct, region=None, Image=FakeImage, pytesseract=tess)

    assert [e.text for e in events] == [
        "We we we discussed the trolley choice we're still discussing the trolley choice."
    ]
    assert watcher._pending_lines == ["Next thing entirely."]


def test_an_ocr_misread_that_shrinks_a_pending_line_does_not_lose_the_fuller_text():
    """A pending line normally only ever grows, but a one-off OCR misread could make a later capture of
    the *same* still-growing caption look shorter. It should still match as growth (the two share a
    prefix) without downgrading the pending text back down to the shorter reading."""
    events = []
    watcher = _watcher(on_text=events.append)

    frames = []
    for i in range(3):
        frames.extend([f"frame-{i}".encode(), f"frame-{i}".encode()])
    sct = FakeSct(frames)
    tess = FakePytesseract(["Testing one two three", "Testing one", "Unrelated next line."])

    for _ in range(3):
        watcher._capture_once(sct, region=None, Image=FakeImage, pytesseract=tess)

    assert [e.text for e in events] == ["Testing one two three"]


def test_an_unstable_mid_scroll_frame_is_skipped_without_ocr():
    """The two settle-check grabs disagree (simulating a frame caught mid-animation) -> _capture_once
    returns without ever calling image_to_string."""
    events = []
    watcher = _watcher(on_text=events.append)

    sct = FakeSct([b"frame-a", b"frame-a-blurred"])

    class ExplodingPytesseract:
        def image_to_string(self, image):
            raise AssertionError("OCR should not run on an unstable frame")

    watcher._capture_once(sct, region=None, Image=FakeImage, pytesseract=ExplodingPytesseract())

    assert events == []
    assert watcher._last_frame_hash is None


def test_flush_pending_emits_whatever_never_got_confirmed_settled():
    events = []
    watcher = _watcher(on_text=events.append)
    watcher._pending_lines = ["Final caption on screen when the meeting ended."]

    watcher._flush_pending()

    assert [e.text for e in events] == ["Final caption on screen when the meeting ended."]
    assert watcher._pending_lines == []
    assert "Final caption on screen when the meeting ended." in watcher._finalized_lines


def test_flush_pending_does_nothing_when_nothing_is_pending():
    events = []
    watcher = _watcher(on_text=events.append)

    watcher._flush_pending()

    assert events == []


def test_resolve_region_returns_the_fixed_rectangle_for_a_region_target():
    target = RegionTarget(left=10, top=20, width=300, height=200)
    watcher = ScreenWatcher(on_text=lambda e: None, target=target)

    assert watcher._resolve_region(sct=None) == {"left": 10, "top": 20, "width": 300, "height": 200}


def test_resolve_region_tracks_a_window_pinned_custom_area_to_its_current_position(monkeypatch):
    """A WindowRegionTarget's absolute position should follow the pinned window wherever it currently
    is — including a move to another monitor — rather than staying at the screen coordinates it was
    drawn at."""
    import meeting_scribe.screen.window_picker as window_picker

    target = WindowRegionTarget(
        hwnd=42, window_title="Microsoft Teams", offset_left=10, offset_top=20, width=300, height=80
    )
    watcher = ScreenWatcher(on_text=lambda e: None, target=target)

    current_rect = {"left": 100, "top": 200, "width": 900, "height": 700}
    monkeypatch.setattr(window_picker, "get_window_region", lambda hwnd: current_rect)
    assert watcher._resolve_region(sct=None) == {"left": 110, "top": 220, "width": 300, "height": 80}

    # The window moved to a second monitor to the left of the primary one (negative coordinates) -> the
    # captured rectangle moves by the same amount, staying pinned to the same spot on the window.
    current_rect = {"left": -1920, "top": 50, "width": 900, "height": 700}
    assert watcher._resolve_region(sct=None) == {"left": -1910, "top": 70, "width": 300, "height": 80}


def test_resolve_region_returns_none_when_the_pinned_window_has_closed(monkeypatch):
    import meeting_scribe.screen.window_picker as window_picker

    target = WindowRegionTarget(
        hwnd=42, window_title="Microsoft Teams", offset_left=10, offset_top=20, width=300, height=80
    )
    watcher = ScreenWatcher(on_text=lambda e: None, target=target)
    monkeypatch.setattr(window_picker, "get_window_region", lambda hwnd: None)

    assert watcher._resolve_region(sct=None) is None


def test_resolve_region_returns_the_whole_virtual_screen_with_no_target():
    class FakeSctWithMonitors:
        monitors = ["whole-virtual-screen"]

    watcher = ScreenWatcher(on_text=lambda e: None, target=None)

    assert watcher._resolve_region(FakeSctWithMonitors()) == "whole-virtual-screen"


def test_ui_noise_rejects_short_avatar_initials_and_icon_rows():
    assert _looks_like_ui_noise("MB")
    assert _looks_like_ui_noise("lO")
    assert _looks_like_ui_noise("@ B x")


def test_ui_noise_accepts_short_but_real_chat_lines():
    assert not _looks_like_ui_noise("On T3.")
    assert not _looks_like_ui_noise("Testing.")


def test_speaker_badge_rejects_name_and_name_with_role_lines():
    # Real examples pulled from a captured Teams caption region: a bare name, a name with trailing
    # reaction-icon OCR garbage, a "Name | Company" badge, and a "Lastname, Firstname" badge.
    assert _looks_like_speaker_badge("Ariaf Hussain")
    assert _looks_like_speaker_badge("Ariaf Hussain e®")
    assert _looks_like_speaker_badge("Jonathan Arnold @")
    assert _looks_like_speaker_badge("John Smith | Strategic Developments")
    assert _looks_like_speaker_badge("John Smith I Strategic Developments e®")
    assert _looks_like_speaker_badge("Brookes, Martin")


def test_speaker_badge_accepts_real_dialogue_lines():
    assert not _looks_like_speaker_badge("Which we are happy with and we are now onboarding them.")
    assert not _looks_like_speaker_badge("17th is it? Sorry.")
    assert not _looks_like_speaker_badge("DFW")
    assert not _looks_like_speaker_badge("On T3.")


def test_new_lines_returns_only_lines_not_already_seen():
    already_seen = {"On T2.", "Testing."}
    text = "On T2.\nTesting.\nOn T3."

    assert _new_lines(text, already_seen) == ["On T3."]


def test_new_lines_filters_blank_noise_and_speaker_badge_lines():
    text = "MB\n\nJonathan Arnold @\n@ B x\nTesting."

    assert _new_lines(text, set()) == ["Testing."]


def test_new_lines_dedups_a_line_repeated_within_the_same_capture():
    text = "Testing.\nOn T3.\nTesting."

    assert _new_lines(text, set()) == ["Testing.", "On T3."]


def test_new_lines_can_keep_speaker_badges_when_asked_to():
    text = "John Smith\nTesting."

    assert _new_lines(text, set(), filter_speaker_badges=False) == ["John Smith", "Testing."]


def test_ocr_region_once_keeps_names_but_still_drops_ui_noise():
    """Unlike the live caption stream, an attendee-list capture wants names kept — that's the content
    it's for — so it must not run the speaker-badge filter."""
    sct = FakeSct([b"frame-a"])
    tess = FakePytesseract(["MB\nJohn Smith\nJane Doe\n@ B x"])

    result = _ocr_region_once(sct, region={"left": 0, "top": 0, "width": 100, "height": 100},
                               Image=FakeImage, pytesseract=tess)

    assert result == "John Smith\nJane Doe"


def test_ocr_region_once_returns_empty_string_for_no_readable_text():
    sct = FakeSct([b"frame-a"])
    tess = FakePytesseract(["MB\n@ B x"])

    result = _ocr_region_once(sct, region={"left": 0, "top": 0, "width": 100, "height": 100},
                               Image=FakeImage, pytesseract=tess)

    assert result == ""
