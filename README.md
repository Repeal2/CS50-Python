# Meeting Scribe

A standalone Windows application that sits in the background during a meeting, listens, watches the
screen, and turns the meeting into a local record — then hands a copy off to Copilot Studio to manage
and parse from there. This app doesn't call an AI API and doesn't wait for one to answer; it records,
saves everything locally, and pushes.

## What it does

The window has three pages, picked from the sidebar: **Record**, **Library** and **Settings**. It follows
Windows' light/dark app setting.

- **Records audio** from the microphone *and* the system output (WASAPI loopback), so both sides of a
  call are captured even when remote participants' audio never touches the mic. The Record page shows a
  live level meter next to each device picker, and **Test mic** listens for three seconds and says what it
  heard, right under the meters, without opening a dialog.
- **Follows the devices Teams is using.** With "Switch devices automatically" on (Settings, on by
  default), the app records whichever microphone and speaker Teams has open, switching mid-meeting if
  Teams does; when that can't be told, it falls back to the speaker that's playing and a headset mic
  recognized by name. A capture stream that dies mid-meeting is reopened, and silence on the system
  track is written out as silence so both tracks stay in step.
- **Warns about bad input while it can still be fixed.** A device Windows no longer has, a mic producing
  digital silence, or an input that has heard nothing while the other track was busy shows up as a
  warning line under the meters and in the activity log. A dead-silent mic also flashes the OCR box red.
- **Offers to start and stop recording on its own.** When a Teams call starts, a small prompt offers to
  record it (pre-filling the meeting title from Teams); when a recorded call ends, one offers to stop.
  Optional system-wide shortcuts (Settings) start and stop recording without leaving Teams.
- **Reads on-screen text from a box you place.** When recording starts, an OCR box appears over the
  bottom of the Teams call window (where live captions show), or wherever it was left last time. Drag it
  to move, drag its handles to resize, and press **Start OCR** on it to begin reading. Its inside is
  see-through and click-through, so the call underneath stays usable. The text read is saved to disk as
  it arrives, so it survives a crash and a Retry.
- **Takes notes as you go.** The Notes panel saves as you type. Enter continues a bullet, Tab / Shift+Tab
  indent and outdent, and **Ctrl+T** stamps the recording time (`[12:34]`) using the same clock as the
  transcript. The project and title stay editable for the whole meeting, and the title dropdown suggests
  titles already used in the project.
- **Captures attendees and documents.** "Capture attendees" reads a participants panel you drag a box
  around. "Attach document" files a PDF, Word doc, image or text file under the project (and the meeting,
  while one is recording, or the selected meeting in the Library).
- **Transcribes locally in the background** after Stop, so the next meeting can start straight away. The
  sidebar shows how many meetings are still finishing, and the running recording clock is shown on the
  Record page and in the window title (so it's visible from the taskbar). Optionally, the system-audio
  track goes to a Runpod WhisperX endpoint for speaker labels instead, and is transcribed locally if that
  fails.
- **Keeps a searchable local library.** The Library lists meetings by project, and its search box
  (**Ctrl+F**) looks through every project's titles, transcripts, notes and attendees. Each meeting shows
  its transcript, on-screen text, notes, attendees and documents, with **Copy**, **Export…** (one plain-text
  file with every section), **Attach document** and **Open folder**. A meeting whose transcription didn't
  finish keeps its recording and offers **Retry**.
- **Pushes each finished meeting to Copilot Studio** as a named file package dropped into a folder
  OneDrive/SharePoint is already syncing (see "The Copilot push" below). This is one-way: the app doesn't
  call an AI API and doesn't wait for anything back.

## Why it's built this way

| Concern | Choice | Reasoning |
|---|---|---|
| Speech-to-text | [faster-whisper](https://github.com/SYSTRAN/faster-whisper), local | Running Whisper locally keeps meeting audio on the machine and avoids per-minute STT billing. |
| Screen OCR | [pytesseract](https://github.com/madmaze/pytesseract) (wraps Tesseract) | Lightweight, no GPU, no ML runtime to bundle — important for a single-file Windows executable. Requires the Tesseract binary (see Packaging). |
| System audio capture | [PyAudioWPatch](https://github.com/s0d3s/PyAudioWPatch) | A PyAudio fork with WASAPI loopback support, i.e. it can record "what the speakers are playing" on Windows without a virtual audio cable. |
| Long recordings | Each track rolls over into numbered WAV parts (`mic.wav`, `mic.part2.wav`, …) just under 2 GiB | A WAV's RIFF header stores every chunk size as a 32-bit integer, so one file stops being describable somewhere under 4 GiB — and many readers treat those sizes as signed, which halves it. Writing past that point raises mid-write and kills the capture thread, silently ending the recording. Transcription stitches the parts back onto one clock. |
| OCR area | An on-screen box (Tkinter, `-transparentcolor`) | One borderless, always-on-top window whose inside is see-through and click-through, so the area being read is visible and adjustable at all times without getting in the way of the call. |
| Look and feel | Flat ttk styles on the built-in "clam" theme (`gui/theme.py`) | The native Windows ttk theme can't be recoloured. Styling clam gets a modern, dark-mode-aware look with no extra dependency to bundle. |
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
  screen/ocr_box.py   # the on-screen OCR box: drag to move, handles to resize, Start/Stop OCR button
  screen/window_picker.py  # finds the Teams call window (meeting name, where to place prompts and the box)
  screen/region_picker.py  # drag-to-select a rectangle (used for Capture Attendees)
  screen/meeting_detector.py  # notices a Teams call starting/ending, for the start/stop prompts
  transcription/engine.py  # faster-whisper wrapper, merges audio + screen text by timestamp
  ai/copilot_push.py    # one-way, named-file-package drop of a finished meeting to Copilot Studio
  storage/database.py   # SQLite schema: projects, meetings, transcript segments, documents
  storage/documents.py  # Text extraction for uploaded PDFs/docx/images/text
  gui/app.py             # Tkinter window: Record, Library and Settings pages
  gui/theme.py           # palette (light/dark), fonts and ttk styles
  gui/meeting_prompt.py  # the "Teams meeting detected" / "Meeting ended" prompts
  main.py                 # Entry point (GUI by default; `list-projects` for scripting)
packaging/
  build.py               # Invokes PyInstaller with the right flags/data files
  meeting_scribe.spec     # PyInstaller spec (hidden imports, bundled tesseract data, etc.)
tests/                    # Unit tests for the parts that don't need Windows hardware
```

## Data model

- **Project** — a named bucket ("Acme Q3 Renewal", "Team Standups"). Everything below belongs to one.
- **Meeting** — one recorded session: raw audio files, the merged transcript, the manual notes typed on
  the Record page while it was in progress, and any attendee list captured via OCR. Assigned a `meetingID`
  (`meeting_code` in the database, e.g. `20260728-1030`) once at creation — this is what the Copilot push
  package's file names are keyed on, not the database row id. This is the permanent local record,
  independent of the copy pushed to Copilot Studio.
- **Transcript segment** — a timestamped line from either `mic`, `system`, or `screen_ocr`, tied to a
  meeting.
- **Document** — any uploaded file, optionally tied to a specific meeting (e.g. that meeting's invite) or
  just to the project in general (e.g. a spec doc). Both its extracted text and a copy of its original
  bytes are kept (the latter under a synthetic name — see `storage/documents.py::save_original_copy`), so
  a meeting-attached document can still be handed off under its real filename in the Copilot push package.

Everything above is browsable and searchable in the app's Library (plain text search over titles,
transcripts, notes and attendees). Summarizing or asking questions across meetings is Copilot Studio's job
once the data has been pushed to it, not this app's.

## Setup (development)

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -r requirements.txt
python -m meeting_scribe.main       # launches the GUI by default (equivalent to `... main.py gui`)
```

Point the app at your Copilot push folder on the **Settings** page (see below) — no environment variable
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

1. **Settings → Copilot Studio handoff → "Sync folder"**: pick a folder that's inside a location OneDrive or SharePoint
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

- Speaker names — local transcription only splits "you" (mic) from "everyone else" (system audio);
  the optional Runpod path adds per-speaker labels (`SPEAKER_00`, …), but nothing yet maps those to real
  names.
