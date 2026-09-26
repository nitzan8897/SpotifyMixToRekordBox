#!/usr/bin/env python3
"""Phase 0 - fetch the audio the mix is built from.

rekordbox cannot play Spotify tracks, so the mix has to be rebuilt over local
files. This downloads one file per track in the mix, using spotdl.

    python phase0_download.py                      # newest run
    python phase0_download.py output/discovery/<run>
    python phase0_download.py --only "Party Funk" --only "Ela ke Leitada"

The track list comes from the run's transitions.json, so this has to run
*after* Phase 2. Nothing is hardcoded: the URLs are the ones the capture
resolved, and the destination is paths.music_dir from config.yaml.

The thing to know about spotdl
-----------------------------
A Spotify URL tells spotdl which track's *tags* to write. It does not tell it
which audio to fetch - spotdl searches YouTube and picks a result. That search
regularly lands on a different edit of the right song: a sped-up version, an
extended mix, a re-upload with a long intro.

That matters more here than in a normal download job, because every cue this
project writes is an absolute offset into Spotify's timeline. A file whose
music is arranged differently puts every cue in the wrong place, and no amount
of care elsewhere in the pipeline can recover it.

So this script checks what it got. After downloading it compares each file's
length against Spotify's and reports the ones that disagree. To force the
audio for a stubborn track, pass the pairing form to spotdl by hand:

    python -m spotdl download "https://youtu.be/<id>|https://open.spotify.com/track/<id>"

The left side fixes which recording is fetched; the right side still supplies
the tags.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from src.align import CONTENT_TOLERANCE_MS
from src.config import ConfigError, load_config
from src.rekordbox import DURATION_TOLERANCE_MS, fold, read_duration_ms

#: Audio format and bitrate to ask spotdl for.
FORMAT = "mp3"
BITRATE = "320k"


def newest_run(output_dir: Path) -> Path:
    runs = sorted((output_dir / "discovery").glob("*/"), key=lambda p: p.name)
    if not runs:
        raise SystemExit(f"No discovery runs under {output_dir / 'discovery'}. "
                         "Run phase1_discover.py then phase2_extract.py first.")
    return runs[-1]


def tracks_in_mix(run_dir: Path) -> list[dict]:
    """Every distinct track of the mix, in running order, from transitions.json.

    The running order is the outgoing track of each transition followed by the
    final incoming one - the same rule Phase 3 uses to build the playlist.
    """
    path = run_dir / "transitions.json"
    if not path.is_file():
        raise SystemExit(f"No transitions.json in {run_dir}. Run phase2_extract.py first.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SystemExit(f"Could not read {path}: {e}") from e

    transitions = data.get("transitions") or []
    if not transitions:
        raise SystemExit(f"{path} lists no transitions.")

    ordered = [t["from_track"] for t in transitions] + [transitions[-1]["to_track"]]
    out, seen = [], set()
    for t in ordered:
        key = t.get("spotify_id") or fold(t.get("title"))
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


def describe(track: dict) -> str:
    who = ", ".join(track.get("artists") or []) or "?"
    return f"{track.get('title') or '?'} - {who}"


def check_downloads(tracks: list[dict], music_dir: Path) -> list[tuple[dict, int | None, int | None]]:
    """Compare what landed on disk against what Spotify says each track is.

    Returns one row per track: ``(track, file_ms, delta_ms)``. ``file_ms`` is
    None when no file could be matched to it at all.
    """
    from src.rekordbox import match_local_file, scan_music_dirs

    files = scan_music_dirs([music_dir])
    rows = []
    for t in tracks:
        path, delta = match_local_file(t.get("title"), t.get("artists") or [],
                                       files, t.get("duration_ms"))
        rows.append((t, read_duration_ms(path) if path else None, delta))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", nargs="?", help="Phase 1/2 run folder (default: the newest)")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--music-dir", metavar="DIR",
                    help="where to download to (default: paths.music_dir from config.yaml)")
    ap.add_argument("--only", action="append", default=[], metavar="TITLE",
                    help="download just the tracks whose title contains this; repeat to add "
                         "more. Use it to replace the handful that came back wrong.")
    ap.add_argument("--check-only", action="store_true",
                    help="download nothing; just report which local files disagree with "
                         "Spotify's durations")
    args = ap.parse_args()

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    music_dir = Path(args.music_dir).expanduser() if args.music_dir else cfg.music_dir
    if not music_dir:
        print("ERROR: no music directory. Set paths.music_dir in config.yaml or pass "
              "--music-dir.", file=sys.stderr)
        return 2

    run_dir = Path(args.run_dir).resolve() if args.run_dir else newest_run(cfg.output_dir)
    tracks = tracks_in_mix(run_dir)

    if args.only:
        wanted = [fold(s) for s in args.only]
        tracks = [t for t in tracks
                  if any(w and w in fold(t.get("title")) for w in wanted)]
        if not tracks:
            print(f"ERROR: --only matched none of the tracks in {run_dir.name}.",
                  file=sys.stderr)
            return 2

    print(f"Run:       {run_dir.name}")
    print(f"Tracks:    {len(tracks)}")
    print(f"Music dir: {music_dir}")
    print("=" * 70)

    if not args.check_only:
        urls = [f"https://open.spotify.com/track/{t['spotify_id']}"
                for t in tracks if t.get("spotify_id")]
        missing = [describe(t) for t in tracks if not t.get("spotify_id")]
        if missing:
            print(f"{len(missing)} track(s) have no Spotify id in the capture and cannot be "
                  "downloaded by URL:")
            for m in missing:
                print(f"   {m}")
            print()
        if not urls:
            print("ERROR: no downloadable tracks.", file=sys.stderr)
            return 1

        music_dir.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, "-m", "spotdl", "download", *urls,
               "--output", str(music_dir), "--format", FORMAT, "--bitrate", BITRATE]
        print(f"Downloading {len(urls)} track(s) with spotdl...")
        result = subprocess.run(cmd)
        if result.returncode != 0:
            print(f"\nspotdl exited {result.returncode}. Checking what did land anyway.",
                  file=sys.stderr)
        print("=" * 70)

    print("Checking each file against Spotify's duration...")
    rows = check_downloads(tracks, music_dir)
    wrong, absent, fine = [], [], 0
    for t, file_ms, delta in rows:
        if file_ms is None:
            absent.append(t)
        elif delta is not None and abs(delta) > DURATION_TOLERANCE_MS:
            wrong.append((t, delta))
        else:
            fine += 1

    print(f"   {fine} file(s) match Spotify's length")
    if absent:
        print(f"   {len(absent)} track(s) have no local file:")
        for t in absent:
            print(f"      {describe(t)}")
    if wrong:
        wrong.sort(key=lambda r: -abs(r[1]))
        print(f"   {len(wrong)} file(s) are a DIFFERENT EDIT of the right song:")
        for t, delta in wrong:
            print(f"      {delta:+7d} ms   {describe(t)}")
        print()
        print("   Every cue is an absolute offset into Spotify's timeline, so on these")
        print("   files the cues land on the wrong music. Re-fetch them with the audio")
        print("   source pinned, one at a time:")
        for t, _ in wrong[:3]:
            sid = t.get("spotify_id") or "<id>"
            print(f'      python -m spotdl download "<youtube url>|'
                  f'https://open.spotify.com/track/{sid}"')
        print(f"   (a difference under {CONTENT_TOLERANCE_MS} ms is usually just encoder "
              "padding and is fine)")

    return 1 if (wrong or absent) else 0


if __name__ == "__main__":
    sys.exit(main())
