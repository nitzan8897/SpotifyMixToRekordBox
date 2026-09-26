"""Turning a Phase 1 capture into structured transitions.

Where the numbers come from
---------------------------
The mix editor keeps each transition in the DOM as two waveform sliders,
tagged ``data-transition-waveform="trackA"`` and ``"trackB"``:

    trackA.now   how far into the outgoing track the overlap begins
    trackB.now   how far into the incoming track the overlap begins
    trackB.min   the negative of the overlap length

That reading is not guesswork. Phase 1 caught one ``connect-state/v1/cluster``
response while a transition preview was playing, and its player state lines up
with the snapshot of the same transition exactly:

    trackA.now  55630  ==  prev_tracks[-1].metadata['audio.fade_out_start_time']
    trackB.now      0  ==  track.metadata['audio.fade_in_start_time']
    -trackB.min  2510  ==  track.metadata['audio.fade_overlap']
                         ==  audio.fade_in_duration == audio.fade_out_duration

So one transition is confirmed against the player itself; the rest are read
the same way. :func:`cluster_ground_truth` re-checks that whenever a cluster
response is present in the run, and the check is reported, not assumed.

Track identity
--------------
The editor DOM carries the title, artists, BPM and Camelot key of both sides
but no track URI. Phase 1 also captures ``metadata/{n}/track/{gid}`` bodies,
which carry the gid, name, artists, duration and ISRC. Those are matched back
by title+artist, and the gid converts to the usual base62 track id.
"""
from __future__ import annotations

import html as html_mod
import json
import logging
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path

from src.automix import describe, parse_automation
from src.ingredients import (loop_ms, parse_ingredients, parse_mode,
                             summarize as ingredients_summary)
from src.automix import to_dict as automation_to_dict

log = logging.getLogger("src.transitions")

# Spotify orders lowercase before uppercase. Getting this backwards yields an
# id with the right letters in the wrong case, which still looks plausible -
# gid_to_base62 is checked against the URIs in the cluster capture for that.
BASE62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
# The editor writes the tempo either way round depending on the UI language:
# "BPM 170" in a left-to-right build, "170 BPM" in a right-to-left one. Both
# have been seen in real captures, and reading only one of them silently loses
# every BPM - which in turn loses the loop lengths, since those are computed
# from beats and tempo.
_BPM = re.compile(r"(?:BPM\s*(\d{2,3})|(\d{2,3})\s*BPM)", re.I)
_CAMELOT = re.compile(r"^(1[0-2]|[1-9])[AB]$")
_IMAGE = re.compile(r"spotify:image:([0-9a-f]{40})")
_BIDI = dict.fromkeys(map(ord, "‎‏‪‫‬⁦⁧⁨⁩"))

# The editor names each side's boxes with CSS view-transition names.
_SIDE_MARKER = "mixing-metadata-track-{side}-trailing"


class ExtractError(Exception):
    """The capture does not contain what Phase 2 needs."""


def gid_to_base62(gid_hex: str) -> str | None:
    """Spotify's 32-hex track gid -> the 22-char base62 id used in URIs."""
    try:
        n = int(gid_hex, 16)
    except (TypeError, ValueError):
        return None
    out = []
    for _ in range(22):
        n, rem = divmod(n, 62)
        out.append(BASE62[rem])
    return "".join(reversed(out))


def normalize(s: str | None) -> str:
    """Fold a title/artist for comparison: no case, accents or punctuation."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s.translate(_BIDI))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^\w\s]", " ", s.lower())
    return _WS.sub(" ", s).strip()


@dataclass
class Track:
    title: str | None = None
    artists: list[str] = field(default_factory=list)
    bpm: int | None = None
    camelot: str | None = None
    image_id: str | None = None
    # filled in from the captured track metadata, when a match is found
    spotify_id: str | None = None
    duration_ms: int | None = None
    isrc: str | None = None

    @property
    def uri(self) -> str | None:
        return f"spotify:track:{self.spotify_id}" if self.spotify_id else None

    def label(self) -> str:
        who = ", ".join(self.artists) if self.artists else "?"
        return f"{self.title or '?'} - {who}"


@dataclass
class Transition:
    index: int
    snapshot: str
    from_track: Track
    to_track: Track
    #: ms into the outgoing track where the overlap starts
    out_point_ms: int | None = None
    #: ms into the incoming track where the overlap starts
    in_point_ms: int | None = None
    #: length of the crossfade in ms (from -trackB.min)
    overlap_ms: int | None = None
    #: the editor's allowed drag ranges, kept for sanity-checking
    out_range: list[int | None] = field(default_factory=lambda: [None, None])
    in_range: list[int | None] = field(default_factory=lambda: [None, None])
    #: the five transition "ingredients" as chosen in the editor:
    #: volume / eq / filter / effects / loop. See src.ingredients.
    ingredients: dict = field(default_factory=dict)
    #: "custom" or "automatic", from the chip above the incoming track
    mode: str | None = None
    #: position in the mix's running order, read straight off the page: the
    #: index of the chip whose editor was open. None when no chip was open,
    #: which means the snapshot caught a stale editor panel.
    order_index: int | None = None
    #: how many transition chips the page showed, i.e. how many transitions
    #: the mix has in total. Lets the extract say whether any were missed.
    chips_on_page: int | None = None

    def loop_beats(self) -> int | None:
        return ((self.ingredients.get("loop") or {}).get("beats")) if self.ingredients else None

    def loop_length_ms(self) -> int | None:
        """The loop ingredient's length on the incoming track, in ms."""
        return loop_ms(self.loop_beats(), self.to_track.bpm)

    def style(self) -> str:
        return ingredients_summary(self.ingredients) if self.ingredients else ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["from_track"]["uri"] = self.from_track.uri
        d["to_track"]["uri"] = self.to_track.uri
        return d


# --------------------------------------------------------------------------
# Track catalogue, from the captured metadata bodies
# --------------------------------------------------------------------------
def load_track_catalog(run_dir: Path) -> list[dict]:
    """Every ``metadata/{n}/track/{gid}`` body in the run, parsed."""
    index = run_dir / "index.jsonl"
    if not index.is_file():
        raise ExtractError(f"No index.jsonl in {run_dir}. Is that a Phase 1 run folder?")

    catalog: list[dict] = []
    seen: set[str] = set()
    for line in index.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        ep = row.get("endpoint") or ""
        if "/metadata/" not in ep or "/track/" not in ep or not row.get("body_file"):
            continue
        path = run_dir / row["body_file"]
        if not path.is_file():
            continue
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        gid = body.get("gid")
        if not gid or gid in seen:
            continue
        seen.add(gid)
        images = []
        for img in ((body.get("album") or {}).get("cover_group") or {}).get("image", []):
            fid = img.get("file_id")
            if fid:
                images.append(fid[16:])  # the size prefix differs from the DOM's
        isrc = next((e.get("id") for e in body.get("external_id", [])
                     if e.get("type") == "isrc"), None)
        catalog.append({
            "gid": gid,
            "spotify_id": gid_to_base62(gid),
            "title": body.get("name"),
            "artists": [a.get("name") for a in body.get("artist", []) if a.get("name")],
            "duration_ms": body.get("duration"),
            "isrc": isrc,
            "image_suffixes": images,
        })
    return catalog


def match_track(track: Track, catalog: list[dict]) -> dict | None:
    """Find the captured metadata for a track seen in the editor DOM.

    Title+artist first. The album image hash is only a tiebreak: an album
    cover is shared by every track on it, so it cannot identify one alone.
    """
    want_title = normalize(track.title)
    if not want_title:
        return None
    by_title = [c for c in catalog if normalize(c["title"]) == want_title]
    if not by_title:
        # The editor truncates long titles with an ellipsis; fall back to prefix.
        stem = want_title.rstrip(". ")
        by_title = [c for c in catalog if normalize(c["title"]).startswith(stem)] if stem else []
    if not by_title:
        return None
    if len(by_title) == 1:
        return by_title[0]

    want_artists = {normalize(a) for a in track.artists}
    scored = []
    for c in by_title:
        have = {normalize(a) for a in c["artists"]}
        score = len(want_artists & have)
        if track.image_id and track.image_id[16:] in c["image_suffixes"]:
            score += 1
        scored.append((score, c))
    scored.sort(key=lambda x: x[0], reverse=True)
    if scored[0][0] == 0:
        return None
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        log.warning("Ambiguous match for %r - %d candidates scored equally; leaving unresolved.",
                    track.title, sum(1 for s, _ in scored if s == scored[0][0]))
        return None
    return scored[0][1]


# --------------------------------------------------------------------------
# Parsing one DOM snapshot
# --------------------------------------------------------------------------
def _clean(segment: str) -> list[str]:
    # Unescape after stripping tags, so an escaped "&lt;div&gt;" in the text
    # cannot turn into something that looks like markup.
    text = html_mod.unescape(_TAG.sub(" | ", segment)).translate(_BIDI)
    parts = [p.strip() for p in _WS.sub(" ", text).split("|")]
    return [p for p in parts if p]


def _looks_like_metadata(part: str) -> bool:
    """True for a fragment that is a number or tempo rather than a name.

    A belt-and-braces guard on the artist list: if a future build reorders the
    tempo label again, the worst case is a missing BPM, not an artist called
    "170 BPM".
    """
    p = part.strip()
    return bool(re.fullmatch(r"[\d\s.]+", p)) or "bpm" in p.lower()


def parse_side(html: str, side: str) -> Track | None:
    """Read one half of the transition editor (``side`` is 'a' or 'b')."""
    marker = _SIDE_MARKER.format(side=side)
    i = html.find(marker)
    if i < 0:
        return None
    # Skip past the rest of the style attribute the marker lives in.
    start = html.find('">', i)
    if start < 0:
        return None
    segment = html[start + 2:start + 2600]

    # The cover image sits either just before the marker or just inside the
    # block it opens, depending on the build. Take whichever is nearest.
    before = list(_IMAGE.finditer(html[max(0, i - 2500):i]))
    after = _IMAGE.search(segment)
    if before:
        image_id = before[-1].group(1)
    else:
        image_id = after.group(1) if after else None

    parts = _clean(segment)
    track = Track(image_id=image_id)
    if "•" in parts:  # the bullet between title and artists
        b = parts.index("•")
        track.title = " ".join(parts[:b]).strip() or None
        rest = parts[b + 1:]
    else:
        track.title = parts[0] if parts else None
        rest = parts[1:]

    for p in rest:
        if p == ",":
            continue
        m = _BPM.search(p)
        if m:
            track.bpm = int(m.group(1) or m.group(2))
            continue
        if _CAMELOT.match(p):
            track.camelot = p
            continue
        # Artists come first; once BPM/key have been seen we are past them.
        if track.bpm is None and track.camelot is None and len(p) <= 60:
            if _looks_like_metadata(p):
                continue          # never let a stray tempo become an artist
            track.artists.append(p)
        elif track.bpm is not None and track.camelot is not None:
            break
    return track


def parse_sliders(snapshot_json: Path) -> dict[str, dict]:
    """The two waveform sliders of a snapshot, keyed 'trackA' / 'trackB'."""
    out: dict[str, dict] = {}
    try:
        frames = json.loads(snapshot_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    for frame in frames:
        for s in frame.get("sliders", []):
            which = (s.get("data") or {}).get("transitionWaveform")
            if which:
                out[which] = s
    return out


def _as_int(v) -> int | None:
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def parse_snapshot(html_path: Path, index: int) -> Transition | None:
    """Build a Transition from one DOM snapshot, or None if the editor was closed."""
    json_path = html_path.with_suffix(".json")
    html = html_path.read_text(encoding="utf-8", errors="replace")
    a, b = parse_side(html, "a"), parse_side(html, "b")
    if not a or not b or not a.title or not b.title:
        return None

    mode = parse_mode(html)
    sliders = parse_sliders(json_path)
    sa, sb = sliders.get("trackA", {}), sliders.get("trackB", {})
    out_point = _as_int(sa.get("now"))
    in_point = _as_int(sb.get("now"))
    b_min = _as_int(sb.get("min"))
    overlap = -b_min if b_min is not None and b_min < 0 else None

    return Transition(
        index=index, snapshot=html_path.name, from_track=a, to_track=b,
        out_point_ms=out_point, in_point_ms=in_point, overlap_ms=overlap,
        out_range=[_as_int(sa.get("min")), _as_int(sa.get("max"))],
        in_range=[b_min, _as_int(sb.get("max"))],
        ingredients=parse_ingredients(html),
        mode=mode.get("value") or mode.get("raw"),
        order_index=mode.get("index"),
        chips_on_page=mode.get("chips") or None,
    )


# --------------------------------------------------------------------------
# Whole-run extraction
# --------------------------------------------------------------------------
def cluster_ground_truth(run_dir: Path) -> dict | None:
    """The fade values the player itself reported, if a cluster body was caught.

    This is what makes the DOM reading verifiable rather than assumed.
    """
    index = run_dir / "index.jsonl"
    if not index.is_file():
        return None
    for line in index.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if "connect-state" not in (row.get("endpoint") or "") or not row.get("body_file"):
            continue
        path = run_dir / row["body_file"]
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        state = body.get("player_state") or {}
        cur = (state.get("track") or {}).get("metadata") or {}
        prevs = state.get("prev_tracks") or []
        prev = (prevs[-1].get("metadata") or {}) if prevs else {}
        if "audio.fade_in_start_time" not in cur:
            continue
        auto = parse_automation(prev, cur)
        auto.from_uri = prev.get("requested_uri")
        auto.to_uri = cur.get("requested_uri")
        return {
            "automation": automation_to_dict(auto),
            "automation_summary": describe(auto),
            "source": row["body_file"],
            "from_uri": prev.get("requested_uri"),
            "to_uri": cur.get("requested_uri"),
            "out_point_ms": _as_int(prev.get("audio.fade_out_start_time")),
            "in_point_ms": _as_int(cur.get("audio.fade_in_start_time")),
            "overlap_ms": _as_int(cur.get("audio.fade_overlap")),
            "fade_in_duration_ms": _as_int(cur.get("audio.fade_in_duration")),
            "fade_out_duration_ms": _as_int(prev.get("audio.fade_out_duration")),
            "automix_mode": cur.get("automix.mode"),
        }
    return None


def verify_against_cluster(transitions: list[Transition], truth: dict) -> dict:
    """Check the DOM reading against the player's own numbers.

    Matches on the out point, which is the most distinctive of the three.
    """
    result = {"checked": False, "matched_snapshot": None, "agrees": None, "detail": ""}
    if not truth or truth.get("out_point_ms") is None:
        result["detail"] = "no cluster response in this run"
        return result
    hits = [t for t in transitions if t.out_point_ms == truth["out_point_ms"]]
    if not hits:
        result["checked"] = True
        result["agrees"] = False
        result["detail"] = (f"player reported out point {truth['out_point_ms']} ms, "
                            "but no snapshot has it")
        return result
    t = hits[0]
    agrees = (t.in_point_ms == truth["in_point_ms"] and t.overlap_ms == truth["overlap_ms"])
    result.update(checked=True, matched_snapshot=t.snapshot, agrees=agrees)
    result["detail"] = (
        f"snapshot {t.snapshot}: out {t.out_point_ms} vs {truth['out_point_ms']}, "
        f"in {t.in_point_ms} vs {truth['in_point_ms']}, "
        f"overlap {t.overlap_ms} vs {truth['overlap_ms']}")
    return result


def dedupe(transitions: list[Transition]) -> list[Transition]:
    """Keep the last snapshot of each distinct track pair.

    Snapshotting the same transition twice is normal - the later one reflects
    whatever the editor finally showed.
    """
    by_pair: dict[tuple, Transition] = {}
    for t in transitions:
        key = (normalize(t.from_track.title), normalize(t.to_track.title),
               t.from_track.bpm, t.to_track.bpm)
        by_pair[key] = t
    out = list(by_pair.values())
    for i, t in enumerate(out):
        t.index = i
    return out


#: how many DFS steps longest_chain will spend before settling for its best find
_CHAIN_BUDGET = 200_000


def longest_chain(transitions: list[Transition]) -> list[Transition]:
    """The longest run of transitions that forms a single A->B->C path.

    A correct capture is exactly that: each track hands over to the next, and
    no track is entered twice. A stale snapshot breaks it - the mix editor
    keeps showing the last transition it rendered, so pressing 's' before
    opening a real one records a pair that was never in the mix. That pair
    usually still parses, so it has to be rejected structurally rather than
    by looking at its numbers.

    Greedy walking is not enough: at a track with two outgoing candidates,
    taking the wrong one dead-ends and loses the whole tail. This searches
    with backtracking and keeps the longest path found.
    """
    by_from: dict[str, list[Transition]] = {}
    for t in transitions:
        by_from.setdefault(normalize(t.from_track.title), []).append(t)

    best: list[Transition] = []
    steps = 0

    def walk(cur: Transition, path: list[Transition], visited: set[str]) -> None:
        nonlocal best, steps
        steps += 1
        path.append(cur)
        if len(path) > len(best):
            best = list(path)
        if steps < _CHAIN_BUDGET:
            nxt_key = normalize(cur.to_track.title)
            for nxt in by_from.get(nxt_key, []):
                to_key = normalize(nxt.to_track.title)
                if to_key in visited:
                    continue           # revisiting a track: not a single chain
                visited.add(to_key)
                walk(nxt, path, visited)
                visited.discard(to_key)
        path.pop()

    for start in transitions:
        walk(start, [], {normalize(start.from_track.title),
                         normalize(start.to_track.title)})
        if len(best) == len(transitions):
            break                      # every transition used: cannot do better
    return best


def order_by_page(transitions: list[Transition]) -> tuple[list[Transition], list[Transition]] | None:
    """Order by the position the page itself reported, if it reported one.

    Each transition chip carries its own place in the running order, so when
    every snapshot recorded one there is nothing left to infer: sort by it.
    A snapshot with no position caught a stale editor panel - no chip was open -
    and is dropped.

    Returns None when the positions are missing or contradictory, so the
    caller can fall back to reading the order out of the track names.
    """
    placed = [t for t in transitions if t.order_index is not None]
    stale = [t for t in transitions if t.order_index is None]
    if not placed:
        return None

    # Two snapshots of the same position are the same transition seen twice;
    # the later one reflects whatever the editor finally showed.
    by_position: dict[int, Transition] = {}
    for t in placed:
        by_position[t.order_index] = t
    chain = [by_position[k] for k in sorted(by_position)]

    # Sanity-check it against the track names: in a real running order each
    # transition hands over to the next. One mismatch means the page indices
    # and the track names disagree, and guessing between them is worse than
    # falling back to the name-based ordering.
    for a, b in zip(chain, chain[1:]):
        if normalize(a.to_track.title) != normalize(b.from_track.title):
            log.debug("Page order disagrees with the track names at position %d; "
                      "falling back to the name chain.", a.order_index)
            return None

    dropped = stale + [t for t in placed if by_position.get(t.order_index) is not t]
    for i, t in enumerate(chain):
        t.index = i
    return chain, dropped


def order_into_chain(transitions: list[Transition]) -> tuple[list[Transition], list[Transition]]:
    """Sort transitions into the order the mix plays them.

    Returns ``(chain, dropped)``. ``dropped`` holds the snapshots that are not
    part of the running order - almost always a stale editor panel captured
    before a real transition was opened. They are returned rather than
    discarded quietly so the caller can report them.

    The page's own chip positions are used when present, since they are the
    order Spotify shows rather than one inferred here. Otherwise the order is
    read out of the track names, which is all the older captures carry.
    """
    if len(transitions) < 2:
        return transitions, []

    from_page = order_by_page(transitions)
    if from_page is not None:
        return from_page

    chain = longest_chain(transitions)
    if len(chain) < 2:
        return transitions, []         # no structure to trust; leave it alone

    on_chain = {id(t) for t in chain}
    dropped = [t for t in transitions if id(t) not in on_chain]
    for i, t in enumerate(chain):
        t.index = i
    return chain, dropped


def extract(run_dir: Path) -> tuple[list[Transition], dict]:
    """Read every DOM snapshot in a run and return (transitions, report)."""
    dom = run_dir / "dom"
    if not dom.is_dir():
        raise ExtractError(f"No dom/ folder in {run_dir}. Run phase1_discover.py first.")
    snapshots = sorted(dom.glob("*.html"))
    if not snapshots:
        raise ExtractError(f"No DOM snapshots in {dom}. Take some with 's' during discovery.")

    parsed, skipped = [], []
    for i, path in enumerate(snapshots):
        t = parse_snapshot(path, i)
        (parsed if t else skipped).append(t or path.name)

    if not parsed:
        raise ExtractError(
            f"None of the {len(snapshots)} snapshots had the mix editor open. "
            "Re-run discovery and press 's' while a transition is on screen.")

    # Ordering: the page's own chip positions when it gave them, otherwise
    # inferred from the track names. Which one was used is reported.
    positioned = sum(1 for t in parsed if t.order_index is not None)
    transitions, dropped = order_into_chain(dedupe(parsed))
    order_source = ("the page's own transition positions"
                    if positioned and transitions and transitions[0].order_index is not None
                    else "the chain of track names")

    catalog = load_track_catalog(run_dir)
    resolved = 0
    for t in transitions:
        for track in (t.from_track, t.to_track):
            hit = match_track(track, catalog)
            if hit:
                track.spotify_id = hit["spotify_id"]
                track.duration_ms = hit["duration_ms"]
                track.isrc = hit["isrc"]
                resolved += 1

    # Completeness. The page itself says how many transitions the mix has -
    # one chip per transition - so comparing against that is exact. The track
    # catalogue is not a fair comparison: it holds metadata for everything the
    # capture happened to see, which can include tracks outside the mix.
    tracks_on_chain = len(transitions) + 1 if transitions else 0
    chips = max((t.chips_on_page or 0 for t in parsed), default=0) or None
    if chips:
        complete = len(transitions) == chips
        missing = chips - len(transitions)
    else:
        complete = bool(catalog) and tracks_on_chain == len(catalog)
        missing = None

    truth = cluster_ground_truth(run_dir)
    report = {
        "run": run_dir.name,
        "snapshots": len(snapshots),
        "snapshots_without_editor": skipped,
        "transitions_parsed": len(parsed),
        "transitions": len(transitions),
        "order_source": order_source,
        "modes": {m: sum(1 for t in transitions if t.mode == m)
                  for m in sorted({t.mode for t in transitions if t.mode})},
        "dropped_not_on_chain": [
            {"snapshot": t.snapshot, "from": t.from_track.label(),
             "to": t.to_track.label(), "out_point_ms": t.out_point_ms}
            for t in dropped
        ],
        "tracks_in_catalog": len(catalog),
        "tracks_on_chain": tracks_on_chain,
        "transitions_on_page": chips,
        "transitions_missing": missing,
        "capture_complete": complete,
        "track_refs_resolved": resolved,
        "track_refs_total": 2 * len(transitions),
        "ground_truth": truth,
        "verification": verify_against_cluster(transitions, truth or {}),
    }
    return transitions, report
