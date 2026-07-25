# Meeting Scribe

A standalone Windows application that sits in the background during a meeting, listens, watches the
screen, and turns the meeting into a searchable record.

## What it does

- **Records audio** from the microphone *and* the system output (WASAPI loopback), so it captures both
  sides of a call even when remote participants' audio never touches the mic. If a machine has more than
  one mic or speaker, the Settings tab lets you pick which one gets recorded instead of always trusting
  whatever Windows currently calls "default" — and the Record tab shows a live input-level meter for
  each, so you can actually see it's picking up audio rather than guessing.
- **Watches the screen** at a low frame rate and OCRs it, so on-screen captions, shared slides, and chat
  messages become part of the transcript even if they're never spoken aloud. You can point this at the
  whole screen or at a single selected window (e.g. just the Teams/Zoom window), so it doesn't also OCR
  unrelated desktop content.
- **Transcribes** the recorded audio locally (no audio ever leaves the machine unless you opt into a
  cloud model) and merges it with the OCR stream into one time-ordered transcript.
- **Generates notes and action items** by sending the finished transcript to Claude, guided by a system
  prompt you can edit in the Settings tab (prepopulated with a recommended default that explains what
  Claude is receiving and how the output gets used).
- **Files the meeting under a project.** Every meeting, plus any documents you attach to it (meeting
  invites, agendas, screenshots), is indexed so you can later ask "what did we decide about X" and get an
  answer synthesized from everything on file for that project.
- **Accepts context documents** — PDFs, Word docs, images, plain text — uploaded to a project at any time,
  not just during a meeting (e.g. a screenshot of the calendar invite, a spec doc).

## Why it's built this way

| Concern | Choice | Reasoning |
|---|---|---|
| Speech-to-text | [faster-whisper](https://github.com/SYSTRAN/faster-whisper), local | Claude has no audio input; running Whisper locally keeps meeting audio on the machine and avoids per-minute STT billing. |
| Screen OCR | [pytesseract](https://github.com/madmaze/pytesseract) (wraps Tesseract) | Lightweight, no GPU, no ML runtime to bundle — important for a single-file Windows executable. Requires the Tesseract binary (see Packaging). |
| System audio capture | [PyAudioWPatch](https://github.com/s0d3s/PyAudioWPatch) | A PyAudio fork with WASAPI loopback support, i.e. it can record "what the speakers are playing" on Windows without a virtual audio cable. |
| Window selection for OCR | [pywin32](https://github.com/mhammond/pywin32) (`win32gui`) | Enumerates open windows and re-reads a selected window's bounding box every capture cycle (it may move/resize), so OCR can be scoped to one app instead of the whole desktop. |
| Notes/actions generation | Anthropic Claude | Given a finished transcript, produces a structured summary, decisions, and action items. |
| Project search | SQLite FTS5 (full-text search), then Claude synthesizes the answer | Keeps the app dependency-light (no torch/embedding model to bundle) while still giving good keyword recall; Claude does the reasoning over the retrieved passages. This is intentionally swappable — see `storage/embeddings.py` stub if semantic search is wanted later. |
| Packaging | PyInstaller, one-file build | Produces the standalone `.exe` the project requires. |
| GUI | Tkinter | Ships with Python, keeps the PyInstaller build small and dependency-free. |

## Project layout

```
src/meeting_scribe/
  config.py          # data directory, API keys, per-project paths
  session.py          # orchestrates one meeting: start/stop recording, merge, notes, save
  audio/recorder.py   # mic + WASAPI loopback capture (Windows-only at runtime)
  screen/capture.py   # periodic screenshot + OCR, deduplicated, optionally scoped to one window
  screen/window_picker.py  # enumerates open windows so one can be picked as the OCR target
  transcription/engine.py  # faster-whisper wrapper, merges audio + screen text by timestamp
  ai/notes.py          # Claude call that turns a transcript into notes + action items
  ai/search.py          # Ask-a-question-about-a-project flow (retrieve + Claude synthesis)
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
- **Meeting** — one recorded session: raw audio files, the merged transcript, generated notes.
- **Transcript segment** — a timestamped line from either `mic`, `system`, or `screen_ocr`, tied to a
  meeting.
- **Document** — any uploaded file, optionally tied to a specific meeting (e.g. that meeting's invite) or
  just to the project in general (e.g. a spec doc).

All of the above are indexed in SQLite FTS5 so `ai/search.py` can pull the most relevant passages for a
question before handing them to Claude.

## Setup (development)

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -r requirements.txt
python -m meeting_scribe.main       # launches the GUI by default (equivalent to `... main.py gui`)
```

Set your Anthropic API key and pick a model in the app's **Settings** tab — no environment variable
needed. It's saved to `settings.json` in the app's data directory and takes effect immediately. (Setting
`ANTHROPIC_API_KEY` / `MEETING_SCRIBE_MODEL` in the environment still works too, e.g. for scripted/CLI
use, but a value saved via the Settings tab takes precedence.) Recording, transcription, and screen OCR
all work with no key configured — only note generation and "Ask" need one.

The Settings tab also has the **notes system prompt** sent to Claude alongside every transcript when a
meeting finishes. It ships prepopulated with a recommended default that tells Claude what it's receiving
(a merged, automated transcript from mic audio, system audio, and screen OCR — timestamped but imperfect)
and how the output is used (saved as the meeting's permanent record, later retrieved to answer questions
across a project), plus the section structure the app expects back. Edit it to change tone, sections, or
detail level; "Reset to recommended default" restores the original text.

For development, install [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki) and make sure
`tesseract.exe` is on `PATH` (or point `MEETING_SCRIBE_TESSERACT_PATH` at it). The packaged `.exe` (below)
bundles its own copy, so end users running the built app don't need to install Tesseract at all.

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

This is the initial scaffold: the storage layer, document ingestion, transcript merging, notes generation,
and project search are implemented and unit-tested. The audio and screen-capture modules are implemented
against the Windows-only APIs they depend on (WASAPI loopback, live screenshots) and are not testable in a
headless Linux CI/dev container — they need to be exercised on an actual Windows machine. The GUI wires
everything together but likewise needs a Windows desktop session to click through.

## Roadmap / open decisions

- Swap FTS5 keyword search for embeddings if recall becomes a problem on large projects.
- Speaker diarization (who said what) — faster-whisper alone doesn't separate speakers; mic vs. system
  audio gives a coarse "you" vs. "everyone else" split today.
- Auto-detect meeting start (e.g. when Teams/Zoom is foregrounded) instead of a manual start button.
- A selected window that's moved to another monitor still captures correctly (bounds are re-read every
  cycle), but there's no UI feedback yet if the selected window closes mid-meeting — it just silently
  stops contributing screen text for the rest of the meeting.
