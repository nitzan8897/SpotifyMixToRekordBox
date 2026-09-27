# Transferring a Spotify mix to rekordbox

This walks the whole pipeline, from a mixed playlist in Spotify to a rekordbox
playlist with cues on every mix point. It also says plainly what does not
survive the trip, because that matters more here than in most import jobs.

- [What actually transfers](#what-actually-transfers)
- [Before you start](#before-you-start)
- [The four phases](#the-four-phases)
- [Phase 1: capture](#phase-1-capture)
- [Phase 2: extract](#phase-2-extract)
- [Phase 0: get the audio](#phase-0-get-the-audio)
- [Phase 3: build the rekordbox XML](#phase-3-build-the-rekordbox-xml)
- [Reading the output](#reading-the-output)
- [The one problem that ruins everything](#the-one-problem-that-ruins-everything)
- [Troubleshooting](#troubleshooting)

## What actually transfers

Spotify's mix editor holds three kinds of information per transition. They
transfer very differently.

| What Spotify has | Transfers? | Where it ends up |
| --- | --- | --- |
| Running order | Fully | The rekordbox playlist order |
| Out point, in point, overlap length | Fully | Memory cues, to the millisecond |
| BPM and key | Fully | `AverageBpm` and `Tonality` |
| Which effects you chose | As text | The cue sheet, for you to perform |
| Loop length in beats | As a real loop | A loop marker of the right length |
| Volume and EQ automation curves | No | Nothing in rekordbox XML can hold them |

The last row is the honest limit. Spotify runs a continuous volume curve and a
three-band EQ curve across every overlap. rekordbox XML has no field for mixer
automation of any kind, because that lives in the mixer, not in track
metadata. No exporter can put it there. What you get instead is every cue in
the right place, the effect settings written down, and loops where Spotify had
loops, so you can perform the blend yourself.

If you want unattended playback that sounds like Spotify, no rekordbox XML can
give you that. [Rendering the mix](rendering-the-mix.md) can: it applies the
automation here and writes finished audio, sidestepping the format entirely.

## Before you start

You need:

- **Python 3.11+** and `pip install -r requirements.txt`.
- **The Spotify desktop app.** The mix editor does not exist in the web
  player, so `browser.target` is `desktop` in `config.yaml` and should stay
  that way.
- **A playlist you own** with transitions set up. Set `playlist_url` in
  `config.yaml`.
- **A folder for your audio**, set as `paths.music_dir` in `config.yaml`.

One safety note: capture runs with a write guard on
(`discovery.block_playlist_writes`, see [src/guard.py](../src/guard.py)). It
aborts any request that would modify a playlist or your library, so a capture
cannot damage the mix it is reading. Leave it on.

## The phases

```text
Phase 1  phase1_discover.py   drive Spotify, record the editor      -> output/discovery/<run>/
Phase 2  phase2_extract.py    turn the capture into structured data -> transitions.json, cue_sheet.md
Phase 0  phase0_download.py   fetch the audio (needs Phase 2 first) -> your music_dir
Phase 3  phase3_rekordbox.py  build the importable playlist         -> rekordbox.xml
Phase 4  phase4_render.py     mix it down to finished audio         -> mix.mp3
```

Phase 0 is numbered before Phase 1 because the audio comes first conceptually,
but it runs after Phase 2, because it takes its track list from what the
capture resolved.

## Phase 1: capture

Open Spotify, open your playlist, and **open the Mix view** so the transition
chips are visible between tracks. Those chips, labelled `Custom` or
`Automatic`, are how the capture finds transitions. Then:

```bash
python phase1_discover.py --auto
```

`--auto` walks every transition on its own: it clicks each chip, waits for
that chip to report its editor open, previews the transition so the player
reports its curves, and snapshots the DOM. Expect roughly two minutes for
twenty-four transitions.

The wait matters. The editor keeps showing the previous transition for a moment
after a click, so snapshotting on a timer records the wrong one. The walk waits
for the clicked chip's `aria-checked` to turn true and skips the transition
with a warning if it never does, rather than saving a stale panel.

Useful flags:

| Flag | Effect |
| --- | --- |
| `--preview-ms N` | How long to let each transition play. Default 4000. |
| `--preview-ms 0` | Skip previewing. Much faster, captures no automation curves. |

If you would rather drive it yourself, run without `--auto` and use the
prompt: `a` runs the same automatic walk, `s` snapshots the current transition,
`c` lists buttons that might open the editor, `q` finishes.

## Phase 2: extract

```bash
python phase2_extract.py
```

Reads the newest run and writes `transitions.json` and `cue_sheet.md`. It is
offline and re-runnable, so you can run it again after any parser change
without re-capturing.

Check three lines in its output.

**Where the order came from.** `Running order taken from the page's own
transition positions` means the capture worked properly. Every chip carries its
place in the running order, so nothing has to be inferred. The fallback,
`the chain of track names`, still works but is guesswork by comparison.

**Whether anything was dropped.** A snapshot taken before a real transition was
opened records whatever the editor was still showing, which is a pair the mix
never plays. Those are dropped by name and reported.

**Whether the capture is complete.** `Capture is complete: all N transitions
the mix has were captured` is exact, not an estimate: the page shows one chip
per transition, so the chip count is how many the mix actually has. If any were
missed it says how many and tells you to re-run the walk.

## Phase 0: get the audio

```bash
python phase0_download.py
```

Takes the track list from `transitions.json`, downloads with spotdl into
`paths.music_dir`, then checks what arrived. Nothing is hardcoded.

| Flag | Effect |
| --- | --- |
| `--check-only` | Download nothing, just audit what you already have |
| `--fix` | Re-fetch every file that is a different edit, choosing the source whose length matches Spotify's |
| `--only TITLE` | Download just matching tracks; repeat for several |

Read [the section below](#the-one-problem-that-ruins-everything) before
trusting the result.

## Phase 3: build the rekordbox XML

```bash
python phase3_rekordbox.py
```

Writes `rekordbox.xml` into the run folder, using `paths.music_dir` unless you
pass `--music-dir`.

| Flag | Effect |
| --- | --- |
| `--hot-cues` | First eight cues per track become hot cues (pads A-H) |
| `--grid` | Also write a beatgrid from Spotify's BPM. Off by default, and rightly so: it assumes beat one sits exactly at 0.000s at a constant tempo, which is wrong for most files, and rekordbox trusts an imported grid instead of analysing. Cue positions do not depend on it. |
| `--no-align` | Do not measure files or shift cues onto them |
| `--playlist-name` | Name inside rekordbox |

### Cues it writes

Per transition, numbered from 01:

- `→ NN out` on the outgoing track, where the blend starts.
- `NN in →` on the incoming track, where it comes in.
- `NN loop Nb` on the incoming track when that transition has a Loop
  ingredient. Its length is the beat count against the track's tempo, which is
  the loop Spotify actually plays.
- `→ NN loop` and `NN in loop` over the overlap window when there is no Loop
  ingredient, marking where the blend happens.
- `spotify 0:00` on any track whose cues had to be shifted, showing where
  Spotify's start landed.

A track used twice in the mix gets one set per appearance.

### Automatic alignment

A download is a different encode from Spotify's copy, and encodes disagree
about where the music starts. A YouTube rip often carries a second or two of
silence up front, which would put every cue on that track late.

Phase 3 decodes each file, measures the silence at both ends, and compares the
remaining content length against Spotify's duration. If the content matches but
the file is longer and starts with silence, the extra is at the front: Spotify's
zero is at the end of that silence, and every cue shifts by exactly that much.
A `spotify 0:00` marker records where the shift put it, so the adjustment is
visible rather than hidden.

Where the file holds more music than Spotify's whole duration, it is a
different edit. No single shift can fix that, so none is applied and the track
is reported instead. See [src/align.py](../src/align.py).

## Reading the output

Each run folder holds:

| File | What it is |
| --- | --- |
| `cue_sheet.md` | The mix in readable form: every transition with its points, tempo, key, mode and five ingredient settings. This is the document to keep open while you play. |
| `transitions.json` | The same data structured, plus the extraction report |
| `rekordbox.xml` | The importable collection and playlist |
| `dom/` | Raw DOM snapshots, one per transition |
| `bodies/`, `index.jsonl` | Captured network responses |
| `*.log` | What each phase did |

### The five ingredients

Spotify builds every transition from five slots, and the cue sheet lists what
you chose for each. The wording matches the app.

| Slot | Options seen |
| --- | --- |
| Volume | Crossfade, Smooth crossfade, Overlap, Fade in fade out, Fade in cut out, Unknown (a curve dragged off any preset) |
| EQ | Start bass swap, Centre bass swap, End bass swap, Bass fade out, 3-band fade |
| Filter | High-pass filter in or out, Low-pass filter in or out, and combinations |
| Effects | Reverb cut end, Echo ½ out end, Echo ¾ cut end |
| Looping | 1-beat, 2-beat, 8-beat loop |

Any option not in the table is kept verbatim and reported, so a new Spotify
option shows up as unmapped text rather than vanishing.

### The automation curves

When a transition was previewed during capture, the player reports its actual
volume and three-band EQ curves, and Phase 2 decodes them into a readable
recipe at the end of `cue_sheet.md`. EQ values are knob positions where 0.5 is
the centre detent and 0 is a full kill.

Spotify attaches these only to the transition currently playing, never to
upcoming tracks, so previewing is the only way to capture them and you get one
recipe per previewed transition.

## The one problem that ruins everything

Every cue is an **absolute offset into Spotify's timeline**. That is only
meaningful if your local file is the same recording Spotify streamed.

A Spotify URL tells spotdl which track's *tags* to write. It does not tell it
which audio to fetch: spotdl searches YouTube and takes a result. That search
regularly lands on a different edit of the right song, a sped-up version, an
extended mix, a re-upload with a long intro. The filename and tags look
perfect. The music is arranged differently, so every cue on that file points
at the wrong bar.

Every phase that touches audio compares it against Spotify's duration and names
the offenders. The comparison is on *content* length - the audio once silence at
each end is discounted - because raw file length misleads in both directions. A
download can be seconds longer purely because of an outro tail and still be the
same recording; one that ends early cannot be.

To fix them:

```bash
python phase0_download.py --fix
```

This searches for the upload whose length best matches Spotify's, instead of
taking whatever the search returns first, which is how the wrong versions get
in. Replaced files are moved to a sibling folder rather than deleted. To pin a
specific source by hand:

```bash
python -m spotdl download "https://youtu.be/<video id>|https://open.spotify.com/track/<track id>"
```

The left side fixes which recording is fetched, the right side still supplies
the tags.

## Troubleshooting

**"Found no transition chips."** The Mix view is not open. The chips are the
`Custom` and `Automatic` labels between tracks.

**Every snapshot captured the same transition.** The clicks are not switching
the editor. Check the log: it distinguishes finding no chips from finding them
and failing to open them.

**"No cluster response in this run."** Nothing was previewed, so no automation
curves were captured. Cue positions are unaffected. Run with a non-zero
`--preview-ms` if you want the curves.

**"Running order covers N tracks but the capture holds metadata for M."** Some
transitions were never captured. Re-run the walk.

**Timings feel wrong in rekordbox.** Almost always the audio, not the capture.
Run `python phase0_download.py --check-only`. Also confirm rekordbox is not
adding its own transitions on top, which is covered in
[playing-the-mix-in-rekordbox.md](playing-the-mix-in-rekordbox.md).

**Cue positions changed after re-running Phase 2.** Expected if a parser was
fixed between runs. The capture is the source of truth and Phase 2 is
re-runnable against it.
