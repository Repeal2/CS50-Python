"""Periodic screenshot + OCR, so on-screen captions/chat/slides make it into the transcript even when
nobody says them out loud.

Runs on a background thread, screenshotting at `interval_seconds` and only paying the OCR cost when the
frame has settled (see `_grab_stable_frame`) and actually changed (a cheap content hash short-circuits
static screens). Dedup happens at the *line* level, not the whole-frame level: a scrolling chat/captions
pane re-shows most of its previous content on every capture (nothing scrolled off, or only the top line
did), so comparing whole OCR blobs means almost every capture looks "new" and the transcript balloons with
the same messages repeated over and over.

Live captions (Teams/Zoom-style) add a further wrinkle on top of that: a caption line typically renders
word-by-word and only reaches its final wording a second or two after it first appears, so even *line*-level
exact-match dedup treats every partial state as a distinct new line ("We we we" / "We we we discussed the
trolley choice we're" / the finished sentence, all three kept). `_pending_lines` holds each capture's
not-yet-settled lines and only turns one into a `ScreenTextEvent` once a later capture shows it's stopped
growing — see `_reconcile_lines`. Events are timestamped relative to when watching started so they can be
interleaved with the audio transcript by `transcription.engine`.

The speaker name badge Teams renders alongside each caption line (see `_looks_like_speaker_badge`) is
filtered out of that caption text as chrome, but it's independently useful: unlike the caption sentence
next to it, a name badge is short and doesn't need to "settle" across captures, so it's reported as its own
`SpeakerNameEvent` as soon as it's read rather than going through the pending/growth machinery above.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable

from meeting_scribe.screen.region_picker import RegionTarget, WindowRegionTarget
from meeting_scribe.screen.window_picker import WindowTarget

# Tuned to reject short/symbol-heavy OCR misreads of UI chrome (avatar initials like "MB", icon rows
# like "@ B x") without also rejecting genuinely short spoken/chat lines ("On T3."). Not reliable against
# a longer garbled misread that happens to look like real words (e.g. a decorative divider line OCR'd as
# "np sewn ee ee ener meee eee") — that would need something like a dictionary check, which this doesn't
# attempt.
_MIN_ALPHA_CHARS = 3
_MIN_ALPHA_RATIO = 0.5

# A caption region's OCR text includes each speaker's name badge alongside their words (Teams renders it
# right above/beside the caption line), so it shows up in the same text blob. A badge is short, every word
# in it is Title-Cased (a person's name, or "Name | Company"), and — unlike a finished spoken sentence,
# which this app's captions always end with punctuation for — it never ends in sentence punctuation. That
# combination is a reasonable proxy for "this is chrome, not something anyone said," but it's not perfect:
# an all-caps or all-Title-Case *spoken* fragment with no trailing punctuation ("OK OK") would also match.
# In practice that's rare because Teams punctuates a caption once it settles, so this only really misfires
# on a line that never finishes settling before the capture stops.
_NAME_BADGE_RE = re.compile(
    r"^[A-Z][A-Za-z'.-]*(?:[,\s]\s*[A-Z][A-Za-z'.-]*){1,3}"  # 2-4 Title-Case words, comma- or space-joined
    r"(?:\s*[|I]\s*[A-Z][A-Za-z0-9 &'.-]*)?"  # optional " | Company" (pipe sometimes OCR's as "I")
    r"\s*[A-Za-z@&%«»©®]{0,3}$"  # optional trailing reaction-icon/glyph garbage
)
_MAX_BADGE_LENGTH = 45

# `_grab_stable_frame` waits for a frame that holds still, but some never do: a whole Teams window with a
# participant's live video in it changes between any two grabs, however close together. Waiting for
# stillness there meant waiting forever — OCR silently stopped for as long as anyone's camera was on,
# which for most calls is most of the meeting. After this many unsettled captures in a row, the latest
# frame is read anyway.
_UNSETTLED_CAPTURES_BEFORE_READING_ANYWAY = 2

# A capture that fails (the window moved half off-screen, Tesseract choked on a frame, a monitor was
# unplugged) is skipped rather than ending on-screen capture for the rest of the meeting, as it used to.
# Consecutive failures back off to this, so a failure that repeats every cycle doesn't spin.
_MAX_FAILURE_BACKOFF_SECONDS = 30.0

# How long stop() waits for a capture in progress to finish. OCR of a large frame can take several
# seconds; giving up early used to mean the lines still settling on screen at the end of the meeting
# were flushed after the meeting had already collected its on-screen text, and never made it in.
_STOP_TIMEOUT_SECONDS = 60.0


def _looks_like_ui_noise(line: str) -> bool:
    letters = sum(1 for ch in line if ch.isalpha())
    non_space = sum(1 for ch in line if not ch.isspace())
    if non_space == 0:
        return True
    return letters < _MIN_ALPHA_CHARS or (letters / non_space) < _MIN_ALPHA_RATIO


def _looks_like_speaker_badge(line: str) -> bool:
    if line[-1:] in ".?!" or len(line) > _MAX_BADGE_LENGTH:
        return False
    return bool(_NAME_BADGE_RE.match(line))


def _speaker_badge_lines(text: str) -> list[str]:
    """Every line in one OCR capture that looks like a Teams-style speaker name badge. Deliberately not
    deduped against anything already seen, unlike `_new_lines` — the same name reappearing capture after
    capture while someone keeps talking is the point, not noise to filter out."""
    return [
        line
        for raw_line in text.splitlines()
        if (line := raw_line.strip()) and not _looks_like_ui_noise(line) and _looks_like_speaker_badge(line)
    ]


def _new_lines(text: str, already_seen: set[str], *, filter_speaker_badges: bool = True) -> list[str]:
    """Splits one capture's OCR text into lines and returns only the ones that are new: not blank, not
    UI noise, not (when `filter_speaker_badges`) a speaker name badge, and not in `already_seen` (or
    already returned earlier in this same call, in case a line repeats within one capture). Order matches
    first appearance in `text`. Doesn't mutate `already_seen` — the caller adds the returned lines once
    it's decided to use them.

    `filter_speaker_badges` defaults on for the live caption stream, where name badges are chrome bleeding
    into the captured region, but is turned off for `_ocr_region_once`'s attendee-list capture, where a
    person's name is exactly the wanted content."""
    new: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line in already_seen or line in new or _looks_like_ui_noise(line):
            continue
        if filter_speaker_badges and _looks_like_speaker_badge(line):
            continue
        new.append(line)
    return new


def _is_growth(previous: str, candidate: str) -> bool:
    """True if `candidate` looks like `previous` having grown by one more capture cycle of a live caption
    rendering in — the two are the same logical line, not a coincidence. A live caption always grows by
    appending new words at the end, so a strict prefix relationship (either direction, since a capture
    could catch it right as it shrinks back to nothing at the very start of the next sentence) is what
    that looks like. Deliberately not a fuzzy/similarity match: two short *unrelated* lines easily share
    enough characters to look "similar" by ratio (e.g. "Slide 1" / "Slide 2" score high on most string
    similarity measures) without being the same evolving line at all."""
    return candidate.startswith(previous) or previous.startswith(candidate)


def _find_growth_match(candidate: str, pending: list[str]) -> str | None:
    for previous in pending:
        if _is_growth(previous, candidate):
            return previous
    return None


@dataclass(frozen=True)
class ScreenTextEvent:
    timestamp_seconds: float
    text: str


@dataclass(frozen=True)
class SpeakerNameEvent:
    timestamp_seconds: float
    name: str


class ScreenWatcher:
    def __init__(
        self,
        on_text: Callable[[ScreenTextEvent], None],
        interval_seconds: float = 3.0,
        tesseract_cmd: str | None = None,
        target: WindowTarget | RegionTarget | WindowRegionTarget | None = None,
        settle_seconds: float = 0.15,
        on_speaker_name: Callable[[SpeakerNameEvent], None] | None = None,
    ):
        self._on_text = on_text
        self._on_speaker_name = on_speaker_name
        self._interval = interval_seconds
        self._tesseract_cmd = tesseract_cmd
        self._target = target
        self._settle_seconds = settle_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at: float | None = None
        self._last_frame_hash: str | None = None
        self._finalized_lines: set[str] = set()
        # Lines seen in the last capture that hadn't stopped growing yet — see module docstring.
        self._pending_lines: list[str] = []
        self._unsettled_captures = 0
        self._notices: list[str] = []
        self._notices_lock = threading.Lock()

    @property
    def target(self) -> WindowTarget | RegionTarget | WindowRegionTarget | None:
        return self._target

    def set_target(self, target: WindowTarget | RegionTarget | WindowRegionTarget | None) -> None:
        """Changes what's captured from the next cycle on — see session.MeetingSession.follow_screen_window.
        A plain attribute swap: the capture thread reads it once per cycle."""
        self._target = target
        self._last_frame_hash = None

    def notices(self) -> tuple[str, ...]:
        """Anything that went wrong with on-screen capture, as ready-to-show sentences — each distinct
        problem once."""
        with self._notices_lock:
            return tuple(self._notices)

    def _record_notice(self, message: str) -> None:
        with self._notices_lock:
            if message not in self._notices:
                self._notices.append(message)

    def start(self) -> None:
        self._stop_event.clear()
        self._started_at = time.monotonic()
        self._thread = threading.Thread(target=self._run, daemon=True, name="screen-watcher")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=max(_STOP_TIMEOUT_SECONDS, self._interval + 5))
            if self._thread.is_alive():
                self._record_notice(
                    "On-screen capture was still reading a frame when the meeting stopped; the last "
                    "on-screen lines may be missing."
                )

    def _run(self) -> None:
        try:
            import mss
            import pytesseract
            from PIL import Image

            if self._tesseract_cmd:
                pytesseract.pytesseract.tesseract_cmd = self._tesseract_cmd

            with mss.mss() as sct:
                failures = 0
                while not self._stop_event.is_set():
                    loop_start = time.monotonic()
                    try:
                        region = self._resolve_region(sct)
                        if region is not None:
                            self._capture_once(sct, region, Image, pytesseract)
                        failures = 0
                    except Exception as exc:
                        failures += 1
                        self._record_notice(
                            f"On-screen capture skipped a frame ({type(exc).__name__}: {exc}) and carried on."
                        )
                    elapsed = time.monotonic() - loop_start
                    wait = self._interval - elapsed
                    if failures:
                        wait = max(wait, min(_MAX_FAILURE_BACKOFF_SECONDS, self._interval * 2 ** (failures - 1)))
                    self._stop_event.wait(max(0.0, wait))
        except Exception as exc:  # couldn't even start: no screen capture or no Tesseract at all
            self._record_notice(f"On-screen capture couldn't run: {type(exc).__name__}: {exc}")
        finally:
            self._flush_pending()

    def _resolve_region(self, sct) -> dict | None:
        """Returns the mss region to capture: the whole virtual screen, a fixed user-drawn rectangle, a
        user-drawn rectangle pinned to a window's current position, or the selected window's current
        bounds — None if a selected window (pinned-area or whole-window) has since closed or been
        minimized, in which case the caller skips that capture cycle rather than falling back to the
        whole screen."""
        if self._target is None:
            return sct.monitors[0]  # index 0 == a single virtual monitor spanning all displays
        if isinstance(self._target, RegionTarget):
            return self._target.mss_region
        from meeting_scribe.screen.window_picker import get_window_region

        if isinstance(self._target, WindowRegionTarget):
            window_rect = get_window_region(self._target.hwnd)
            return None if window_rect is None else self._target.mss_region(window_rect)
        return get_window_region(self._target.hwnd)

    def _grab_stable_frame(self, sct, region, Image):
        """Grabs two frames `_settle_seconds` apart and returns the image only if they're pixel-identical.
        A live-caption region is mid-animation (text still fading/sliding in) far more often than it's
        genuinely static at any given instant, and OCR'ing a transitional frame is what produces the
        heavily garbled misreads (stray characters, whole words replaced by noise) seen in a scrolling
        caption stream. Returns None when the two grabs differ — the caller just tries again next cycle,
        which costs nothing since OCR (the expensive step) never runs on a rejected frame."""
        shot_a = sct.grab(region)
        image_a = Image.frombytes("RGB", shot_a.size, shot_a.rgb)
        self._stop_event.wait(self._settle_seconds)
        shot_b = sct.grab(region)
        image_b = Image.frombytes("RGB", shot_b.size, shot_b.rgb)
        if image_a.tobytes() != image_b.tobytes():
            self._unsettled_captures += 1
            if self._unsettled_captures < _UNSETTLED_CAPTURES_BEFORE_READING_ANYWAY:
                return None
            # See _UNSETTLED_CAPTURES_BEFORE_READING_ANYWAY. Reset, so the next frame gets its own chance
            # to settle before being read unsettled again.
            self._unsettled_captures = 0
        return image_b

    def _capture_once(self, sct, region, Image, pytesseract) -> None:
        image = self._grab_stable_frame(sct, region, Image)
        if image is None:
            return  # frame was still changing (mid-scroll/animation); try again next cycle
        self._unsettled_captures = 0
        frame_hash = hashlib.blake2b(image.tobytes(), digest_size=16).hexdigest()
        if frame_hash == self._last_frame_hash:
            return  # screen hasn't changed since the last capture; skip the OCR cost
        self._last_frame_hash = frame_hash

        text = pytesseract.image_to_string(image)
        self._reconcile_lines(text)
        self._emit_speaker_names(text)

    def _emit_speaker_names(self, text: str) -> None:
        """Reports every speaker name badge found in one capture's OCR text, if anyone wants them (see
        `on_speaker_name`) — a name recurs for as long as its badge stays on screen, which is what lets a
        caller later line these timestamped sightings up against a diarized transcript's speaker turns."""
        if self._on_speaker_name is None:
            return
        elapsed = time.monotonic() - self._started_at
        for name in _speaker_badge_lines(text):
            self._on_speaker_name(SpeakerNameEvent(timestamp_seconds=elapsed, name=name))

    def _reconcile_lines(self, text: str) -> None:
        """Folds one capture's OCR text into `_pending_lines`. A candidate that looks like a previous
        cycle's pending line having grown replaces it (still pending, not emitted yet). A pending line
        that *didn't* reappear or grow this round has settled — it's finalized and included in this
        cycle's event, so a real caption is only ever emitted once, in its most-complete form."""
        candidates = _new_lines(text, self._finalized_lines)
        still_pending = list(self._pending_lines)
        new_pending: list[str] = []
        for candidate in candidates:
            match = _find_growth_match(candidate, still_pending)
            if match is not None:
                still_pending.remove(match)
                # Keep whichever is longer: normally that's `candidate` (the caption grew), but an
                # occasional OCR misread can make a later capture look like it *shrank* — don't let that
                # downgrade a pending line we already had in fuller form.
                candidate = max(candidate, match, key=len)
            new_pending.append(candidate)

        self._pending_lines = new_pending
        if not still_pending:
            return
        self._finalized_lines.update(still_pending)
        elapsed = time.monotonic() - self._started_at
        self._on_text(ScreenTextEvent(timestamp_seconds=elapsed, text="\n".join(still_pending)))

    def _flush_pending(self) -> None:
        """Finalizes whatever's still pending when watching stops, so the last caption on screen isn't
        silently dropped just because nothing arrived afterward to confirm it had stopped growing."""
        if not self._pending_lines:
            return
        self._finalized_lines.update(self._pending_lines)
        elapsed = time.monotonic() - self._started_at
        text = "\n".join(self._pending_lines)
        self._pending_lines = []
        self._on_text(ScreenTextEvent(timestamp_seconds=elapsed, text=text))


def ocr_region(region: dict, tesseract_cmd: str | None = None) -> str:
    """One-shot OCR of a fixed screen rectangle (an mss-style region dict — see RegionTarget.mss_region).
    Unlike ScreenWatcher this isn't a continuous background watcher; it grabs exactly one frame right now
    and returns whatever Tesseract reads from it, for on-demand captures like reading a meeting's
    attendee list off a participants panel."""
    import mss
    import pytesseract
    from PIL import Image

    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd

    with mss.mss() as sct:
        return _ocr_region_once(sct, region, Image, pytesseract)


def _ocr_region_once(sct, region, Image, pytesseract) -> str:
    """The actual grab-and-read, split out from ocr_region() so it's testable with fakes the same way
    ScreenWatcher._capture_once is. Runs the same blank/junk/duplicate-line filter as the live OCR stream,
    since avatar-initial and icon-row garbage shows up in a one-shot capture exactly like it does in a
    continuous one — but leaves speaker-badge filtering off, since this is used to read an attendee list
    off a participants panel, where a person's name *is* the wanted content rather than chrome."""
    shot = sct.grab(region)
    image = Image.frombytes("RGB", shot.size, shot.rgb)
    text = pytesseract.image_to_string(image)
    return "\n".join(_new_lines(text, already_seen=set(), filter_speaker_badges=False))
