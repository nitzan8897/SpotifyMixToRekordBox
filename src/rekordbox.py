"""Writing a rekordbox XML collection + playlist from extracted transitions.

The thing to know up front: **rekordbox cannot play Spotify tracks.** Pioneer
dropped Spotify integration in 2020, and rekordbox XML addresses every track
by a local file path (``Location="file://localhost/..."``). So this module
turns the mix into a playlist over *your own audio files*, using the Spotify
capture only for the running order and the cue positions.

Tracks that have no local file are still written into the collection, with
their ``Location`` pointing at a path under a clearly fake directory, and
they are listed as unmatched so you can fix them up. rekordbox will show
them as missing files rather than silently dropping the cues.

Cue points
----------
Each transition contributes two memory cues:

    "→ NN out"   on the outgoing track, at the moment the crossfade starts
    "NN in →"    on the incoming track, at the point it comes in

and, when the capture has an overlap length, a memory loop over that same
span on each side ("→ NN loop" / "NN in loop"), so the crossfade window
itself is marked, not just its start.

A track that appears several times in the mix gets one cue per appearance.
Memory cues are ``POSITION_MARK`` with ``Num="-1"``; hot cues use ``Num``
0-7 (A-H), which is what ``--hot-cues`` switches to for the DDJ-200's pads.

What this does **not** carry: Spotify's automix runs a volume curve and a
3-band EQ curve across the overlap. Those *are* captured - see
:mod:`src.automix`, which reads them out of the player's cluster
response - but rekordbox XML has no field to put them in. They are mixer
automation, not track metadata. The loop marker above is the practical
stand-in: it puts the DJ's hands on the right bars, and the cue sheet written
by Phase 2 spells out the blend to perform over them.

A warning about cue accuracy
----------------------------
Every cue here is an **absolute offset into Spotify's timeline**. That only
lines up if the local file is the same recording Spotify streamed. A download
matched by title is very often the right song in a different edit, and then
every cue on it lands somewhere else in the music. :func:`match_local_file`
compares durations to catch that, and Phase 3 reports the offenders.
"""
from __future__ import annotations

import logging
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from src.align import Alignment, align

log = logging.getLogger("src.rekordbox")

AUDIO_SUFFIXES = {".mp3", ".m4a", ".aac", ".wav", ".aiff", ".aif", ".flac", ".ogg", ".mp4"}

KIND_BY_SUFFIX = {
    ".mp3": "MP3 File", ".m4a": "M4A File", ".aac": "AAC File", ".wav": "WAV File",
    ".aiff": "AIFF File", ".aif": "AIFF File", ".flac": "FLAC File", ".ogg": "OGG File",
    ".mp4": "MP4 File",
}

# Camelot wheel -> the key names rekordbox writes in Tonality.
CAMELOT_TO_KEY = {
    "1A": "Abm", "2A": "Ebm", "3A": "Bbm", "4A": "Fm", "5A": "Cm", "6A": "Gm",
    "7A": "Dm", "8A": "Am", "9A": "Em", "10A": "Bm", "11A": "F#m", "12A": "Dbm",
    "1B": "B", "2B": "F#", "3B": "Db", "4B": "Ab", "5B": "Eb", "6B": "Bb",
    "7B": "F", "8B": "C", "9B": "G", "10B": "D", "11B": "A", "12B": "E",
}

MISSING_DIR = "SPOTIFY_TRACK_NOT_FOUND_LOCALLY"

_WS = re.compile(r"\s+")
_BIDI = dict.fromkeys(map(ord, "\u200e\u200f\u202a\u202b\u202c\u2066\u2067\u2068\u2069"))
# Things that differ between a Spotify title and a downloaded filename.
_NOISE = re.compile(
    r"\b(official|video|audio|lyrics?|hd|hq|full|version|remaster(ed)?|explicit|"
    r"free\s*download|bootleg|extended|radio\s*edit|clip)\b")


def fold(s: str | None) -> str:
    """Normalize a title/artist/filename for loose comparison."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s.translate(_BIDI))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[\[\(][^\])]*[\])]", " ", s)   # (feat. X), [Slowed]
    s = _NOISE.sub(" ", s)
    s = re.sub(r"[^\w\s]", " ", s)
    return _WS.sub(" ", s).strip()


def tokens(s: str) -> set[str]:
    return {t for t in fold(s).split() if len(t) > 1}


#: How far a local file's length may differ from Spotify's before it is
#: treated as a different edit. Encoder padding and the odd trimmed tail are
#: worth a few hundred ms; a second and a half is another recording.
DURATION_TOLERANCE_MS = 1500


@dataclass
class LocalFile:
    path: Path
    stem_tokens: set[str] = field(default_factory=set)
    #: playing length in ms, when it could be read
    duration_ms: int | None = None


def read_duration_ms(path: Path) -> int | None:
    """Length of an audio file in ms, or None if it cannot be read.

    Uses mutagen when it is installed. It is optional: without it every
    duration is None and matching falls back to filenames alone, which is
    what this module did before.
    """
    try:
        import mutagen
    except ImportError:
        return None
    try:
        f = mutagen.File(str(path))
        return int(round(f.info.length * 1000)) if f and f.info else None
    except Exception:                                    # unreadable/corrupt
        return None


def scan_music_dirs(dirs: list[Path]) -> list[LocalFile]:
    """Every audio file under the given directories, indexed by filename tokens."""
    files: list[LocalFile] = []
    for d in dirs:
        if not d.is_dir():
            log.warning("Music directory does not exist, skipping: %s", d)
            continue
        for p in sorted(d.rglob("*")):
            if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES:
                files.append(LocalFile(path=p, stem_tokens=tokens(p.stem),
                                       duration_ms=read_duration_ms(p)))
    known = sum(1 for f in files if f.duration_ms is not None)
    log.info("Indexed %d audio file(s)%s.", len(files),
             "" if known == len(files) else f" ({known} with a readable duration)")
    if files and not known:
        log.warning("Could not read the length of any file, so cue positions cannot be checked "
                    "against Spotify's timeline. Install mutagen (pip install mutagen) to catch "
                    "downloads that are a different edit of the right song.")
    return files


def match_local_file(title: str | None, artists: list[str], files: list[LocalFile],
                     duration_ms: int | None = None) -> tuple[Path | None, int | None]:
    """Best local file for a track, as ``(path, duration_delta_ms)``.

    Scores on how much of the title appears in the filename, with the artist
    as a bonus. Requires most of the title to be present, so a wrong file is
    left unmatched rather than guessed at.

    ``duration_ms`` is Spotify's length for the track. When it is known, it
    breaks ties in favour of the file that is actually the same recording -
    the reason this matters is that a download by title can easily be the
    right song in the wrong edit, and every cue position in this tool is an
    absolute offset into Spotify's timeline. A file 10 seconds longer is not
    a worse match, it is a different arrangement, and every cue on it lands
    somewhere else in the music.

    ``duration_delta_ms`` is ``file - spotify``, or None when either length
    is unknown. The caller decides what to do about it.
    """
    want = tokens(title or "")
    if not want:
        return None, None
    artist_tokens = set().union(*(tokens(a) for a in artists)) if artists else set()

    best, best_score, best_delta = None, 0.0, None
    for f in files:
        overlap = len(want & f.stem_tokens) / len(want)
        if overlap < 0.75:
            continue
        score = overlap + 0.3 * bool(artist_tokens & f.stem_tokens)
        delta = None
        if duration_ms and f.duration_ms:
            delta = f.duration_ms - duration_ms
            # A length that agrees is strong evidence of the same recording;
            # keep it below the artist bonus so it breaks ties without
            # letting a coincidental length outrank the right title.
            if abs(delta) <= DURATION_TOLERANCE_MS:
                score += 0.2
        if score > best_score:
            best, best_score, best_delta = f.path, score, delta
    return best, best_delta


def location_uri(path: Path) -> str:
    """rekordbox's file URI form: file://localhost/ + percent-encoded path."""
    p = str(path).replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p                      # C:/x -> /C:/x
    return "file://localhost" + quote(p, safe="/:")


@dataclass
class Cue:
    name: str
    seconds: float
    #: set for a loop marker (POSITION_MARK Type="4"): where the loop ends.
    #: None means a plain point cue (Type="0").
    loop_end: float | None = None


@dataclass
class Entry:
    """One track in the rekordbox collection."""
    track_id: int
    title: str
    artists: list[str]
    bpm: int | None
    camelot: str | None
    duration_ms: int | None
    spotify_id: str | None
    local_path: Path | None
    cues: list[Cue] = field(default_factory=list)
    #: local file length minus Spotify's, in ms; None when either is unknown
    duration_delta_ms: int | None = None
    #: how the local file sits against Spotify's timeline
    alignment: Alignment | None = None

    @property
    def offset_ms(self) -> int:
        """Shift applied to every cue so it lands on the same music locally."""
        return self.alignment.offset_ms if self.alignment else 0

    @property
    def last_used_ms(self) -> int:
        """The latest moment the mix actually plays of this track."""
        if not self.cues:
            return 0
        return int(round(1000 * max(max(c.seconds, c.loop_end or 0) for c in self.cues)))

    def covers_its_cues(self) -> bool:
        """True when the local file reaches past everything the mix uses of it."""
        if self.alignment is None or self.alignment.probe is None:
            return False
        return self.alignment.probe.duration_ms > self.last_used_ms > 0

    def is_different_edit(self) -> bool:
        """True when the matched file is too far off to carry these cues.

        The alignment verdict leads, because it compares *content* length - the
        audio once the silence at each end is discounted. Raw file length alone
        misleads in both directions: a download can be seconds longer purely
        because of an outro tail and still be the same recording, and one that
        starts with silence is handled by shifting rather than rejecting.

        One more allowance on top of that verdict. A track's cues sit where its
        blends are, which is usually nowhere near its end, so a file that falls
        short only *after* the last moment the mix plays of it still carries
        every cue. Reporting that as a different edit sends someone hunting for
        a replacement that would change nothing - and in one real case no
        better upload existed anyway.

        Without an alignment (no decoder installed, or --no-align) this falls
        back to raw length, which is then the best available signal.
        """
        if self.alignment is not None and self.alignment.probe is not None:
            if not self.alignment.judged:
                # No Spotify duration to compare against. Unknown is not an
                # accusation: without a reference there is nothing to be wrong
                # about, and saying otherwise sends someone replacing a file
                # that may be perfectly correct.
                return False
            if self.alignment.same_recording:
                return False
            short = (self.alignment.delta_ms or 0) < 0
            if short and self.covers_its_cues():
                return False
            return True
        return (self.duration_delta_ms is not None
                and abs(self.duration_delta_ms) > DURATION_TOLERANCE_MS)

    @property
    def artist_string(self) -> str:
        return ", ".join(self.artists)

    def location(self) -> str:
        if self.local_path:
            return location_uri(self.local_path)
        stem = re.sub(r'[<>:"/\\|?*]', "_", f"{self.artist_string} - {self.title}")[:150]
        return location_uri(Path(f"/{MISSING_DIR}/{stem}.mp3"))

    def kind(self) -> str:
        if self.local_path:
            return KIND_BY_SUFFIX.get(self.local_path.suffix.lower(), "MP3 File")
        return "MP3 File"


def _track_key(title: str | None, spotify_id: str | None) -> str:
    return spotify_id or fold(title) or "?"


def build_entries(transitions: list, files: list[LocalFile],
                  auto_align: bool = True) -> list[Entry]:
    """Collect the distinct tracks of the mix, in running order, with their cues.

    With ``auto_align`` (the default) each matched file is measured and its
    cues are shifted so they land on the music Spotify measured them from -
    see :mod:`src.align`. The shift is applied when the XML is written,
    so ``Cue.seconds`` stays in Spotify's timeline throughout.
    """
    entries: dict[str, Entry] = {}
    order: list[str] = []

    def ensure(track) -> Entry:
        key = _track_key(track.title, track.spotify_id)
        if key not in entries:
            path, delta = (match_local_file(track.title, track.artists, files,
                                            track.duration_ms)
                           if files else (None, None))
            entries[key] = Entry(
                track_id=len(entries) + 1,
                title=track.title or "Unknown",
                artists=list(track.artists),
                bpm=track.bpm,
                camelot=track.camelot,
                duration_ms=track.duration_ms,
                spotify_id=track.spotify_id,
                local_path=path,
                duration_delta_ms=delta,
                alignment=align(path, track.duration_ms) if path and auto_align else None,
            )
            order.append(key)
        return entries[key]

    for t in transitions:
        a, b = ensure(t.from_track), ensure(t.to_track)
        n = t.index + 1
        if t.out_point_ms is not None:
            a.cues.append(Cue(f"\u2192 {n:02d} out", t.out_point_ms / 1000.0))
        if t.in_point_ms is not None:
            b.cues.append(Cue(f"{n:02d} in \u2192", t.in_point_ms / 1000.0))
        # Loops. Two different things can produce one, and they are not the
        # same length:
        #
        #  * the transition's own Loop ingredient, when the editor has one set
        #    ("2 beat loop"). Its length follows from the beat count and the
        #    incoming track's BPM, and it is the loop Spotify actually plays.
        #  * otherwise the crossfade window, as a guide to where the blend
        #    happens. Spotify's volume/EQ/filter automation over that span has
        #    no home in rekordbox XML, so marking the window is the closest
        #    rekordbox can hold: it puts the DJ's hands on the right bars.
        beats = t.loop_beats()
        ingredient_loop = t.loop_length_ms()
        if ingredient_loop and t.out_point_ms is not None:
            # On the outgoing track, at its out point: the player reports the
            # loop as fade_out_roll_time, so it is a beat repeat on the track
            # that is leaving, not an entry loop on the one arriving.
            start = t.out_point_ms / 1000.0
            a.cues.append(Cue(f"{n:02d} roll {beats}b", start,
                              loop_end=start + ingredient_loop / 1000.0))
        elif t.overlap_ms is not None and t.overlap_ms > 0:
            if t.out_point_ms is not None:
                start = t.out_point_ms / 1000.0
                a.cues.append(Cue(f"→ {n:02d} loop", start,
                                  loop_end=start + t.overlap_ms / 1000.0))
            if t.in_point_ms is not None:
                start = t.in_point_ms / 1000.0
                b.cues.append(Cue(f"{n:02d} in loop", start,
                                  loop_end=start + t.overlap_ms / 1000.0))

    return [entries[k] for k in order]


def playlist_order(transitions: list, entries: list[Entry]) -> list[Entry]:
    """The tracks in the order the mix plays them.

    The chain is A->B, B->C, ..., so the running order is each transition's
    outgoing track followed by the very last incoming one.
    """
    by_key = {_track_key(e.title, e.spotify_id): e for e in entries}
    out: list[Entry] = []
    for t in transitions:
        e = by_key.get(_track_key(t.from_track.title, t.from_track.spotify_id))
        if e is not None:
            out.append(e)
    if transitions:
        last = by_key.get(_track_key(transitions[-1].to_track.title,
                                     transitions[-1].to_track.spotify_id))
        if last is not None:
            out.append(last)
    return out


def build_xml(entries: list[Entry], order: list[Entry], playlist_name: str,
              hot_cues: bool = False, write_grid: bool = False) -> ET.ElementTree:
    root = ET.Element("DJ_PLAYLISTS", {"Version": "1.0.0"})
    ET.SubElement(root, "PRODUCT",
                  {"Name": "src", "Version": "1.0", "Company": "SpotifyMixToRekordBox"})

    collection = ET.SubElement(root, "COLLECTION", {"Entries": str(len(entries))})
    for e in entries:
        attrs = {
            "TrackID": str(e.track_id),
            "Name": e.title,
            "Artist": e.artist_string,
            "Kind": e.kind(),
            "Location": e.location(),
        }
        if e.duration_ms:
            attrs["TotalTime"] = str(round(e.duration_ms / 1000))
        if e.bpm:
            attrs["AverageBpm"] = f"{e.bpm:.2f}"
        if e.camelot and e.camelot in CAMELOT_TO_KEY:
            attrs["Tonality"] = CAMELOT_TO_KEY[e.camelot]
        if e.spotify_id:
            attrs["Comments"] = f"spotify:track:{e.spotify_id}"
        track_el = ET.SubElement(collection, "TRACK", attrs)

        if e.bpm and write_grid:
            # Off by default, and rightly so: this claims beat 1 falls exactly
            # at 0.000s at a constant whole-number BPM. Any lead-in silence or
            # real tempo drift makes that grid wrong, and rekordbox trusts an
            # imported grid instead of analyzing. Cues do not depend on it -
            # POSITION_MARK Start is absolute seconds - so omitting this just
            # lets rekordbox work the grid out properly.
            ET.SubElement(track_el, "TEMPO", {
                "Inizio": "0.000", "Bpm": f"{e.bpm:.2f}", "Metro": "4/4", "Battito": "1"})

        # Cue positions are held in Spotify's timeline; the shift onto this
        # particular file happens here, once, at the point of writing.
        shift = e.offset_ms / 1000.0
        cues = list(e.cues)
        if shift:
            # Mark where Spotify's 0:00 actually falls, so the shift is
            # visible in rekordbox rather than an invisible fudge. Position 0
            # in Spotify's timeline *is* the shift once written out below.
            cues.append(Cue("spotify 0:00", 0.0))

        for i, cue in enumerate(sorted(cues, key=lambda c: c.seconds)):
            mark = {
                "Name": cue.name,
                "Type": "4" if cue.loop_end is not None else "0",
                "Start": f"{max(cue.seconds + shift, 0.0):.3f}",
                "Num": str(i) if hot_cues and i < 8 else "-1",
            }
            if cue.loop_end is not None:
                mark["End"] = f"{max(cue.loop_end + shift, 0.0):.3f}"
            ET.SubElement(track_el, "POSITION_MARK", mark)

    playlists = ET.SubElement(root, "PLAYLISTS")
    root_node = ET.SubElement(playlists, "NODE", {"Type": "0", "Name": "ROOT", "Count": "1"})
    node = ET.SubElement(root_node, "NODE", {
        "Name": playlist_name, "Type": "1", "KeyType": "0", "Entries": str(len(order))})
    for e in order:
        ET.SubElement(node, "TRACK", {"Key": str(e.track_id)})

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    return tree
