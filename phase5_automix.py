#!/usr/bin/env python3
"""Phase 5 - trim every song to its Spotify mix points, for rekordbox Automix.

    python phase5_automix.py
    python phase5_automix.py "D:/DOK-transfer/Hype Driving" --music-dir ".../music"

Writes ``<run>/automix/``: one file per song, numbered in playing order, each
cut to exactly the span the Spotify mix plays of it - from the point it comes
in to the end of its overlap into the next song. Nothing is blended in. Every
file holds one song, at full volume, so both songs of a transition are really
there for rekordbox to mix.

Why trimming and not phrase cues
--------------------------------
rekordbox Automix does not read memory cues, and its phrase analysis (Intro,
Outro, ...) lives in binary ANLZ files it writes itself; rekordbox XML has no
field for either. What Automix *does* honour is where a file starts and ends:
it begins each track at 0:00 and crossfades over the end of the outgoing one.
So the file edges are made to be Spotify's mix points.

Two cues go on each trimmed file in ``<run>/automix/rekordbox.xml``:

    "MIX IN"   at 0:00                      - where Spotify brings it in
    "MIX OUT"  overlap seconds before end   - where Spotify starts the blend

Those are the MIX POINT LINK points too (Performance mode), which plays the
next deck from MIX IN exactly as this deck reaches MIX OUT.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from src.config import ConfigError, load_config
from src.logs import setup_logging
from src.rekordbox import Cue, Entry, build_entries, build_xml, scan_music_dirs, _track_key
from src.render import (TARGET_RATE, Piece, RenderError, db_to_gain, load_audio,
                        loudness_trims, piece_filename, plan)
from src.transitions import ExtractError

from phase3_rekordbox import load_transitions, newest_run

log = logging.getLogger("src.automix.cli")

#: Fade at each cut, so a slice that starts mid-waveform does not click.
DECLICK_MS = 5


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", nargs="?",
                    help="folder holding transitions.json (default: the newest run)")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--music-dir", action="append", default=[], metavar="DIR",
                    help="folder of your audio files (default: paths.music_dir)")
    ap.add_argument("--format", default="flac", metavar="EXT", help="flac (default) or wav")
    ap.add_argument("--playlist-name", default=None,
                    help="playlist name inside rekordbox (default: <run folder> Automix)")
    ap.add_argument("--no-align", action="store_true",
                    help="do not shift cuts onto files that start with extra silence")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    try:
        cfg = load_config(args.config)
        run_dir = Path(args.run_dir).resolve() if args.run_dir else newest_run(cfg.output_dir)
    except (ConfigError, ExtractError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    out_dir = run_dir / "automix"
    out_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(out_dir / "automix.log", verbose=args.verbose)

    try:
        transitions = load_transitions(run_dir / "transitions.json")
    except ExtractError as e:
        log.error("%s", e)
        return 1

    music_dirs = [Path(d).expanduser() for d in args.music_dir] or (
        [cfg.music_dir] if cfg.music_dir else [])
    files = scan_music_dirs(music_dirs)
    if not files:
        log.error("No audio files found. Pass --music-dir.")
        return 1

    entries = build_entries(transitions, files, auto_align=not args.no_align)
    by_key = {_track_key(e.title, e.spotify_id): e for e in entries}
    segments, report = plan(transitions, lambda t: by_key.get(_track_key(t.title, t.spotify_id)))
    if report.missing:
        log.error("No local file for: %s", ", ".join(report.missing))
        return 1

    import numpy as np
    import soundfile as sf

    rate = TARGET_RATE
    trims = loudness_trims(segments, rate)
    suffix = "." + args.format.lower().lstrip(".")
    fade = int(DECLICK_MS / 1000 * rate)

    out_entries: list[Entry] = []
    for i, seg in enumerate(segments):
        log.info("  [%2d/%d] %s", i + 1, len(segments), seg.title)
        audio = load_audio(seg.path, rate)
        a = max(int(seg.start_ms / 1000 * rate), 0)
        b = min(int(seg.end_ms / 1000 * rate), len(audio))
        if b <= a:
            log.warning("Nothing to take from %s; skipping.", seg.title)
            continue
        body = audio[a:b].copy()
        del audio
        body *= db_to_gain(trims.get(str(seg.path), 0.0))
        if len(body) > 2 * fade:
            ramp = np.linspace(0.0, 1.0, fade)[:, None]
            body[:fade] *= ramp
            body[-fade:] *= ramp[::-1]
        peak = float(np.abs(body).max())
        if peak > 0.97:
            body *= 0.97 / peak

        length_s = len(body) / rate
        path = out_dir / piece_filename(Piece(i + 1, seg.title, 0, 0), suffix)
        sf.write(str(path), body, rate)

        src = next(e for e in entries if e.local_path == seg.path)
        cues = [Cue("MIX IN", 0.0)]
        if seg.overlap_ms:
            cues.append(Cue("MIX OUT", max(length_s - seg.overlap_ms / 1000, 0.0)))
        out_entries.append(Entry(
            track_id=i + 1, title=f"{i + 1:02d} {src.title}", artists=src.artists,
            bpm=src.bpm, camelot=src.camelot, duration_ms=int(length_s * 1000),
            spotify_id=src.spotify_id, local_path=path, cues=cues))

    name = args.playlist_name or f"{run_dir.name} Automix"
    tree = build_xml(out_entries, out_entries, name)
    xml_path = out_dir / "rekordbox.xml"
    tree.write(xml_path, encoding="UTF-8", xml_declaration=True)

    overlaps = sorted(s.overlap_ms for s in segments if s.overlap_ms)
    log.info("")
    log.info("Written %d file(s) and %s", len(out_entries), xml_path)
    if overlaps:
        log.info("Spotify overlaps: %.1f-%.1f s, median %.1f s. Set Automix's fade/crossfade "
                 "length near the median.", overlaps[0] / 1000, overlaps[-1] / 1000,
                 overlaps[len(overlaps) // 2] / 1000)
    if report.wrong_edit:
        log.warning("Different edit of the song, so cuts land off the music: %s",
                    ", ".join(report.wrong_edit))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RenderError as e:
        log.error("%s", e)
        sys.exit(1)
