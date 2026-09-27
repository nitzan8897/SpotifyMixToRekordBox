"""Rendering the mix to one continuous audio file.

Why this exists
---------------
rekordbox XML can carry cue points but not mixer automation, so an imported
playlist can only ever *mark* where the blends go - something still has to
perform them. That is fine if you want to DJ the set. It is useless if you
want the mix to simply play.

So this does the mixing here instead. It reads the same transition data the
rekordbox export uses - out point, in point, overlap, and the five ingredient
settings - applies the volume, EQ and filter moves across each overlap, and
writes one continuous track. The result plays anywhere, needs no controller
and no timing skill, and can be loaded into rekordbox as a single long track
if you want to play over the top of it.

Where the automation comes from
-------------------------------
Two sources, in order of preference:

1. The curves the player itself reported, when the transition was previewed
   during capture. Exact, and already decoded by :mod:`src.automix`.
2. The named ingredient, otherwise. "Centre bass swap" and the rest describe
   the move precisely enough to rebuild, and the capture has a name for every
   transition even when it has curves for none.

Both end up as the same thing: a gain envelope per band per side, sampled
across the overlap.

What is not reproduced
----------------------
The Effects slot - reverb and echo tails. Those need a reverb and a delay
line, and faking them badly is worse than leaving them out, so a transition
using one is rendered without it and the caller is told. In the capture this
was written against, 20 of 24 transitions used no effect at all.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("src.render")

#: Everything is resampled to this before mixing, so tracks of different rates
#: can be joined. 44.1k is what a DJ set is usually delivered at.
TARGET_RATE = 44100

#: Crossover points for the three EQ bands, in Hz. These match how a DJ mixer
#: splits low/mid/high closely enough for the bass swaps to sound right.
LOW_CROSSOVER_HZ = 250.0
HIGH_CROSSOVER_HZ = 4000.0

#: An EQ gain of 0.5 is the centre detent (unity). This maps the 0..1 knob
#: scale onto an amplitude multiplier: 0 kills the band, 0.5 leaves it alone,
#: 1.0 is +6 dB.
def knob_to_gain(v: float) -> float:
    """Turn a 0..1 EQ knob position into an amplitude multiplier."""
    if v <= 0.0:
        return 0.0
    return float(v) / 0.5 if v < 0.5 else 1.0 + (float(v) - 0.5) * 2.0


class RenderError(Exception):
    """The mix cannot be rendered from what is available."""


# --------------------------------------------------------------------------
# Audio plumbing
# --------------------------------------------------------------------------
from src.automix import BANDS, value_at


def _np():
    try:
        import numpy as np
    except ImportError as e:                                # pragma: no cover
        raise RenderError("numpy is required to render audio (pip install numpy)") from e
    return np


def load_audio(path: Path, rate: int = TARGET_RATE):
    """Read a file as stereo float32 at ``rate``.

    Resampling is linear, which is not hi-fi but is inaudible for the small
    rate corrections this needs (48k to 44.1k and the like).
    """
    np = _np()
    try:
        import soundfile as sf
    except ImportError as e:                                # pragma: no cover
        raise RenderError("soundfile is required to render audio "
                          "(pip install soundfile)") from e
    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    if data.shape[1] == 1:
        data = np.repeat(data, 2, axis=1)
    elif data.shape[1] > 2:
        data = data[:, :2]
    if sr != rate:
        n_out = int(round(len(data) * rate / sr))
        src = np.linspace(0.0, len(data) - 1, n_out, dtype=np.float64)
        idx = np.floor(src).astype(np.int64)
        frac = (src - idx).astype(np.float32)[:, None]
        nxt = np.minimum(idx + 1, len(data) - 1)
        data = data[idx] * (1.0 - frac) + data[nxt] * frac
    return np.ascontiguousarray(data, dtype=np.float32)


#: Filter order for the band split. Steep enough that a killed bass really
#: goes, shallow enough not to ring.
BAND_ORDER = 4


def _lowpass(x, cutoff_hz: float, rate: int):
    """Zero-phase low-pass.

    Zero-phase matters here because the three bands are summed back together:
    a band that came out phase-shifted would hollow out the sum instead of
    rebuilding it. filtfilt runs the filter both ways, which cancels the shift.
    """
    from scipy.signal import butter, filtfilt

    b, a = butter(BAND_ORDER, min(cutoff_hz / (rate / 2.0), 0.99), btype="low")
    # filtfilt needs a few times the filter length to settle.
    if len(x) <= 3 * max(len(a), len(b)):
        return x.copy()
    return filtfilt(b, a, x, axis=0).astype("float32")


def split_bands(x, rate: int = TARGET_RATE):
    """Split audio into (low, mid, high). The three sum back to the input."""
    try:
        import scipy.signal  # noqa: F401
    except ImportError as e:                                # pragma: no cover
        raise RenderError("scipy is required to render audio (pip install scipy)") from e
    low = _lowpass(x, LOW_CROSSOVER_HZ, rate)
    below_high = _lowpass(x, HIGH_CROSSOVER_HZ, rate)
    mid = below_high - low
    high = x - below_high
    return low, mid, high


# --------------------------------------------------------------------------
# Envelopes: what each knob does across one overlap
# --------------------------------------------------------------------------
@dataclass
class Move:
    """The automation for one side of one transition, as sampled envelopes.

    Each array runs over the overlap, one value per sample. ``volume`` is an
    amplitude multiplier; the band gains are too, already converted off the
    knob scale.
    """
    volume: object
    low: object
    mid: object
    high: object


def _ramp(np, n, start, end):
    return np.linspace(start, end, n, dtype=np.float32)


def _step(np, n, before, after, at=0.5):
    out = np.full(n, float(after), dtype=np.float32)
    out[:int(n * at)] = float(before)
    return out


def volume_envelopes(np, n: int, style: str | None):
    """(outgoing, incoming) volume across the overlap, by Volume setting."""
    fade_in = _ramp(np, n, 0.0, 1.0)
    fade_out = _ramp(np, n, 1.0, 0.0)
    full = np.ones(n, dtype=np.float32)

    if style == "overlap":
        # Both at full for the whole overlap, then the outgoing stops dead.
        return full.copy(), full.copy()
    if style == "fade in cut out":
        return full.copy(), fade_in
    if style == "smooth crossfade":
        # Equal power, so the sum does not dip in the middle.
        t = np.linspace(0.0, 1.0, n, dtype=np.float32)
        return np.cos(t * np.pi / 2).astype(np.float32), np.sin(t * np.pi / 2).astype(np.float32)
    if style in ("crossfade", "fade in fade out"):
        return fade_out, fade_in
    # "custom", or a name we do not know: equal power is the safe default.
    t = np.linspace(0.0, 1.0, n, dtype=np.float32)
    return np.cos(t * np.pi / 2).astype(np.float32), np.sin(t * np.pi / 2).astype(np.float32)


#: Where each named bass swap hands the low end over, as a fraction of the
#: overlap. These sit *inside* the overlap deliberately. Putting "start" at 0.0
#: and "end" at 1.0 looks natural and is useless: the swap then falls on the
#: boundary and never happens during the blend, which left the incoming track
#: with no bass for the whole overlap - the Tuf Tuf into YUMMI transition, 7.4
#: seconds of it, was audibly hollow because of exactly that.
SWAP_AT = {"start bass swap": 0.25, "centre bass swap": 0.5, "end bass swap": 0.75}

#: The cut Spotify applies to the non-bass bands during a 3-band fade: knob
#: 0.2, measured off the one transition where the player reported its curves.
BAND_CUT = 0.2


def eq_envelopes(np, n: int, style: str | None):
    """(outgoing, incoming) band gains across the overlap, by EQ setting.

    Grounded in the one transition whose real curves the capture caught. Two
    things that measurement settled, both counter to how the names read:

    * Spotify's "3-band fade" is not a fade. All three bands *step* at the
      midpoint - low from unity to kill on the way out and kill to unity on
      the way in, mid and high between unity and a 0.2 cut. So it is a
      three-band swap, and it is rendered as one.
    * The 0.2 cut belongs to that style. A bass swap only moves the low band;
      leaving the incoming mids and highs cut through the whole overlap made
      the new track sound distant for seconds at a time.
    """
    unity = np.ones(n, dtype=np.float32)
    kill = np.zeros(n, dtype=np.float32)
    cut = np.full(n, knob_to_gain(BAND_CUT), dtype=np.float32)

    if style in SWAP_AT:
        at = SWAP_AT[style]
        # Only the low end changes hands. Everything else stays where it is,
        # which is what makes this a bass swap rather than a full handover.
        return (
            Move(None, _step(np, n, 1.0, 0.0, at), unity.copy(), unity.copy()),
            Move(None, _step(np, n, 0.0, 1.0, at), unity.copy(), unity.copy()),
        )
    if style == "bass fade out":
        # The one style that really is a ramp: the outgoing bass rides down.
        return (
            Move(None, _ramp(np, n, 1.0, 0.0), unity.copy(), unity.copy()),
            Move(None, unity.copy(), unity.copy(), unity.copy()),
        )
    if style == "3 band fade":
        half = 0.5
        return (
            Move(None, _step(np, n, 1.0, 0.0, half),
                 _step(np, n, 1.0, knob_to_gain(BAND_CUT), half),
                 _step(np, n, 1.0, knob_to_gain(BAND_CUT), half)),
            Move(None, _step(np, n, 0.0, 1.0, half),
                 _step(np, n, knob_to_gain(BAND_CUT), 1.0, half),
                 _step(np, n, knob_to_gain(BAND_CUT), 1.0, half)),
        )
    return (Move(None, unity.copy(), unity.copy(), unity.copy()),
            Move(None, unity.copy(), unity.copy(), unity.copy()))


def apply_filter_setting(np, n: int, style: str | None, out_move: Move, in_move: Move) -> None:
    """Fold the Filter setting into the band gains, in place.

    A high-pass sweep is the low band coming out; a low-pass sweep is the high
    band coming out. Expressing them as band gains keeps everything in one
    mechanism instead of adding a second filter stage.
    """
    if not style:
        return
    for part in style.split(" + "):
        part = part.strip()
        side = in_move if part.endswith(" in") else out_move
        if part.startswith("high pass"):
            # High-pass: low end removed. "in" sweeps it back in on the
            # incoming side, "out" sweeps it away on the outgoing side.
            side.low = side.low * (_ramp(np, n, 0.0, 1.0) if part.endswith(" in")
                                   else _ramp(np, n, 1.0, 0.0))
        elif part.startswith("low pass"):
            side.high = side.high * (_ramp(np, n, 0.0, 1.0) if part.endswith(" in")
                                     else _ramp(np, n, 1.0, 0.0))


def curves_from_capture(np, n: int, automation) -> tuple[Move, Move] | None:
    """Envelopes sampled from the player's own reported curves, if present."""

    if not automation:
        return None
    pos = np.linspace(0.0, 1.0, n, dtype=np.float64)

    def side(s):
        if not s.volume:
            return None
        vol = np.array([value_at(s.volume, p) or 0.0 for p in pos], dtype=np.float32)
        bands = {}
        for b in BANDS:
            segs = s.eq.get(b)
            if not segs:
                bands[b] = np.ones(n, dtype=np.float32)
            else:
                bands[b] = np.array([knob_to_gain(value_at(segs, p) or 0.5) for p in pos],
                                    dtype=np.float32)
        return Move(vol, bands["low"], bands["mid"], bands["high"])

    out, inc = side(automation.outgoing), side(automation.incoming)
    return (out, inc) if out and inc else None


def build_moves(np, n: int, ingredients: dict, automation=None) -> tuple[Move, Move]:
    """The full automation for one transition, both sides."""
    captured = curves_from_capture(np, n, automation)
    if captured:
        return captured

    def value(slot):
        d = (ingredients or {}).get(slot) or {}
        return None if d.get("off") else d.get("value")

    out_move, in_move = eq_envelopes(np, n, value("eq"))
    out_vol, in_vol = volume_envelopes(np, n, value("volume"))
    out_move.volume, in_move.volume = out_vol, in_vol
    apply_filter_setting(np, n, value("filter"), out_move, in_move)
    return out_move, in_move


def apply_move(np, chunk, move: Move):
    """Apply one side's volume and band gains to a slice of audio."""
    low, mid, high = split_bands(chunk)
    mixed = (low * move.low[:, None]
             + mid * move.mid[:, None]
             + high * move.high[:, None])
    return mixed * move.volume[:, None]


# --------------------------------------------------------------------------
# Assembling the whole mix
# --------------------------------------------------------------------------
@dataclass
class Segment:
    """One track's place in the rendered mix."""
    title: str
    path: Path
    #: where this track's audio starts being used, in its own timeline (ms)
    start_ms: int
    #: where it stops, i.e. the end of the overlap into the next track (ms)
    end_ms: int
    #: length of the blend into the next track (ms); 0 on the last one
    overlap_ms: int
    #: position in the finished mix (ms), filled in as it is laid out
    at_ms: int = 0


@dataclass
class RenderReport:
    segments: list = field(default_factory=list)
    duration_ms: int = 0
    effects_skipped: list = field(default_factory=list)
    loops_applied: list = field(default_factory=list)
    wrong_edit: list = field(default_factory=list)
    missing: list = field(default_factory=list)
    peak: float = 0.0


def plan(transitions: list, entry_for) -> tuple[list[Segment], RenderReport]:
    """Work out which slice of each track is used, before touching any audio.

    ``entry_for(track)`` returns the rekordbox Entry for a track, which is
    where the local path and the alignment offset live. Planning first means
    a missing file is reported before an hour of audio is decoded.
    """
    report = RenderReport()
    segments: list[Segment] = []
    if not transitions:
        raise RenderError("No transitions to render.")

    # Each transition says where the outgoing track bows out and where the
    # incoming one enters. A track's slice therefore runs from its own in
    # point (0 for the first track) to the end of its overlap out.
    entered_at = 0
    for i, t in enumerate(transitions):
        entry = entry_for(t.from_track)
        if entry is None or not entry.local_path:
            report.missing.append(t.from_track.title)
            return [], report
        offset = entry.offset_ms
        overlap = t.overlap_ms or 0
        out_at = t.out_point_ms if t.out_point_ms is not None else 0
        segments.append(Segment(
            title=t.from_track.title,
            path=entry.local_path,
            start_ms=entered_at + offset,
            end_ms=out_at + overlap + offset,
            overlap_ms=overlap,
        ))
        if entry.is_different_edit():
            report.wrong_edit.append(t.from_track.title)
        eff = ((t.ingredients or {}).get("effects") or {})
        if eff.get("value"):
            report.effects_skipped.append(f"{i + 1:02d} {eff['value']}")
        entered_at = t.in_point_ms if t.in_point_ms is not None else 0

    last = transitions[-1].to_track
    entry = entry_for(last)
    if entry is None or not entry.local_path:
        report.missing.append(last.title)
        return [], report
    tail = load_duration_ms(entry.local_path)
    segments.append(Segment(
        title=last.title, path=entry.local_path,
        start_ms=entered_at + entry.offset_ms,
        end_ms=tail if tail else entered_at + entry.offset_ms,
        overlap_ms=0,
    ))
    if entry.is_different_edit():
        report.wrong_edit.append(last.title)

    # Lay the segments out end to end, each one starting its own overlap
    # before the previous finishes. The mix is as long as the furthest point
    # any segment reaches - which is the last one's end, not the sum of the
    # advances, since the final segment has no overlap to subtract.
    at = 0
    end = 0
    for seg in segments:
        seg.at_ms = at
        body = max(seg.end_ms - seg.start_ms, 0)
        end = max(end, at + body)
        at += max(body - seg.overlap_ms, 0)
    report.segments = segments
    report.duration_ms = end
    return segments, report


def load_duration_ms(path: Path) -> int | None:
    from src.rekordbox import read_duration_ms
    return read_duration_ms(path)


def render(transitions: list, entry_for, rate: int = TARGET_RATE,
           progress=None) -> tuple[object, RenderReport]:
    """Mix the whole thing down to one stereo array.

    Each track contributes one slice. The last ``overlap`` of a slice is the
    blend into the next track; the first ``overlap`` of a slice is the blend
    out of the previous one. Both are shaped on the track's own audio before
    it is added to the mix, so the two sides of a blend simply sum - which is
    what a mixer does anyway.

    Tracks are decoded one at a time and released, so peak memory stays near
    one track rather than the whole set.
    """
    np = _np()
    segments, report = plan(transitions, entry_for)
    if report.missing:
        raise RenderError("No local file for: " + ", ".join(report.missing))

    # A second of slack so a segment that runs a touch long is not clipped by
    # the buffer; the mix is trimmed back to its real length before returning.
    length = int(report.duration_ms / 1000 * rate)
    mix = np.zeros((length + rate, 2), dtype=np.float32)

    def ms(v):
        return int(round(v / 1000 * rate))

    # The automation for transition i, shared by segment i (its outgoing half)
    # and segment i+1 (its incoming half), so both are built from one call.
    moves: dict[int, tuple[Move, Move]] = {}

    def moves_for(i: int, n: int) -> tuple[Move, Move]:
        if i not in moves:
            t = transitions[i]
            moves[i] = build_moves(np, n, t.ingredients,
                                   getattr(t, "automation", None))
        return moves[i]

    for i, seg in enumerate(segments):
        if progress:
            progress(i + 1, len(segments), seg.title)
        audio = load_audio(seg.path, rate)
        a = max(ms(seg.start_ms), 0)
        b = min(max(ms(seg.end_ms), 0), len(audio))
        if b <= a:
            log.warning("Nothing to take from %s (slice %d-%d ms); skipping.",
                        seg.title, seg.start_ms, seg.end_ms)
            del audio
            continue
        body = audio[a:b].copy()
        del audio

        # Blend out of the previous track: shape the head of this slice.
        if i > 0 and segments[i - 1].overlap_ms:
            n = min(ms(segments[i - 1].overlap_ms), len(body))
            if n > 0:
                _, in_move = moves_for(i - 1, ms(segments[i - 1].overlap_ms))
                body[:n] = apply_move(np, body[:n], _truncate(in_move, n))

        # Blend into the next track: shape the tail of this slice.
        if seg.overlap_ms and i + 1 < len(segments):
            n = min(ms(seg.overlap_ms), len(body))
            if n > 0:
                trans = transitions[i]
                # The Looping ingredient first: the roll replaces the audio
                # under the blend, so it has to happen before the fades and
                # EQ are applied on top of it.
                roll = roll_ms_from(getattr(trans, "automation", None), trans)
                if roll and roll < seg.overlap_ms:
                    body[-n:] = apply_roll(np, body[-n:], roll, rate)
                    report.loops_applied.append(f"{i + 1:02d} {roll} ms")
                out_move, _ = moves_for(i, ms(seg.overlap_ms))
                body[-n:] = apply_move(np, body[-n:], _truncate(out_move, n))

        _add(mix, body, ms(seg.at_ms))

    mix = mix[:length]                      # drop the slack, not real audio
    report.peak = float(np.abs(mix).max()) if len(mix) else 0.0
    return mix, report


def _truncate(move: Move, n: int) -> Move:
    return Move(move.volume[:n], move.low[:n], move.mid[:n], move.high[:n])


def _add(mix, chunk, at: int) -> None:
    n = min(len(chunk), len(mix) - at)
    if n > 0:
        mix[at:at + n] += chunk[:n]


def normalize(mix, ceiling: float = 0.97):
    """Scale the mix down if summing tracks pushed it past full scale."""
    np = _np()
    peak = float(np.abs(mix).max()) if len(mix) else 0.0
    if peak > ceiling:
        mix *= ceiling / peak
    return mix, peak


def write(mix, path: Path, rate: int = TARGET_RATE) -> None:
    import soundfile as sf
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), mix, rate)


# --------------------------------------------------------------------------
# Splitting the mix into one file per song
# --------------------------------------------------------------------------
@dataclass
class Piece:
    """One output file: a song, with the blends already at its edges."""
    index: int
    title: str
    #: where this piece begins and ends in the rendered mix (ms)
    from_ms: int
    to_ms: int

    @property
    def duration_ms(self) -> int:
        return max(self.to_ms - self.from_ms, 0)


def split_points(segments: list[Segment]) -> list[Piece]:
    """Where to cut the finished mix so each song becomes one file.

    The cut goes at the **end** of each overlap. That is the only split that
    survives being played back as separate files: everything between two cuts
    is one continuous stretch of the real mix, so playing the pieces in order
    with no gap reproduces the mix exactly, sample for sample.

    The consequence is worth being clear about. A piece holds the tail of its
    own song *including the blend into the next one*, so the last seconds of
    piece N already contain the opening of song N+1. That is what a blend is -
    two songs sounding at once - and it cannot be otherwise while the files
    play one after another rather than overlapping.

    It also means playback has to be gapless. Any silence inserted between
    files lands in the middle of a blend.
    """
    pieces: list[Piece] = []
    for i, seg in enumerate(segments):
        body = max(seg.end_ms - seg.start_ms, 0)
        start = pieces[-1].to_ms if pieces else 0
        pieces.append(Piece(index=i + 1, title=seg.title,
                            from_ms=start, to_ms=seg.at_ms + body))
    return pieces


def render_solo(transitions: list, entry_for, rate: int = TARGET_RATE,
                progress=None):
    """Render each song on its own, edges shaped, no neighbour audio.

    This cannot be done by slicing the finished mix: across an overlap the mix
    holds both songs summed, and no cut separates them again. So each track is
    rendered by itself here - its own slice, with the same head and tail
    automation applied - and never added to anything.

    The files then survive shuffling or playing alone, but the blends are gone:
    a blend is two songs sounding at once, and that never happens when each
    file holds one song. What is left is the running order, the trimmed start
    and end points, and a fade at each edge.

    Yields ``(Piece, audio)`` per track.
    """
    np = _np()
    segments, report = plan(transitions, entry_for)
    if report.missing:
        raise RenderError("No local file for: " + ", ".join(report.missing))

    def ms(v):
        return int(round(v / 1000 * rate))

    moves: dict[int, tuple[Move, Move]] = {}

    def moves_for(i: int, n: int) -> tuple[Move, Move]:
        if i not in moves:
            t = transitions[i]
            moves[i] = build_moves(np, n, t.ingredients, getattr(t, "automation", None))
        return moves[i]

    for i, seg in enumerate(segments):
        if progress:
            progress(i + 1, len(segments), seg.title)
        audio = load_audio(seg.path, rate)
        a = max(ms(seg.start_ms), 0)
        b = min(max(ms(seg.end_ms), 0), len(audio))
        if b <= a:
            log.warning("Nothing to take from %s; skipping.", seg.title)
            del audio
            continue
        body = audio[a:b].copy()
        del audio

        if i > 0 and segments[i - 1].overlap_ms:
            n = min(ms(segments[i - 1].overlap_ms), len(body))
            if n > 0:
                _, in_move = moves_for(i - 1, ms(segments[i - 1].overlap_ms))
                body[:n] = apply_move(np, body[:n], _truncate(in_move, n))
        if seg.overlap_ms and i + 1 < len(segments):
            n = min(ms(seg.overlap_ms), len(body))
            if n > 0:
                trans = transitions[i]
                roll = roll_ms_from(getattr(trans, "automation", None), trans)
                if roll and roll < seg.overlap_ms:
                    body[-n:] = apply_roll(np, body[-n:], roll, rate)
                out_move, _ = moves_for(i, ms(seg.overlap_ms))
                body[-n:] = apply_move(np, body[-n:], _truncate(out_move, n))

        piece = Piece(index=i + 1, title=seg.title, from_ms=0,
                      to_ms=int(len(body) / rate * 1000))
        yield piece, body


def write_solo(transitions: list, entry_for, out_dir: Path, suffix: str = ".wav",
               rate: int = TARGET_RATE, progress=None) -> list[Path]:
    """Write one standalone file per song."""
    import soundfile as sf

    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for piece, audio in render_solo(transitions, entry_for, rate, progress):
        peak = float(abs(audio).max()) if len(audio) else 0.0
        if peak > 0.97:
            audio = audio * (0.97 / peak)
        path = out_dir / piece_filename(piece, suffix)
        sf.write(str(path), audio, rate)
        written.append(path)
    return written


#: Characters Windows will not accept in a filename.
_UNSAFE = str.maketrans({c: "-" for c in '<>:"/|?*' + chr(92)})


def piece_filename(piece: Piece, suffix: str) -> str:
    """A filename that sorts into playing order in any file browser."""
    title = (piece.title or "track").translate(_UNSAFE).strip().rstrip(".")
    return f"{piece.index:02d} - {title[:70]}{suffix}"


def write_pieces(mix, pieces: list[Piece], out_dir: Path, suffix: str = ".wav",
                 rate: int = TARGET_RATE) -> list[Path]:
    """Cut the rendered mix into one file per song."""
    import soundfile as sf

    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for piece in pieces:
        a = max(int(piece.from_ms / 1000 * rate), 0)
        b = min(int(piece.to_ms / 1000 * rate), len(mix))
        if b <= a:
            log.warning("Piece %02d (%s) is empty; skipping.", piece.index, piece.title)
            continue
        path = out_dir / piece_filename(piece, suffix)
        sf.write(str(path), mix[a:b], rate)
        written.append(path)
    return written


# --------------------------------------------------------------------------
# The Looping ingredient
# --------------------------------------------------------------------------
def roll_ms_from(automation, transition, fallback_bpm: int | None = None) -> int | None:
    """How long the transition's loop is, in milliseconds.

    Two sources. The player reports ``audio.fade_out_roll_time_curves`` when it
    has them, which is authoritative. Otherwise the editor's Looping setting
    gives a beat count, and a beat count against the outgoing track's tempo is
    the same number.
    """
    if automation is not None:
        segs = automation.outgoing.extra.get("roll_time")
        if segs:
            v = value_at(segs, 0.0)
            if v:
                return int(round(v))
    beats = transition.loop_beats() if hasattr(transition, "loop_beats") else None
    bpm = fallback_bpm or getattr(getattr(transition, "from_track", None), "bpm", None)
    return loop_ms_from_beats(beats, bpm)


def loop_ms_from_beats(beats: int | None, bpm: int | None) -> int | None:
    if not beats or not bpm:
        return None
    return int(round(beats * 60_000 / bpm))


def apply_roll(np, chunk, roll_ms: int, rate: int = TARGET_RATE):
    """Repeat the first ``roll_ms`` of ``chunk`` to fill it - a beat repeat.

    This is what the Looping ingredient does to the outgoing track at a
    transition: instead of playing on, the opening bar or beat of the blend is
    caught and repeated underneath it. Spotify calls it a roll.

    The period has to stay exactly ``roll_ms``. An earlier version smoothed the
    wrap by shortening the loop, which made every repeat land a few
    milliseconds earlier than the last and walked the roll off the beat.
    Instead the loop is read a little long and its overhang is folded back over
    its own start, which hides the seam while leaving the period untouched.
    """
    n = len(chunk)
    period = int(round(roll_ms / 1000 * rate))
    if period <= 0 or period >= n:
        return chunk

    blend = min(int(0.004 * rate), period // 8)      # about 4 ms
    if blend > 1 and period + blend <= n:
        loop = chunk[:period + blend].copy()
        ramp = np.linspace(0.0, 1.0, blend, dtype=np.float32)[:, None]
        # Fold what follows the loop over its opening, so the end of one
        # repeat runs into the start of the next without a step.
        loop[:blend] = loop[:blend] * ramp + loop[period:period + blend] * (1.0 - ramp)
        loop = loop[:period]
    else:
        loop = chunk[:period].copy()

    out = np.empty_like(chunk)
    for start in range(0, n, period):
        take = min(period, n - start)
        out[start:start + take] = loop[:take]
    return out
