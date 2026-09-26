"""The five transition "ingredients" Spotify's mix editor exposes.

When a transition is open in the editor, the DOM carries a control block
tagged ``data-curve-editing-ingredient-controls``. Inside it is one button
per ingredient, each rendering two lines of text: the ingredient's name and
the option currently chosen for *this* transition::

    Volume   Smooth crossfade
    EQ       Bass swap in the middle
    Filter   No option
    Effects  No option
    Loop     No option

That is the whole custom-transition setting, per transition, sitting in the
page - no preview and no network capture needed. It is also the only place
the *named* choice appears: the cluster response carries the resulting
curves, but never the name of the preset they came from.

Localisation
------------
The editor renders in the account's display language, so the labels are
whatever language the user browses in. Matching therefore works on a table
of known strings per language, and anything unrecognised is still kept
verbatim under ``raw`` rather than dropped. English and Hebrew are mapped
here because those are the builds seen so far; adding a language is adding
rows, not code.

Spotify itself prints "Unknown" for a volume curve that has been dragged
away from any named preset. That is preserved as ``custom``.
"""
from __future__ import annotations

import html as html_mod
import re
import unicodedata

#: The five ingredient slots, in the order the editor lists them.
SLOTS = ("volume", "eq", "filter", "effects", "loop")

_BIDI = dict.fromkeys(map(ord, "‎‏‪‫‬⁦⁧⁨⁩"))
_WS = re.compile(r"\s+")

_BLOCK = "data-curve-editing-ingredient-controls"


def _norm(s: str) -> str:
    """Fold a UI string for table lookup.

    Case, bidi marks and odd spacing go. Hyphens become spaces, so the editor's
    "High-pass filter in" and a plainer "high pass filter in" are one key. NFKC
    also rewrites the vulgar fractions the effects names use - the glyph "1/2"
    becomes "1", U+2044, "2" - so the fraction slash is folded to a plain one
    and the rest of the module only ever sees "1/2".
    """
    s = unicodedata.normalize("NFKC", (s or "").translate(_BIDI))
    s = s.replace("⁄", "/").replace("-", " ").replace("–", " ")
    return _WS.sub(" ", s).strip().lower()


# -- ingredient names ------------------------------------------------------
# English is the reference wording, taken from the editor itself. Other
# languages are aliases; a build in a language with no aliases here still
# yields the raw strings, and unmapped() reports them.
_SLOT_NAMES = {
    "volume": ["volume", "עוצמת השמע"],
    "eq": ["eq"],
    "filter": ["filter", "מסנן"],
    "effects": ["effects", "אפקטים"],
    "loop": ["looping", "loop", "בלופ", "לופ"],
}
SLOT_BY_LABEL = {_norm(v): slot for slot, vs in _SLOT_NAMES.items() for v in vs}

#: "None" - the ingredient is switched off for this transition.
_NONE_VALUES = {_norm(v) for v in ("none", "no option", "אף אפשרות")}

# -- option names ----------------------------------------------------------
# Keys are Spotify's own English wording, so the cue sheet reads the same as
# the app. Each list is the spellings seen for it.
_OPTIONS = {
    "volume": {
        "smooth crossfade": ["smooth crossfade", "קרוספייד חלק"],
        "crossfade": ["crossfade", "קרוספייד"],
        "fade in fade out": ["fade in fade out", "פייד אין פייד אאוט"],
        "fade in cut out": ["fade in cut out", "פייד אין קאט אאוט"],
        "overlap": ["overlap", "חפיפה"],
        # What the editor shows once a curve has been dragged off any preset.
        "custom": ["unknown"],
    },
    "eq": {
        "start bass swap": ["start bass swap", "bass swap at the start", "החלפת בס בהתחלה"],
        "centre bass swap": ["centre bass swap", "center bass swap",
                             "bass swap in the middle", "החלפת בס באמצע"],
        "end bass swap": ["end bass swap", "bass swap at the end", "החלפת בס בסוף"],
        "bass fade out": ["bass fade out", "פייד אאוט בס"],
        "3 band fade": ["3 band fade", "פייד 3 תחומי תדרים"],
    },
    "filter": {
        "high pass filter in": ["high pass filter in", "high pass in",
                                "פילטר מעביר תדרים גבוהים אין"],
        "high pass filter out": ["high pass filter out", "high pass out",
                                 "פילטר מעביר תדרים גבוהים אאוט"],
        "low pass filter in": ["low pass filter in", "low pass in",
                               "פילטר מעביר תדרים נמוכים אין"],
        "low pass filter out": ["low pass filter out", "low pass out",
                                "פילטר מעביר תדרים נמוכים אאוט"],
    },
    "effects": {
        "reverb cut end": ["reverb cut end", "reverb cut at the end", "ריוורב קאט בסוף"],
        "reverb out end": ["reverb out end", "reverb out at the end", "ריוורב אאוט בסוף"],
    },
    "loop": {},        # handled by the beat-count rule below
}

# The filter slot runs an in-choice and an out-choice together, e.g.
# "Low-pass filter in low-pass filter out". Longest match first so the "in"
# spelling does not consume the start of the "out" one.
_FILTER_PARTS = sorted(
    ((_norm(v), key) for key, vs in _OPTIONS["filter"].items() for v in vs),
    key=lambda kv: -len(kv[0]))

# "Echo 1/2 out end", "Echo 3/4 cut end". The amount is already a plain "1/2"
# by the time this runs; see _norm.
_ECHO = re.compile(r"(?:echo|אקו)\s*(\d+(?:/\d+)?)?\s*(.*)")

# "8-beat loop" (hyphen already folded to a space), and the Hebrew form which
# puts the count after the word: "loop of beat 1".
_BEATS = re.compile(r"(?:(\d+)\s*(?:beats?|ביטים|ביט)|(?:beats?|ביטים|ביט)\s*(\d+))")


def _match_simple(slot: str, value: str) -> str | None:
    n = _norm(value)
    for key, spellings in _OPTIONS.get(slot, {}).items():
        if any(_norm(s) == n for s in spellings):
            return key
    return None


def _parse_filter(value: str) -> str | None:
    """Filter shows an in-choice and an out-choice run together."""
    n = _norm(value)
    found, pos = [], 0
    while pos < len(n):
        for text, key in _FILTER_PARTS:
            if n.startswith(text, pos):
                found.append(key)
                pos += len(text)
                break
        else:
            pos += 1
    # de-duplicate while keeping order
    seen, out = set(), []
    for k in found:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return " + ".join(out) if out else None


def _parse_effects(value: str) -> str | None:
    simple = _match_simple("effects", value)
    if simple:
        return simple
    n = _norm(value)
    m = _ECHO.search(n)
    if not m:
        return None
    amount, tail = m.group(1) or "", m.group(2) or ""
    how = "cut" if ("cut" in tail or "קאט" in tail) else "out"
    return f"echo {amount} {how} end".replace("  ", " ")


def _parse_loop(value: str) -> tuple[str | None, int | None]:
    """Returns (option key, loop length in beats)."""
    m = _BEATS.search(_norm(value))
    if not m:
        return None, None
    beats = int(m.group(1) or m.group(2))
    return f"{beats} beat loop", beats


def parse_value(slot: str, value: str) -> tuple[str | None, int | None]:
    """Canonical option key for one ingredient, plus beats when it is a loop.

    Returns ``(None, None)`` for "no option" and for anything unrecognised;
    the caller keeps the raw string either way.
    """
    if _norm(value) in _NONE_VALUES:
        return None, None
    if slot == "loop":
        return _parse_loop(value)
    if slot == "filter":
        return _parse_filter(value), None
    if slot == "effects":
        return _parse_effects(value), None
    return _match_simple(slot, value), None


def parse_ingredients(html: str) -> dict:
    """Read the ingredient panel out of a DOM snapshot.

    The returned dict always has the five slots. Each is
    ``{"raw": <as shown>, "value": <canonical key or None>}``, with
    ``"beats"`` added on the loop slot when it names a beat count.
    """
    result: dict = {s: {"raw": None, "value": None, "off": False} for s in SLOTS}
    i = html.find(_BLOCK)
    if i < 0:
        return result
    block = html[i:i + 9000]

    for button in re.split(r"(?=<button)", block)[1:]:
        text = html_mod.unescape(re.sub(r"<[^>]+>", "|", button))
        parts = [p.strip() for p in re.sub(r"\|+", "|", text).strip("|").split("|") if p.strip()]
        if len(parts) < 2:
            continue
        slot = SLOT_BY_LABEL.get(_norm(parts[0]))
        if not slot or result[slot]["raw"] is not None:
            continue
        raw = parts[1]
        value, beats = parse_value(slot, raw)
        result[slot] = {"raw": raw, "value": value, "off": _norm(raw) in _NONE_VALUES}
        if slot == "loop":
            result[slot]["beats"] = beats
    return result


def loop_ms(beats: int | None, bpm: int | None) -> int | None:
    """How long a beat-count loop lasts on a track at ``bpm``."""
    if not beats or not bpm:
        return None
    return round(beats * 60_000 / bpm)


def summarize(ing: dict) -> str:
    """One line naming the options actually in use."""
    bits = []
    for slot in SLOTS:
        d = ing.get(slot) or {}
        if d.get("off") or not d.get("raw"):
            continue
        bits.append(f"{slot}: {d.get('value') or d['raw']}")
    return "; ".join(bits) if bits else "no ingredients recorded"


def unmapped(ing: dict) -> list[tuple[str, str]]:
    """Slots that held a setting this module could not name.

    Worth reporting rather than swallowing: it means Spotify added an option,
    or the editor was in a language with no table here.
    """
    return [(s, (ing[s] or {}).get("raw"))
            for s in SLOTS
            if (ing.get(s) or {}).get("raw") and not ing[s].get("value")
            and not ing[s].get("off")]


# --------------------------------------------------------------------------
# The transition's mode chip
# --------------------------------------------------------------------------
# Above each track the mix view shows a chip naming that transition's mode.
# Its aria-checked marks the transition whose editor is open, which is how the
# snapshot can be tied to the right transition.
# aria-pressed is only on the chips in the track list. The editor has chips of
# its own - the overlap length reads "2 bars" - and they must not be counted,
# or the indices stop lining up with the running order.
_CHIP = re.compile(
    r'<button'
    r'(?=[^>]*data-encore-id="chip")'
    r'(?=[^>]*role="checkbox")'
    r'(?=[^>]*aria-pressed)'
    r'([^>]*)>(.*?)</button>',
    re.S)

_MODES = {
    "custom": ("custom", "בהתאמה אישית"),
    "automatic": ("automatic", "auto", "אוטומטי"),
}


def _chip_text(inner: str) -> str:
    return _WS.sub(" ", html_mod.unescape(re.sub(r"<[^>]+>", " ", inner))).strip()


def parse_mode(html: str) -> dict:
    """The mode of the transition whose editor is open in this snapshot.

    Returns ``{"raw": <chip text>, "value": "custom"|"automatic"|None,
    "index": <which chip was open>, "chips": <how many chips the page had>}``.
    ``index`` is the transition's position in the running order, straight from
    the page, which is a useful independent check on the order Phase 2 derives
    from the track names.
    """
    out = {"raw": None, "value": None, "index": None, "chips": 0}
    chips = list(_CHIP.finditer(html))
    out["chips"] = len(chips)
    for i, m in enumerate(chips):
        if 'aria-checked="true"' not in m.group(1):
            continue
        raw = _chip_text(m.group(2))
        out["raw"] = raw or None
        out["index"] = i
        n = _norm(raw)
        for key, spellings in _MODES.items():
            if any(_norm(sp) == n for sp in spellings):
                out["value"] = key
                break
        break
    return out
