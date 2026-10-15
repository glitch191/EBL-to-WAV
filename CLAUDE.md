# EBL-to-WAV

Windows desktop app that converts E-mu Emulator X samples (`.ebl`, usually in the `SamplePool`
folder of an `.exb` bank) into WAV or FLAC files. Sister project of Soundfont-to-XRNI (same owner):
the window, the scan-then-convert flow, archive unpacking, build and CI are taken from it.
Inspired by https://github.com/mattetti/e-mu-soundbanks (Go CLI, no licence file: do not copy its
code, the format facts are re-measured and documented in `ebl_to_wav/ebl.py`).

## Working with the owner

- The owner writes in French: answer in French. Everything inside the repository (code, comments,
  UI text, docs, file names) must be in English: no French anywhere in the tree.
- No em dashes in user-facing prose (README, release notes, forum posts). Use colons or commas.
- The owner is new to GitHub and publishes with GitHub Desktop (commit, push) and the github.com
  website (releases).
- Ask before destructive or outward-facing actions (deleting files, publishing).

## Layout

```
ebl_to_wav/
  __init__.py   APP_NAME, __version__ (single source of the version number)
  __main__.py   entry point: window by default, --cli for headless use, --help, --version
  ebl.py        .ebl reader (header layout documented at the top)
  engine.py     scan (files, folders, .exb, archives), bank naming, WAV/FLAC writer, manifest, runner
  gui.py        tkinter + tkinterdnd2 window (scan first, then Convert)
tests/          unittest suite; generates its own small .ebl files (tests/test_conversion.py: ebl_bytes)
app.py          PyInstaller entry point
build.py        builds dist/EBL-to-WAV.exe and the release .zip
make_icon.py    regenerates icon.ico and docs/icon.png (needs Pillow)
.github/workflows/build.yml   CI: tests on every push; on a v* tag, builds and attaches the .zip
```

## Commands

```
py -m pip install -r requirements.txt          # numpy, soundfile, tkinterdnd2, py7zr
py -m ebl_to_wav                               # run the window from source
py -m ebl_to_wav --cli PATH... [--out DIR] [--flac]
py -m unittest discover -s tests -v            # 18 tests (4 drive the real window, hidden)
py -m pip install -r requirements-build.txt    # + pyinstaller, pillow
py build.py                                    # dist/EBL-to-WAV.exe + dist/*-windows-x64.zip
```

Environment variables: `E2W_WORKERS=N` forces the thread count, `E2W_7ZIP=off` ignores an
installed 7-Zip. Settings (destination, format) live in `%APPDATA%\EBL-to-WAV\settings.json`.

## Architecture

1. Scan (`engine.collect_jobs`, in a thread from the GUI): walks dropped files, folders and
   archives (unpacked to temp folders), reads each `.ebl` header, returns one `Job` per sample.
   A dropped `.exb` means "its sibling SamplePool folder".
2. The GUI lists the jobs (New / Replace) and writes nothing until the user clicks Convert.
   Changing the destination or the format re-scans the pending files.
3. `Runner`: thread pool (up to 8), big samples first. `process_job` writes to `<dst>.tmp` and
   renames.
4. `finalize`: on a complete run, deletes files of an earlier run that were not rewritten (for
   example .wav after switching to FLAC), then updates the hidden manifest `.ebl-to-wav`.
   Files not in the manifest belong to the user and are never touched.

Bank name: the single `.exb` file next to the `SamplePool` folder, else the folder name, else the
archive / dropped folder name; a trailing `.exb` is stripped (banks are often shipped as a folder
or archive called `Name.exb`). Output: `<destination>/<bank>/<sample name>.<wav|flac>`.
Archives (owner's request): everything goes under `<destination>/<archive name>/` and keeps the
archive's folder layout (`engine._archive_tree`): a `<bank>.exb/SamplePool` pair becomes `<bank>`,
a leading folder that repeats the archive name is dropped, an archive inside an archive goes at its
place in the outer layout. `Job.root` (manifest, stale-file removal) is the archive folder; the
list's Folder column shows `Job.folder`. Dropped folders and .exb files are not affected.

## EBL facts (measured on 3,089 files: PROcussion, SP-1200, Vintage Keys, Proteus 2000)

- Header version byte at chunk body + 4: 1 (older, mono only) or 2.
- Positions are byte offsets from chunk body + 2; "end" and "loop end" are the last sample
  (inclusive). Loop end inclusive was checked by waveform continuity: 1,782 vs 170 loops.
- Stereo (version 2, ch2 start != ch1 start): channels are stored one after the other; 9 Proteus
  files have one padding sample between them (the Go tool reads them wrong).
- Many files end with 40 extra bytes inside the chunk: ignored.
- Loop on: flag 0x10000 in the field after the sample rate. Its low 16 bits vary (maybe a tune
  value): unknown, not used.
- Category string (UTF-16BE, "Piano", "Percussion"...) at body + 118, empty in older banks.
- The `.exb` bank (presets, key zones, root keys) is not parsed. WAV `smpl` unity note is 60.

## Conventions

- Version: `ebl_to_wav/__init__.py`, the CHANGELOG heading and the git tag (`vX.Y.Z`) must match.
- Never commit sound banks or audio (copyright); `.gitignore` excludes them.
- Keep personal data out of the repo: no user names, e-mail addresses or absolute user paths.
  `LICENSE` holds the owner's name on purpose.
- README screenshots: capture only the app window with `PrintWindow`, using synthetic demo banks
  (generated with `tests/test_conversion.py: ebl_bytes`).
- Match the existing code style: type hints, short docstrings, comments only where the reason
  is not obvious.
