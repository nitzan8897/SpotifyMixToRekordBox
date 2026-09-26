"""Keyword scanning over captured JSON / text, plus redaction for reports."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterator
from urllib.parse import urlparse

_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z]|\d|\b)|[A-Z]?[a-z]+|[A-Z]+|\d+")

# Keys whose values must never appear in a report (tokens, personal data).
SENSITIVE_KEY_RE = re.compile(
    r"(token|secret|auth|cookie|password|passwd|session|email|e_mail|phone|birth|"
    r"postal|address|client_id|clientid|device_id|deviceid|sp_dc|sp_key|signature|credential)",
    re.IGNORECASE,
)
SENSITIVE_VALUE_RE = re.compile(r"^(Bearer\s+\S+|[A-Za-z0-9_\-]{100,})$")

SPOTIFY_ID_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{22}(?![A-Za-z0-9])")
HEX_ID_RE = re.compile(r"(?<![0-9a-f])[0-9a-f]{32,40}(?![0-9a-f])")
NUM_SEGMENT_RE = re.compile(r"^\d+$")


def tokenize(name: str) -> tuple[str, ...]:
    """Split a key like 'crossfadeStartMs' / 'fade_in-ms' into lowercase tokens."""
    tokens: list[str] = []
    for part in re.split(r"[^A-Za-z0-9]+", name):
        tokens.extend(t.lower() for t in _CAMEL_RE.findall(part))
    return tuple(tokens)


@dataclass(frozen=True)
class Keyword:
    text: str
    tokens: tuple[str, ...]

    def matches_tokens(self, key_tokens: tuple[str, ...]) -> bool:
        n = len(self.tokens)
        if n == 0 or n > len(key_tokens):
            return False
        for i in range(len(key_tokens) - n + 1):
            window = key_tokens[i : i + n]
            if n == 1:
                t, kw = window[0], self.tokens[0]
                # Allow plural/inflected forms for longer words: transition(s), fades.
                if t == kw or (len(kw) >= 4 and t.startswith(kw) and len(t) - len(kw) <= 3):
                    return True
            elif window == self.tokens:
                return True
        return False


def compile_keywords(words: list[str]) -> list[Keyword]:
    out = []
    for w in words:
        toks = tokenize(w)
        if toks:
            out.append(Keyword(w, toks))
    return out


def match_key(key: str, keywords: list[Keyword]) -> list[str]:
    toks = tokenize(key)
    return [k.text for k in keywords if k.matches_tokens(toks)]


def match_text(text: str, keywords: list[Keyword]) -> list[str]:
    """Match free text (string values, DOM labels) word by word."""
    words = tokenize(text)
    return [k.text for k in keywords if k.matches_tokens(words)]


@dataclass
class Hit:
    path: str  # JSONPath-ish location, e.g. $.data.items[3].transition.fadeMs
    key: str
    where: str  # "key" (key name matched) or "value" (string value matched)
    keywords: list[str]
    value_preview: str


def walk(obj: Any, path: str = "$") -> Iterator[tuple[str, str, Any]]:
    """Yield (path, key, value) for every dict entry / list item, depth first."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(k)) else f"{path}[{k!r}]"
            yield p, str(k), v
            yield from walk(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            p = f"{path}[{i}]"
            yield p, "", v
            yield from walk(v, p)


def preview(value: Any, limit: int = 120) -> str:
    if isinstance(value, dict):
        return "{" + ", ".join(list(map(str, value.keys()))[:8]) + (", ..." if len(value) > 8 else "") + "}"
    if isinstance(value, list):
        return f"[list of {len(value)}]"
    s = repr(value)
    return s if len(s) <= limit else s[: limit - 3] + "..."


def scan_json(obj: Any, keywords: list[Keyword], match_values: bool = True) -> list[Hit]:
    hits: list[Hit] = []
    for path, key, value in walk(obj):
        if key:
            kws = match_key(key, keywords)
            if kws:
                shown = "<redacted>" if SENSITIVE_KEY_RE.search(key) else preview(value)
                hits.append(Hit(path, key, "key", kws, shown))
                continue
        if match_values and isinstance(value, str) and 0 < len(value) <= 200 and not SENSITIVE_KEY_RE.search(key):
            kws = match_text(value, keywords)
            if kws:
                hits.append(Hit(path, key, "value", kws, preview(value)))
    return hits


def generalize_path(path: str) -> str:
    """$.items[3].x[0].y -> $.items[*].x[*].y (group hits across list items)."""
    return re.sub(r"\[\d+\]", "[*]", path)


def printable_strings(data: bytes, min_len: int = 4) -> list[str]:
    """ASCII runs from a binary body (e.g. protobuf), like `strings`."""
    return [m.decode("ascii") for m in re.findall(rb"[\x20-\x7e]{%d,}" % min_len, data)]


def endpoint_template(url: str) -> str:
    """Stable, ID-free name for an endpoint: host + path with IDs replaced."""
    u = urlparse(url)
    segs = []
    for seg in u.path.split("/"):
        if SPOTIFY_ID_RE.fullmatch(seg):
            segs.append("{id}")
        elif HEX_ID_RE.fullmatch(seg):
            segs.append("{hex}")
        elif NUM_SEGMENT_RE.match(seg):
            segs.append("{n}")
        elif seg.startswith("spotify:"):
            segs.append("{uri}")
        else:
            segs.append(seg)
    return f"{u.netloc}{'/'.join(segs)}"


def redact(obj: Any, max_list: int = 3, max_depth: int = 8, max_str: int = 160, _depth: int = 0) -> Any:
    """Copy of `obj` safe to paste into a report: secrets removed, lists and depth trimmed."""
    if _depth >= max_depth:
        return "<...>"
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if SENSITIVE_KEY_RE.search(str(k)):
                out[k] = "<redacted>"
            else:
                out[k] = redact(v, max_list, max_depth, max_str, _depth + 1)
        return out
    if isinstance(obj, list):
        items = [redact(v, max_list, max_depth, max_str, _depth + 1) for v in obj[:max_list]]
        if len(obj) > max_list:
            items.append(f"<... {len(obj) - max_list} more items>")
        return items
    if isinstance(obj, str):
        if SENSITIVE_VALUE_RE.match(obj):
            return "<redacted>"
        return obj if len(obj) <= max_str else obj[:max_str] + "<...>"
    return obj


def get_at(obj: Any, path: str) -> Any:
    """Resolve a path produced by `walk` back to its value."""
    cur = obj
    for m in re.finditer(r"\.([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]|\[('(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")\]", path[1:]):
        name, idx, quoted = m.groups()
        if name is not None:
            cur = cur[name]
        elif idx is not None:
            cur = cur[int(idx)]
        else:
            cur = cur[quoted[1:-1]]
    return cur


def parent_path(path: str) -> str:
    m = re.match(r"^(.*?)(\.[A-Za-z_][A-Za-z0-9_]*|\[[^\]]*\])$", path)
    return m.group(1) if m and m.group(1) else "$"
