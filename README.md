# Spotify mix → rekordbox

Takes the transitions you built in a Spotify **mixed playlist** and turns them
into a rekordbox playlist with a cue at every mix point.

Spotify has no public API for transition data, so this records your own
logged-in session while it clicks through the playlist for you. The mix editor
only exists in the **desktop app**, so it drives that over the Chrome DevTools
Protocol, the client being Chromium underneath.

## Documentation

- **[Transferring a Spotify mix](docs/transferring-a-spotify-mix.md)** - the
  full pipeline, what survives the trip, and what to check at each step.
- **[Playing the mix in rekordbox](docs/playing-the-mix-in-rekordbox.md)** -
  importing, the setting that makes rekordbox mix on top of your mix, and how
  to perform each transition.

## Quick start

```bash
pip install -r requirements.txt
# set playlist_url and paths.music_dir in config.yaml

# open Spotify, open the playlist, open the Mix view, then:
python phase1_discover.py --auto     # walks every transition by itself
python phase2_extract.py             # -> transitions.json, cue_sheet.md
python phase0_download.py            # fetch the audio, then audit it
python phase3_rekordbox.py           # -> rekordbox.xml
```

Then import `rekordbox.xml`, and **turn off rekordbox's Automix and Fade
In/Out** or it will add its own transitions on top of the imported cues. That
trap and the rest of playback are covered in the
[rekordbox guide](docs/playing-the-mix-in-rekordbox.md).

## What transfers, and what does not

| What Spotify has | Transfers? |
| --- | --- |
| Running order, out/in points, overlap length | Fully, to the millisecond |
| BPM and key | Fully |
| Which of the five effect slots you chose | As text, in the cue sheet |
| Loop length in beats | As a real loop marker |
| Volume and EQ automation curves | **No** |

rekordbox XML has no field for mixer automation, so the curves cannot be
imported by any tool. What you get is every cue in the right place and a
written record of the blend to perform over it. There is no setting that makes
rekordbox replay a Spotify mix unattended.

## Where the transition data actually lives

Phase 1 answered this, so Phase 2 does not guess. Four independent sources:

**The mix editor DOM** holds the current transition as two waveform sliders
tagged `data-transition-waveform`. `trackA.now` is where the outgoing track
starts fading, `trackB.now` where the incoming one comes in, `-trackB.min` the
overlap. The editor also shows each side's title, artists, BPM and key.

**The ingredient panel**, under `data-curve-editing-ingredient-controls`, names
the five slots for the open transition: Volume, EQ, Filter, Effects, Looping.
This is the only place the chosen preset is named; the network carries the
resulting curves but never their names.

**The transition chips** between tracks, one per transition, labelled `Custom`
or `Automatic`. The chip's `aria-checked` marks which editor is open, so the
capture knows when a click has actually landed, and the chip's position gives
the running order straight from the page rather than inferred.

**The network**, only while a transition preview is playing, in
`connect-state/v1/cluster`: `audio.fade_in_start_time`,
`audio.fade_out_start_time`, `audio.fade_overlap`, and the full volume and
three-band EQ curves. Spotify attaches these only to the track playing and the
one just played, never to upcoming tracks, so there is no bulk fetch.

That last source is what makes the first trustworthy. When a run catches a
cluster response, Phase 2 cross-checks the DOM reading against the player's own
numbers and reports whether they agree.

## Ground rules the code follows

- **Your password is never touched.** The desktop app is already signed in and
  is never asked for credentials. On the `web` target you log in on Spotify's
  own page and only cookies are kept, in `.browser-profile/`.
- **Read-only.** While recording, any request that would modify a playlist or
  your library is aborted before it leaves the browser ([src/guard.py](src/guard.py)):
  GraphQL mutations, playlist `/changes`, Web API writes, and PUT/PATCH/DELETE
  to anything transition-shaped. Blocked requests are logged to
  `blocked_requests.jsonl`. The automatic walk only ever clicks a transition's
  own chip and its own preview button, never Save.
- **No audio, no DRM.** Audio, video and image responses are never saved.
  Nothing tries to decrypt anything.
- **Slow.** Nothing is requested in bulk, and `slow_mo` paces the script's own
  actions.
- **Fail loudly.** If data cannot be found or parsed, the scripts say so and
  exit non-zero rather than writing partial results.

## The one thing that will bite you

Every cue is an **absolute offset into Spotify's timeline**, which is only
meaningful if your local file is the same recording Spotify streamed.

A Spotify URL tells spotdl which track's *tags* to write, not which audio to
fetch. It searches YouTube and takes a result, which regularly lands on a
different edit: a sped-up version, an extended mix, a re-upload with a long
intro. Tags and filename look perfect, the music is arranged differently, and
every cue on that file lands on the wrong bar.

Phase 0 and Phase 3 both measure file lengths against Spotify's and name the
offenders. Run `python phase0_download.py --check-only` any time the timing
feels off.

## Layout

```text
phase0_download.py     fetch the audio the mix needs, then audit it
phase1_discover.py     drive Spotify and record the mix editor
phase1_analyze.py      survey a raw capture for where the data lives
phase2_extract.py      capture -> transitions.json + cue_sheet.md
phase3_rekordbox.py    transitions.json -> rekordbox.xml

src/
  config.py        config.yaml loading and validation
  guard.py         the read-only write guard
  desktop.py       finding, launching and attaching to the Spotify app
  scan.py          keyword search over captured responses
  transitions.py   parsing a snapshot; ordering the mix
  ingredients.py   the five effect slots and the mode chip
  automix.py       decoding the volume and EQ curves
  align.py         matching a local file to Spotify's timeline
  rekordbox.py     the collection, the cues and the XML
  logs.py          logging setup

docs/              the two guides linked above
tests/             one file per module
```

## Requirements

Python 3.11+, and `pip install -r requirements.txt`:

| Package | For |
| --- | --- |
| playwright | driving Spotify |
| PyYAML | config |
| mutagen | reading file durations |
| soundfile, numpy | silence detection for cue alignment |

mutagen, soundfile and numpy degrade gracefully: without them the export still
works, it just cannot catch a wrong-edit download or shift cues onto a file
that starts with silence.

## Tests

```bash
python -m unittest discover -s tests
```

The fixtures are real strings from real captures, including both the English
and Hebrew editor builds, so a parser change that breaks one language shows up
immediately.
