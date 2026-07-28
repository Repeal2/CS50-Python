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
  area is active so it's always visible on screen what's being captured.
- **Transcribes** the recorded audio locally (no audio ever leaves the machine) and merges it with the
  OCR stream into one time-ordered transcript. This — plus the push to Copilot Studio below — happens in
  the background after you hit Stop, so it doesn't block starting the next meeting right away; the
  activity log tags each background job's lines with that meeting's title so back-to-back meetings
  finishing up at the same time stay distinguishable.
- **Pushes the finished meeting to Copilot Studio** — the transcript plus any manual notes, dropped as a
  plain-text file into a folder OneDrive/SharePoint is already syncing (see "The Copilot push" below).
  This is one-way and fire-and-forget: the app doesn't call an AI API directly (governance doesn't allow
  that for this org) and doesn't wait for or ingest anything back. Whatever picks that file up from
  there — a Power Automate flow, Copilot Studio, or however it's wired up — owns managing, summarizing,
  and parsing the information; this app's job stops at the handoff.
- **Keeps a running activity log** on the Record tab while a meeting is in progress (and while it's
  finishing up) — timestamped lines for the meeting starting, stopping, transcription finishing, and the
  push to Copilot Studio — so it's obvious something is happening, without dumping the live
  transcript/OCR text into view.
- **Takes manual notes** in a full-size, freely-editable box on the Record tab (roughly half its vertical
  space, matching the activity log) — type continuously rather than adding one note at a time; Enter
  continues whatever bullet/indent the current line has, and Tab / Shift+Tab indent or dedent it, so a
  nested bulleted list is just typing. Saved to that meeting incrementally as you type (debounced, so
  nothing is lost if the app closes mid-meeting) — shown afterward on the Projects & Search tab's
  "Manual notes" tab, alongside the OCR and audio transcripts, and included in what gets pushed to
  Copilot Studio.
- **Files the meeting under a project**, kept as a permanent local record — every meeting (transcript,
  manual notes) and any documents attached to it (invites, agendas, screenshots) stays browsable in this
  app under its project regardless of what happens to the copy pushed to Copilot Studio.
- **Accepts context documents** — PDFs, Word docs, images, plain text — attached from the Record tab
  (to the project, or to whichever meeting is currently in progress) at any time, not just during a
  meeting (e.g. a screenshot of the calendar invite, a spec doc).

## Why it's built this way

| Concern | Choice | Reasoning |
|---|---|---|
| Speech-to-text | [faster-whisper](https://github.com/SYSTRAN/faster-whisper), local | Running Whisper locally keeps meeting audio on the machine and avoids per-minute STT billing. |
| Screen OCR | [pytesseract](https://github.com/madmaze/pytesseract) (wraps Tesseract) | Lightweight, no GPU, no ML runtime to bundle — important for a single-file Windows executable. Requires the Tesseract binary (see Packaging). |
| System audio capture | [PyAudioWPatch](https://github.com/s0d3s/PyAudioWPatch) | A PyAudio fork with WASAPI loopback support, i.e. it can record "what the speakers are playing" on Windows without a virtual audio cable. |
| Window selection for OCR | [pywin32](https://github.com/mhammond/pywin32) (`win32gui`) | Enumerates open windows and re-reads a selected window's bounding box every capture cycle (it may move/resize), so OCR can be scoped to one app instead of the whole desktop. |
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
  ai/copilot_push.py    # one-way file drop of a finished meeting to Copilot Studio — no response handling
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
- **Meeting** — one recorded session: raw audio files, the merged transcript, and the manual notes typed
  on the Record tab while it was in progress. This is the permanent local record, independent of the
  copy pushed to Copilot Studio.
- **Transcript segment** — a timestamped line from either `mic`, `system`, or `screen_ocr`, tied to a
  meeting.
- **Document** — any uploaded file, optionally tied to a specific meeting (e.g. that meeting's invite) or
  just to the project in general (e.g. a spec doc).

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
2. When a meeting finishes, the app writes one plain-text file to `Inbox/` (project, title, the merged
   transcript, and any manual notes) and returns immediately. OneDrive/SharePoint sync uploads it to the
   cloud from there.

Whatever picks that file up on the other end — a Power Automate flow, Copilot Studio, or however an org
wires it up — owns managing, summarizing, and parsing the information from that point on. Building and
owning that side (and whatever Copilot Studio capacity/licensing it needs) is outside this codebase —
that's a Power Platform admin/maker task, not something this app depends on to function. If nothing is
configured to pick the file up at all, the meeting is still fully recorded and saved locally; nothing
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
- A selected window that's moved to another monitor still captures correctly (bounds are re-read every
  cycle), but there's no UI feedback yet if the selected window closes mid-meeting — it just silently
  stops contributing screen text for the rest of the meeting.
