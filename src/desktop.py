"""Driving the Spotify **desktop** app instead of the web player.

The desktop client is Chromium (CEF) under the hood, so it speaks the Chrome
DevTools Protocol when started with ``--remote-debugging-port``. Playwright
attaches to that port and can then record exactly what it records on the web:
responses, WebSocket frames and the DOM.

Why bother: the mix / transition editor is not in the web player, only in the
desktop app.

Three things differ from the web player and the discovery script has to know:

* **The app is already logged in.** Its session is not a cookie on
  ``open.spotify.com``, so the web login check does not apply. We look for the
  user widget in the DOM instead.
* **``page.goto`` must never be used.** xpui routes with an in-memory history,
  so ``location`` stays ``/index.html`` no matter which page is shown.
  Navigation goes through the app's own router (:func:`navigate_to_playlist`).
* **No HAR.** ``record_har_path`` is a browser-context creation option, and
  here the context already exists. ``index.jsonl`` + ``bodies/`` still get
  written, which is all phase1_analyze.py reads.

Command-line flags are only picked up on a cold start: if the app is already
running, a second launch just hands off to the running instance and the
debugging port never opens.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("src.desktop")

# The app page inside the desktop client. Everything else (crashpad, GPU,
# network service) is not a "page" target and Playwright will not see it.
XPUI_URL_FRAGMENT = "xpui.app.spotify.com"

DEFAULT_CDP_PORT = 9222

# Chromium 111+ rejects DevTools WebSocket upgrades that carry an Origin
# header it was not told to allow; harmless when it is not needed.
LAUNCH_FLAGS = ("--remote-debugging-port={port}", "--remote-allow-origins=*")


class DesktopError(Exception):
    """Spotify desktop could not be found, launched, or attached to."""


# --------------------------------------------------------------------------
# Finding the executable
# --------------------------------------------------------------------------
def _windows_candidates() -> list[Path]:
    env = os.environ
    out = []
    for var, rel in (
        ("APPDATA", "Spotify/Spotify.exe"),              # normal installer
        ("LOCALAPPDATA", "Microsoft/WindowsApps/Spotify.exe"),  # Microsoft Store alias
        ("PROGRAMFILES", "Spotify/Spotify.exe"),
        ("PROGRAMFILES(X86)", "Spotify/Spotify.exe"),
    ):
        base = env.get(var)
        if base:
            out.append(Path(base) / rel)
    return out


def _mac_candidates() -> list[Path]:
    return [
        Path("/Applications/Spotify.app/Contents/MacOS/Spotify"),
        Path.home() / "Applications/Spotify.app/Contents/MacOS/Spotify",
    ]


def _linux_candidates() -> list[Path]:
    found = [Path(p) for p in (shutil.which("spotify"), shutil.which("spotify-client")) if p]
    found += [Path("/snap/bin/spotify"), Path("/var/lib/flatpak/exports/bin/com.spotify.Client")]
    return found


def find_spotify_exe(explicit: str | Path | None = None) -> Path:
    """Locate the Spotify desktop executable, or raise :class:`DesktopError`.

    The Microsoft Store build is found through its app-execution alias in
    ``WindowsApps``; that alias is a zero-byte reparse point which forwards
    our command-line flags to the packaged app, so it works like a real exe.
    """
    if explicit:
        p = Path(explicit).expanduser()
        if not p.exists():
            raise DesktopError(f"browser.spotify_exe points at a file that does not exist: {p}")
        return p

    system = platform.system()
    if system == "Windows":
        candidates = _windows_candidates()
    elif system == "Darwin":
        candidates = _mac_candidates()
    else:
        candidates = _linux_candidates()

    for c in candidates:
        if c.exists():
            log.info("Found Spotify desktop: %s", c)
            return c

    raise DesktopError(
        "Could not find the Spotify desktop app. Install it from spotify.com, or set "
        "browser.spotify_exe in config.yaml to its full path. Looked in:\n  "
        + "\n  ".join(str(c) for c in candidates)
    )


# --------------------------------------------------------------------------
# Process handling
# --------------------------------------------------------------------------
def running_spotify_pids() -> list[int]:
    """PIDs of running Spotify processes (all of them: the app spawns several)."""
    try:
        if platform.system() == "Windows":
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq Spotify.exe", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=20,
            ).stdout
            pids = []
            for line in out.splitlines():
                parts = [p.strip('" ') for p in line.split('","')]
                if len(parts) >= 2 and parts[0].lower() == "spotify.exe" and parts[1].isdigit():
                    pids.append(int(parts[1]))
            return pids
        out = subprocess.run(["pgrep", "-x", "Spotify" if platform.system() == "Darwin" else "spotify"],
                             capture_output=True, text=True, timeout=20).stdout
        return [int(x) for x in out.split() if x.isdigit()]
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        log.debug("Could not list Spotify processes: %s", e)
        return []


def close_spotify(timeout: float = 15.0) -> None:
    """Terminate every running Spotify process, then wait for them to go away.

    Needed because the debugging port only opens on a cold start. Spotify has
    no unsaved state of its own, but this does stop whatever is playing.
    """
    pids = running_spotify_pids()
    if not pids:
        return
    log.warning("Closing %d running Spotify process(es) - playback will stop.", len(pids))
    if platform.system() == "Windows":
        subprocess.run(["taskkill", "/IM", "Spotify.exe", "/F", "/T"],
                       capture_output=True, text=True, timeout=30)
    else:
        name = "Spotify" if platform.system() == "Darwin" else "spotify"
        subprocess.run(["pkill", "-x", name], capture_output=True, text=True, timeout=30)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not running_spotify_pids():
            log.info("Spotify closed.")
            return
        time.sleep(0.4)
    raise DesktopError("Spotify processes are still running after being asked to close. "
                       "Quit Spotify by hand (tray icon -> Quit) and run again.")


def launch(exe: Path, port: int, extra_args: list[str] | None = None) -> subprocess.Popen | None:
    """Start Spotify with remote debugging enabled.

    The returned handle is not very meaningful: on Windows the Store alias and
    the installer stub both hand off to a separate process and exit. Readiness
    is decided by :func:`wait_for_cdp`, not by this handle.
    """
    args = [str(exe)] + [f.format(port=port) for f in LAUNCH_FLAGS] + list(extra_args or [])
    log.info("Launching: %s", " ".join(args))
    try:
        kwargs = {}
        if platform.system() == "Windows":
            # Don't let the app die with us; it is the user's own music player.
            kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        else:
            kwargs["start_new_session"] = True
        return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
    except OSError as e:
        raise DesktopError(f"Could not start Spotify ({exe}): {e}") from e


# --------------------------------------------------------------------------
# CDP endpoint
# --------------------------------------------------------------------------
def _cdp_get(port: int, path: str, timeout: float = 3.0):
    url = f"http://127.0.0.1:{port}{path}"
    with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 - fixed localhost URL
        return json.loads(r.read().decode("utf-8"))


def cdp_version(port: int, timeout: float = 3.0) -> dict | None:
    """The ``/json/version`` payload, or None if nothing is listening yet."""
    try:
        return _cdp_get(port, "/json/version", timeout)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def cdp_page_targets(port: int) -> list[dict]:
    try:
        targets = _cdp_get(port, "/json/list")
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
        log.debug("Could not list CDP targets: %s", e)
        return []
    return [t for t in targets if t.get("type") == "page"]


def wait_for_cdp(port: int, timeout: float = 60.0) -> dict:
    """Poll until the debugging port answers, then return its version info."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = cdp_version(port)
        if info:
            log.info("Spotify desktop is debuggable on port %d (%s, %s)",
                     port, info.get("Browser"), _spotify_version(info))
            return info
        time.sleep(0.5)
    raise DesktopError(
        f"Spotify did not open a debugging port on {port} within {timeout:.0f}s.\n"
        "Most likely another Spotify instance was already running (the flag is only read on a "
        "cold start). Quit Spotify completely - on Windows check the system tray - and retry, "
        "or run with --close-spotify to let this script close it for you."
    )


def _spotify_version(info: dict) -> str:
    ua = info.get("User-Agent", "")
    for token in ua.split():
        if token.startswith("Spotify/"):
            return token
    return "unknown Spotify version"


def wait_for_app_page(port: int, timeout: float = 60.0) -> dict:
    """Wait until the xpui application page target exists."""
    deadline = time.monotonic() + timeout
    last: list[dict] = []
    while time.monotonic() < deadline:
        last = cdp_page_targets(port)
        for t in last:
            if XPUI_URL_FRAGMENT in (t.get("url") or ""):
                return t
        time.sleep(0.5)
    raise DesktopError(
        "Spotify is debuggable but its application window never appeared.\n"
        f"Page targets seen: {[t.get('url') for t in last] or 'none'}"
    )


# --------------------------------------------------------------------------
# Talking to the running app through Playwright
# --------------------------------------------------------------------------
def pick_app_page(context):
    """The xpui page out of a CDP context, preferring the real app window."""
    for p in context.pages:
        if XPUI_URL_FRAGMENT in p.url:
            return p
    return context.pages[0] if context.pages else None


# xpui renders with react-router over an in-memory history, so `location` never
# changes and page.goto() would tear the app down. The history object itself is
# reachable on a fiber node's props; we cache it on window for reuse.
_FIND_HISTORY_JS = """() => {
  if (window.__smtrHistory && typeof window.__smtrHistory.push === 'function') {
    return {cached: true, location: window.__smtrHistory.location};
  }
  const root = document.querySelector('#main') || document.body;
  if (!root) return null;
  const key = Object.keys(root).find(
    (k) => k.startsWith('__reactFiber$') || k.startsWith('__reactContainer$'));
  if (!key) return null;
  const seen = new Set();
  let hit = null;
  const walk = (node, depth) => {
    if (!node || hit || depth > 80 || seen.has(node)) return;
    seen.add(node);
    const props = node.memoizedProps;
    if (props && typeof props === 'object' && props.history
        && typeof props.history.push === 'function' && props.history.location) {
      hit = props.history;
      return;
    }
    walk(node.child, depth + 1);
    walk(node.sibling, depth);
  };
  walk(root[key], 0);
  if (!hit) return null;
  window.__smtrHistory = hit;
  return {cached: false, location: hit.location};
}"""

_CURRENT_ROUTE_JS = """() => (window.__smtrHistory && window.__smtrHistory.location
    ? window.__smtrHistory.location.pathname : null)"""

# The app is signed in as whoever uses it; there is no sp_dc cookie to check.
_LOGGED_IN_JS = """() => {
  const el = document.querySelector('[data-testid="user-widget-link"], [data-testid="user-widget-avatar"]');
  return {
    loggedIn: !!el,
    hasLoginButton: !!document.querySelector('[data-testid="login-button"]'),
    title: document.title,
  };
}"""


async def current_route(page) -> str | None:
    """Router path currently shown (e.g. ``/playlist/<id>``), if known."""
    try:
        await page.evaluate(_FIND_HISTORY_JS)
        return await page.evaluate(_CURRENT_ROUTE_JS)
    except Exception as e:  # page mid-navigation, app reloading...
        log.debug("Could not read current route: %s", e)
        return None


async def is_logged_in(page) -> bool:
    try:
        state = await page.evaluate(_LOGGED_IN_JS)
    except Exception as e:
        log.debug("Login probe failed: %s", e)
        return False
    return bool(state.get("loggedIn"))


def open_uri(uri: str) -> None:
    """Hand a ``spotify:`` URI to the OS so the running client handles it."""
    system = platform.system()
    try:
        if system == "Windows":
            os.startfile(uri)  # noqa: S606 - a spotify: URI we built ourselves
        elif system == "Darwin":
            subprocess.run(["open", uri], check=False, timeout=15)
        else:
            subprocess.run(["xdg-open", uri], check=False, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        log.debug("Could not dispatch %s: %s", uri, e)


async def navigate_to_playlist(page, playlist_id: str, settle_ms: int = 4000) -> bool:
    """Show a playlist in the desktop app. Returns True if it is on screen.

    Tries the app's own router first, then the ``spotify:`` protocol handler.
    Never uses ``page.goto``: that would reload xpui out from under the app.
    """
    want = f"/playlist/{playlist_id}"

    async def arrived() -> bool:
        return (await current_route(page)) == want

    if await arrived():
        log.info("Desktop app is already showing the playlist.")
        return True

    found = await page.evaluate(_FIND_HISTORY_JS)
    if found:
        log.info("Navigating via the app router to %s", want)
        try:
            await page.evaluate(f"() => window.__smtrHistory.push({json.dumps(want)})")
            await page.wait_for_timeout(settle_ms)
            if await arrived():
                return True
        except Exception as e:
            log.debug("Router push failed: %s", e)
    else:
        log.debug("App router not reachable; falling back to the spotify: URI.")

    log.info("Navigating via spotify:playlist:%s", playlist_id)
    open_uri(f"spotify:playlist:{playlist_id}")
    await page.wait_for_timeout(settle_ms)
    if await arrived():
        return True

    log.warning("Could not confirm the playlist is open. Open it yourself in the Spotify window "
                "(search for it in the left sidebar), then continue.")
    return False


async def attach(pw, port: int, slow_mo_ms: int = 0):
    """Attach Playwright to a debuggable Spotify and return (browser, context, page)."""
    endpoint = f"http://127.0.0.1:{port}"
    log.info("Attaching Playwright over CDP: %s", endpoint)
    browser = await pw.chromium.connect_over_cdp(endpoint, slow_mo=slow_mo_ms)
    if not browser.contexts:
        raise DesktopError("Attached to Spotify but it exposes no browser context.")
    context = browser.contexts[0]
    page = pick_app_page(context)
    if page is None:
        raise DesktopError("Attached to Spotify but it exposes no page to record.")
    log.info("Attached to: %s", page.url)
    return browser, context, page


def start_and_attach_preflight(exe: Path | str | None, port: int, close_running: bool,
                               attach_only: bool) -> None:
    """Make sure something debuggable is listening on ``port`` before attaching.

    ``attach_only`` skips launching, for when the user started Spotify with the
    flag themselves.
    """
    if cdp_version(port):
        log.info("Something is already listening on port %d; attaching to it.", port)
        wait_for_app_page(port)
        return

    if attach_only:
        raise DesktopError(
            f"--attach was given but nothing is listening on port {port}.\n"
            f"Start Spotify yourself with:  Spotify.exe --remote-debugging-port={port}\n"
            "(quit any running Spotify first - the flag is only read on a cold start)."
        )

    pids = running_spotify_pids()
    if pids:
        if not close_running:
            raise DesktopError(
                f"Spotify is already running ({len(pids)} process(es)) without remote debugging.\n"
                "The debugging flag is only read on a cold start, so it has to be restarted.\n"
                "Quit Spotify completely (Windows: check the system tray) and run again, or pass "
                "--close-spotify to let this script close it for you. Playback will stop."
            )
        close_spotify()

    launch(find_spotify_exe(exe), port)
    wait_for_cdp(port)
    wait_for_app_page(port)


def describe_environment(port: int) -> str:
    info = cdp_version(port) or {}
    return (f"Spotify desktop {_spotify_version(info)} on {info.get('Browser', 'unknown Chromium')} "
            f"(CDP {info.get('Protocol-Version', '?')})")


if __name__ == "__main__":  # quick manual check: python -m src.desktop
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    p = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CDP_PORT
    print("executable:", find_spotify_exe())
    print("running pids:", running_spotify_pids())
    print("cdp version:", cdp_version(p))
    print("page targets:", [t.get("url") for t in cdp_page_targets(p)])
