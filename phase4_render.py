#!/usr/bin/env python3
"""Phase 4 - render the mix to one continuous audio file.

    python phase4_render.py
    python phase4_render.py output/discovery/<run> -o "my set.mp3"

This is the answer to "I just want the playlist to play". Phase 3 writes cue
points, which mark where every blend goes but still need someone to perform
them, because rekordbox XML cannot carry mixer automation. This does the
mixing here instead and hands you a finished set: one long track with the
blends already in it.

The result plays in anything. Load it into rekordbox as a single track if you
want to run a controller over the top, add your own effects, loop a section or
drop out of it - but the mix itself is already done and nothing has to be
timed by hand.

What it reproduces
------------------
Out points, in points and overlap lengths exactly as captured, plus the
Volume, EQ and Filter moves of each transition. Where the capture caught the
player's own automation curves those are used directly; otherwise the move is
rebuilt from the named setting ("Centre bass swap" and so on), which the
capture has for every transition.

The Effects slot - reverb and echo tails - is not reproduced. Those transitions
still blend correctly, just without the tail, and the run says which ones.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from src.config import ConfigError, load_config
from src.logs import setup_logging
from src.rekordbox import build_entries, scan_music_dirs, _track_key
from src.render import RenderError, normalize, render, write
from src.transitions import ExtractError

from phase3_rekordbox import load_transitions, newest_run

log = logging.getLogger("src.render.cli")


def _hms(ms: float) -> str:
    s = int(ms / 1000)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", nargs="?", help="Phase 1/2 run folder (default: the newest)")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--music-dir", action="append", default=[], metavar="DIR",
                    help="folder of your audio files (default: paths.music_dir)")
    ap.add_argument("-o", "--output", default=None, metavar="PATH",
                    help="where to write the mix (default: <run>/mix.mp3). The extension "
                         "picks the format: .mp3, .wav or .flac.")
    ap.add_argument("--no-align", action="store_true",
                    help="do not shift cues onto files that start with extra silence")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    try:
        run_dir = Path(args.run_dir).resolve() if args.run_dir else newest_run(cfg.output_dir)
    except ExtractError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    setup_logging(run_dir / "render.log", verbose=args.verbose)

    try:
        transitions = load_transitions(run_dir / "transitions.json")
    except ExtractError as e:
        log.error("%s", e)
        log.error("Run: python phase2_extract.py %s", run_dir)
        return 1

    music_dirs = [Path(d).expanduser() for d in args.music_dir]
    if not music_dirs and cfg.music_dir:
        music_dirs = [cfg.music_dir]
    if not music_dirs:
        log.error("No music directory. Set paths.music_dir in config.yaml or pass --music-dir. "
                  "Rendering needs the actual audio, not just the cue positions.")
        return 2

    files = scan_music_dirs(music_dirs)
    if not files:
        log.error("No audio files found in %s.", ", ".join(str(d) for d in music_dirs))
        return 1

    entries = build_entries(transitions, files, auto_align=not args.no_align)
    by_key = {_track_key(e.title, e.spotify_id): e for e in entries}

    def entry_for(track):
        return by_key.get(_track_key(track.title, track.spotify_id))

    def progress(i, n, title):
        log.info("  [%2d/%d] %s", i, n, title)

    log.info("Rendering %d transition(s) over %d track(s)...",
             len(transitions), len(entries))
    started = time.time()
    try:
        mix, report = render(transitions, entry_for, progress=progress)
    except RenderError as e:
        log.error("%s", e)
        return 1

    mix, peak = normalize(mix)
    out_path = Path(args.output).resolve() if args.output else run_dir / "mix.mp3"
    write(mix, out_path)

    log.info("")
    log.info("Written: %s", out_path)
    log.info("Length:  %s", _hms(report.duration_ms))
    if peak > 0.97:
        log.info("Peak was %.2f before normalising, so the mix was turned down to fit.", peak)

    if report.effects_skipped:
        log.warning("%d transition(s) use an Effects setting, which is not rendered. They blend "
                    "correctly but without the tail: %s",
                    len(report.effects_skipped), ", ".join(report.effects_skipped))

    if report.wrong_edit:
        log.warning("")
        log.warning("%d track(s) are a DIFFERENT EDIT of the right song, so their blends land "
                    "on the wrong part of the music. This is audible and no render setting "
                    "fixes it - the audio has to be replaced:", len(report.wrong_edit))
        for title in report.wrong_edit:
            log.warning("   %s", title)
        log.warning("Run: python phase0_download.py --check-only")

    log.info("")
    log.info("Took %.0fs. Play it anywhere, or load it into rekordbox as one track "
             "to run a controller over the top.", time.time() - started)
    return 0


if __name__ == "__main__":
    sys.exit(main())
