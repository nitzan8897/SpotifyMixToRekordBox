"""Spotify's automix transition automation: volume + 3-band EQ curves.

Where this comes from
---------------------
The mix editor's DOM only gives three numbers per transition - the out point,
the in point and the overlap length. The *shape* of the transition lives in
the player, and reaches the client in a ``connect-state/v1/cluster`` response
as track metadata on the two tracks involved:

    outgoing (player_state.prev_tracks[-1].metadata)
        audio.fade_out_start_time      ms into the outgoing track
        audio.fade_out_duration        ms
        audio.fade_out_curves              volume
        audio.fade_out_eq_low_gain_curves  \\
        audio.fade_out_eq_mid_gain_curves   > 3-band EQ
        audio.fade_out_eq_high_gain_curves /

    incoming (player_state.track.metadata)
        audio.fade_in_start_time, audio.fade_in_duration, audio.fade_overlap
        audio.fade_in_curves, audio.fade_in_eq_{low,mid,high}_gain_curves

Important: Spotify only attaches these to the track that is *playing* and the
one that just played. ``next_tracks`` carry none. So a capture holds the
automation for one transition per cluster response - to get all of them, the
mix has to actually play through while Phase 1 records.

Curve format
------------
Each ``*_curves`` value is a JSON list of segments::

    [{"start_point": 0, "end_point": 0.5,
      "fade_curve": [{"x": 0, "y": 0}, {"x": 1, "y": 1}]}, ...]

``start_point``/``end_point`` are positions in the overlap, normalized 0..1.
Inside a segment, ``fade_curve`` x is normalized 0..1 *within that segment*
and y is the gain. Points are joined linearly; two points sharing an x are a
deliberate step discontinuity, which is how the hard swaps are expressed.

Gain scale
----------
Gains run 0..1 on the same scale as a mixer's EQ knob: **0.5 is the centre
detent (unity), 0 is a full kill, 1 is maximum boost.** These are Spotify's
internal knob positions; the dB they correspond to is not in the capture, so
nothing here converts to dB and invents precision that was never measured.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

#: gain value that means "no change" - the centre detent of an EQ knob
UNITY = 0.5

BANDS = ("low", "mid", "high")


@dataclass
class Segment:
    start: float
    end: float
    points: list[tuple[float, float]]


def parse_curve(raw: str | list | None) -> list[Segment]:
    """Parse one ``*_curves`` metadata value into segments."""
    if raw is None:
        return []
    data = json.loads(raw) if isinstance(raw, str) else raw
    out = []
    for s in data or []:
        pts = [(float(p["x"]), float(p["y"])) for p in s.get("fade_curve", [])]
        if not pts:
            continue
        out.append(Segment(start=float(s["start_point"]), end=float(s["end_point"]), points=pts))
    out.sort(key=lambda s: s.start)
    return out


def value_at(segments: list[Segment], pos: float) -> float | None:
    """Gain at ``pos``, a position in the overlap normalized to 0..1.

    At a step discontinuity this returns the value *after* the step, which is
    what the listener hears from that instant on.
    """
    if not segments:
        return None
    pos = min(max(pos, 0.0), 1.0)
    seg = None
    for s in segments:
        if s.start <= pos <= s.end:
            seg = s
            if pos < s.end:      # prefer the segment this position is inside
                break
    if seg is None:
        seg = segments[0] if pos < segments[0].start else segments[-1]

    span = seg.end - seg.start
    x = 0.0 if span <= 0 else (pos - seg.start) / span
    pts = seg.points
    if x <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= x <= x1:
            if x1 == x0:
                return y1        # step: take the value after it
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return pts[-1][1]


def step_points(segments: list[Segment], eps: float = 1e-6) -> list[float]:
    """Overlap positions (0..1) where the gain jumps rather than glides."""
    steps = []
    for seg in segments:
        span = seg.end - seg.start
        for (x0, y0), (x1, y1) in zip(seg.points, seg.points[1:]):
            if abs(x1 - x0) < eps and abs(y1 - y0) > eps:
                steps.append(seg.start + span * x0)
    return sorted(steps)


@dataclass
class Side:
    """One half of a transition: what happens to this track over the overlap."""
    volume: list[Segment] = field(default_factory=list)
    eq: dict[str, list[Segment]] = field(default_factory=dict)
    start_time_ms: int | None = None
    duration_ms: int | None = None

    def sample(self, pos: float) -> dict[str, float | None]:
        return {"volume": value_at(self.volume, pos),
                **{b: value_at(self.eq.get(b, []), pos) for b in BANDS}}


@dataclass
class Automation:
    """The complete shape of one transition."""
    overlap_ms: int | None = None
    outgoing: Side = field(default_factory=Side)
    incoming: Side = field(default_factory=Side)
    mode: str | None = None
    from_uri: str | None = None
    to_uri: str | None = None

    def swap_point(self) -> float | None:
        """Where the bass hands over, as a fraction of the overlap.

        The low band is the one that steps; that instant is the moment the
        mix actually changes hands.
        """
        outs = step_points(self.outgoing.eq.get("low", []))
        ins = step_points(self.incoming.eq.get("low", []))
        both = outs + ins
        return sum(both) / len(both) if both else None

    def swap_ms(self) -> int | None:
        p = self.swap_point()
        return None if p is None or self.overlap_ms is None else round(p * self.overlap_ms)


def _int(v) -> int | None:
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def parse_automation(prev_md: dict, cur_md: dict) -> Automation:
    """Build an :class:`Automation` from the two tracks' cluster metadata."""
    out = Side(
        volume=parse_curve(prev_md.get("audio.fade_out_curves")),
        eq={b: parse_curve(prev_md.get(f"audio.fade_out_eq_{b}_gain_curves")) for b in BANDS},
        start_time_ms=_int(prev_md.get("audio.fade_out_start_time")),
        duration_ms=_int(prev_md.get("audio.fade_out_duration")),
    )
    inc = Side(
        volume=parse_curve(cur_md.get("audio.fade_in_curves")),
        eq={b: parse_curve(cur_md.get(f"audio.fade_in_eq_{b}_gain_curves")) for b in BANDS},
        start_time_ms=_int(cur_md.get("audio.fade_in_start_time")),
        duration_ms=_int(cur_md.get("audio.fade_in_duration")),
    )
    return Automation(
        overlap_ms=_int(cur_md.get("audio.fade_overlap")),
        outgoing=out, incoming=inc, mode=cur_md.get("automix.mode"),
    )


# --------------------------------------------------------------------------
# Turning the curves into something a DJ can actually perform
# --------------------------------------------------------------------------
def knob(v: float | None) -> str:
    """Describe an EQ gain, where 0.5 is the centre detent."""
    if v is None:
        return "?"
    if v <= 0.001:
        return "KILL"
    if abs(v - UNITY) <= 0.001:
        return "centre"
    return f"{v:.2f}" + ("-" if v < UNITY else "+")


def fader(v: float | None) -> str:
    """Describe a volume level, where 1.0 is full and 0 is silent."""
    if v is None:
        return "?"
    if v <= 0.001:
        return "silent"
    if v >= 0.999:
        return "full"
    return f"{v*100:.0f}%"


def describe(a: Automation) -> list[str]:
    """A step-by-step account of the transition, in mixer terms."""
    lines: list[str] = []
    ov = a.overlap_ms
    lines.append(f"Overlap: {ov} ms" + (f"  ({ov/1000:.2f}s)" if ov else ""))
    if a.outgoing.start_time_ms is not None:
        lines.append(f"Outgoing starts fading at {a.outgoing.start_time_ms} ms")
    if a.incoming.start_time_ms is not None:
        lines.append(f"Incoming enters at {a.incoming.start_time_ms} ms into itself")

    swap = a.swap_point()
    if swap is not None:
        at = f"{swap*100:.0f}% of the overlap"
        if a.swap_ms() is not None:
            at += f" ({a.swap_ms()} ms in)"
        lines.append(f"Bass swap: {at}")

    lines.append("")
    lines.append(f"{'point':>7}  {'--- outgoing ---':^34}  {'--- incoming ---':^34}")
    lines.append(f"{'':>7}  {'vol':>8} {'low':>8} {'mid':>8} {'high':>7}"
                 f"  {'vol':>8} {'low':>8} {'mid':>8} {'high':>7}")
    marks = [0.0, 0.25, 0.49, 0.51, 0.75, 1.0]
    if swap is not None:
        marks = sorted(set(marks + [max(swap - 0.01, 0.0), min(swap + 0.01, 1.0)]))
    for p in marks:
        o, i = a.outgoing.sample(p), a.incoming.sample(p)
        lines.append(
            f"{p*100:>6.0f}%  {fader(o['volume']):>8} {knob(o['low']):>8} "
            f"{knob(o['mid']):>8} {knob(o['high']):>7}"
            f"  {fader(i['volume']):>8} {knob(i['low']):>8} "
            f"{knob(i['mid']):>8} {knob(i['high']):>7}")
    return lines


def to_dict(a: Automation) -> dict:
    """Serializable form, including a sampled version of each curve."""
    def side(s: Side) -> dict:
        return {
            "start_time_ms": s.start_time_ms,
            "duration_ms": s.duration_ms,
            "volume": [{"start": g.start, "end": g.end, "points": g.points} for g in s.volume],
            "eq": {b: [{"start": g.start, "end": g.end, "points": g.points}
                       for g in s.eq.get(b, [])] for b in BANDS},
        }
    return {
        "overlap_ms": a.overlap_ms,
        "mode": a.mode,
        "from_uri": a.from_uri,
        "to_uri": a.to_uri,
        "swap_point": a.swap_point(),
        "swap_ms": a.swap_ms(),
        "outgoing": side(a.outgoing),
        "incoming": side(a.incoming),
    }
