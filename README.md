# Meeting Scribe

A standalone Windows application that sits in the background during a meeting, listens, watches the
screen, and turns the meeting into a local record — then hands a copy off to Copilot Studio to manage
and parse from there. This app doesn't call an AI API and doesn't wait for one to answer; it records,
saves everything locally, and pushes.

## What it does

- **Records audio** from the microphone *and* the system output (WASAPI loopback), so it captures both
  sides of a call even when remote participants' audio never touches the mic. If a machine has more than
  one mic or speaker, a dropdown right on the Record tab (also in Settings) lets you pick which one gets
  recorded instead of always trusting whatever Windows currently calls "default" — and a live input-level
  meter for each confirms it's actually picking up audio rather than guessing. The meters use a
  logarithmic (dBFS) scale rather than a plain linear ratio, since normal microphone volume is often only
  a few percent of full scale and would otherwise barely move a linear meter.
- **Watches the screen** at a low frame rate and OCRs it, so on-screen captions, shared slides, and chat
  messages become part of the transcript even if they're never spoken aloud. You can point this at the
  whole screen, a single selected window (e.g. just the Teams/Zoom window), or a custom rectangle you
  drag out yourself (e.g. just a captions bar) — the app draws a live boundary around whichever custom
  area is active so it's always visible on screen what's being captured. A custom rectangle can also be
  pinned to a window ("Pin area to window" next to "Select area…") instead of a fixed screen position —
  e.g. just the captions bar within the Teams window — so the captured area (and its on-screen outline)
  moves and scales with that window: dragged to another monitor, resized, or re-laid-out at a different
  per-monitor DPI scale, it stays in roughly the same relative spot rather than at fixed screen pixels.
- **Transcribes** the recorded audio locally (no audio ever leaves the machine) and merges it with the
  OCR stream into one time-ordered transcript. This — plus the push to Copilot Studio below — happens in
  the background after you hit Stop, so it doesn't block starting the next meeting right away; the
  activity log tags each background job's lines with that meeting's title so back-to-back meetings
  finishing up at the same time stay distinguishable.
- **Pushes the finished meeting to Copilot Studio** as a named file package — separate audio and screen
  transcripts, a copy of each reference document attached to that meeting, and a completion manifest,
  all dropped into a folder OneDrive/SharePoint is already syncing (see "The Copilot push" below). This is
  one-way and fire-and-forget: the app doesn't call an AI API directly (governance doesn't allow that for
  this org) and doesn't wait for or ingest anything back. Whatever picks that package up from there — a
  Power Automate flow, Copilot Studio, or however it's wired up — owns managing, summarizing, and parsing
  the information; this app's job stops at the handoff.
- **Keeps a running activity log** on the Record tab while a meeting is in progress (and while it's
  finishing up) — timestamped lines for the meeting starting, stopping, transcription finishing, and the
  push to Copilot Studio — so it's obvious something is happening, without dumping the live
  transcript/OCR text into view.
- **Takes manual notes** in a full-size, freely-editable box on the Record tab (roughly half its vertical
  space, matching the activity log) — type continuously rather than adding one note at a time; Enter
  continues whatever bullet/indent the current line has, and Tab / Shift+Tab indent or dedent it, so a
  nested bulleted list is just typing. Saved to that meeting incrementally as you type (debounced, so
  nothing is lost if the app closes mid-meeting) — shown afterward on the Projects & Search tab's
  "Manual notes" tab, alongside the OCR and audio transcripts, and handed off in the Copilot push package
  as a reference-doc-style entry (see below).
- **Files the meeting under a project**, kept as a permanent local record — every meeting (transcript,
  manual notes) and any documents attached to it (invites, agendas, screenshots) stays browsable in this
  app under its project regardless of what happens to the copy pushed to Copilot Studio.
- **Accepts context documents** — PDFs, Word docs, images, plain text — attached from the Record tab (to
  the project, or to whichever meeting is currently in progress) at any time, not just during a meeting
  (e.g. a screenshot of the calendar invite, a spec doc). Documents can also be attached *after* a meeting
  has ended, from the Projects & Search tab's own "Upload Document…" button — to whichever past meeting is
  selected there, or to the project generally if none is.
- **Lets the meeting title be renamed any time before it ends** — it's a normal editable field on the
  Record tab, not fixed once at Start, and every keystroke is saved immediately so the title shown
  elsewhere in the app never lags behind. It's also a dropdown: typing a project name into the Project
  field populates it with that project's previous meeting titles (most recently used first), so a
  recurring meeting ("Weekly Client Meeting") is one click instead of retyping.
- **Captures the attendee list via OCR** — a "Capture Attendees…" button on the Record tab (enabled while
  a meeting is recording) opens the same drag-to-select overlay used for screen OCR, reads whatever
  participants panel you draw a box around immediately (not on a delay, and not part of the continuous
  screen watcher), and appends the result to that meeting's attendee list. Shown afterward in its own
  "Attendees" tab on the Projects & Search tab, alongside the OCR/audio transcripts and manual notes, and
  handed off in the Copilot push package as a reference-doc-style entry (see below).

## Why it's built this way

| Concern | Choice | Reasoning |
|---|---|---|
| Speech-to-text | [faster-whisper](https://github.com/SYSTRAN/faster-whisper), local | Running Whisper locally keeps meeting audio on the machine and avoids per-minute STT billing. |
| Screen OCR | [pytesseract](https://github.com/madmaze/pytesseract) (wraps Tesseract) | Lightweight, no GPU, no ML runtime to bundle — important for a single-file Windows executable. Requires the Tesseract binary (see Packaging). |
| System audio capture | [PyAudioWPatch](https://github.com/s0d3s/PyAudioWPatch) | A PyAudio fork with WASAPI loopback support, i.e. it can record "what the speakers are playing" on Windows without a virtual audio cable. |
| Long recordings | Each track rolls over into numbered WAV parts (`mic.wav`, `mic.part2.wav`, …) just under 2 GiB | A WAV's RIFF header stores every chunk size as a 32-bit integer, so one file stops being describable somewhere under 4 GiB — and many readers treat those sizes as signed, which halves it. Writing past that point raises mid-write and kills the capture thread, silently ending the recording. Transcription stitches the parts back onto one clock. |
| Window selection for OCR | [pywin32](https://github.com/mhammond/pywin32) (`win32gui`) | Enumerates open windows and re-reads a selected window's bounding box every capture cycle (it may move/resize), so OCR can be scoped to one app instead of the whole desktop. |
| Multi-monitor DPI coordinates | Per-monitor DPI awareness (`shcore.SetProcessDpiAwareness`, set before any window is created) | Without this, Windows virtualizes window/monitor coordinates for the process on any monitor that isn't running the primary monitor's DPI scale, which would throw `GetWindowRect` and mss's screen capture out of sync with each other on a mixed-DPI multi-monitor setup. |
| Handoff to Copilot Studio | One-way file drop (see below), no API call, no response | Governance doesn't allow calling a third-party AI API directly. This app's scope ends at recording and handing off; managing/parsing the information is Copilot Studio's job, not this app's. |
| Packaging | PyInstaller, one-file build | Produces the standalone `.exe` the project requires. |
| GUI | Tkinter | Ships with Python, keeps the PyInstaller build small and dependency-free. |

## Project layout

```
src/meeting_scribe/
  config.py          # data directory, Copilot push folder, per-project paths
  session.py          # orchestrates one meeting: start/stop recording, merge, push, save
  audio/recorder.py   # mic + WASAPI loopback capture (Windows-only at runtime)
  audio/device_picker.py  # enumerates mic/speaker devices so one can be picked instead of the OS default
  screen/capture.py   # periodic screenshot + OCR, deduplicated, optionally scoped to one window or area
  screen/window_picker.py  # enumerates open windows so one can be picked as the OCR target
  screen/region_picker.py  # drag-to-select a custom OCR rectangle + its on-screen boundary outline
  transcription/engine.py  # faster-whisper wrapper, merges audio + screen text by timestamp
  ai/copilot_push.py    # one-way, named-file-package drop of a finished meeting to Copilot Studio
  storage/database.py   # SQLite schema: projects, meetings, transcript segments, documents
  storage/documents.py  # Text extraction for uploaded PDFs/docx/images/text
  gui/app.py             # Tkinter control panel
  main.py                 # Entry point (GUI by default, --cli for scripting)
packaging/
  build.py               # Invokes PyInstaller with the right flags/data files
  meeting_scribe.spec     # PyInstaller spec (hidden imports, bundled tesseract data, etc.)
tests/                    # Unit tests for the parts that don't need Windows hardware
```

## Data model

- **Project** — a named bucket ("Acme Q3 Renewal", "Team Standups"). Everything below belongs to one.
- **Meeting** — one recorded session: raw audio files, the merged transcript, the manual notes typed on
  the Record tab while it was in progress, and any attendee list captured via OCR. Assigned a `meetingID`
  (`meeting_code` in the database, e.g. `20260728-1030`) once at creation — this is what the Copilot push
  package's file names are keyed on, not the database row id. This is the permanent local record,
  independent of the copy pushed to Copilot Studio.
- **Transcript segment** — a timestamped line from either `mic`, `system`, or `screen_ocr`, tied to a
  meeting.
- **Document** — any uploaded file, optionally tied to a specific meeting (e.g. that meeting's invite) or
  just to the project in general (e.g. a spec doc). Both its extracted text and a copy of its original
  bytes are kept (the latter under a synthetic name — see `storage/documents.py::save_original_copy`), so
  a meeting-attached document can still be handed off under its real filename in the Copilot push package.

Everything above is browsable in the app (Projects & Search tab: pick a project, pick a meeting, its
transcripts/notes/documents are right there) — there's no in-app search/Ask feature, since querying and
synthesizing across meetings is Copilot Studio's job once the data has been pushed to it, not this app's.

## Setup (development)

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -r requirements.txt
python -m meeting_scribe.main       # launches the GUI by default (equivalent to `... main.py gui`)
```

Point the app at your Copilot push folder in the **Settings** tab (see below) — no environment variable
needed, though `MEETING_SCRIBE_COPILOT_SYNC_DIR` also works for scripted/CLI use. Recording, transcription,
and screen OCR all work with no folder configured; the meeting is just recorded and saved locally, not
pushed anywhere.

For development, install [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki) and make sure
`tesseract.exe` is on `PATH` (or point `MEETING_SCRIBE_TESSERACT_PATH` at it). The packaged `.exe` (below)
bundles its own copy, so end users running the built app don't need to install Tesseract at all.

## The Copilot push

There's no supported way for this app to call Copilot Studio directly over an outbound HTTP request —
and even if there were, an unsigned desktop app making its own AI API calls is exactly what governance
ruled out. There's also nothing to wait for: this app's job is to record and hand off, not to consume a
synthesized result. `ai/copilot_push.py` does the whole thing in two steps:

1. **Settings tab → "Copilot sync folder"**: pick a folder that's inside a location OneDrive or SharePoint
   is already syncing to this machine. The app creates an `Inbox/` subfolder under it.
2. When a meeting finishes, the app writes that meeting's whole file package to `Inbox/` and returns
   immediately. OneDrive/SharePoint sync uploads it to the cloud from there.

### File naming contract

Every meeting gets a **meetingID** — `YYYYMMDD-HHMM` (e.g. `20260728-1030`), or `YYYYMMDD-HHMM-XXX` with a
random 3-character suffix if another meeting already started in that same minute — assigned once when the
meeting is created and used to prefix every file in its package. The app's own generated files (the two
transcripts, the manual notes, the attendee list) also carry the meeting title between the meetingID and
the description of what the file is, for readability in the inbox folder:

| File | Naming pattern | Example |
|---|---|---|
| Audio transcript | `{meetingID}_{title}_transcript-audio.txt` | `20260728-1030_Kickoff_transcript-audio.txt` |
| Screen transcript | `{meetingID}_{title}_transcript-screen.txt` | `20260728-1030_Kickoff_transcript-screen.txt` |
| Manual notes | `{meetingID}_{title}_meeting-notes.txt` | `20260728-1030_Kickoff_meeting-notes.txt` |
| Attendee list | `{meetingID}_{title}_attendees.txt` | `20260728-1030_Kickoff_attendees.txt` |
| Reference document(s) | `{meetingID}_{original-filename}.{ext}` | `20260728-1030_Q3 Budget Proposal.pdf` |
| Completion manifest | `{meetingID}_done.json` | `20260728-1030_done.json` |

`{title}` is the meeting title with characters Windows rejects in filenames (`<>:"/\|?*` and control
characters) swapped for a space and whitespace collapsed (see `ai/copilot_push.py::_sanitize_title_for_filename`);
a title that sanitizes down to nothing falls back to `Untitled`.

Uploaded reference documents are copied under their real original filename (just prefixed with the
meetingID, no title), not renamed or converted — this app keeps a copy of the original bytes it was
uploaded with (see `storage/documents.py::save_original_copy`) specifically so it has something to hand off
here later, since only its *extracted text* is otherwise kept in the local database. Any two files in the
same meeting that land on the same saved filename get `-2`, `-3`, etc. appended before the extension to
avoid overwriting each other; the manifest's `reference_docs` entries carry both the original and saved
filename, so a rename is always visible there.

Manual notes and the OCR'd attendee list aren't uploaded files, but they're handed off the same
reference-doc-style way — same manifest entry shape — as `{meetingID}_{title}_meeting-notes.txt` and
`{meetingID}_{title}_attendees.txt` respectively, written from whatever's saved in the local database
rather than copied from a file on disk. Either (or both) is simply omitted from `reference_docs` if nothing
was recorded for that meeting — a manual notes box that was never typed in, or an attendee list that was
never captured, doesn't produce an empty file.

The **completion manifest is written last**, only once the transcripts and every reference document have
been fully copied — it's the single file the downstream workflow should watch for and trigger on; nothing
else appearing in the folder should cause a trigger. Its shape:

```json
{
  "meetingID": "20260728-1030",
  "projectName": "Acme Rollout",
  "meetingTitle": "Kickoff",
  "files": {
    "transcript_audio": "20260728-1030_Kickoff_transcript-audio.txt",
    "transcript_screen": "20260728-1030_Kickoff_transcript-screen.txt",
    "reference_docs": [
      {
        "original_filename": "meeting-notes.txt",
        "saved_filename": "20260728-1030_Kickoff_meeting-notes.txt"
      },
      {
        "original_filename": "attendees.txt",
        "saved_filename": "20260728-1030_Kickoff_attendees.txt"
      },
      {
        "original_filename": "Q3 Budget Proposal.pdf",
        "saved_filename": "20260728-1030_Q3 Budget Proposal.pdf"
      }
    ]
  },
  "reference_count": 3,
  "timestamp_completed": "2026-07-28T10:47:32Z"
}
```

`reference_docs` is `[]` (and `reference_count` is `0`) when no reference documents were attached to that
meeting. `timestamp_completed` is always UTC, `Z`-suffixed ISO 8601. `projectName` and `meetingTitle` are
the project and meeting names as entered locally — they aren't part of the file-naming contract (that's
still keyed on `meetingID` alone), just metadata for whatever consumes the manifest.

Whatever picks that package up on the other end — a Power Automate flow, Copilot Studio, or however an org
wires it up — owns managing, summarizing, and parsing the information from that point on. Building and
owning that side (and whatever Copilot Studio capacity/licensing it needs) is outside this codebase —
that's a Power Platform admin/maker task, not something this app depends on to function. If nothing is
configured to pick the package up at all, the meeting is still fully recorded and saved locally; nothing
is lost, it just never gets pushed anywhere.

## Building the .exe

```bash
pip install pyinstaller

# Optional but recommended: fold your local Tesseract install into the exe so end users don't need
# to install Tesseract themselves. Requires Tesseract to already be installed on the build machine —
# see the link above. Skip this and the exe still works, but Tesseract must then be present on PATH
# on whatever machine runs it.
python packaging/vendor_tesseract.py

python packaging/build.py
```

This produces `dist/MeetingScribe.exe`. `packaging/meeting_scribe.spec` bundles whatever
`vendor_tesseract.py` staged into `packaging/vendor/tesseract/` (nothing extra needed at runtime —
`config.py` finds the bundled binary automatically via PyInstaller's `sys._MEIPASS`); if that folder is
absent it builds without Tesseract bundled, matching the pre-existing PATH-based behavior.

## CI builds and releases

`.github/workflows/build-windows-exe.yml` builds the exe on a real `windows-latest` runner on every push
to this branch (PyInstaller doesn't cross-compile, so this can't happen on Linux CI). Each build:

1. Runs the test suite as a gate — a failing test blocks the build.
2. Vendors Tesseract (via Chocolatey) and runs `packaging/build.py`.
3. Computes a version as `{meeting_scribe.__version__}-build{run number}` (e.g. `0.1.0-build7`), so every
   build is uniquely identifiable even between deliberate version bumps.
4. Publishes a **GitHub Release** tagged `v{version}` with the versioned exe attached, pinned to the exact
   commit it was built from. Releases are permanent — unlike the workflow-run artifact (also uploaded,
   for convenience, but expires after 90 days), a Release is the durable record of what shipped when.

Bump `__version__` in `src/meeting_scribe/__init__.py` for a meaningful milestone; every push still gets
its own release regardless, tagged off whatever the base version currently is.

## Status

This is the initial scaffold: the storage layer, document ingestion, transcript merging, and the one-way
Copilot Studio push are implemented and unit-tested. The audio and screen-capture modules are implemented
against the Windows-only APIs they depend on (WASAPI loopback, live screenshots) and are not testable in a
headless Linux CI/dev container — they need to be exercised on an actual Windows machine. The GUI wires
everything together but likewise needs a Windows desktop session to click through.

## Roadmap / open decisions

- Speaker diarization (who said what) — faster-whisper alone doesn't separate speakers; mic vs. system
  audio gives a coarse "you" vs. "everyone else" split today.
- Auto-detect meeting start (e.g. when Teams/Zoom is foregrounded) instead of a manual start button.
- A selected window (or a custom area pinned to one) that's moved to another monitor, or resized, still
  captures correctly (bounds are re-read every cycle, and a pinned area's offset/size scale with the
  window rather than staying at fixed pixels), but there's no UI feedback yet if the selected/pinned
  window closes mid-meeting — it just silently stops contributing screen text for the rest of the
  meeting.
- A pinned custom area's proportional scaling is a best-effort approximation, not real layout tracking:
  it assumes whatever's inside the area moves/resizes in proportion to the window, which holds for a
  simple corner/edge crop but not for UI an app clamps to a fixed size or recenters regardless of window
  size — that could still need re-picking after a big resize.
