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
across the overlap. On top of those go a real filter sweep, the reverb or
echo tail, the loop, and the tempo match the player makes across every blend
whose two tempi are close.
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

    Resampling is polyphase. It used to be linear interpolation, which was
    fine while every source was a 44.1k MP3 and audibly was not once the
    better downloads arrived: those are 48k, and a linear 48k-to-44.1k
    conversion folds the top octave back down as grit and dulls what is left.
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
        from scipy.signal import resample_poly
        g = math.gcd(int(sr), int(rate))
        data = resample_poly(data, rate // g, int(sr) // g, axis=0)
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


def _half(np, n, start, end, first: bool):
    """A ramp across one half of the overlap, flat at the far value elsewhere.

    ``first`` puts the ramp in the opening half (a fade in), otherwise in the
    closing half (a fade out).
    """
    out = np.full(n, float(end if first else start), dtype=np.float32)
    h = n // 2
    if first:
        out[:h] = np.linspace(start, end, h, dtype=np.float32)
    else:
        out[h:] = np.linspace(start, end, n - h, dtype=np.float32)
    return out


def volume_envelopes(np, n: int, style: str | None):
    """(outgoing, incoming) volume across the overlap, by Volume setting.

    The fades take half the overlap, not all of it. That is what the player's
    own curves show for every style they were caught using: the incoming track
    comes up over the first half and the outgoing one goes down over the
    second. Full-length ramps, which this used before, kept both tracks at
    part volume for the whole blend and sank it in the middle.
    """
    full = np.ones(n, dtype=np.float32)
    fade_in = _half(np, n, 0.0, 1.0, first=True)
    fade_out = _half(np, n, 1.0, 0.0, first=False)

    if style == "overlap":
        # Both at full for the whole overlap, then the outgoing stops dead.
        return full.copy(), full.copy()
    if style == "fade in cut out":
        return full.copy(), fade_in
    if style == "fade in fade out":
        return fade_out, fade_in
    if style is None:
        # No volume move chosen. The player still takes the outgoing track
        # down over the second half; the incoming one is simply there.
        return fade_out, full.copy()
    if style == "crossfade":
        return _ramp(np, n, 1.0, 0.0), _ramp(np, n, 0.0, 1.0)
    # "smooth crossfade", "custom", or a name we do not know: equal power, so
    # the sum does not dip in the middle. The player's own smooth crossfade is
    # a quick switch in the middle fifth, and a captured one is played that
    # way; a rebuilt one keeps this, which is what the blends that already
    # sounded right in the first renders were made with.
    t = np.linspace(0.0, 1.0, n, dtype=np.float32)
    return np.cos(t * np.pi / 2).astype(np.float32), np.sin(t * np.pi / 2).astype(np.float32)


#: Where each named bass swap hands the low end over, as a fraction of the
#: overlap, read off the player's own curves: "start" in the first few
#: milliseconds, "centre" just before the midpoint, "end" in the last few
#: tens of milliseconds. An earlier guess put them at a quarter and three
#: quarters, which moved every start and end swap by seconds.
SWAP_AT = {"start bass swap": 0.002, "centre bass swap": 0.49, "end bass swap": 0.985}

#: Where the 3-band fade moves each band, from the same curves. The highs
#: change hands first, at a quarter; the bass and mids at the midpoint.
THREE_BAND_AT = {"low": 0.49, "mid": 0.5, "high": 0.25}

#: The cut Spotify applies to the non-bass bands during a 3-band fade: knob
#: 0.2, measured off the transitions where the player reported its curves.
BAND_CUT = 0.2


def swap_point(np, style: str, out_vol=None, in_vol=None) -> float:
    """Where a named bass swap happens, kept where someone is carrying the bass.

    An end swap leaves the incoming track without bass until the last moment,
    and a start swap takes it off the outgoing one at once. With both tracks at
    full volume that is the point of them. Paired with a fade, though, the
    track that owns the bass can be all but gone while it still owns it, and
    the blend goes hollow - Tuf Tuf into YUMMI did, for seconds. So the swap
    comes no later than the outgoing fade passing half volume, and no earlier
    than the incoming one reaching it.
    """
    at = SWAP_AT[style]
    if out_vol is not None and len(out_vol):
        gone = np.nonzero(out_vol < 0.5)[0]
        if len(gone):
            at = min(at, gone[0] / len(out_vol))
    if in_vol is not None and len(in_vol):
        there = np.nonzero(in_vol >= 0.5)[0]
        if len(there):
            at = max(at, there[0] / len(in_vol))
    return at


def eq_envelopes(np, n: int, style: str | None, out_vol=None, in_vol=None):
    """(outgoing, incoming) band gains across the overlap, by EQ setting.

    Grounded in the transitions whose real curves the capture caught. Two
    things those settled, both counter to how the names read:

    * Spotify's "3-band fade" is not a fade. Every band *steps* - low from
      unity to kill on the way out and kill to unity on the way in, mid and
      high between unity and a 0.2 cut - the highs at a quarter of the way
      through and the rest at the midpoint. So it is a three-band swap, and it
      is rendered as one.
    * The 0.2 cut belongs to that style. A bass swap only moves the low band;
      leaving the incoming mids and highs cut through the whole overlap made
      the new track sound distant for seconds at a time.

    ``out_vol`` and ``in_vol``, when given, keep a bass swap from landing where
    neither track is carrying the low end (see :func:`swap_point`).
    """
    unity = np.ones(n, dtype=np.float32)

    if style in SWAP_AT:
        at = swap_point(np, style, out_vol, in_vol)
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
        cut = knob_to_gain(BAND_CUT)
        low, mid, high = (THREE_BAND_AT[b] for b in ("low", "mid", "high"))
        return (
            Move(None, _step(np, n, 1.0, 0.0, low),
                 _step(np, n, 1.0, cut, mid), _step(np, n, 1.0, cut, high)),
            Move(None, _step(np, n, 0.0, 1.0, low),
                 _step(np, n, cut, 1.0, mid), _step(np, n, cut, 1.0, high)),
        )
    return (Move(None, unity.copy(), unity.copy(), unity.copy()),
            Move(None, unity.copy(), unity.copy(), unity.copy()))


#: Points a captured curve is evaluated at before being stretched to one
#: value per sample. Evaluating it per sample in Python took seconds per
#: transition; this many points places a step to within a few milliseconds,
#: and the declick smooths over that anyway.
CURVE_POINTS = 4096


def sample_curve(np, segs, n: int, default: float):
    """A captured curve as one value per sample, or a flat line if absent."""
    if not segs or n <= 0:
        return np.full(max(n, 0), default, dtype=np.float32)
    pos = np.linspace(0.0, 1.0, CURVE_POINTS)
    vals = np.array([value_at(segs, float(p)) for p in pos], dtype=np.float64)
    vals = np.where(np.isnan(vals.astype(float)), default, vals)
    return np.interp(np.linspace(0.0, 1.0, n), pos, vals).astype(np.float32)


def curves_from_capture(np, n: int, automation) -> tuple[Move, Move] | None:
    """Envelopes sampled from the player's own reported curves, if present."""

    if not automation:
        return None

    def side(s):
        if not s.volume:
            return None
        vol = sample_curve(np, s.volume, n, 0.0)
        bands = {}
        for b in BANDS:
            segs = s.eq.get(b)
            if not segs:
                bands[b] = np.ones(n, dtype=np.float32)
            else:
                knob = sample_curve(np, segs, n, 0.5)
                bands[b] = np.where(knob <= 0.0, 0.0,
                                    np.where(knob < 0.5, knob / 0.5,
                                             1.0 + (knob - 0.5) * 2.0)).astype(np.float32)
        return Move(vol, bands["low"], bands["mid"], bands["high"])

    out, inc = side(automation.outgoing), side(automation.incoming)
    return (out, inc) if out and inc else None


#: Every step in a curve is eased over this long. A gain that jumps between
#: two samples is a click, and on the low band a thump; Spotify's own steps
#: are not audible as either, so they are not rendered as either.
DECLICK_S = 0.008


def _declick(np, env, width: int):
    if width < 2 or len(env) < width:
        return env
    from scipy.ndimage import uniform_filter1d
    return uniform_filter1d(env.astype(np.float32), width, mode="nearest")


def build_moves(np, n: int, ingredients: dict, automation=None,
                rate: int = TARGET_RATE) -> tuple[Move, Move]:
    """The full automation for one transition, both sides.

    Filter sweeps are not part of this: they are a real filter, applied by
    :func:`apply_filter_sweep` before the fader. An earlier version also
    folded the named filter into the band gains here, so every rebuilt filter
    was applied twice.
    """
    moves = curves_from_capture(np, n, automation)
    if not moves:
        def value(slot):
            d = (ingredients or {}).get(slot) or {}
            return None if d.get("off") else d.get("value")

        out_vol, in_vol = volume_envelopes(np, n, value("volume"))
        out_move, in_move = eq_envelopes(np, n, value("eq"), out_vol, in_vol)
        out_move.volume, in_move.volume = out_vol, in_vol
        moves = (out_move, in_move)

    width = int(DECLICK_S * rate)
    for m in moves:
        for name in ("volume", "low", "mid", "high"):
            setattr(m, name, _declick(np, getattr(m, name), width))
    out_move, in_move = moves
    # The outgoing track always ends at the end of the overlap and the
    # incoming one always starts at its beginning. Both edges are cuts in the
    # audio, so both get a few milliseconds of fade whatever the style says.
    k = min(width, n // 4)
    if k > 1:
        out_move.volume[-k:] *= np.linspace(1.0, 0.0, k, dtype=np.float32)
        in_move.volume[:k] *= np.linspace(0.0, 1.0, k, dtype=np.float32)
    return out_move, in_move


def apply_move(np, chunk, move: Move):
    """Apply one side's volume and band gains to a slice of audio."""
    low, mid, high = split_bands(chunk)
    mixed = (low * move.low[:, None]
             + mid * move.mid[:, None]
             + high * move.high[:, None])
    return mixed * move.volume[:, None]


# --------------------------------------------------------------------------
# Filter, reverb and echo
# --------------------------------------------------------------------------
# These are the ingredients that make a blend sound deliberate rather than
# like one track stopping and the next starting. Without them an "Overlap"
# volume setting - both tracks at full, then a cut - lands as an abrupt change.
#
# The player reports them per transition, as curves:
#   filter_cutoff / filter_resonance   0..1, 0.5 neutral
#   reverb_dry_wet and five more       the reverb's settings, decay in ms
# and they are rebuilt from the ingredient name when no curves were captured.

#: The sweep a filter curve covers. 0.5 is neutral - no filtering - and the
#: value moves toward one end or the other.
FILTER_MIN_HZ = 60.0
FILTER_MAX_HZ = 18000.0
#: How far the resonance control can lift the filter's Q.
FILTER_MAX_Q = 4.0
#: Samples between coefficient updates. A sweep is a filter whose corner keeps
#: moving; updating it every 1.5 ms with the filter's memory carried across is
#: smooth to the ear and costs a fraction of a second per transition.
FILTER_BLOCK = 64
#: How long the hand-over between the dry signal and the filter takes, at the
#: moments the sweep leaves or returns to neutral.
FILTER_XFADE_S = 0.02


def _cutoff_hz(value: float, high_pass: bool) -> float:
    """Map a 0..1 filter position to a corner frequency.

    At 0.5 the filter is out of the way, so a high-pass sits at its lowest
    corner and a low-pass at its highest. Away from neutral the corner sweeps
    in, taking the band with it.
    """
    amount = max(0.0, (0.5 - value) / 0.5) if not high_pass else max(0.0, (value - 0.5) / 0.5)
    amount = min(amount, 1.0)
    if high_pass:
        return FILTER_MIN_HZ * (FILTER_MAX_HZ / FILTER_MIN_HZ) ** amount
    return FILTER_MAX_HZ * (FILTER_MIN_HZ / FILTER_MAX_HZ) ** amount


def _biquad(hz: float, q: float, high_pass: bool, rate: int) -> list[float]:
    """One second-order section, as a row of an sos matrix (RBJ cookbook)."""
    w0 = 2.0 * math.pi * min(max(hz, 10.0), rate * 0.45) / rate
    cos_w = math.cos(w0)
    alpha = math.sin(w0) / (2.0 * q)
    if high_pass:
        b0, b1 = (1.0 + cos_w) / 2.0, -(1.0 + cos_w)
    else:
        b0, b1 = (1.0 - cos_w) / 2.0, 1.0 - cos_w
    a0 = 1.0 + alpha
    return [b0 / a0, b1 / a0, b0 / a0, 1.0, -2.0 * cos_w / a0, (1.0 - alpha) / a0]


def sweep_filter(np, chunk, cutoff, resonance, high_pass: bool,
                 rate: int = TARGET_RATE):
    """Apply a moving filter across a chunk.

    One filter runs over the whole chunk, its coefficients updated every
    :data:`FILTER_BLOCK` samples and its memory carried from block to block,
    so the sweep is continuous. The dry signal is used wherever the curve sits
    at neutral, and the two are crossfaded where it leaves or returns - at the
    filter's most open setting, where they barely differ.

    The version before cut the chunk into 25 ms hops, filtered each from
    scratch, and windowed every hop with both halves of a Hann window at once.
    That multiplied the audio by a hump that peaked at a quarter, forty times
    a second: a buzzing tremolo 12 dB down on every filtered blend, which is
    the "noise going up and down" those transitions were heard to have.
    """
    from scipy.signal import sosfilt, sosfilt_zi

    n = len(chunk)
    grid = np.linspace(0.0, 1.0, len(cutoff))
    rgrid = np.linspace(0.0, 1.0, len(resonance))
    wet = np.empty_like(chunk)
    active = np.zeros(n, dtype=np.float32)
    zi = None
    for start in range(0, n, FILTER_BLOCK):
        end = min(start + FILTER_BLOCK, n)
        mid = (start + end) / 2 / max(n - 1, 1)
        c = float(np.interp(mid, grid, cutoff))
        r = float(np.interp(mid, rgrid, resonance))
        q = 0.7071 + max(0.0, (r - 0.5) / 0.5) * (FILTER_MAX_Q - 0.7071)
        sos = np.array([_biquad(_cutoff_hz(c, high_pass), q, high_pass, rate)])
        if zi is None:
            # Start settled on the first sample rather than from rest, so the
            # filter does not thump on its way in.
            zi = sosfilt_zi(sos)[:, :, None] * chunk[0][None, None, :]
        wet[start:end], zi = sosfilt(sos, chunk[start:end], axis=0, zi=zi)
        if abs(c - 0.5) >= 0.005:
            active[start:end] = 1.0
    blend = _declick(np, active, int(FILTER_XFADE_S * rate))[:, None]
    return (chunk * (1.0 - blend) + wet * blend).astype(np.float32)


def _curve_samples(np, segs, n: int, default: float) -> object:
    """Sample a curve across the overlap, or a flat line when there is none."""
    return sample_curve(np, segs, n, default)


def apply_filter_sweep(np, chunk, side, ingredients: dict, which: str,
                       rate: int = TARGET_RATE):
    """Put the Filter ingredient onto one side of a blend.

    A filter is an insert, so this runs before the volume and EQ move. ``side``
    is the player's reported automation when there is any; otherwise the sweep
    is rebuilt from the name the editor showed.

    Returns None when the filter is neutral and nothing was done. An earlier
    version returned the chunk unchanged and left the caller to spot it with
    ``is not``, which never worked: ``body[-n:]`` builds a fresh slice object
    every time it is evaluated, so the comparison was always true and the run
    reported a filter on all 26 transitions when only 16 have one.
    """
    n = len(chunk)
    if n < 64:
        return None
    cutoff = _curve_samples(np, side.extra.get("filter_cutoff") if side else None, n, 0.5)
    resonance = _curve_samples(np, side.extra.get("filter_resonance") if side else None,
                               n, 0.5)
    named = ((ingredients or {}).get("filter") or {}).get("value")
    if float(np.abs(cutoff - 0.5).max()) < 0.01 and named:
        cutoff = _named_filter_curve(np, n, named, which)
    if float(np.abs(cutoff - 0.5).max()) < 0.01:
        return None          # neutral: nothing to do, and the caller must know
    high_pass = bool(cutoff.max() > 0.5 + 1e-6)
    return sweep_filter(np, chunk, cutoff, resonance, high_pass, rate)


def _named_filter_curve(np, n: int, named: str, which: str):
    """Rebuild a filter sweep from the editor's wording.

    The shapes are the player's own: an "out" sweep holds neutral for the
    first half and closes over the second, as the track leaves; an "in" sweep
    starts closed and is open again by the midpoint. Spreading either across
    the whole overlap, as this used to, filtered both tracks at once for the
    entire blend.
    """
    parts = [p.strip() for p in named.split("+")]
    mine = [p for p in parts if p.endswith(" " + ("in" if which == "in" else "out"))]
    if not mine:
        return np.full(n, 0.5, dtype=np.float32)
    part = mine[0]
    # A high-pass sweeps up from neutral; a low-pass sweeps down.
    far = 1.0 if part.startswith("high pass") else 0.0
    if which == "in":
        return _half(np, n, far, 0.5, first=True)
    return _half(np, n, 0.5, far, first=False)


#: The longest a reverb or echo tail rings on past the end of its blend.
MAX_TAIL_S = 6.0
#: Reverb settings for a transition whose curves were not captured: the
#: player's own values from the one reverb it reported.
REVERB_DEFAULTS = {"reverb_decay_time": 7260.0, "reverb_damping": 0.1,
                   "reverb_room_size": 0.5, "reverb_brightness": 0.69}
#: Each echo repeat is this much quieter than the one before.
ECHO_FEEDBACK = 0.5
#: The send into a reverb or echo is high-passed here, so the tail carries the
#: track's character and not a wash of bass under the incoming one.
TAIL_HIGH_PASS_HZ = 200.0


def reverb_ir(np, decay_ms: float, damping: float, room: float, brightness: float,
              rate: int = TARGET_RATE):
    """A stereo reverb impulse response: decaying noise, darkened and delayed.

    Convolving with this is a smooth, dense tail. It replaced four comb
    filters into two allpasses, which rang metallically on anything tonal.
    Decay is the time to fall 60 dB; brightness and damping set how much top
    end the tail keeps; room size sets the pre-delay.
    """
    from scipy.signal import butter, sosfilt

    decay_s = max(decay_ms, 200.0) / 1000.0
    length = int(min(decay_s, MAX_TAIL_S) * rate)
    t = np.arange(length, dtype=np.float32) / rate
    ir = np.random.default_rng(7).standard_normal((length, 2)).astype(np.float32)
    ir *= (10.0 ** (-3.0 * t / decay_s)).astype(np.float32)[:, None]
    fade = min(int(0.3 * rate), length)
    ir[-fade:] *= np.linspace(1.0, 0.0, fade, dtype=np.float32)[:, None]
    top = 2000.0 + 14000.0 * float(brightness) * (1.0 - 0.7 * float(damping))
    ir = sosfilt(butter(2, min(top / (rate / 2.0), 0.95), output="sos"), ir, axis=0)
    pre = int((0.005 + 0.025 * float(room)) * rate)
    ir = np.concatenate([np.zeros((pre, 2), dtype=np.float32), ir.astype(np.float32)])
    return ir / (np.sqrt((ir ** 2).sum(axis=0, keepdims=True)) + 1e-12)


def echo_ir(np, delay_s: float, rate: int = TARGET_RATE):
    """Repeats every ``delay_s``, each :data:`ECHO_FEEDBACK` of the last."""
    d = max(int(delay_s * rate), 1)
    repeats = max(1, min(int(math.log(0.03) / math.log(ECHO_FEEDBACK)),
                         int(MAX_TAIL_S * rate) // d))
    ir = np.zeros((d * repeats + 1, 2), dtype=np.float32)
    for k in range(1, repeats + 1):
        ir[k * d] = ECHO_FEEDBACK ** (k - 1)
    return ir


def effect_tail(np, dry, side, ingredients: dict, bpm: float | None,
                rate: int = TARGET_RATE):
    """The reverb or echo on a track that is leaving, or None if it has none.

    ``dry`` is the outgoing side of the blend before its fader. The send into
    the effect follows the dry/wet curve - the player's when captured, else
    its usual shape for these settings: nothing for the first half, rising to
    half wet by the end.

    Returned separately, and longer than ``dry``, so the caller can add it
    *after* the volume move and let it ring past the end of the blend. That is
    the whole point of the effect: the tail has to keep sounding while the dry
    track is cut away, which is what "Reverb cut end" means. The version
    before stopped the tail dead at the end of the overlap, mid-ring.
    """
    from scipy.signal import butter, fftconvolve, sosfilt

    n = len(dry)
    if n < 64:
        return None
    effect = ((ingredients or {}).get("effects") or {}).get("value") or ""
    send = _curve_samples(np, side.extra.get("reverb_dry_wet") if side else None, n, 0.0)
    if float(send.max()) < 0.01 and ("reverb" in effect or "echo" in effect):
        send = _half(np, n, 0.0, 0.5, first=False)
    if float(send.max()) < 0.01:
        return None

    import re
    echo = re.search(r"echo\s+(\d+)\s*/\s*(\d+)", effect)
    if echo and not (side and side.extra.get("reverb_dry_wet")):
        beat_s = 60.0 / float(bpm or 120)
        ir = echo_ir(np, beat_s * int(echo.group(1)) / int(echo.group(2)), rate)
    else:
        def setting(name):
            return _first_value(side, name, REVERB_DEFAULTS[name])
        ir = reverb_ir(np, setting("reverb_decay_time"), setting("reverb_damping"),
                       setting("reverb_room_size"), setting("reverb_brightness"), rate)

    hp = butter(2, TAIL_HIGH_PASS_HZ / (rate / 2.0), btype="high", output="sos")
    feed = sosfilt(hp, dry * send[:, None], axis=0).astype(np.float32)
    return fftconvolve(feed, ir, axes=0).astype(np.float32)


def _first_value(side, name: str, default: float) -> float:
    if side is None:
        return default
    v = value_at(side.extra.get(name) or [], 0.0)
    return float(v) if v is not None else default


# --------------------------------------------------------------------------
# Tempo: the incoming track meets the outgoing one's beat
# --------------------------------------------------------------------------
# The player matches tempo across a blend. Its own numbers say so: the
# incoming side of a captured transition is reported as lasting
# overlap x (outgoing bpm / incoming bpm) - 15387 ms of Mimosa 2000 at 125 bpm
# across a 15864 ms overlap out of Move Ya Body at 121 - so the incoming track
# is played that much faster or slower, pitch kept, until the outgoing one has
# gone. Mixing the two at their own tempi instead slides the beats apart a
# whole beat over a long blend: a train wreck under every filter sweep.

#: The furthest a rebuilt transition pushes the incoming tempo. The player's
#: reports show it matching differences of 3 to 8 % and leaving 36 % alone.
MAX_STRETCH = 0.09


def overlap_of(t) -> int:
    """The blend's length in ms: the player's figure when captured.

    The two can differ - Dirty Cash into La Mama reads 13997 ms in the editor
    and 14548 in the player - and the player's is the one that was heard.
    """
    auto = getattr(t, "automation", None)
    if auto is not None and getattr(auto, "overlap_ms", None):
        return int(auto.overlap_ms)
    return int(t.overlap_ms or 0)


def tempo_ratio(t) -> float:
    """How fast the incoming track plays during the blend (1.0 = as recorded)."""
    ov = overlap_of(t)
    auto = getattr(t, "automation", None)
    if auto is not None and ov and getattr(auto.incoming, "duration_ms", None):
        r = auto.incoming.duration_ms / ov
        return r if abs(r - 1.0) <= 0.2 else 1.0
    a = getattr(getattr(t, "from_track", None), "bpm", None)
    b = getattr(getattr(t, "to_track", None), "bpm", None)
    if not a or not b:
        return 1.0
    r = float(a) / float(b)
    return r if abs(r - 1.0) <= MAX_STRETCH else 1.0


def _ffmpeg() -> str | None:
    import shutil
    found = shutil.which("ffmpeg")
    if found:
        return found
    for name in ("ffmpeg.exe", "ffmpeg"):
        p = Path.home() / ".spotdl" / name
        if p.exists():
            return str(p)
    return None


def time_stretch(np, audio, tempo: float, rate: int = TARGET_RATE):
    """``audio`` played ``tempo`` times as fast, pitch unchanged, or None.

    Done by Rubber Band through ffmpeg, which is the stretcher a DJ deck's key
    lock is built on and keeps drums tight where a phase vocoder smears them.
    None when no ffmpeg with Rubber Band is available; the caller then plays
    the track at its own tempo and says so.
    """
    import subprocess

    ff = _ffmpeg()
    if not ff:
        return None
    af = (f"rubberband=tempo={tempo:.6f}:transients=crisp:detector=compound:"
          "phase=laminar:window=standard:pitchq=quality:channels=together")
    fmt = ["-f", "f32le", "-ar", str(rate), "-ac", "2"]
    try:
        run = subprocess.run([ff, "-hide_banner", "-loglevel", "error", *fmt, "-i", "pipe:0",
                              "-af", af, *fmt, "pipe:1"],
                             input=np.ascontiguousarray(audio, dtype="<f4").tobytes(),
                             capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError) as e:
        log.warning("Time-stretch unavailable (%s); blending at the recorded tempo.", e)
        return None
    return np.frombuffer(run.stdout, dtype="<f4").reshape(-1, 2).copy()


def stretched_slice(np, audio, a: int, b: int, n_head: int, ratio: float,
                    rate: int = TARGET_RATE):
    """``audio[a:b]`` with its opening blend played at ``ratio``.

    The first ``n_head * ratio`` samples are stretched to last ``n_head``, and
    the rest follows at the recorded tempo from exactly where the stretched
    part leaves off, joined over 10 ms. The stretcher is fed half a second of
    run-up before ``a`` so it has settled by the time the blend begins.
    """
    src = int(round(n_head * ratio))
    join = int(0.01 * rate)
    if a + src + join >= b:
        return None
    pre = min(a, int(0.5 * rate))
    post = int(0.2 * rate)
    piece = audio[a - pre:min(a + src + post, len(audio))]
    out = time_stretch(np, piece, ratio, rate)
    if out is None:
        # No stretcher: change speed instead. The pitch moves with it for the
        # length of the blend, but the beats still line up, and the slice
        # keeps the length the mix was laid out for.
        from fractions import Fraction
        from scipy.signal import resample_poly
        log.info("No Rubber Band; matching tempo by speed instead, so the pitch "
                 "moves for the length of the blend.")
        f = Fraction(1.0 / ratio).limit_denominator(1000)
        out = resample_poly(piece, f.numerator, f.denominator, axis=0).astype(np.float32)
    first = int(round(pre / ratio))
    head = out[first:first + n_head + join]
    rest = audio[a + src:b]
    if len(head) < n_head + join or len(rest) < join:
        return None
    ramp = np.linspace(0.0, 1.0, join, dtype=np.float32)[:, None]
    seam = head[n_head:] * (1.0 - ramp) + rest[:join] * ramp
    return np.concatenate([head[:n_head], seam, rest[join:]]).astype(np.float32)


# --------------------------------------------------------------------------
# Loudness matching
# --------------------------------------------------------------------------
# Spotify plays every track at a common loudness, so a quiet master and a
# crushed one sit at the same level. A render straight off the files does not,
# and the difference is not subtle: across one 27-track playlist the quietest
# track measured -13.9 LUFS against +0.7 for the loudest, a 14.6 dB spread.
# Sean Paul's "Temperature" was the quiet one, and it audibly dropped out of
# the mix.
#
# Loudness here follows ITU-R BS.1770: K-weight the audio, take the mean square
# over 400 ms blocks, then gate away the quiet ones so a track with long intros
# is not measured as quieter than it plays.
LOUDNESS_BLOCK_S = 0.400
LOUDNESS_STEP_S = 0.100
#: Blocks below this are silence and never count.
ABSOLUTE_GATE_LUFS = -70.0
#: And blocks more than this far below the ungated average are too quiet to
#: represent the track.
RELATIVE_GATE_DB = 10.0
#: Never move a track by more than this, however far off it measures. A gain
#: that large means the measurement is being asked to fix something it cannot.
MAX_TRIM_DB = 12.0
#: The loudest the tracks are set to. Modern masters measure -5 to -8 LUFS,
#: and two of them summed at full volume - which is what an "Overlap" blend
#: is - would need 6 dB more than there is. At -10 a blend still fits under
#: the limiter with a few dB to spare, and the set plays as loud as any
#: club master turned down a notch.
MIX_LUFS = -10.0


def _k_weight(np, x, rate: int):
    """The BS.1770 weighting: a high shelf, then a high-pass at 38 Hz.

    It approximates how loud something actually sounds, which plain RMS does
    not - RMS rates a bass-heavy master far louder than it plays.
    """
    from scipy.signal import butter, lfilter, sosfilt

    # The standard's own shelf, which at 48 kHz reproduces its published
    # coefficients (b0 = 1.53512...). The one here before multiplied the whole
    # filter by the shelf gain a second time, which lifted the *bass* about
    # 8 dB instead of the treble 4: bass-heavy tracks read as loud, Temperature
    # was pushed up 10 dB, and the set peaked at 2.45 and had to be turned
    # down 8 dB as a whole.
    f0, gain_db, q = 1681.974450955533, 3.999843853973347, 0.7071752369554196
    k = np.tan(np.pi * f0 / rate)
    vh = 10 ** (gain_db / 20)
    vb = vh ** 0.4996667741545416
    a0 = 1 + k / q + k * k
    b = np.array([vh + vb * k / q + k * k, 2 * (k * k - vh), vh - vb * k / q + k * k]) / a0
    a = np.array([a0, 2 * (k * k - 1), 1 - k / q + k * k]) / a0
    shelved = lfilter(b, a, x, axis=0)
    sos = butter(2, 38.0 / (rate / 2), btype="high", output="sos")
    return sosfilt(sos, shelved, axis=0)


def measure_loudness(audio, rate: int = TARGET_RATE) -> float | None:
    """Integrated loudness in LUFS, or None if there is nothing to measure."""
    np = _np()
    try:
        import scipy.signal  # noqa: F401
    except ImportError:
        return None
    if audio is None or len(audio) < int(LOUDNESS_BLOCK_S * rate):
        return None

    weighted = _k_weight(np, audio, rate)
    block = int(LOUDNESS_BLOCK_S * rate)
    step = int(LOUDNESS_STEP_S * rate)
    starts = np.arange(0, len(weighted) - block + 1, step)
    if not len(starts):
        return None
    # Sum the channels' mean squares, as the standard specifies.
    power = np.array([np.mean(weighted[i:i + block] ** 2, axis=0).sum() for i in starts])
    loud = -0.691 + 10 * np.log10(power + 1e-12)

    above_absolute = loud[loud > ABSOLUTE_GATE_LUFS]
    if not len(above_absolute):
        return None
    ungated = -0.691 + 10 * np.log10(np.mean(10 ** ((above_absolute + 0.691) / 10)))
    kept = above_absolute[above_absolute > ungated - RELATIVE_GATE_DB]
    use = kept if len(kept) else above_absolute
    return float(-0.691 + 10 * np.log10(np.mean(10 ** ((use + 0.691) / 10))))


def loudness_trims(segments: list, rate: int = TARGET_RATE,
                   progress=None) -> dict[str, float]:
    """A gain per file, in dB, that brings every track to a common loudness.

    The target is the set's own median, capped at :data:`MIX_LUFS`. The median
    keeps a quiet playlist where it is instead of pushing it up into the
    limiter; the cap leaves a loud one room for its blends.
    """
    np = _np()
    measured: dict[str, float] = {}
    for i, seg in enumerate(segments):
        key = str(seg.path)
        if key in measured:
            continue
        if progress:
            progress(i + 1, len(segments), seg.title)
        value = measure_loudness(load_audio(seg.path, rate), rate)
        if value is not None:
            measured[key] = value
    if not measured:
        return {}

    target = min(float(np.median(list(measured.values()))), MIX_LUFS)
    trims = {}
    for key, value in measured.items():
        trims[key] = float(np.clip(target - value, -MAX_TRIM_DB, MAX_TRIM_DB))
    log.info("Loudness: target %.1f LUFS, spread %.1f dB across %d track(s).",
             target, max(measured.values()) - min(measured.values()), len(measured))
    return trims


def db_to_gain(db: float) -> float:
    return float(10 ** (db / 20))


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
    #: the blend out of the previous track: how long it lasts in the mix (ms)
    #: and how fast this track plays during it to meet that track's tempo
    head_ms: int = 0
    head_ratio: float = 1.0

    @property
    def length_ms(self) -> int:
        """How long this slice lasts in the mix, its stretched opening included."""
        body = max(self.end_ms - self.start_ms, 0)
        if self.head_ratio == 1.0 or not self.head_ms:
            return body
        return max(int(round(body - self.head_ms * self.head_ratio + self.head_ms)), 0)


@dataclass
class RenderReport:
    segments: list = field(default_factory=list)
    duration_ms: int = 0
    effects_skipped: list = field(default_factory=list)
    loops_applied: list = field(default_factory=list)
    loudness_trims: dict = field(default_factory=dict)
    filters_applied: list = field(default_factory=list)
    reverbs_applied: list = field(default_factory=list)
    tempo_matched: list = field(default_factory=list)
    tempo_skipped: list = field(default_factory=list)
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
    head_ms, head_ratio = 0, 1.0
    for i, t in enumerate(transitions):
        entry = entry_for(t.from_track)
        if entry is None or not entry.local_path:
            report.missing.append(t.from_track.title)
            return [], report
        offset = entry.offset_ms
        overlap = overlap_of(t)
        out_at = t.out_point_ms if t.out_point_ms is not None else 0
        seg = Segment(
            title=t.from_track.title,
            path=entry.local_path,
            start_ms=entered_at + offset,
            end_ms=out_at + overlap + offset,
            overlap_ms=overlap,
            head_ms=head_ms,
            head_ratio=head_ratio,
        )
        # A stretched opening has to finish before this track's own blend out
        # begins; on a slice too short for that, play it as recorded.
        if seg.start_ms + seg.head_ms * seg.head_ratio > seg.end_ms - overlap:
            seg.head_ratio = 1.0
        segments.append(seg)
        if entry.is_different_edit():
            report.wrong_edit.append(t.from_track.title)
        eff = ((t.ingredients or {}).get("effects") or {})
        if eff.get("value"):
            report.effects_skipped.append(f"{i + 1:02d} {eff['value']}")
        entered_at = t.in_point_ms if t.in_point_ms is not None else 0
        head_ms, head_ratio = overlap, tempo_ratio(t)

    last = transitions[-1].to_track
    entry = entry_for(last)
    if entry is None or not entry.local_path:
        report.missing.append(last.title)
        return [], report
    tail = load_duration_ms(entry.local_path)
    seg = Segment(
        title=last.title, path=entry.local_path,
        start_ms=entered_at + entry.offset_ms,
        end_ms=tail if tail else entered_at + entry.offset_ms,
        overlap_ms=0, head_ms=head_ms, head_ratio=head_ratio,
    )
    if seg.start_ms + seg.head_ms * seg.head_ratio > seg.end_ms:
        seg.head_ratio = 1.0
    segments.append(seg)
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
        body = seg.length_ms
        end = max(end, at + body)
        at += max(body - seg.overlap_ms, 0)
    report.segments = segments
    report.duration_ms = end
    return segments, report


def load_duration_ms(path: Path) -> int | None:
    from src.rekordbox import read_duration_ms
    return read_duration_ms(path)


def render(transitions: list, entry_for, rate: int = TARGET_RATE,
           progress=None, match_loudness: bool = True) -> tuple[object, RenderReport]:
    """Mix the whole thing down to one stereo array.

    Each track contributes one slice. The last ``overlap`` of a slice is the
    blend into the next track; the first ``overlap`` of a slice is the blend
    out of the previous one, played at the previous track's tempo. Both are
    shaped on the track's own audio before it is added to the mix, so the two
    sides of a blend simply sum - which is what a mixer does anyway. A reverb
    or echo tail is the exception: it is added on its own and rings on past
    the end of its slice.

    Tracks are decoded one at a time and released, so peak memory stays near
    one track rather than the whole set.
    """
    np = _np()
    segments, report = plan(transitions, entry_for)
    if report.missing:
        raise RenderError("No local file for: " + ", ".join(report.missing))

    # Every track is brought to a common loudness before anything is mixed, so
    # the blends are shaped on audio that already sits at the right level.
    trims = loudness_trims(segments, rate, progress) if match_loudness else {}
    report.loudness_trims = dict(trims)

    # A second of slack so a segment that runs a touch long is not clipped by
    # the buffer, and room for the last tail to ring; the mix is trimmed back
    # to its real length before returning.
    length = int(report.duration_ms / 1000 * rate)
    mix = np.zeros((length + rate + int(MAX_TAIL_S * rate), 2), dtype=np.float32)

    def ms(v):
        return int(round(v / 1000 * rate))

    # The automation for transition i, shared by segment i (its outgoing half)
    # and segment i+1 (its incoming half), so both are built from one call.
    moves: dict[int, tuple[Move, Move]] = {}

    def moves_for(i: int, n: int) -> tuple[Move, Move]:
        if i not in moves:
            t = transitions[i]
            moves[i] = build_moves(np, n, t.ingredients,
                                   getattr(t, "automation", None), rate)
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
        body = None
        if i > 0 and seg.head_ratio != 1.0 and seg.head_ms:
            body = stretched_slice(np, audio, a, b, ms(seg.head_ms), seg.head_ratio, rate)
            if body is None:
                report.tempo_skipped.append(f"{i:02d}")
            else:
                report.tempo_matched.append(f"{i:02d} x{seg.head_ratio:.3f}")
        if body is None:
            body = audio[a:b].copy()
        del audio

        # Bring this track to the mix's loudness before anything else, so the
        # blends are shaped on audio that is already at the right level.
        trim = trims.get(str(seg.path))
        if trim:
            body *= db_to_gain(trim)

        # Blend out of the previous track: shape the head of this slice.
        if i > 0 and segments[i - 1].overlap_ms:
            n = min(ms(segments[i - 1].overlap_ms), len(body))
            if n > 0:
                prev = transitions[i - 1]
                prev_auto = getattr(prev, "automation", None)
                head = apply_filter_sweep(np, body[:n],
                                          prev_auto.incoming if prev_auto else None,
                                          prev.ingredients, "in", rate)
                if head is not None:
                    body[:n] = head
                _, in_move = moves_for(i - 1, ms(segments[i - 1].overlap_ms))
                body[:n] = apply_move(np, body[:n], _truncate(in_move, n))

        # Blend into the next track: shape the tail of this slice.
        tail = None
        if seg.overlap_ms and i + 1 < len(segments):
            n = min(ms(seg.overlap_ms), len(body))
            if n > 0:
                trans = transitions[i]
                auto = getattr(trans, "automation", None)
                out_side = auto.outgoing if auto else None
                # The Looping ingredient first: the roll replaces the audio
                # under the blend, so everything else sits on top of it.
                roll = roll_ms_from(auto, trans)
                if roll and roll < seg.overlap_ms:
                    body[-n:] = apply_roll(np, body[-n:], roll, rate, before=body[:-n])
                    report.loops_applied.append(f"{i + 1:02d} {roll} ms")
                # Filter next: an insert, so before the fader.
                filtered = apply_filter_sweep(np, body[-n:], out_side,
                                              trans.ingredients, "out", rate)
                if filtered is not None:
                    body[-n:] = filtered
                    report.filters_applied.append(f"{i + 1:02d}")
                tail = effect_tail(np, body[-n:], out_side, trans.ingredients,
                                   getattr(trans.from_track, "bpm", None), rate)
                out_move, _ = moves_for(i, ms(seg.overlap_ms))
                body[-n:] = apply_move(np, body[-n:], _truncate(out_move, n))
                # And the tail after the fader, so it outlives the cut.
                if tail is not None:
                    _add(mix, tail, ms(seg.at_ms) + len(body) - n)
                    report.reverbs_applied.append(f"{i + 1:02d}")

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


#: Where the limiter holds the peaks: -1 dBFS, which leaves an MP3 encoder
#: room for the overshoot it adds.
LIMIT_CEILING = 0.891
#: The limiter looks this far ahead and eases its gain down over it, then
#: holds a reduction this long before easing back.
LIMIT_ATTACK_S = 0.010
LIMIT_HOLD_S = 0.060


def limit(mix, ceiling: float = LIMIT_CEILING, rate: int = TARGET_RATE,
          chunk_s: float = 30.0):
    """Hold the mix under ``ceiling`` with a look-ahead peak limiter, in place.

    Scaling the whole mix by its single highest peak, which is what
    :func:`normalize` does, let one hot blend set the level of the entire
    hour: the last render peaked at 2.45 and came out 8 dB quieter throughout.
    This only turns down the moments that need it.

    The gain is the lowest required within a window that opens one attack
    ahead of a peak and holds one hold time after it, averaged over the attack
    so it eases rather than steps; that average can never overshoot the gain
    a peak needs. Processed in chunks so an hour of audio does not need
    several more copies of itself in memory.
    """
    np = _np()
    from scipy.ndimage import minimum_filter1d, uniform_filter1d

    attack = max(int(LIMIT_ATTACK_S * rate) | 1, 3)
    hold = int(LIMIT_HOLD_S * rate)
    size = hold + attack + 1
    margin = size + attack
    peak_in = float(np.abs(mix).max()) if len(mix) else 0.0
    if peak_in <= ceiling:
        return mix, peak_in

    step = int(chunk_s * rate)
    for start in range(0, len(mix), step):
        lo, hi = max(start - margin, 0), min(start + step + margin, len(mix))
        level = np.abs(mix[lo:hi]).max(axis=1)
        need = np.minimum(1.0, ceiling / np.maximum(level, 1e-9)).astype(np.float32)
        # Window [t - hold, t + attack]: a centred minimum of that width, shifted.
        held = minimum_filter1d(need, size, mode="nearest")
        shift = size // 2 - hold
        held = np.roll(held, -shift)
        if shift > 0:
            held[-shift:] = held[-shift - 1]
        elif shift < 0:
            held[:-shift] = held[-shift]
        # Causal average over the attack: centred average shifted back.
        gain = uniform_filter1d(held, attack, mode="nearest")
        gain = np.roll(gain, attack // 2)
        gain[:attack // 2] = gain[attack // 2]
        a, b = start - lo, start - lo + min(step, len(mix) - start)
        mix[start:start + (b - a)] *= np.minimum(gain[a:b], need[a:b])[:, None]
    return mix, peak_in


def write(mix, path: Path, rate: int = TARGET_RATE) -> None:
    """Write the mix. An MP3 goes out at 320 kbps constant bitrate.

    soundfile's MP3 default is variable bitrate around 140 kbps, which is
    what the earlier mixes were - fine for a phone, thin over a PA.
    """
    import soundfile as sf
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".mp3":
        sf.write(str(path), mix, rate, bitrate_mode="CONSTANT", compression_level=0.0)
    else:
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
        body = seg.length_ms
        start = pieces[-1].to_ms if pieces else 0
        pieces.append(Piece(index=i + 1, title=seg.title,
                            from_ms=start, to_ms=seg.at_ms + body))
    return pieces


def render_solo(transitions: list, entry_for, rate: int = TARGET_RATE,
                progress=None, match_loudness: bool = True):
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

    trims = loudness_trims(segments, rate) if match_loudness else {}

    moves: dict[int, tuple[Move, Move]] = {}

    def moves_for(i: int, n: int) -> tuple[Move, Move]:
        if i not in moves:
            t = transitions[i]
            moves[i] = build_moves(np, n, t.ingredients, getattr(t, "automation", None), rate)
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
        trim = trims.get(str(seg.path))
        if trim:
            body *= db_to_gain(trim)

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
                    body[-n:] = apply_roll(np, body[-n:], roll, rate, before=body[:-n])
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


def apply_roll(np, chunk, roll_ms: int, rate: int = TARGET_RATE, before=None):
    """Fill ``chunk`` with a repeated ``roll_ms`` - a beat repeat.

    This is what the Looping ingredient does to the outgoing track at a
    transition: instead of playing on, the last beat or bar before the blend
    is caught and repeated underneath it. Spotify calls it a roll.

    Which beat matters. ``before`` is the audio leading up to ``chunk``, and
    when it is given the loop is the ``roll_ms`` that *ends* at the out point:
    the beat just heard, held. Looping the first beat of the blend instead -
    the only behaviour before - caught the beat after: Suavemente's 1-beat
    roll went "suavemente eh eh eh" where Spotify plays "suavemente mente
    mente mente". Without ``before`` the opening of ``chunk`` is used.

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
    if before is not None and len(before) >= period:
        # What follows the held beat is the blend's own opening, so the fold
        # below joins each repeat to the next exactly as the first one joins
        # the out point.
        chunk = np.concatenate([before[-period:], chunk])

    blend = min(int(0.004 * rate), period // 8)      # about 4 ms
    if blend > 1 and period + blend <= len(chunk):
        loop = chunk[:period + blend].copy()
        ramp = np.linspace(0.0, 1.0, blend, dtype=np.float32)[:, None]
        # Fold what follows the loop over its opening, so the end of one
        # repeat runs into the start of the next without a step.
        loop[:blend] = loop[:blend] * ramp + loop[period:period + blend] * (1.0 - ramp)
        loop = loop[:period]
    else:
        loop = chunk[:period].copy()

    out = np.empty((n,) + chunk.shape[1:], dtype=chunk.dtype)
    for start in range(0, n, period):
        take = min(period, n - start)
        out[start:start + take] = loop[:take]
    return out
