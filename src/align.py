"""Lining a downloaded file up with Spotify's timeline.

The problem
-----------
Every cue this project writes is an absolute offset into *Spotify's* copy of
a track. A file downloaded from anywhere else is a different encode of the
same music, and encodes disagree about where the music starts: a YouTube rip
routinely carries a second or two of silence in front, and sometimes a long
tail. Feed such a file a cue measured at 115.800 s and the cue lands
somewhere else in the song.

What can and cannot be measured
-------------------------------
Spotify's audio is not in the capture, so the two waveforms cannot be
compared directly. Two things *are* known: how long Spotify says the track
is, and the local file itself. From those:

* the local file's leading silence and trailing silence, measured here;
* its *content length* - what is left once both are removed.

When the content length matches Spotify's duration, the two are the same
recording and any difference in total length is silence. If that silence sits
at the front, Spotify's ``t=0`` is at the end of it, and every cue needs
shifting by exactly that much. That is the case this module detects.

When the content lengths disagree by more than a moment, the file is a
different edit and no single shift can rescue it - the music itself differs.
Those are reported, never silently "corrected".
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("src.align")

#: A frame quieter than this counts as silence.
SILENCE_DB = -45.0
#: Analysis frame length.
FRAME_MS = 20
#: Leading silence below this is not worth shifting for.
MIN_SHIFT_MS = 120
#: How far content length may differ from Spotify's and still be the same cut.
CONTENT_TOLERANCE_MS = 1500


@dataclass
class Probe:
    """What was measured from one audio file."""
    duration_ms: int
    lead_in_ms: int
    tail_ms: int

    @property
    def content_ms(self) -> int:
        return self.duration_ms - self.lead_in_ms - self.tail_ms


@dataclass
class Alignment:
    """How a local file sits against Spotify's timeline."""
    probe: Probe | None = None
    spotify_ms: int | None = None
    #: add this to every Spotify cue position to hit the same music locally
    offset_ms: int = 0
    #: True when the file is the same recording, just packaged differently
    same_recording: bool = False
    reason: str = "not analysed"

    @property
    def delta_ms(self) -> int | None:
        if self.probe is None or self.spotify_ms is None:
            return None
        return self.probe.duration_ms - self.spotify_ms


def probe(path: Path, silence_db: float = SILENCE_DB, frame_ms: int = FRAME_MS) -> Probe | None:
    """Measure a file's length and the silence at each end.

    Returns None when the audio cannot be decoded - soundfile and numpy are
    optional, and a missing decoder must not break the export.
    """
    try:
        import numpy as np
        import soundfile as sf
    except ImportError:
        return None
    try:
        data, rate = sf.read(str(path), dtype="float32", always_2d=True)
    except Exception as e:                                  # unreadable/corrupt
        log.debug("Could not decode %s: %s", path, e)
        return None
    if not len(data) or not rate:
        return None

    mono = data.mean(axis=1)
    total_ms = int(round(len(mono) / rate * 1000))

    n = max(1, int(rate * frame_ms / 1000))
    usable = len(mono) // n * n
    if usable < n:
        return Probe(duration_ms=total_ms, lead_in_ms=0, tail_ms=0)
    frames = mono[:usable].reshape(-1, n)
    rms = np.sqrt((frames ** 2).mean(axis=1) + 1e-12)
    db = 20 * np.log10(rms + 1e-12)
    loud = np.flatnonzero(db > silence_db)
    if not len(loud):
        return Probe(duration_ms=total_ms, lead_in_ms=0, tail_ms=0)

    lead = int(round(loud[0] * n / rate * 1000))
    tail = int(round((len(frames) - 1 - loud[-1]) * n / rate * 1000))
    return Probe(duration_ms=total_ms, lead_in_ms=lead, tail_ms=tail)


def align(path: Path, spotify_ms: int | None) -> Alignment:
    """Work out the shift, if any, that puts Spotify's cues on this file."""
    p = probe(path)
    if p is None:
        return Alignment(reason="could not decode the audio")
    if not spotify_ms:
        return Alignment(probe=p, reason="no Spotify duration to compare against")

    a = Alignment(probe=p, spotify_ms=spotify_ms)
    content_gap = p.content_ms - spotify_ms

    delta = p.duration_ms - spotify_ms

    # Two ways to prove a different edit, and both are needed.
    #
    # Too much music: the content overruns Spotify's whole duration, so there
    # is more here than Spotify has room for.
    if content_gap > CONTENT_TOLERANCE_MS:
        a.reason = (f"local audio holds {content_gap} ms more music than Spotify's whole "
                    "duration, so this is a different edit")
        return a
    # Too little: the file is simply shorter than Spotify says the track is.
    # Content being *slightly* short is normal, since Spotify's duration
    # includes whatever padding its own master carries - but a file that ends
    # seconds early is a different cut, not a different encode. Missing this
    # was how a 9-second-short game edit passed as the same recording.
    if delta < -CONTENT_TOLERANCE_MS:
        a.reason = (f"local audio is {-delta} ms shorter than Spotify's duration, so this is "
                    "a different edit")
        return a
    a.same_recording = True

    if delta <= MIN_SHIFT_MS:
        a.reason = "same length as Spotify, no shift needed"
        return a
    if p.lead_in_ms < MIN_SHIFT_MS:
        a.reason = (f"{delta} ms longer than Spotify, but the extra is at the end "
                    "(no leading silence), so cues still line up")
        return a

    # The file is longer and it starts with silence: the extra is at the front,
    # capped at the lead-in so a long tail is never mistaken for a head start.
    a.offset_ms = min(p.lead_in_ms, delta)
    a.reason = (f"{p.lead_in_ms} ms of silence before the music and {delta} ms longer than "
                f"Spotify: Spotify's 0:00 sits at {a.offset_ms} ms into this file")
    return a
