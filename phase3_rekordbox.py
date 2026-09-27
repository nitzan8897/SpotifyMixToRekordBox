#!/usr/bin/env python3
"""Phase 3 - rekordbox playlist.

Turns transitions.json (from phase2_extract.py) into a rekordbox XML file
holding a collection and one playlist, with a memory cue at every mix point.

    python phase3_rekordbox.py --music-dir "D:/Music"
    python phase3_rekordbox.py output/discovery/<run> --music-dir D:/Music --hot-cues

Read this before you run it
---------------------------
**rekordbox cannot play Spotify tracks.** Pioneer removed Spotify support in
2020, and rekordbox XML addresses tracks by local file path. So this builds a
playlist over *your own audio files*: the Spotify capture supplies the running
order, the BPM, the key and the cue positions, and `--music-dir` supplies the
audio.

Without `--music-dir`, the XML is still written, but every track points at a
path under SPOTIFY_TRACK_NOT_FOUND_LOCALLY/ and rekordbox will show them as
missing files. That is useful to inspect the cues, not to play.

Importing
---------
rekordbox 7: File -> Import -> Import Playlist, or set the XML as the
"Imported Library" under Preferences -> View -> Layout -> rekordbox xml, then
drag the playlist into your collection. Importing never touches master.db
directly; rekordbox does its own import.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from src.config import ConfigError, load_config
from src.logs import setup_logging
from src.rekordbox import (MISSING_DIR, build_entries, build_xml, playlist_order,
                           scan_music_dirs)
from src.automix import automation_from_dict
from src.transitions import ExtractError, Track, Transition

log = logging.getLogger("src.rekordbox.cli")


def load_transitions(path: Path) -> list[Transition]:
    """Rebuild Transition objects from transitions.json."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ExtractError(f"Could not read {path}: {e}") from e

    out = []
    for row in data.get("transitions", []):
        def track(d: dict) -> Track:
            return Track(title=d.get("title"), artists=list(d.get("artists") or []),
                         bpm=d.get("bpm"), camelot=d.get("camelot"),
                         image_id=d.get("image_id"), spotify_id=d.get("spotify_id"),
                         duration_ms=d.get("duration_ms"), isrc=d.get("isrc"))
        out.append(Transition(
            index=row.get("index", len(out)), snapshot=row.get("snapshot", ""),
            from_track=track(row.get("from_track") or {}),
            to_track=track(row.get("to_track") or {}),
            out_point_ms=row.get("out_point_ms"), in_point_ms=row.get("in_point_ms"),
            overlap_ms=row.get("overlap_ms"),
            # Carries the Loop ingredient's beat count, which sets the loop
            # length written for this transition.
            ingredients=row.get("ingredients") or {},
            # The player's own curves, when the capture caught them. The
            # renderer prefers these over the ingredient names.
            automation=automation_from_dict(row.get("automation")),
        ))
    if not out:
        raise ExtractError(f"{path} contains no transitions. Re-run phase2_extract.py.")
    return out


def newest_run(output_dir: Path) -> Path:
    runs = sorted((output_dir / "discovery").glob("*/"), key=lambda p: p.name)
    if not runs:
        raise ExtractError(f"No discovery runs under {output_dir / 'discovery'}.")
    return runs[-1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", nargs="?", help="Phase 1/2 run folder (default: the newest)")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--music-dir", action="append", default=[], metavar="DIR",
                    help="folder of your audio files; repeat for more than one. "
                         "Defaults to paths.music_dir from config.yaml.")
    ap.add_argument("--playlist-name", default=None,
                    help="name of the playlist inside rekordbox (default: Spotify Mix <run>)")
    ap.add_argument("--hot-cues", action="store_true",
                    help="write the first 8 cues per track as hot cues (pads A-H) "
                         "instead of memory cues")
    ap.add_argument("--grid", action="store_true",
                    help="also write a beatgrid from Spotify's BPM. Off by default: it assumes "
                         "beat 1 sits at 0.000s at a constant BPM, which is wrong for most files, "
                         "and rekordbox trusts an imported grid instead of analyzing. Cue "
                         "positions do not depend on it.")
    ap.add_argument("--no-align", action="store_true",
                    help="do not measure the local files or shift cues onto them. By default "
                         "each file's leading silence is measured and the cues are moved so "
                         "they land on the music Spotify measured them from.")
    ap.add_argument("-o", "--output", default=None, metavar="PATH",
                    help="where to write the XML (default: <run>/rekordbox.xml)")
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

    setup_logging(run_dir / "rekordbox.log", verbose=args.verbose)

    try:
        transitions = load_transitions(run_dir / "transitions.json")
    except ExtractError as e:
        log.error("%s", e)
        log.error("Run: python phase2_extract.py %s", run_dir)
        return 1

    music_dirs = [Path(d).expanduser() for d in args.music_dir]
    if not music_dirs and cfg.music_dir:
        music_dirs = [cfg.music_dir]
        log.info("Using paths.music_dir from %s: %s", cfg.path.name, cfg.music_dir)
    files = scan_music_dirs(music_dirs) if music_dirs else []
    if music_dirs and not files:
        log.warning("No audio files found in the given music directory(ies).")

    entries = build_entries(transitions, files, auto_align=not args.no_align)
    order = playlist_order(transitions, entries)
    name = args.playlist_name or f"Spotify Mix {run_dir.name}"
    tree = build_xml(entries, order, name, hot_cues=args.hot_cues, write_grid=args.grid)

    out_path = Path(args.output).resolve() if args.output else run_dir / "rekordbox.xml"
    tree.write(out_path, encoding="UTF-8", xml_declaration=True)

    matched = [e for e in entries if e.local_path]
    unmatched = [e for e in entries if not e.local_path]
    total_cues = sum(len(e.cues) for e in entries)

    log.info("Transitions: %d", len(transitions))
    log.info("Tracks: %d distinct, %d playlist entries", len(entries), len(order))
    log.info("Cues written: %d (%s)", total_cues, "hot cues" if args.hot_cues else "memory cues")
    log.info("Written: %s", out_path)

    if not music_dirs:
        log.warning("No music directory configured, so every track points at %s/ and rekordbox will show "
                    "them as missing files. rekordbox cannot play Spotify tracks; pass a folder "
                    "of your own audio to get a playable playlist.", MISSING_DIR)
    else:
        log.info("Matched to local files: %d/%d", len(matched), len(entries))

        wrong_edit = [e for e in entries if e.is_different_edit()]
        if wrong_edit:
            log.warning("")
            log.warning("%d track(s) matched a local file that is a DIFFERENT EDIT of the song. "
                        "Every cue in this XML is an absolute offset into Spotify's timeline, so "
                        "on these files the cues do not land on the music they were measured "
                        "from. This is the single biggest cause of a mix that feels out of time:",
                        len(wrong_edit))
            for e in sorted(wrong_edit, key=lambda e: -abs(e.duration_delta_ms or 0)):
                log.warning("   %+7d ms  %s - %s", e.duration_delta_ms, e.title, e.artist_string)
                log.warning("             %s", e.local_path)
            log.warning("Re-download these from the exact Spotify track, or pin the audio source "
                        "by hand. spotdl takes 'youtube_url|spotify_url' to force which recording "
                        "it fetches; downloading by Spotify URL alone only fixes the tags, not "
                        "which upload the audio came from.")
            log.warning("")

        shifted = [e for e in entries if e.offset_ms]
        if shifted:
            log.info("")
            log.info("%d track(s) start with silence that Spotify's copy does not have. Their "
                     "cues were shifted onto the music automatically, and a 'spotify 0:00' "
                     "marker records where the shift puts Spotify's start:", len(shifted))
            for e in sorted(shifted, key=lambda e: -e.offset_ms):
                log.info("   +%5d ms  %s", e.offset_ms, e.title)
            log.info("")

        undecodable = [e for e in entries
                       if e.alignment is not None and e.alignment.probe is None]
        if undecodable:
            log.warning("Could not decode %d file(s) to check their alignment. Install soundfile "
                        "(pip install soundfile) for automatic silence detection.",
                        len(undecodable))

        drifting = [e for e in entries
                    if e.duration_delta_ms is not None and not e.is_different_edit()
                    and abs(e.duration_delta_ms) > 250]
        if drifting:
            log.info("%d track(s) are the right recording but off by 250-1500 ms (encoder "
                     "padding or a trimmed tail). Cues may sit a beat fraction early or late "
                     "on these; nudge them in rekordbox if it bothers you.", len(drifting))

        if unmatched:
            log.warning("No local file for %d track(s):", len(unmatched))
            for e in unmatched:
                log.warning("   %s - %s", e.title, e.artist_string)
            log.warning("Those entries point under %s/. Add the files and re-run, or fix the "
                        "Location attributes by hand.", MISSING_DIR)

    return 0 if (not music_dirs or matched) else 1


if __name__ == "__main__":
    sys.exit(main())
