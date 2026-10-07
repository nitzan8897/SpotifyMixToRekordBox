# Rendering the mix to one audio file

For when you want the set to simply play, with the transitions already in it,
and you do not want to learn to perform them.

- [Why this exists](#why-this-exists)
- [Running it](#running-it)
- [What it reproduces](#what-it-reproduces)
- [What it does not](#what-it-does-not)
- [One file per song, for an autoplay playlist](#one-file-per-song-for-an-autoplay-playlist)
- [Using a DJ controller over the render](#using-a-dj-controller-over-the-render)
- [How the mixing works](#how-the-mixing-works)
- [Troubleshooting](#troubleshooting)

## Why this exists

The rekordbox export writes cue points. A cue marks where a blend goes, but
something still has to perform it, because rekordbox XML has no field for
mixer automation and never will. That is fine if you want to DJ the set. It is
useless if you want to press play.

So this does the mixing here instead. It reads the same captured data, applies
each transition's volume, EQ and filter moves across the overlap, and writes a
single continuous track. No controller, no timing, no learning curve.

## Running it

Run it after Phase 2, with the audio already downloaded:

```bash
python phase4_render.py
```

Writes `mix.mp3` into the run folder. On a 25-track set expect under a minute.

| Flag | Effect |
| --- | --- |
| `-o PATH` | Where to write. The extension picks the format: `.mp3`, `.wav`, `.flac`. |
| `--separate` | One file per song instead of a single mix. See below. |
| `--solo` | With `--separate`, each song alone, blends removed |
| `--format EXT` | Format for `--separate` pieces: `wav` (default), `flac`, `mp3` |
| `--music-dir DIR` | Override `paths.music_dir` |
| `--no-align` | Do not shift for files that start with extra silence |
| `-v` | Verbose |

Use `.wav` or `.flac` if the render is going into further editing; MP3 is fine
for listening.

## What it reproduces

**Timing.** Out points, in points and overlap lengths exactly as captured,
which for the run this was built against matched Spotify's own player to the
millisecond.

**Volume.** Each transition's Volume setting, as its actual shape. The shapes
are the player's own, read off the transitions whose curves were captured:

| Setting | What the render does |
| --- | --- |
| Overlap | Both tracks at full for the overlap, then the outgoing stops |
| Crossfade | Linear fade down against linear fade up |
| Smooth crossfade | Equal-power curve, so the middle does not dip |
| Fade in fade out | Incoming up over the first half, outgoing down over the second |
| Fade in cut out | Incoming up over the first half, outgoing cut at the end |
| None | Incoming at full at once, outgoing down over the second half |
| Unknown | A curve dragged off any preset; equal-power is used |

**EQ.** The bass swaps are swaps, not fades: the outgoing low band is killed
and the incoming one restored at the same instant - in the first milliseconds,
just before the midpoint, or in the last moments, as set. A swap that would
land where the track holding the bass has already faded below half volume is
brought forward to that point, so a blend never goes hollow. The 3-band fade
steps every band, the highs at a quarter of the way and the rest at the
midpoint. Bass fade out is a ramp.

**Filter.** A real resonant filter, swept from the player's cutoff and
resonance curves when captured. Rebuilt from the name otherwise: an "out"
sweep holds open for the first half and closes over the second, an "in" sweep
starts closed and is open by the midpoint.

**Effects.** The reverb is a convolution with a decaying-noise tail, set from
the player's decay, damping, room size and brightness, or from its values for
the one reverb it reported. Echo repeats at the named beat division. Either
follows the player's dry/wet curve and keeps ringing for up to six seconds
after the outgoing track is cut, which is what "cut end" means.

**Looping.** A beat repeat on the outgoing track, at that track's tempo, for
the length the setting names. The loop is the beat or bar that *ends* at the
out point: the moment just heard is held. The player calls it a roll and
reports it as `fade_out_roll_time`, which is what settles that it belongs to
the track leaving rather than the one arriving.

**Tempo.** The player plays the incoming track at the outgoing one's tempo
for the length of the blend; a captured transition reports the incoming side
as lasting the overlap times the tempo ratio. The render does the same, with
Rubber Band through ffmpeg, so the pitch stays put. Captured transitions use
the player's own ratio; rebuilt ones match tempos up to 9 % apart and leave
bigger jumps alone, as the player does. Without an ffmpeg that has Rubber
Band, the speed changes instead and the pitch moves with it for the blend.

**Level.** Every track is measured (ITU-R BS.1770) and brought to the set's
median loudness, but no louder than -10 LUFS, which leaves room for two tracks
playing at once. A look-ahead limiter then holds the peaks at -1 dBFS,
turning down only the moments that need it. MP3 is written at 320 kbps.

**Alignment.** Files that begin with silence Spotify's copy does not have are
shifted so the blend lands on the music, the same correction the cue export
applies.

Where the capture caught the player's own automation curves, those are used
directly instead of rebuilding from the setting name. Curves are only reported
for transitions that were previewed during capture, so most runs rebuild from
names, which is why the name tables matter.

## What it does not

**Anything on a wrong-edit file.** If a download is a different recording of
the right song, the out point is the wrong musical moment and the blend will
sound wrong. The render names those tracks. No render setting fixes it; the
audio has to be replaced. See the main guide's section on
[the one problem that ruins everything](transferring-a-spotify-mix.md#the-one-problem-that-ruins-everything).

## One file per song, for an autoplay playlist

```bash
python phase4_render.py --separate --format flac
```

Writes a numbered file per song into `tracks/`. Put them in a playlist, turn on
autoplay, and you hear the mix.

**Gapless playback has to be on.** Each cut falls at the end of a blend, so the
pieces tile the mix with no gap and no overlap: played back to back they
reproduce it sample for sample, which the tests check by concatenating them and
comparing against the continuous render. Any silence a player inserts between
files lands in the middle of a blend.

Use `wav` or `flac`. MP3 joins are not reliably gapless, because encoder padding
adds a few milliseconds of silence at every join, which you hear as a click.

One consequence worth understanding. The last seconds of each file already
contain the opening of the next song, because a blend is two songs sounding at
once, and that has to live in one file or the other when files play in sequence
rather than overlapping. So the pieces are a mix cut into chapters, not 25
independent songs. Shuffling them, or playing one alone, will sound odd at the
end.

### If you want genuinely standalone songs

```bash
python phase4_render.py --separate --solo --format flac
```

Each file then holds only its own song, trimmed to the mix's in and out points
with a fade at each edge, written into `tracks_solo/`. These survive shuffling
and play fine alone.

What you lose is the blends. A blend is the moment two songs sound together,
and that moment cannot exist when each file holds one song, so this is not a
slice of the mix - each track is rendered separately. Played back to back you
get the running order and the trimmed edges, not the transitions. On the set
this was written against, solo runs 43:42 against the mix's 40:57; the
difference is the overlaps that no longer happen.

## Using a DJ controller over the render

The point of a rendered set is that it plays itself, but nothing stops you
performing over it.

Load `mix.mp3` into rekordbox as a single track and analyse it. You get one
long waveform with every blend already in place. From there the controller
does what it always does: EQ and filter over the top, effects, loops on
anything you want to extend, cue points wherever you want to jump.

Two things worth doing:

- **Turn off Automix and Fade In/Out.** The set is already mixed; anything that
  adds its own crossfade will fight it. This is the same trap covered in the
  [rekordbox guide](playing-the-mix-in-rekordbox.md#turn-off-rekordboxs-own-mixing).
- **Set memory cues at the track changes** if you want to jump around. The cue
  sheet lists every transition's position in the original timeline, though the
  render's positions differ because overlaps compress the running time.

If you want to mix live between two decks instead, that is the cue-point route
and the rekordbox guide covers it.

## How the mixing works

Each track contributes one slice, from where it enters to the end of its blend
into the next. The last part of a slice is the outgoing half of a blend, the
first part is the incoming half of the previous one. Both halves are shaped on
their own audio and then simply summed, which is what a mixer does.

For the EQ work the audio is split into three bands with zero-phase filters at
250 Hz and 4 kHz. Zero-phase matters because the bands are summed back
together: a phase-shifted band would hollow out the sum instead of rebuilding
it. The split is lossless, which the tests check by summing the bands back to
the original.

Gains use Spotify's own scale, where 0.5 is the centre detent, 0 is a full kill
and 1 is maximum boost.

Overlapping tracks sum past full scale, so the finished mix is scaled down to
fit if its peak went over. The run says when that happened.

Tracks are decoded one at a time and released, so memory stays near one track
rather than the whole set.

## Troubleshooting

**"No music directory."** Rendering needs the actual audio, not just cue
positions. Set `paths.music_dir` in `config.yaml`.

**"No local file for X."** That track was never downloaded. Run
`python phase0_download.py`.

**Some blends sound wrong, most sound right.** Wrong-edit files. Run
`python phase0_download.py --check-only`.

**The mix sounds quieter than the source tracks.** Overlaps summed past full
scale and the whole thing was scaled to fit. Raise it afterwards if you want,
or accept it; the relative balance is unchanged.

**A blend sounds abrupt.** Check that transition's Volume setting in
`cue_sheet.md`. Fade in cut out and Overlap both end with a hard stop, because
that is what they do in Spotify.

**Silence at the end.** The last track's own outro. The render stops where the
final track stops.
