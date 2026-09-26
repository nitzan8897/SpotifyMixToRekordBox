# Spotify mix → rekordbox

Pulls the transitions you built in a Spotify **mixed playlist** (fade length,
start/end points, EQ/filter) and turns them into something you can use in
rekordbox 7 with a DDJ-200.

Spotify has no public API for transition data, so this tool records your own
logged-in web player session in a normal, visible browser window.

| Phase | Script | Status |
|---|---|---|
| 1. Discovery: find where Spotify keeps transition data | `phase1_discover.py`, `phase1_analyze.py` | **ready** |
| 2. Extraction to `transitions.json` | `phase2_extract.py` | waiting on the Phase 1 findings |
| 3. Cue sheet and rekordbox XML | `phase3_rekordbox.py` | waiting on Phase 2 |

Phase 2 is only written after we know the real data format. That way it
never guesses at transition values.

## Ground rules the code follows

- **Your password is never touched.** You log in on Spotify's own page. Only
  the browser profile (cookies) is kept, in `.browser-profile/`.
- **Read-only.** While recording, requests that would modify playlists or
  your library are aborted before they leave the browser
  (`spotimix/guard.py`). This includes GraphQL mutations, playlist
  `/changes`, Web API writes, and PUT/PATCH/DELETE to anything that looks
  like transitions. Every blocked request is logged to `blocked_requests.jsonl`.
  Don't click Save/Apply in the editor anyway.
- **No audio, no DRM.** Audio, video and image responses are never saved.
  Nothing tries to decrypt anything.
- **Slow.** Nothing is requested in bulk. The only requests are the ones the
  web player makes while you click around, and `slow_mo` slows down the
  script's own actions.
- **Fail loudly.** If data can't be found or parsed, the scripts say so and
  exit non-zero. They don't write partial results.

## Setup

Requires Python 3.11+.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate     macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

Edit `config.yaml` and set `playlist_url` to your mixed playlist
(Share → Copy link to playlist; the `?si=...` part can stay).

Optional: set `browser.channel: chrome` to use your installed Google Chrome.
It is closer to a normal browser. Playwright's bundled Chromium can't play
DRM audio, which may matter if the editor wants to preview a transition.

## Phase 1: discovery

```bash
python phase1_discover.py          # add -v to log every captured response
```

1. A browser window opens at open.spotify.com. Log in by hand, then press
   Enter in the terminal. Next time the profile remembers you.
2. The script opens your playlist, takes a first DOM snapshot, and lists any
   buttons whose labels mention mix/transition.
3. Open the playlist's mix/transitions editor, either by clicking it yourself
   or with `c` (list candidates) and `o N` (click candidate N). The script
   refuses to click anything labelled like Save/Apply/Remove/etc.
4. Click through **each transition** so its details load. While each one is
   shown, type `s` to snapshot the DOM, which saves the sliders, labels and a
   screenshot. Scroll the whole playlist too.
5. Type `q`. The browser closes and `network.har` is written.

Then analyze the capture (this works offline and can be re-run):

```bash
python phase1_analyze.py                     # newest run
python phase1_analyze.py output/discovery/<run>
```

It searches response bodies (JSON and protobuf strings), WebSocket frames,
the DOM snapshots, and the web player's own JS bundles (GraphQL operation
names). Keyword matching is configurable in `config.yaml`. It writes:

- `report.md`: verdict, ranked endpoints with transition-like fields, a
  redacted sample of the raw structure, and a per-parameter coverage table
  (out point, in point, length, curve, EQ, filter, effects, track identity).
- `report.json`: the same data in machine-readable form.

Exit code 0 means transition data was found. 1 means it wasn't, and the
report says why.

**Send back `report.md` only.** The run folder also contains
`network.har` and `bodies/`, which hold live session tokens. Never share or
commit them (`output/` is git-ignored).

### Run folder layout

```
output/discovery/<timestamp>/
  network.har             full HAR with embedded bodies (contains tokens!)
  index.jsonl             one line per response: endpoint, GraphQL op, status, body file
  bodies/                 raw response bodies (JSON / protobuf / text)
  ws_frames.jsonl         WebSocket frames
  blocked_requests.jsonl  writes the guard stopped
  dom/                    per-snapshot .json (keyword elements, sliders), .html, .png
  discover.log, analyze.log
  report.md, report.json
```

## Fallback: desktop app via CDP

If the report says **"No transition data observed"**, the web player
probably doesn't expose mixing. The feature may only exist in the desktop
and mobile apps. The next option is to attach to the Spotify desktop app,
which is a Chromium (CEF) app, over the Chrome DevTools Protocol with
Playwright's `connect_over_cdp`, by starting Spotify with
`--remote-debugging-port=9222`. Whether current Spotify builds still honor
that flag has to be checked on your machine first. This is not built yet and
will only be added after the Phase 1 report.

## Known limits

- The feature is a Spotify beta. Field names, endpoints and UI change
  without notice, so the analyzer uses keyword heuristics and the coverage
  table needs a human sanity check.
- The write guard recognizes known write patterns. A brand-new write
  endpoint could slip through, so don't press Save/Apply while recording.
- Keyword hits on track titles (e.g. "Extended Mix") show up as *value*
  hits. They don't count toward the verdict.
- Signing in with Google or Apple inside an automated browser is sometimes
  refused. Use email/password or a one-time code on Spotify's page if so.
- rekordbox output (XML memory cues on Spotify tracks) depends on how
  rekordbox references streaming tracks in its XML. This is checked in
  Phase 3 using a small export from your library. The tool never writes to
  `master.db`.

## Development

```bash
python -m unittest discover -s tests -t .
```
