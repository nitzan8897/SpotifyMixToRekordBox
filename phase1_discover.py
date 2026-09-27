#!/usr/bin/env python3
"""Phase 1 - Discovery.

Records a real, logged-in Spotify session while you click through your mixed
playlist. Two targets:

  desktop  the Spotify desktop app, driven over the Chrome DevTools Protocol
           (src/desktop.py). This is what config.yaml ships with: the
           mix / transition editor is not in the web player.
  web      the web player in a headed browser with a persistent profile,
           which you log into by hand.

Pick one with browser.target in config.yaml, or --target on the command line.

Either way it opens the playlist from config.yaml and records:

  * a HAR file with embedded response bodies (network.har; web target only)
  * every Spotify response body on its own (bodies/) plus an index
  * WebSocket frames (ws_frames.jsonl)
  * DOM snapshots of the mix/transition editor, taken on your command

Nothing is parsed into transitions here. Run phase1_analyze.py afterwards
to search the capture and write the discovery report.

Safety:
  * Your password is typed into Spotify's own login page; this script never
    reads it. Only the browser profile (cookies) is kept, in the profile dir.
    The desktop target reuses the app's existing sign-in and asks for nothing.
  * Requests that would modify playlists / your library are aborted
    (src/guard.py). Don't click Save/Apply in the editor anyway.
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

from src import desktop
from src.config import TARGETS, ConfigError, load_config
from src.guard import graphql_operation_name, is_suspicious_transition_post, write_block_reason
from src.logs import setup_logging
from src.scan import endpoint_template

log = logging.getLogger("src.discover")

DOM_PROBE_JS = (Path(__file__).parent / "src" / "dom_probe.js").read_text(encoding="utf-8")

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
    a        walk EVERY transition automatically: open each, snapshot its
             settings. Open the playlist's Mix view first.
    w        play the playlist and skip along it, to capture Spotify's real
             automation curves (the only way to get them)
    s        snapshot the DOM now (do this with the transition editor OPEN,
             once per transition you inspect)
    c        list buttons that might open a mix/transition editor
    o N      click candidate N from the last 'c' list (safe labels only)
    p        re-open the playlist page
    q        finish: stop recording and write all files
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


async def wait_for_web_login(context) -> None:
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


async def wait_for_desktop_login(page) -> None:
    """The desktop app carries its own sign-in; just confirm it is signed in."""
    if await desktop.is_logged_in(page):
        log.info("Spotify desktop is signed in.")
        return
    print("\n=== Sign in to the Spotify desktop app ===\n"
          "Use the Spotify window itself. This script never sees your password.")
    while True:
        await ainput("Press Enter once the app shows your library... ")
        if await desktop.is_logged_in(page):
            log.info("Spotify desktop is signed in.")
            return
        log.warning("Still not signed in (no user widget in the app window).")
        if (await ainput("Enter / skip: ")).strip().lower() == "skip":
            log.warning("Continuing without confirmed sign-in.")
            return


async def goto_playlist(cfg, page) -> None:
    """Show the configured playlist, the way this target needs it shown."""
    if cfg.is_desktop:
        await desktop.navigate_to_playlist(page, cfg.playlist_id)
    else:
        await page.goto(cfg.playlist_url, wait_until="domcontentloaded")


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


async def _wire_recorder(context, rec) -> None:
    """Route every request through the write guard and record every response."""
    await context.route("**/*", rec.route)
    context.on("response", rec.on_response)
    context.on("page", lambda p: p.on("websocket", rec.on_websocket))
    for p in context.pages:
        p.on("websocket", rec.on_websocket)


async def open_web_session(pw, cfg, rec, run_dir: Path):
    """Headed browser on open.spotify.com. Returns (context, page, finish)."""
    cfg.browser_profile.mkdir(parents=True, exist_ok=True)
    log.info("Launching headed browser (profile: %s)", cfg.browser_profile)
    context = await pw.chromium.launch_persistent_context(
        user_data_dir=str(cfg.browser_profile),
        channel=cfg.browser_channel,
        headless=False,
        slow_mo=cfg.slow_mo_ms,
        viewport={"width": 1400, "height": 900},
        record_har_path=str(run_dir / "network.har"),
        record_har_content="embed",
        # Requests made by a service worker would bypass our recorder and write guard.
        service_workers="block",
    )
    await _wire_recorder(context, rec)
    page = context.pages[0] if context.pages else await context.new_page()
    await page.goto("https://open.spotify.com/", wait_until="domcontentloaded")
    await wait_for_web_login(context)

    async def finish():
        log.info("Closing browser and writing HAR (this can take a moment)...")
        await context.close()

    return context, page, finish


async def open_desktop_session(pw, cfg, rec, args):
    """Spotify desktop over CDP. Returns (context, page, finish).

    No HAR here: record_har_path only applies to a context we create, and this
    one already exists inside the running app. index.jsonl and bodies/ are
    written exactly as on the web target, which is all the analyzer reads.
    """
    desktop.start_and_attach_preflight(cfg.spotify_exe, cfg.cdp_port,
                                       close_running=args.close_spotify, attach_only=args.attach)
    log.info("%s", desktop.describe_environment(cfg.cdp_port))
    browser, context, page = await desktop.attach(pw, cfg.cdp_port, cfg.slow_mo_ms)
    await _wire_recorder(context, rec)  # the app page is already in context.pages

    if not args.no_reload:
        # We attach after the app has already booted, so its startup requests and
        # the dealer WebSocket are long gone. Reloading xpui replays all of it
        # with the recorder listening. It does not sign you out.
        log.info("Reloading the Spotify UI so its startup traffic is recorded...")
        await page.reload(wait_until="domcontentloaded")
        await page.wait_for_timeout(9000)

    await wait_for_desktop_login(page)

    async def finish():
        log.info("Disconnecting from Spotify. The app stays open.")
        try:
            await context.unroute_all(behavior="ignoreErrors")
        except Exception as e:  # older Playwright, or already disconnected
            log.debug("unroute_all failed: %s", e)
        await browser.close()

    return context, page, finish


async def run(cfg, run_dir: Path, args) -> int:
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

    async with async_playwright() as pw:
        if cfg.is_desktop:
            context, page, finish = await open_desktop_session(pw, cfg, rec, args)
        else:
            context, page, finish = await open_web_session(pw, cfg, rec, run_dir)

        log.info("Opening playlist %s", cfg.playlist_url)
        await goto_playlist(cfg, page)
        await page.wait_for_timeout(4000)  # let the playlist and its lazy requests load
        first = await snapshot_dom(page, run_dir, "playlist_loaded", patterns)
        candidates = first["candidates"] if first else []
        _print_candidates(candidates)

        where = "the Spotify window" if cfg.is_desktop else "the browser"
        print(f"\n=== Now open the mix / transitions editor ===\n"
              f"In {where}: open the playlist's mix/transition view (or try 'c' / 'o N' below).\n"
              "Click each transition so its details load, and take a snapshot ('s') while each\n"
              "one is shown. Scroll through the whole playlist too.\n"
              "Go slowly; do not press Save/Apply.")
        print(COMMANDS_HELP)
        snap_n = 0

        if args.play:
            await play_through(page, run_dir, patterns, args.play_dwell_ms)

        if args.auto:
            captured = await auto_walk(page, run_dir, patterns, args.preview_ms)
            if captured:
                log.info("Auto-walk captured %d transition(s). Nothing else to do by hand.",
                         captured)
            else:
                log.warning("Auto-walk captured nothing. Open the mix editor on the playlist, "
                            "then use 'a' to retry or 's' to snapshot by hand.")

        while True:
            cmd = (await ainput("discover> ")).strip()
            if cmd in ("q", "quit", "exit"):
                break
            if cmd == "a":
                await auto_walk(page, run_dir, patterns, args.preview_ms)
            elif cmd == "w":
                await play_through(page, run_dir, patterns, args.play_dwell_ms)
            elif cmd in ("s", ""):
                snap_n += 1
                await snapshot_dom(page, run_dir, f"snapshot_{snap_n:02d}", patterns)
            elif cmd == "c":
                res = await snapshot_dom(page, run_dir, "candidates", patterns)
                candidates = res["candidates"] if res else []
                _print_candidates(candidates)
            elif cmd.startswith("o "):
                await _click_candidate(page, candidates, cmd[2:].strip())
            elif cmd == "p":
                await goto_playlist(cfg, page)
            else:
                print(COMMANDS_HELP)

        await snapshot_dom(page, run_dir, "final", patterns)
        log.info("Waiting for pending response bodies...")
        await page.wait_for_timeout(1500)
        await rec.drain()
        await finish()

    rec.close()
    log.info("Capture complete: %d responses indexed, %d bodies saved, %d write requests blocked.",
             rec.seq, rec.saved, rec.blocked)
    log.info("Run folder: %s", run_dir)
    log.info("Next: python phase1_analyze.py %s", run_dir)
    return 0


# --------------------------------------------------------------------------
# Walking every transition without a human at the keyboard
# --------------------------------------------------------------------------
# The mix view puts a strip above each track for the transition leading into
# it. Each strip holds two controls:
#
#   * a chip naming the transition ("Custom" / "Automatic"). Clicking the chip
#     is what opens that transition in the curve editor, and its aria-checked
#     says whether its editor is the one currently open. That attribute is the
#     readiness signal this walk waits on - without it the editor still shows
#     the previous transition and the snapshot captures the wrong one.
#   * a preview button. Playing a transition is the only way to make the
#     player report its volume/EQ curves, so previewing is how the automation
#     gets captured.
#
# Everything here is keyed on role/aria/data-encore-id attributes rather than
# the hashed class names beside them, which change with every Spotify build.
# aria-pressed is what separates a transition strip's chip from the chips
# inside the editor itself (the overlap-length chip reads "2 bars" and has an
# aria-label instead). Without it the walk also tries to click that one.
TRANSITION_CHIP = ('button[data-encore-id="chip"][role="checkbox"][aria-pressed]')
#: The panel that only exists while a transition is open in the editor.
INGREDIENT_PANEL = "[data-curve-editing-ingredient-controls]"
#: The per-transition preview button. A data-testid, so it does not depend on
#: the UI language the way the aria-label does.
PREVIEW_BUTTON = '[data-testid="transition-preview-button"]'
#: Driving playback: the playlist's play button and the player's skip control.
PLAY_BUTTON = '[data-testid="play-button"]'
SKIP_FORWARD = '[data-testid="control-button-skip-forward"]'
PLAYPAUSE = '[data-testid="control-button-playpause"]' 


async def _chip_is_open(chip) -> bool:
    try:
        return (await chip.get_attribute("aria-checked")) == "true"
    except Exception:
        return False


async def _wait_until_open(page, chip, timeout_ms: int = 8000) -> bool:
    """Wait for this chip's editor to be the open one."""
    waited, step = 0, 200
    while waited < timeout_ms:
        if await _chip_is_open(chip):
            return True
        await page.wait_for_timeout(step)
        waited += step
    return False


async def auto_walk(page, run_dir: Path, patterns: dict, preview_ms: int) -> int:
    """Open each transition in turn, preview it, and snapshot it.

    Returns how many transitions were captured. This only ever clicks a
    transition's own chip and its own preview button; it never touches Save,
    and the write guard stays in force underneath.
    """
    chips = page.locator(TRANSITION_CHIP)
    try:
        count = await chips.count()
    except Exception as e:
        log.error("Could not look for transition chips (%s).", e)
        return 0
    if not count:
        log.error("Found no transition chips (%s). Open the playlist's Mix view first - the "
                  "chips are the 'Custom'/'Automatic' labels between tracks. Then use 'a' to "
                  "retry, or 's' to snapshot by hand.", TRANSITION_CHIP)
        return 0

    log.info("Found %d transition chip(s). Walking them automatically.", count)
    captured = 0
    for i in range(count):
        chip = chips.nth(i)
        try:
            await chip.scroll_into_view_if_needed(timeout=5000)
        except Exception as e:
            log.warning("Transition %d/%d: could not scroll to it (%s); skipping.",
                        i + 1, count, e)
            continue

        if not await _chip_is_open(chip):
            try:
                await chip.click(timeout=5000)
            except Exception as e:
                log.warning("Transition %d/%d: could not open it (%s); skipping.",
                            i + 1, count, e)
                continue

        # Do not snapshot until this transition's editor is the open one -
        # otherwise the panel is still showing the previous transition.
        if not await _wait_until_open(page, chip):
            log.warning("Transition %d/%d: editor did not report itself open; skipping so a "
                        "stale panel is not recorded as this transition.", i + 1, count)
            continue
        try:
            await page.locator(INGREDIENT_PANEL).first.wait_for(state="visible", timeout=8000)
        except Exception:
            log.warning("Transition %d/%d: no ingredient panel appeared; skipping.",
                        i + 1, count)
            continue
        await page.wait_for_timeout(700)      # let the curves and sliders settle

        if preview_ms > 0:
            await _preview_transition(page, chip, preview_ms)

        if await snapshot_dom(page, run_dir, f"auto_{i + 1:02d}", patterns):
            captured += 1
            log.info("  [%d/%d] captured", i + 1, count)

    log.info("Auto-walk finished: %d/%d transition(s) captured.", captured, count)
    return captured


async def _preview_transition(page, chip, preview_ms: int) -> None:
    """Play the transition this chip belongs to."""
    strip = chip.locator("xpath=ancestor::*[.//button][1]")
    for scope in (strip, page):
        button = scope.locator(PREVIEW_BUTTON)
        try:
            if await button.count():
                await button.first.click(timeout=4000)
                await page.wait_for_timeout(preview_ms)
                return
        except Exception as e:
            log.debug("Preview click failed: %s", e)
    log.debug("No preview button found for this transition.")


async def play_through(page, run_dir: Path, patterns: dict, dwell_ms: int,
                       tracks: int | None = None) -> None:
    """Play the playlist and skip along it, to capture the automation curves.

    This is the only way to get Spotify's real curves. The player reports them
    as track metadata on ``connect-state/v1/cluster``, and it only does so for
    a playlist that is *playing* - opening or previewing a transition in the
    editor does not update that state.

    The payoff is large: one response carries the fade metadata for the whole
    queue, around twenty tracks at a time, not just the track on air. So this
    does not have to sit through the set. It starts playback and then skips
    forward, which slides the queue window and makes the player re-report,
    until every transition has been inside a window.

    Nothing here writes: play and skip only.
    """
    try:
        await page.locator(PLAY_BUTTON).first.click(timeout=8000)
    except Exception as e:
        log.error("Could not press play (%s). Start the playlist yourself, then use 'w'.", e)
        return
    log.info("Playing. Letting the player report its state...")
    await page.wait_for_timeout(max(dwell_ms, 4000))
    await snapshot_dom(page, run_dir, "playing_00", patterns)

    n = tracks or 30
    for i in range(n):
        try:
            await page.locator(SKIP_FORWARD).first.click(timeout=5000)
        except Exception as e:
            log.warning("Skip %d failed (%s); stopping the walk.", i + 1, e)
            break
        await page.wait_for_timeout(dwell_ms)
        log.info("  skipped to track %d/%d", i + 2, n + 1)

    # Leave the player paused rather than blaring after the run.
    try:
        await page.locator(PLAYPAUSE).first.click(timeout=4000)
    except Exception:
        pass
    await snapshot_dom(page, run_dir, "playing_end", patterns)
    log.info("Play-through done. Phase 2 will report how many transitions got real curves.")


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
    ap.add_argument("--target", choices=sorted(TARGETS),
                    help="override browser.target: 'desktop' drives the Spotify app, "
                         "'web' the web player")
    ap.add_argument("--cdp-port", type=int, metavar="PORT",
                    help="desktop: DevTools port to start Spotify on (default from config, 9222)")
    ap.add_argument("--spotify-exe", metavar="PATH",
                    help="desktop: path to Spotify.exe, if it is not where we look")
    ap.add_argument("--attach", action="store_true",
                    help="desktop: attach to a Spotify you already started with "
                         "--remote-debugging-port, instead of launching one")
    ap.add_argument("--close-spotify", action="store_true",
                    help="desktop: close a running Spotify so it can be restarted with debugging "
                         "enabled. This stops playback.")
    ap.add_argument("--auto", action="store_true",
                    help="walk every transition in the mix editor automatically: open each one, "
                         "preview it so the player reports its fade curves, and snapshot it. "
                         "No keyboard needed. Open the mix editor first, then run this.")
    ap.add_argument("--play", action="store_true",
                    help="play the playlist and skip along it, to capture Spotify's real "
                         "automation curves. This is the only way to get them - the editor "
                         "does not report them. Combine with --auto to get both the settings "
                         "and the curves in one run.")
    ap.add_argument("--play-dwell-ms", type=int, default=2500, metavar="MS",
                    help="with --play, how long to sit on each track before skipping on "
                         "(default 2500)")
    ap.add_argument("--preview-ms", type=int, default=4000, metavar="MS",
                    help="with --auto, how long to let each transition play so its curves are "
                         "captured (default 4000). 0 previews nothing and only reads the DOM.")
    ap.add_argument("--no-reload", action="store_true",
                    help="desktop: don't reload the UI on attach. Keeps what is on screen, but "
                         "misses the app's startup requests and the dealer WebSocket.")
    args = ap.parse_args()

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    if args.target:
        cfg.browser_target = args.target
    if args.cdp_port:
        cfg.cdp_port = args.cdp_port
    if args.spotify_exe:
        cfg.spotify_exe = args.spotify_exe
    if not cfg.is_desktop and (args.attach or args.close_spotify or args.no_reload):
        print("ERROR: --attach / --close-spotify / --no-reload only apply to --target desktop",
              file=sys.stderr)
        return 2

    run_dir = cfg.output_dir / "discovery" / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(run_dir / "discover.log", verbose=args.verbose)
    log.info("Phase 1 discovery - target %s, playlist %s", cfg.browser_target, cfg.playlist_id)
    log.warning("The run folder will contain session tokens. Do not share or commit it; "
                "share only the report from phase1_analyze.py.")
    try:
        return asyncio.run(run(cfg, run_dir, args))
    except desktop.DesktopError as e:
        log.error("%s", e)
        return 2
    except KeyboardInterrupt:
        log.warning("Interrupted. Partial capture left in %s (HAR may be incomplete).", run_dir)
        return 130


if __name__ == "__main__":
    sys.exit(main())
