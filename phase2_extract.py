#!/usr/bin/env python3
"""Phase 2 - Extraction.

Reads a Phase 1 discovery run and writes transitions.json: for every
transition you snapshotted, the two tracks and where they overlap.

    python phase2_extract.py                      # newest run
    python phase2_extract.py output/discovery/<run>

This is offline and re-runnable; it only reads the capture folder.

Each transition gets:

    from_track / to_track   title, artists, BPM, Camelot key, and - when the
                            capture included the track metadata - the Spotify
                            id, duration and ISRC
    out_point_ms            where the outgoing track starts fading
    in_point_ms             where the incoming track comes in
    overlap_ms              how long the two run together

The numbers come from the mix editor's own sliders. One transition in the
capture can be cross-checked against the player's reported fade values, and
this script prints whether they agree (see src/transitions.py).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from src.config import ConfigError, load_config
from src.logs import setup_logging
from src.ingredients import SLOTS as ING_SLOTS
from src.transitions import ExtractError, extract

log = logging.getLogger("src.extract")


def newest_run(output_dir: Path) -> Path:
    runs = sorted((output_dir / "discovery").glob("*/"), key=lambda p: p.name)
    if not runs:
        raise ExtractError(f"No discovery runs under {output_dir / 'discovery'}. "
                           "Run phase1_discover.py first.")
    return runs[-1]


def write_cue_sheet(path: Path, transitions: list, report: dict) -> None:
    """A human-readable running order, for checking against the app by eye."""
    lines = [f"# Mix transitions - {report['run']}", ""]
    lines.append(f"{len(transitions)} transitions, "
                 f"{report['track_refs_resolved']}/{report['track_refs_total']} tracks resolved "
                 "to Spotify ids.")
    v = report.get("verification") or {}
    if v.get("checked"):
        lines.append(f"Cross-check against the player's own fade values: "
                     f"{'AGREES' if v.get('agrees') else 'DISAGREES'} ({v.get('detail')}).")
    lines.append("")
    for t in transitions:
        a, b = t.from_track, t.to_track
        lines.append(f"## {t.index + 1:02d}. {a.label()}  ->  {b.label()}")
        lines.append(f"    out point : {_ms(t.out_point_ms)} into \"{a.title}\"")
        lines.append(f"    in point  : {_ms(t.in_point_ms)} into \"{b.title}\"")
        lines.append(f"    overlap   : {_ms(t.overlap_ms)}")
        bpm = f"{a.bpm or '?'} -> {b.bpm or '?'} BPM"
        key = f"{a.camelot or '?'} -> {b.camelot or '?'}"
        lines.append(f"    bpm / key : {bpm}   |   {key}")
        if t.mode:
            lines.append(f"    mode      : {t.mode}")
        for slot in ING_SLOTS:
            d = (t.ingredients or {}).get(slot) or {}
            if d.get("off") or not d.get("raw"):
                continue
            shown = d.get("value") or d["raw"]
            if slot == "loop" and t.loop_length_ms():
                shown += f"  ({t.loop_length_ms()} ms at {b.bpm} BPM)"
            lines.append(f"    {slot:<10}: {shown}")
        if a.uri or b.uri:
            lines.append(f"    uris      : {a.uri or '(unresolved)'}  ->  {b.uri or '(unresolved)'}")
        lines.append("")

    gt = report.get("ground_truth") or {}
    summary = gt.get("automation_summary")
    if summary:
        lines.append("## How Spotify actually performs the blend")
        lines.append("")
        lines.append("Captured from the player for "
                     f"{gt.get('from_uri', '?')} -> {gt.get('to_uri', '?')}. Spotify only "
                     "reports these curves for the transition that is playing, so this is the "
                     "one transition in the run that was previewed. Play through the whole mix "
                     "while Phase 1 records to capture the rest.")
        lines.append("")
        lines.append("```")
        lines.extend(summary)
        lines.append("```")
        lines.append("")
        lines.append("EQ values are knob positions: 0.5 is the centre detent, 0 is a full kill. "
                     "rekordbox XML has no field for volume or EQ automation, so this is a "
                     "recipe to perform, not something the import can apply for you.")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _ms(v: int | None) -> str:
    if v is None:
        return "?"
    sign = "-" if v < 0 else ""
    v = abs(v)
    return f"{sign}{v // 60000}:{v // 1000 % 60:02d}.{v % 1000:03d}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", nargs="?", help="a Phase 1 run folder (default: the newest)")
    ap.add_argument("--config", default="config.yaml")
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

    setup_logging(run_dir / "extract.log", verbose=args.verbose)
    log.info("Extracting transitions from %s", run_dir)

    try:
        transitions, report = extract(run_dir)
    except ExtractError as e:
        log.error("%s", e)
        return 1

    out = run_dir / "transitions.json"
    out.write_text(json.dumps({
        "report": report,
        "transitions": [t.to_dict() for t in transitions],
    }, indent=1, ensure_ascii=False), encoding="utf-8")
    write_cue_sheet(run_dir / "cue_sheet.md", transitions, report)

    log.info("Snapshots: %d (%d had no editor open)", report["snapshots"],
             len(report["snapshots_without_editor"]))
    log.info("Transitions: %d distinct", report["transitions"])
    log.info("Track references resolved to Spotify ids: %d/%d",
             report["track_refs_resolved"], report["track_refs_total"])

    if report.get("order_source"):
        log.info("Running order taken from %s.", report["order_source"])

    with_curves = report.get("transitions_with_player_curves") or 0
    captured = report.get("player_curve_pairs_captured") or 0
    if with_curves:
        log.info("The player's own automation curves were captured for %d of %d transition(s). "
                 "Those render exactly; the rest are rebuilt from the named settings.",
                 with_curves, report["transitions"])
    elif captured:
        log.warning("Automation curves were captured for %d transition(s), but none of them "
                    "belong to this playlist - something else was playing. To capture this "
                    "playlist's curves, play it: python phase1_discover.py --play", captured)
    else:
        log.warning("No automation curves in this run, so every transition is rebuilt from its "
                    "named setting rather than reproduced exactly. Spotify only reports the "
                    "curves while a mixed playlist is PLAYING (previewing in the editor does "
                    "not do it): python phase1_discover.py --play")

    for d in report.get("dropped_not_on_chain", []):
        log.warning("DROPPED %s: %s -> %s (out %s ms) - not part of the mix's running "
                    "order. The editor keeps showing the last transition it rendered, so a "
                    "snapshot taken before opening a real one records a pair that was never "
                    "in the mix.",
                    d["snapshot"], d["from"], d["to"], d["out_point_ms"])

    if report.get("capture_complete"):
        total = report.get("transitions_on_page") or report["transitions"]
        log.info("Capture is complete: all %d transition(s) the mix has were captured.", total)
    elif report.get("transitions_missing"):
        log.warning("The mix has %d transition(s) but only %d were captured - %d missing. "
                    "Re-run the walk: python phase1_discover.py --auto",
                    report["transitions_on_page"], report["transitions"],
                    report["transitions_missing"])

    v = report["verification"]
    if v["checked"] and v["agrees"]:
        log.info("Cross-check PASSED against the player's own fade values (%s)", v["detail"])
    elif v["checked"]:
        log.warning("Cross-check FAILED: %s", v["detail"])
        log.warning("The slider reading may be wrong for this Spotify build. Treat the numbers "
                    "as unverified and compare a couple against the app by hand.")
    else:
        log.warning("No cluster response in this run, so the numbers could not be cross-checked "
                    "against the player. Preview a transition during discovery to capture one.")

    missing = [t.index + 1 for t in transitions
               if t.out_point_ms is None or t.in_point_ms is None or t.overlap_ms is None]
    if missing:
        log.warning("Incomplete slider data for transition(s): %s", missing)

    log.info("Written: %s", out)
    log.info("Written: %s", run_dir / "cue_sheet.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
