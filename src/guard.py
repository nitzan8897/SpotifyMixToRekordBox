"""Read-only guard: decide whether a browser request could modify Spotify data.

While we record, every request from the page passes through `write_block_reason`.
Anything that looks like a write to a playlist, the library, or a mix/transition
is aborted before it leaves the browser. Reads (GET, and GraphQL queries sent
as POST) pass through untouched so the web player keeps working.

This is a safety net, not a guarantee: Spotify could add a write endpoint we do
not recognise. So during discovery, also avoid clicking Save/Apply/Done in
the editor.
"""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlparse

READ_METHODS = {"GET", "HEAD", "OPTIONS"}

# GraphQL operation names that read data start with verbs like fetch/query/get.
# Anything starting with one of these verbs is treated as a write.
MUTATING_OPERATION_RE = re.compile(
    r"^(add|remove|delete|move|update|edit|set|save|create|rename|reorder|change|"
    r"follow|unfollow|like|unlike|put|publish|apply|replace|clear|upsert|insert|"
    r"mutate|modify|toggle|reset)",
    re.IGNORECASE,
)

# URL path patterns (spclient / Web API) that write to playlists or the library.
WRITE_PATH_PATTERNS = [
    re.compile(r"/playlist/v2/playlist/[^/]+/changes"),
    re.compile(r"/playlist/v2/playlist/[^/]+/signals"),
    re.compile(r"/playlist/v2/user/[^/]+/rootlist/changes"),
    re.compile(r"/collection/v2/write"),
    re.compile(r"/v1/playlists/"),
    re.compile(r"/v1/me/(tracks|albums|following|episodes|shows)"),
]

# Path fragments that suggest a mix/transition endpoint. Writing verbs
# (PUT/PATCH/DELETE) to these are blocked; POSTs are allowed but flagged,
# because Spotify may legitimately use POST to *fetch* transition data.
TRANSITION_PATH_RE = re.compile(r"(transition|crossfade|/mix|mixed)", re.IGNORECASE)


def graphql_operation_name(url: str, post_data: str | None) -> str | None:
    """Return the GraphQL operationName from a request body or query string."""
    if post_data:
        try:
            body = json.loads(post_data)
        except (ValueError, TypeError):
            body = None
        if isinstance(body, dict) and isinstance(body.get("operationName"), str):
            return body["operationName"]
    qs = parse_qs(urlparse(url).query)
    names = qs.get("operationName")
    return names[0] if names else None


def is_graphql(url: str) -> bool:
    u = urlparse(url)
    return "pathfinder" in u.netloc or "/pathfinder/" in u.path or u.path.endswith("/graphql")


def write_block_reason(method: str, url: str, post_data: str | None) -> str | None:
    """Return why this request must be blocked, or None if it is safe to send."""
    method = method.upper()
    u = urlparse(url)
    host = u.netloc.lower()
    if not (host.endswith("spotify.com") or host.endswith("spotify.net")):
        return None
    # Never interfere with logging in.
    if host.startswith("accounts.") or host.startswith("challenge.") or host.startswith("login"):
        return None

    if is_graphql(url):
        op = graphql_operation_name(url, post_data)
        if op and MUTATING_OPERATION_RE.match(op):
            return f"GraphQL mutation '{op}'"
        return None

    if method in READ_METHODS:
        return None

    path = u.path
    for pat in WRITE_PATH_PATTERNS:
        if pat.search(path):
            return f"{method} to playlist/library write path {path}"
    if method in {"PUT", "PATCH", "DELETE"} and TRANSITION_PATH_RE.search(path):
        return f"{method} to transition-like path {path}"
    return None


def is_suspicious_transition_post(method: str, url: str) -> bool:
    """POST to a transition-looking path: allowed, but worth a loud log line."""
    return method.upper() == "POST" and bool(TRANSITION_PATH_RE.search(urlparse(url).path))
