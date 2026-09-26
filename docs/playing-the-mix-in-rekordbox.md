# Playing the imported mix in rekordbox

How to import the playlist and actually play it, including the setting that
makes rekordbox mix on top of your mix and ruin both.

- [Set expectations first](#set-expectations-first)
- [Importing](#importing)
- [Turn off rekordbox's own mixing](#turn-off-rekordboxs-own-mixing)
- [Three ways to play it](#three-ways-to-play-it)
- [Reading the cues](#reading-the-cues)
- [Performing a transition](#performing-a-transition)
- [Editing cues by hand](#editing-cues-by-hand)
- [Troubleshooting](#troubleshooting)

Menu paths below are rekordbox 6 and 7. Labels move between versions, so treat
them as where to look rather than exact clicks.

## Set expectations first

The import gives you the running order, a cue at every mix point, loops where
Spotify had loops, and a written record of the effects you chose. It does not
give you Spotify's sound, because rekordbox XML cannot carry volume or EQ
automation. That is a limit of the format, not of the export.

So there is no configuration that makes rekordbox replay your Spotify mix
unattended. The cues tell you where every blend happens and the cue sheet tells
you what the blend was. Performing it is the part that stays yours.

## Importing

1. **Back up first** if this is not a fresh collection. `File` → `Library` →
   `Backup Library`. Importing adds cues to tracks you already own.
2. `File` → `Import` → `Import Playlist`, and pick `rekordbox.xml` from the run
   folder. Or set it as the Imported Library under `Preferences` → `View` →
   `Layout` → `rekordbox xml`, then drag the playlist into your collection.
3. **Analyse the tracks.** Select the playlist, right-click, `Analyse Track`.
   Without a beatgrid nothing snaps to a beat and quantised cues cannot work.

Importing never writes to `master.db` directly; rekordbox does its own import.

### Re-importing after a re-export

rekordbox **adds** cues rather than replacing them. Import twice and you get two
sets on the same track, usually a beat or two apart, which looks exactly like a
timing bug.

Before re-importing, delete the old cues on the affected tracks: load the
track, and in the cue list remove each memory cue with the minus button. Do
this whenever you re-run Phase 3 after a fix.

## Turn off rekordbox's own mixing

**This is the setting that causes the "it mixed twice" problem.** Two separate
features will each invent their own transition on top of the cues:

**Automix** (Performance mode, rekordbox 6.6 and later) plays a playlist
unattended, choosing its own transition point and its own crossfade length. It
knows nothing about the imported cues, so it blends at its own position over a
track that was supposed to hand over somewhere else. Switch it off.

**Fade In/Out**, in `Preferences` → `Player`, crossfades every track change
regardless of Automix. It will smear every out point. Switch it off too.

Neither is related to Spotify's automix. Spotify's automix is playback
processing inside Spotify and never touched your downloaded files, which are
plain unprocessed tracks. Any doubling you hear in rekordbox is rekordbox.

## Three ways to play it

### 1. Straight playback, no blending

Turn off Automix and Fade In/Out and let the playlist run. You get the correct
running order with hard cuts between tracks. The cues sit there as markers.
Nothing overlaps and nothing fights. This is the honest unattended option, and
it will not sound like your Spotify mix, because the blends are what made it
sound that way.

### 2. Two decks, performed

What the cues exist for, and the only route that gets close to the original.
See [Performing a transition](#performing-a-transition).

### 3. Automix on its own terms

Let rekordbox mix the playlist its way and ignore the imported cues. You get
the running order and consistent machine transitions that are nothing like the
ones you set in Spotify. Fine if you only wanted the track order.

## Reading the cues

Cues are numbered by transition, from 01.

| Cue | Sits on | Means |
| --- | --- | --- |
| `→ NN out` | Outgoing track | The blend starts here |
| `NN in →` | Incoming track | Start the incoming track here |
| `NN loop Nb` | Incoming track | A real loop of N beats, as Spotify had it |
| `→ NN loop`, `NN in loop` | Both | The overlap window, when there was no loop set |
| `spotify 0:00` | Any shifted track | Where Spotify's start sits in this file |

So transition 04 means: deck A hits `→ 04 out`, deck B starts from `04 in →`.

A `spotify 0:00` marker means that file began with silence Spotify's copy did
not have, and the cues were moved to compensate. Nothing to do; it is there so
the adjustment is visible.

### Useful settings

| Setting | Where | Why |
| --- | --- | --- |
| Quantize, 1 beat | Toolbar | Cues and loops snap to the grid |
| Auto Cue → at memory cue | `Preferences` → `Player` | A loaded track starts at its `in` cue instead of 0:00 |
| Show memory cues | Waveform display options | Otherwise the markers are invisible |

## Performing a transition

Open `cue_sheet.md` from the run folder. It lists, per transition, the out
point, in point, overlap, tempos, keys, and the five ingredient settings.

The general shape:

1. Load the incoming track to the free deck. With Auto Cue at memory cue it
   starts at its `in` point already.
2. Match tempo. The cue sheet gives both BPMs; large jumps were a hard cut in
   Spotify too.
3. When the playing deck reaches its `→ NN out` cue, start the incoming deck.
4. Perform the ingredient moves for that transition over the overlap.

### What the ingredients mean at the mixer

| Setting | What to do |
| --- | --- |
| Volume: Crossfade / Smooth crossfade | Ride the crossfader across the overlap, smooth being the gentler curve |
| Volume: Overlap | Both at full for the overlap, then drop the outgoing |
| Volume: Fade in fade out | Incoming up as outgoing comes down |
| Volume: Fade in cut out | Incoming up, then cut the outgoing dead |
| EQ: Centre bass swap | Incoming in with bass killed; at the midpoint kill the outgoing bass and restore the incoming |
| EQ: Start / End bass swap | Same move at the start or end of the overlap |
| EQ: Bass fade out | Ride the outgoing bass down across the overlap |
| EQ: 3-band fade | All three bands hand over together |
| Filter: High-pass in/out | Sweep a high-pass on that side |
| Filter: Low-pass in/out | Sweep a low-pass on that side |
| Effects: Reverb cut end | Reverb on the outgoing, cut at the end |
| Effects: Echo ½ / ¾ | Echo at that beat division, cut or released at the end |
| Looping | Loop of that many beats at the in point |

Most transitions are a bass swap, which is the standard move: bring the new
track in with no bass, swap the low end at the midpoint.

### The exact curves

If a transition was previewed during capture, the bottom of `cue_sheet.md`
holds the real automation as a table sampled across the overlap, with volume
and all three EQ bands for both sides. EQ values are knob positions: 0.5 is
centre, 0 is a kill.

A worked example from a real capture, a 2.51 second overlap:

- The incoming track enters silent, bass killed, mids and highs cut to 0.2.
- Its volume ramps to full across the first half.
- At 48% of the overlap everything swaps at once: outgoing bass to kill, its
  mids and highs to 0.2, incoming bass and everything else back to centre.
- The outgoing volume cuts to silence in about five milliseconds.

That is a bass-swap cut, not a gentle crossfade, and it is why doing this by
crossfader alone sounds wrong.

## Editing cues by hand

Cue positions cannot be typed in. You place the playhead and store.

1. Load the track in Export mode and make sure it is analysed.
2. Quantize on, 1 beat.
3. Find the spot and store with the Memory button in the cue panel.
4. To replace one, select it in the cue list, remove it with minus, then store
   the new one.

For an out point, do not hunt for the exact millisecond. Play to roughly where
the cue sheet says, then move to the nearest phrase boundary, usually the start
of a 16 or 32 beat block, and store there. Spotify picks its out points on
musical boundaries anyway, so a phrase start is closer to what it did than a
literal timestamp into a file that is arranged differently.

## Troubleshooting

**Everything is mixed twice, or overlaps sound smeared.** Automix or Fade
In/Out is on. See [above](#turn-off-rekordboxs-own-mixing).

**Two sets of cues, slightly apart.** You imported twice. rekordbox adds rather
than replaces. Delete the old set.

**Cues are in the wrong place on some tracks but right on others.** Those files
are a different edit of the right song. Run `python phase0_download.py
--check-only`. No rekordbox setting fixes this; the audio has to be replaced.

**Cues do not snap to the beat.** The track is not analysed, or Quantize is off.

**Tracks show as missing files.** The XML was built without a music directory,
so paths point under `SPOTIFY_TRACK_NOT_FOUND_LOCALLY`. Set `paths.music_dir`
in `config.yaml` and re-run Phase 3.

**Loops are the wrong length.** A loop's length is its beat count against the
track's tempo, so a missing or wrong BPM gives a wrong loop. Check the
`bpm / key` line in the cue sheet.
