#!/usr/bin/env python3
"""Phase 1 - Discovery.

Opens the Spotify web player in a headed browser with a persistent profile,
lets you log in by hand, opens the playlist from config.yaml, and records:

  * a HAR file with embedded response bodies (network.har)
  * every Spotify response body on its own (bodies/) plus an index
  * WebSocket frames (ws_frames.jsonl)
  * DOM snapshots of the mix/transition editor, taken on your command

Nothing is parsed into transitions here. Run phase1_analyze.py afterwards
to search the capture and write the discovery report.

Safety:
  * Your password is typed into Spotify's own login page; this script never
    reads it. Only the browser profile (cookies) is kept, in the profile dir.
  * Requests that would modify playlists / your library are aborted
    (spotimix/guard.py). Don't click Save/Apply in the editor anyway.
  * No audio is downloaded: audio/media responses are never saved.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from spotimix.config import ConfigError, load_config
from spotimix.guard import graphql_operation_name, is_suspicious_transition_post, write_block_reason
from spotimix.logs import setup_logging
from spotimix.scan import endpoint_template

log = logging.getLogger("spotimix.discover")

DOM_PROBE_JS = (Path(__file__).parent / "spotimix" / "dom_probe.js").read_text(encoding="utf-8")

# Labels that may open a mix/transition editor, and labels we never click.
OPEN_WORDS = ["mix", "mixed", "transition", "transitions", "crossfade", "blend", "segue", "automix"]
UNSAFE_WORDS = ["save", "apply", "done", "remove", "delete", "confirm", "publish", "reset", "clear",
                "add", "discard", "undo", "follow", "like", "shuffle", "turn off", "turn on", "disable",
                "enable", "update", "change", "edit details", "rename"]

# Never persist media bodies (audio/video/images): no audio downloading, ever.
SKIP_BODY_TYPES = ("audio/", "video/", "image/", "font/", "application/octet-stream+media")
SKIP_BODY_PATH_RE = re.compile(r"(/audio/|/mp3-preview/|\.mp4|\.m4a|\.ogg|\.webm|/segments/|/storage-resolve/)", re.I)
TEXT_TYPES = ("json", "text/", "javascript", "xml", "protobuf", "x-protobuf", "grpc")

COMMANDS_HELP = """
  Commands (type then Enter):
    s        snapshot the DOM now (do this with the transition editor OPEN,
             once per transition you inspect)
    c        list buttons that might open a mix/transition editor
    o N      click candidate N from the last 'c' list (safe labels only)
    p        re-open the playlist page
    q        finish: close the browser and write all files
"""


def js_keyword_pattern(words: list[str]) -> str:
    """Case-insensitive JS regex source without the 'i' flag.

    Short words need a non-letter before them and no lowercase letter after,
    so 'eq' matches 'eqLow' / 'EQ' but not 'sequence'."""
    alts = []
    for w in words:
        toks = [t for t in re.split(r"[^A-Za-z0-9]+", w) if t]
        if not toks:
            continue
        body = r"[_\- ]?".join(
            "".join(f"[{c.lower()}{c.upper()}]" if c.isalpha() else re.escape(c) for c in t) for t in toks
        )
        if len("".join(toks)) < 5:
            alts.append(rf"(?<![A-Za-z]){body}(?![a-z])")
        else:
            alts.append(body)
    return "|".join(alts) or "(?!)"


class Recorder:
    def __init__(self, run_dir: Path, capture_hosts: list[str], max_body_bytes: int, block_writes: bool):
        self.run_dir = run_dir
        self.bodies_dir = run_dir / "bodies"
        self.bodies_dir.mkdir(parents=True, exist_ok=True)
        self.index_file = (run_dir / "index.jsonl").open("a", encoding="utf-8")
        self.ws_file = (run_dir / "ws_frames.jsonl").open("a", encoding="utf-8")
        self.blocked_file = (run_dir / "blocked_requests.jsonl").open("a", encoding="utf-8")
        self.capture_hosts = capture_hosts
        self.max_body_bytes = max_body_bytes
        self.block_writes = block_writes
        self.seq = 0
        self.saved = 0
        self.blocked = 0
        self.errors = 0
        self._tasks: set[asyncio.Task] = set()

    def _host_ok(self, url: str) -> bool:
        host = urlparse(url).netloc.lower().split(":")[0]
        return any(host == h or host.endswith("." + h) for h in self.capture_hosts)

    # ---- request guard -------------------------------------------------
    async def route(self, route):
        req = route.request
        try:
            post = req.post_data
        except Exception:  # binary body that is not valid UTF-8
            post = None
        reason = write_block_reason(req.method, req.url, post) if self.block_writes else None
        if reason:
            self.blocked += 1
            log.warning("BLOCKED write request (%s): %s %s", reason, req.method, endpoint_template(req.url))
            self.blocked_file.write(json.dumps({"t": time.time(), "method": req.method,
                                                "endpoint": endpoint_template(req.url), "reason": reason}) + "\n")
            self.blocked_file.flush()
            await route.abort("blockedbyclient")
            return
        if is_suspicious_transition_post(req.method, req.url):
            log.warning("POST to transition-like endpoint allowed (might be a read): %s - "
                        "do NOT click Save/Apply in the editor", endpoint_template(req.url))
        await route.continue_()

    # ---- responses -----------------------------------------------------
    def on_response(self, response):
        task = asyncio.ensure_future(self._save_response(response))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _save_response(self, response):
        url = response.url
        if not self._host_ok(url):
            return
        req = response.request
        ctype = (response.headers.get("content-type") or "").lower()
        self.seq += 1
        seq = self.seq
        entry = {
            "seq": seq, "t": time.time(), "method": req.method, "status": response.status,
            "endpoint": endpoint_template(url), "url": _strip_query_secrets(url),
            "content_type": ctype, "resource_type": req.resource_type,
            "graphql_operation": None, "body_file": None, "body_bytes": None, "skipped": None,
        }
        try:
            post = req.post_data
        except Exception:
            post = None
        entry["graphql_operation"] = graphql_operation_name(url, post)
        if post and entry["graphql_operation"]:
            # Keep GraphQL variables (playlist uri, offsets) - useful to replay reads in phase 2.
            try:
                body = json.loads(post)
                entry["graphql_variables"] = body.get("variables")
            except (ValueError, TypeError, AttributeError):
                pass

        if ctype.startswith(SKIP_BODY_TYPES) or SKIP_BODY_PATH_RE.search(urlparse(url).path):
            entry["skipped"] = "media"
        elif req.method == "OPTIONS" or response.status in (204, 304) or 300 <= response.status < 400:
            entry["skipped"] = "no body"
        elif not any(t in ctype for t in TEXT_TYPES) and ctype:
            entry["skipped"] = f"content-type {ctype}"
        else:
            try:
                data = await response.body()
            except Exception as e:  # body evicted, request aborted, redirect...
                entry["skipped"] = f"body unavailable: {type(e).__name__}"
            else:
                entry["body_bytes"] = len(data)
                if len(data) > self.max_body_bytes:
                    entry["skipped"] = "too large"
                else:
                    ext = ".json" if "json" in ctype else (".bin" if "protobuf" in ctype or "grpc" in ctype else ".txt")
                    digest = hashlib.sha1(url.encode()).hexdigest()[:8]
                    name = f"{seq:05d}_{_safe_name(entry['graphql_operation'] or entry['endpoint'])}_{digest}{ext}"
                    (self.bodies_dir / name).write_bytes(data)
                    entry["body_file"] = f"bodies/{name}"
                    self.saved += 1
        self.index_file.write(json.dumps(entry) + "\n")
        self.index_file.flush()
        log.debug("#%d %s %s %s -> %s", seq, req.method, response.status,
                  entry["graphql_operation"] or entry["endpoint"], entry["body_file"] or entry["skipped"])

    # ---- websockets ----------------------------------------------------
    def on_websocket(self, ws):
        if not self._host_ok(ws.url):
            return
        log.info("WebSocket opened: %s", endpoint_template(ws.url))
        endpoint = endpoint_template(ws.url)

        def frame(direction):
            def handler(payload):
                if isinstance(payload, bytes):
                    rec = {"t": time.time(), "ws": endpoint, "dir": direction, "binary": True,
                           "hex": payload[:65536].hex()}
                else:
                    rec = {"t": time.time(), "ws": endpoint, "dir": direction, "text": payload[:262144]}
                self.ws_file.write(json.dumps(rec) + "\n")
                self.ws_file.flush()
            return handler

        ws.on("framereceived", frame("in"))
        ws.on("framesent", frame("out"))

    async def drain(self):
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def close(self):
        for f in (self.index_file, self.ws_file, self.blocked_file):
            f.close()


def _safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)[:80].strip("_")


def _strip_query_secrets(url: str) -> str:
    return re.sub(r"(?i)([?&](?:access_token|token|signature|sig|auth)[^=]*=)[^&]+", r"\1<redacted>", url)


async def ainput(prompt: str) -> str:
    # Read stdin in a thread so Playwright's event loop keeps recording meanwhile.
    return await asyncio.to_thread(input, prompt)


async def wait_for_login(context) -> None:
    print("\n=== Log in to Spotify in the browser window ===\n"
          "Type your credentials into Spotify's own page. This script never sees them.\n"
          "If you are already logged in (profile reused), just press Enter.")
    while True:
        await ainput("Press Enter once the web player shows you as logged in... ")
        names = {c["name"] for c in await context.cookies("https://open.spotify.com")}
        if "sp_dc" in names:  # Spotify's login cookie. We check its name only, never its value.
            log.info("Logged-in session detected.")
            return
        log.warning("No Spotify login cookie found yet. Finish logging in, then press Enter again "
                    "(or type 'skip' to continue anyway).")
        if (await ainput("Enter / skip: ")).strip().lower() == "skip":
            log.warning("Continuing without confirmed login.")
            return


async def snapshot_dom(page, run_dir: Path, label: str, patterns: dict) -> dict | None:
    dom_dir = run_dir / "dom"
    dom_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%H%M%S")
    base = dom_dir / f"{stamp}_{_safe_name(label)}"
    results = []
    for i, frame in enumerate(page.frames):
        try:
            res = await frame.evaluate(DOM_PROBE_JS, patterns)
        except Exception as e:
            log.debug("DOM probe failed in frame %d (%s): %s", i, frame.url, e)
            continue
        res["frame_index"] = i
        results.append(res)
    try:
        html = await page.content()
        base.with_suffix(".html").write_text(html, encoding="utf-8")
        await page.screenshot(path=str(base.with_suffix(".png")), full_page=False)
    except Exception as e:
        log.warning("Could not save full HTML/screenshot: %s", e)
    base.with_suffix(".json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    matches = sum(len(r["matches"]) for r in results)
    sliders = sum(len(r["sliders"]) for r in results)
    log.info("DOM snapshot saved: %s (keyword elements: %d, sliders: %d)", base.name, matches, sliders)
    return results[0] if results else None


async def run(cfg, run_dir: Path) -> int:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.error("Playwright is not installed. Run: pip install -r requirements.txt && playwright install chromium")
        return 2

    rec = Recorder(run_dir, cfg.discovery.capture_hosts, cfg.discovery.max_body_bytes,
                   cfg.discovery.block_playlist_writes)
    patterns = {
        "pattern": js_keyword_pattern(cfg.discovery.keywords),
        "openPattern": js_keyword_pattern(OPEN_WORDS),
        "unsafePattern": js_keyword_pattern(UNSAFE_WORDS),
    }
    har_path = run_dir / "network.har"
    cfg.browser_profile.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        log.info("Launching headed browser (profile: %s)", cfg.browser_profile)
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(cfg.browser_profile),
            channel=cfg.browser_channel,
            headless=False,
            slow_mo=cfg.slow_mo_ms,
            viewport={"width": 1400, "height": 900},
            record_har_path=str(har_path),
            record_har_content="embed",
            # Requests made by a service worker would bypass our recorder and write guard.
            service_workers="block",
        )
        await context.route("**/*", rec.route)
        context.on("response", rec.on_response)
        context.on("page", lambda p: p.on("websocket", rec.on_websocket))
        for p in context.pages:
            p.on("websocket", rec.on_websocket)

        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto("https://open.spotify.com/", wait_until="domcontentloaded")
        await wait_for_login(context)

        log.info("Opening playlist %s", cfg.playlist_url)
        await page.goto(cfg.playlist_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)  # let the playlist and its lazy requests load
        first = await snapshot_dom(page, run_dir, "playlist_loaded", patterns)
        candidates = first["candidates"] if first else []
        _print_candidates(candidates)

        print("\n=== Now open the mix / transitions editor ===\n"
              "In the browser: open the playlist's mix/transition view if the web player has one\n"
              "(or try 'c' / 'o N' below). Click each transition so its details load, and take a\n"
              "snapshot ('s') while each one is shown. Scroll through the whole playlist too.\n"
              "Go slowly; do not press Save/Apply.")
        print(COMMANDS_HELP)
        snap_n = 0
        while True:
            cmd = (await ainput("discover> ")).strip()
            if cmd in ("q", "quit", "exit"):
                break
            if cmd in ("s", ""):
                snap_n += 1
                await snapshot_dom(page, run_dir, f"snapshot_{snap_n:02d}", patterns)
            elif cmd == "c":
                res = await snapshot_dom(page, run_dir, "candidates", patterns)
                candidates = res["candidates"] if res else []
                _print_candidates(candidates)
            elif cmd.startswith("o "):
                await _click_candidate(page, candidates, cmd[2:].strip())
            elif cmd == "p":
                await page.goto(cfg.playlist_url, wait_until="domcontentloaded")
            else:
                print(COMMANDS_HELP)

        await snapshot_dom(page, run_dir, "final", patterns)
        log.info("Waiting for pending response bodies...")
        await page.wait_for_timeout(1500)
        await rec.drain()
        log.info("Closing browser and writing HAR (this can take a moment)...")
        await context.close()

    rec.close()
    log.info("Capture complete: %d responses indexed, %d bodies saved, %d write requests blocked.",
             rec.seq, rec.saved, rec.blocked)
    log.info("Run folder: %s", run_dir)
    log.info("Next: python phase1_analyze.py %s", run_dir)
    return 0


def _print_candidates(candidates: list[dict]) -> None:
    if not candidates:
        print("  (no buttons mentioning mix/transition found on this view)")
        return
    print("  Possible mix/transition controls:")
    for c in candidates:
        flag = "" if c["safe"] else "  [not clickable: label looks like it changes something]"
        vis = "" if c["visible"] else " (hidden)"
        print(f"   [{c['index']}] <{c['tag']}> {c['label']}{vis}{flag}")


async def _click_candidate(page, candidates: list[dict], arg: str) -> None:
    try:
        idx = int(arg)
        cand = next(c for c in candidates if c["index"] == idx)
    except (ValueError, StopIteration):
        print("  Unknown candidate. Run 'c' first and use one of the listed numbers.")
        return
    if not cand["safe"]:
        print("  Refusing: that label looks like it changes something. Click it yourself if you are sure.")
        return
    log.info("Clicking candidate %d: %s", idx, cand["label"])
    try:
        await page.locator(f'[data-smtr-candidate="{idx}"]').first.click(timeout=5000)
        await page.wait_for_timeout(2000)
    except Exception as e:
        log.warning("Click failed (%s). The page may have changed; run 'c' again.", e)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("-v", "--verbose", action="store_true", help="log every captured response")
    args = ap.parse_args()

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    run_dir = cfg.output_dir / "discovery" / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(run_dir / "discover.log", verbose=args.verbose)
    log.info("Phase 1 discovery - playlist %s", cfg.playlist_id)
    log.warning("The run folder will contain session tokens (HAR). Do not share or commit it; "
                "share only the report from phase1_analyze.py.")
    try:
        return asyncio.run(run(cfg, run_dir))
    except KeyboardInterrupt:
        log.warning("Interrupted. Partial capture left in %s (HAR may be incomplete).", run_dir)
        return 130


if __name__ == "__main__":
    sys.exit(main())
