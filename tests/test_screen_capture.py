from meeting_scribe.screen.capture import (
    ScreenWatcher,
    _looks_like_ui_noise,
    _new_lines,
    _ocr_region_once,
)
from meeting_scribe.screen.region_picker import RegionTarget


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


def test_dedup_skips_unchanged_frames_and_repeated_text():
    events = []
    watcher = ScreenWatcher(on_text=events.append, interval_seconds=0.01)
    watcher._started_at = 0.0

    frame_a = b"frame-a"
    frame_b = b"frame-b"

    sct = FakeSct([frame_a, frame_a, frame_b, frame_b])
    tess = FakePytesseract(["Slide 1", "Slide 1", "Slide 1", "Slide 2"])

    # 1st call: new frame, new text -> event fired
    watcher._capture_once(sct, region=None, Image=FakeImage, pytesseract=tess)
    # 2nd call: identical frame hash -> OCR skipped entirely (no text consumed for it, but our fake
    # queue still advances only for calls that actually reach image_to_string)
    watcher._capture_once(sct, region=None, Image=FakeImage, pytesseract=tess)

    assert len(events) == 1
    assert events[0].text == "Slide 1"


def test_new_frame_but_same_text_does_not_refire():
    events = []
    watcher = ScreenWatcher(on_text=events.append, interval_seconds=0.01)
    watcher._started_at = 0.0

    sct = FakeSct([b"frame-a", b"frame-b"])
    tess = FakePytesseract(["Slide 1", "Slide 1"])

    watcher._capture_once(sct, region=None, Image=FakeImage, pytesseract=tess)
    watcher._capture_once(sct, region=None, Image=FakeImage, pytesseract=tess)

    assert len(events) == 1


def test_new_frame_new_text_fires_again():
    events = []
    watcher = ScreenWatcher(on_text=events.append, interval_seconds=0.01)
    watcher._started_at = 0.0

    sct = FakeSct([b"frame-a", b"frame-b"])
    tess = FakePytesseract(["Slide 1", "Slide 2"])

    watcher._capture_once(sct, region=None, Image=FakeImage, pytesseract=tess)
    watcher._capture_once(sct, region=None, Image=FakeImage, pytesseract=tess)

    assert [e.text for e in events] == ["Slide 1", "Slide 2"]


def test_resolve_region_returns_the_fixed_rectangle_for_a_region_target():
    target = RegionTarget(left=10, top=20, width=300, height=200)
    watcher = ScreenWatcher(on_text=lambda e: None, target=target)

    assert watcher._resolve_region(sct=None) == {"left": 10, "top": 20, "width": 300, "height": 200}


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
    assert not _looks_like_ui_noise("Brookes, Martin")


def test_new_lines_returns_only_lines_not_already_seen():
    already_seen = {"Brookes, Martin", "Testing."}
    text = "Brookes, Martin\nTesting.\nOn T3."

    assert _new_lines(text, already_seen) == ["On T3."]


def test_new_lines_filters_blank_and_noise_lines():
    text = "MB\n\nBrookes, Martin\n@ B x\nTesting."

    assert _new_lines(text, set()) == ["Brookes, Martin", "Testing."]


def test_new_lines_dedups_a_line_repeated_within_the_same_capture():
    text = "Brookes, Martin\nTesting.\nBrookes, Martin"

    assert _new_lines(text, set()) == ["Brookes, Martin", "Testing."]


def test_capture_once_only_emits_newly_scrolled_in_lines():
    """Reproduces the real-world bug: a scrolling chat pane re-shows most of its previous content on
    every capture, so each screenshot's OCR text mostly overlaps the last one plus one new line."""
    events = []
    watcher = ScreenWatcher(on_text=events.append, interval_seconds=0.01)
    watcher._started_at = 0.0

    sct = FakeSct([b"frame-a", b"frame-b", b"frame-c"])
    tess = FakePytesseract(
        [
            "MB\nBrookes, Martin\nTesting.",
            "MB\nMB\nBrookes, Martin\nTesting.\nBrookes, Martin\nOn T3.",
            "MB\nMB\nMB\nBrookes, Martin\nTesting.\nBrookes, Martin\nOn T3.\n@ B x\nBrookes, Martin\n"
            "OK, now we are testing.",
        ]
    )

    watcher._capture_once(sct, region=None, Image=FakeImage, pytesseract=tess)
    watcher._capture_once(sct, region=None, Image=FakeImage, pytesseract=tess)
    watcher._capture_once(sct, region=None, Image=FakeImage, pytesseract=tess)

    assert [e.text for e in events] == ["Brookes, Martin\nTesting.", "On T3.", "OK, now we are testing."]


def test_ocr_region_once_returns_the_cleaned_text():
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
