"""Entry point: GUI by default (paths given on the command line - e.g. files dropped on the .exe
icon - are scanned right away); ``--cli PATH... [--out DIR] [--flac]`` runs headless."""

from __future__ import annotations

import os
import sys

USAGE = r"""Usage:
  EBL-to-WAV [PATH...]                           open the window (PATHs are scanned right away)
  EBL-to-WAV --cli PATH... [--out DIR] [--flac]  convert without a window

PATH: .exb / .ebl files, folders containing them, or .zip / .7z archives.
DIR:  output folder (default: Documents\Samples); one sub-folder per bank.
--flac: write FLAC instead of WAV (smaller, but loop points are only kept in WAV)."""


def _cli(argv: list[str]) -> int:
    from . import engine

    destination, fmt = engine.DEFAULT_DESTINATION, "wav"
    paths = []
    it = iter(argv)
    for a in it:
        if a == "--out":
            destination = next(it, destination)
        elif a == "--flac":
            fmt = "flac"
        elif not a.startswith("--"):
            paths.append(a)
    out = sys.stdout or open(os.devnull, "w", encoding="utf-8")  # the .exe has no console
    if not paths:
        print(USAGE, file=out)
        return 2
    results = engine.run_blocking(paths, destination, fmt,
                                  lambda r: print(f"[{r['status']}] {r['dst']} ({r['size']} bytes) "
                                                  f"{r['message']}", file=out, flush=True))
    return 1 if not results or any(r["status"] == "error" for r in results) else 0


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] in ("-h", "--help", "--version"):
        from . import APP_NAME, __version__
        out = sys.stdout or open(os.devnull, "w", encoding="utf-8")
        print(f"{APP_NAME} {__version__}" if argv[0] == "--version" else USAGE, file=out)
        return
    if "--cli" in argv:
        sys.exit(_cli([a for a in argv if a != "--cli"]))
    from .gui import main as gui_main
    gui_main(argv)


if __name__ == "__main__":
    main()
