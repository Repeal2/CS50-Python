from meeting_scribe.screen.capture import ScreenWatcher


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
