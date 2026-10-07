"""Mixing the set down to one continuous file."""
import unittest
from pathlib import Path

import numpy as np

from src import render
from src.transitions import Track, Transition


class KnobTest(unittest.TestCase):
    """The 0..1 EQ scale, where 0.5 is the centre detent."""

    def test_centre_is_unity(self):
        self.assertAlmostEqual(render.knob_to_gain(0.5), 1.0)

    def test_zero_is_a_kill(self):
        self.assertEqual(render.knob_to_gain(0.0), 0.0)

    def test_the_cut_spotify_uses(self):
        # Knob 0.2 is the cut applied to the incoming mids and highs.
        self.assertAlmostEqual(render.knob_to_gain(0.2), 0.4)
        self.assertLess(render.knob_to_gain(0.2), 1.0)

    def test_above_centre_boosts(self):
        self.assertGreater(render.knob_to_gain(1.0), 1.0)


class BandSplitTest(unittest.TestCase):
    def test_bands_sum_back_to_the_input(self):
        """They are summed again after gaining, so the split must be lossless."""
        x = np.random.default_rng(0).standard_normal((44100, 2)).astype(np.float32)
        low, mid, high = render.split_bands(x)
        self.assertTrue(np.allclose(low + mid + high, x, atol=1e-4))

    def test_low_band_holds_the_bass(self):
        t = np.arange(44100) / 44100.0
        bass = np.sin(2 * np.pi * 60 * t).astype(np.float32)[:, None] * np.ones((1, 2), np.float32)
        low, _, high = render.split_bands(bass)
        self.assertGreater(np.abs(low).mean(), np.abs(high).mean() * 10)

    def test_high_band_holds_the_treble(self):
        t = np.arange(44100) / 44100.0
        tone = np.sin(2 * np.pi * 9000 * t).astype(np.float32)[:, None] * np.ones((1, 2), np.float32)
        low, _, high = render.split_bands(tone)
        self.assertGreater(np.abs(high).mean(), np.abs(low).mean() * 10)

    def test_short_input_is_passed_through(self):
        x = np.ones((4, 2), dtype=np.float32)
        low, mid, high = render.split_bands(x)
        self.assertTrue(np.allclose(low + mid + high, x, atol=1e-4))


class VolumeEnvelopeTest(unittest.TestCase):
    N = 1000

    def env(self, style):
        return render.volume_envelopes(np, self.N, style)

    def test_overlap_keeps_both_at_full(self):
        out, inc = self.env("overlap")
        self.assertTrue(np.allclose(out, 1.0))
        self.assertTrue(np.allclose(inc, 1.0))

    def test_fade_in_cut_out_holds_the_outgoing_then_drops_it(self):
        out, inc = self.env("fade in cut out")
        self.assertTrue(np.allclose(out, 1.0))      # cut happens after the overlap
        self.assertAlmostEqual(float(inc[0]), 0.0)
        self.assertAlmostEqual(float(inc[-1]), 1.0)

    def test_fades_take_half_the_overlap(self):
        """The player's curves fade in over the first half and out over the
        second; spreading both across the whole overlap sank the middle."""
        out, inc = self.env("fade in fade out")
        self.assertAlmostEqual(float(inc[self.N // 2]), 1.0, places=2)
        self.assertAlmostEqual(float(out[self.N // 2 - 1]), 1.0, places=2)
        self.assertAlmostEqual(float(out[-1]), 0.0)
        self.assertAlmostEqual(float(inc[0]), 0.0)

    def test_no_volume_setting_still_takes_the_outgoing_track_out(self):
        out, inc = self.env(None)
        self.assertTrue(np.allclose(inc, 1.0))
        self.assertAlmostEqual(float(out[self.N // 4]), 1.0)
        self.assertAlmostEqual(float(out[-1]), 0.0)

    def test_crossfade_is_linear_both_ways(self):
        out, inc = self.env("crossfade")
        self.assertAlmostEqual(float(out[0]), 1.0)
        self.assertAlmostEqual(float(out[-1]), 0.0)
        self.assertAlmostEqual(float(inc[0]), 0.0)
        self.assertAlmostEqual(float(inc[-1]), 1.0)

    def test_smooth_crossfade_holds_power_through_the_middle(self):
        """A linear cross dips in the middle; an equal-power one does not."""
        out, inc = self.env("smooth crossfade")
        mid = int(self.N / 2)
        power = out[mid] ** 2 + inc[mid] ** 2
        self.assertAlmostEqual(float(power), 1.0, places=3)

    def test_unknown_style_falls_back_without_raising(self):
        out, inc = self.env("something new spotify added")
        self.assertEqual(len(out), self.N)
        self.assertEqual(len(inc), self.N)


class EqEnvelopeTest(unittest.TestCase):
    N = 1000

    def at(self, move, band, pos):
        return float(getattr(move, band)[int(self.N * pos)])

    def test_every_bass_swap_happens_inside_the_overlap(self):
        """The bug this guards made a 7.4 s blend play with no bass at all.

        With "start" at 0.0 and "end" at 1.0 the swap lands on the boundary and
        never fires during the blend, so the incoming track kept a killed low
        end for the whole overlap. Tuf Tuf into YUMMI sounded hollow for that
        reason.
        """
        for style in ("start bass swap", "centre bass swap", "end bass swap"):
            out, inc = render.eq_envelopes(np, self.N, style)
            self.assertAlmostEqual(self.at(out, "low", 0.0), 1.0, msg=style)
            self.assertAlmostEqual(self.at(out, "low", 0.99), 0.0, msg=style)
            self.assertAlmostEqual(self.at(inc, "low", 0.0), 0.0, msg=style)
            self.assertAlmostEqual(self.at(inc, "low", 0.99), 1.0, msg=style)

    def test_the_three_swaps_differ_in_when_not_whether(self):
        points = []
        for style in ("start bass swap", "centre bass swap", "end bass swap"):
            _, inc = render.eq_envelopes(np, self.N, style)
            points.append(int(np.argmax(inc.low > 0.5)))
        self.assertEqual(points, sorted(points))
        self.assertEqual(len(set(points)), 3)

    def test_a_bass_swap_leaves_mids_and_highs_alone(self):
        """Only the low end changes hands; cutting the rest made it distant."""
        out, inc = render.eq_envelopes(np, self.N, "centre bass swap")
        for move in (out, inc):
            self.assertTrue(np.allclose(move.mid, 1.0))
            self.assertTrue(np.allclose(move.high, 1.0))

    def test_three_band_fade_is_a_step_not_a_ramp(self):
        """Measured from the one transition whose real curves were captured."""
        out, inc = render.eq_envelopes(np, self.N, "3 band fade")
        # Low hands over at the midpoint.
        self.assertAlmostEqual(self.at(out, "low", 0.25), 1.0)
        self.assertAlmostEqual(self.at(out, "low", 0.75), 0.0)
        # Mid and high step between unity and the 0.2 knob cut, both ways.
        cut = render.knob_to_gain(render.BAND_CUT)
        self.assertAlmostEqual(self.at(out, "mid", 0.25), 1.0)
        self.assertAlmostEqual(self.at(out, "mid", 0.75), cut)
        # The highs change hands first, at a quarter of the way through.
        self.assertAlmostEqual(self.at(inc, "high", 0.2), cut)
        self.assertAlmostEqual(self.at(inc, "high", 0.3), 1.0)
        self.assertAlmostEqual(self.at(out, "high", 0.3), cut)

    def test_swaps_land_where_the_player_puts_them(self):
        """Start in the first moments, centre just before half, end at the end."""
        where = {}
        for style in ("start bass swap", "centre bass swap", "end bass swap"):
            _, inc = render.eq_envelopes(np, self.N, style)
            where[style] = int(np.argmax(inc.low > 0.5)) / self.N
        self.assertLess(where["start bass swap"], 0.01)
        self.assertAlmostEqual(where["centre bass swap"], 0.49, delta=0.01)
        self.assertGreater(where["end bass swap"], 0.97)

    def test_a_swap_is_never_left_where_no_one_carries_the_bass(self):
        """An end swap under a fade-out would leave seconds with no bass."""
        out_vol, in_vol = render.volume_envelopes(np, self.N, "fade in fade out")
        _, inc = render.eq_envelopes(np, self.N, "end bass swap", out_vol, in_vol)
        swap = int(np.argmax(inc.low > 0.5))
        self.assertGreaterEqual(float(out_vol[swap - 1]), 0.5)

        _, inc = render.eq_envelopes(np, self.N, "start bass swap", out_vol, in_vol)
        swap = int(np.argmax(inc.low > 0.5))
        self.assertGreaterEqual(float(in_vol[swap]), 0.5)

    def test_bass_fade_out_is_a_ramp_not_a_step(self):
        out, _ = render.eq_envelopes(np, self.N, "bass fade out")
        self.assertAlmostEqual(self.at(out, "low", 0.0), 1.0)
        self.assertAlmostEqual(self.at(out, "low", 0.5), 0.5, places=2)
        self.assertLess(self.at(out, "low", 0.99), 0.02)

    def test_no_eq_setting_leaves_every_band_alone(self):
        out, inc = render.eq_envelopes(np, self.N, None)
        for m in (out, inc):
            for band in (m.low, m.mid, m.high):
                self.assertTrue(np.allclose(band, 1.0))


class FilterTest(unittest.TestCase):
    """The Filter ingredient as a real moving filter."""
    RATE = 44100

    def sine(self, hz, seconds=2.0):
        t = np.arange(int(seconds * self.RATE)) / self.RATE
        return (0.5 * np.sin(2 * np.pi * hz * t)).astype(np.float32)[:, None] * np.ones(
            (1, 2), np.float32)

    def rms(self, x):
        return float(np.sqrt(np.mean(x ** 2)))

    def flat(self, n, v):
        return np.full(n, v, dtype=np.float32)

    def test_a_held_filter_does_not_tremble(self):
        """The bug: every 25 ms hop was windowed twice, a 40 Hz tremolo 12 dB
        down. A tone well inside the passband must come through steady."""
        x = self.sine(500)
        n = len(x)
        y = render.sweep_filter(np, x, self.flat(n, 0.3), self.flat(n, 0.5), False, self.RATE)
        body = y[self.RATE // 2: -self.RATE // 2]
        self.assertAlmostEqual(self.rms(body), self.rms(x), delta=0.05 * self.rms(x))
        # Level measured in 20 ms windows barely moves.
        w = int(0.02 * self.RATE)
        levels = [self.rms(body[i:i + w]) for i in range(0, len(body) - w, w)]
        self.assertLess(max(levels) / min(levels), 1.05)

    def test_a_closed_low_pass_takes_the_top_away(self):
        x = self.sine(6000)
        n = len(x)
        y = render.sweep_filter(np, x, self.flat(n, 0.0), self.flat(n, 0.5), False, self.RATE)
        self.assertLess(self.rms(y[self.RATE:]), 0.01 * self.rms(x))

    def test_an_open_high_pass_takes_the_bass_away(self):
        x = self.sine(50)
        n = len(x)
        y = render.sweep_filter(np, x, self.flat(n, 0.9), self.flat(n, 0.5), True, self.RATE)
        self.assertLess(self.rms(y[self.RATE:]), 0.05 * self.rms(x))

    def test_neutral_stretches_are_left_untouched(self):
        x = self.sine(300)
        n = len(x)
        cutoff = render._named_filter_curve(np, n, "low pass filter out", "out")
        y = render.sweep_filter(np, x, cutoff, self.flat(n, 0.5), False, self.RATE)
        self.assertTrue(np.allclose(y[:n // 3], x[:n // 3], atol=1e-6))

    def test_named_out_sweep_closes_over_the_second_half(self):
        c = render._named_filter_curve(np, 1000, "high pass filter out", "out")
        self.assertAlmostEqual(float(c[400]), 0.5)
        self.assertAlmostEqual(float(c[-1]), 1.0)

    def test_named_in_sweep_is_open_by_the_midpoint(self):
        c = render._named_filter_curve(np, 1000, "low pass filter in", "in")
        self.assertAlmostEqual(float(c[0]), 0.0)
        self.assertAlmostEqual(float(c[500]), 0.5)
        self.assertAlmostEqual(float(c[-1]), 0.5)

    def test_build_moves_no_longer_folds_the_filter_into_the_eq(self):
        """It used to, so every rebuilt filter was applied twice."""
        ing = {"filter": {"value": "high pass filter out", "off": False},
               "volume": {"value": "overlap", "off": False}}
        out, _ = render.build_moves(np, 1000, ing)
        self.assertTrue(np.allclose(out.low, 1.0))


class EffectTailTest(unittest.TestCase):
    RATE = 44100

    def test_the_tail_rings_on_past_the_blend(self):
        n = self.RATE * 2
        dry = np.random.default_rng(3).standard_normal((n, 2)).astype(np.float32) * 0.1
        ing = {"effects": {"value": "reverb cut end", "off": False}}
        tail = render.effect_tail(np, dry, None, ing, 120, self.RATE)
        self.assertGreater(len(tail), n + self.RATE)
        after = tail[n + self.RATE // 10: n + self.RATE // 2]
        self.assertGreater(float(np.abs(after).mean()), 1e-4)

    def test_an_echo_repeats_on_the_beat_fraction(self):
        n = self.RATE
        dry = np.zeros((n, 2), dtype=np.float32)
        dry[-100] = 1.0                       # one click near the end of the blend
        ing = {"effects": {"value": "echo 1/2 out end", "off": False}}
        tail = render.effect_tail(np, dry, None, ing, 120, self.RATE)
        half_beat = int(0.25 * self.RATE)
        first = int(np.argmax(np.abs(tail[:, 0]) > 0.05))
        self.assertAlmostEqual(first, n - 100 + half_beat, delta=30)

    def test_no_effect_means_no_tail(self):
        dry = np.ones((4096, 2), dtype=np.float32)
        self.assertIsNone(render.effect_tail(np, dry, None, {}, 120, self.RATE))


class FakeEntry:
    def __init__(self, path="x.mp3", offset=0, wrong=False):
        self.local_path = Path(path)
        self.offset_ms = offset
        self._wrong = wrong

    def is_different_edit(self):
        return self._wrong


class PlanTest(unittest.TestCase):
    """Where each track sits in the finished mix."""

    def transitions(self):
        def tr(frm, to, out, inp, overlap):
            return Transition(index=0, snapshot="", from_track=Track(title=frm),
                              to_track=Track(title=to), out_point_ms=out,
                              in_point_ms=inp, overlap_ms=overlap)
        return [tr("A", "B", 100_000, 0, 5_000), tr("B", "C", 90_000, 2_000, 4_000)]

    def plan(self, last_len=120_000, **kw):
        entries = {t: FakeEntry(**kw) for t in ("A", "B", "C")}
        real = render.load_duration_ms
        render.load_duration_ms = lambda p: last_len
        try:
            return render.plan(self.transitions(), lambda t: entries[t.title])
        finally:
            render.load_duration_ms = real

    def test_each_track_starts_where_the_previous_blend_begins(self):
        segments, _ = self.plan()
        self.assertEqual([s.title for s in segments], ["A", "B", "C"])
        # A runs 0 -> its out point plus the overlap.
        self.assertEqual((segments[0].start_ms, segments[0].end_ms), (0, 105_000))
        # B enters at its in point and runs to its own out point plus overlap.
        self.assertEqual((segments[1].start_ms, segments[1].end_ms), (0, 94_000))
        # C enters at its in point and plays out.
        self.assertEqual(segments[2].start_ms, 2_000)

    def test_overlaps_are_subtracted_from_the_running_position(self):
        segments, _ = self.plan()
        self.assertEqual(segments[0].at_ms, 0)
        self.assertEqual(segments[1].at_ms, 100_000)          # 105000 - 5000
        self.assertEqual(segments[2].at_ms, 190_000)          # + (94000 - 4000)

    def test_duration_is_the_end_of_the_last_track_not_a_double_count(self):
        """The bug this guards left a whole track's worth of silence at the end."""
        segments, report = self.plan(last_len=120_000)
        last = segments[-1]
        self.assertEqual(report.duration_ms, last.at_ms + (last.end_ms - last.start_ms))
        self.assertEqual(report.duration_ms, 190_000 + 118_000)

    def test_alignment_offset_moves_the_slice(self):
        segments, _ = self.plan(offset=1_500)
        self.assertEqual(segments[0].start_ms, 1_500)
        self.assertEqual(segments[0].end_ms, 106_500)

    def test_close_tempi_are_matched_across_the_blend(self):
        """Spotify plays the incoming track at the outgoing tempo while they
        overlap; the slice is shorter or longer in the mix by that much."""
        ts = self.transitions()
        ts[0].from_track.bpm, ts[0].to_track.bpm = 121, 125
        entries = {t: FakeEntry() for t in ("A", "B", "C")}
        real = render.load_duration_ms
        render.load_duration_ms = lambda p: 120_000
        try:
            segments, _ = render.plan(ts, lambda t: entries[t.title])
        finally:
            render.load_duration_ms = real
        b = segments[1]
        self.assertAlmostEqual(b.head_ratio, 121 / 125)
        self.assertEqual(b.head_ms, 5_000)
        self.assertAlmostEqual(b.length_ms, 94_000 - 5_000 * 121 / 125 + 5_000, delta=1)

    def test_far_apart_tempi_are_left_alone(self):
        ts = self.transitions()
        ts[0].from_track.bpm, ts[0].to_track.bpm = 89, 121
        self.assertEqual(render.tempo_ratio(ts[0]), 1.0)

    def test_the_players_overlap_wins_over_the_editors(self):
        class Auto:
            overlap_ms = 14_548
            incoming = type("S", (), {"duration_ms": 14_772})()
        t = self.transitions()[0]
        t.overlap_ms, t.automation = 13_997, Auto()
        self.assertEqual(render.overlap_of(t), 14_548)
        self.assertAlmostEqual(render.tempo_ratio(t), 14_772 / 14_548)

    def test_wrong_edits_are_reported(self):
        _, report = self.plan(wrong=True)
        self.assertEqual(len(report.wrong_edit), 3)

    def test_a_missing_file_is_reported_before_any_audio_is_read(self):
        segments, report = render.plan(self.transitions(), lambda t: None)
        self.assertEqual(segments, [])
        self.assertTrue(report.missing)

    def test_no_transitions_is_an_error(self):
        with self.assertRaises(render.RenderError):
            render.plan([], lambda t: None)


class ApplyMoveTest(unittest.TestCase):
    def test_silence_in_silence_out(self):
        n = 2048
        chunk = np.zeros((n, 2), dtype=np.float32)
        out, _ = render.eq_envelopes(np, n, None)
        out.volume = np.ones(n, dtype=np.float32)
        self.assertTrue(np.allclose(render.apply_move(np, chunk, out), 0.0))

    def test_unity_move_returns_the_audio_unchanged(self):
        n = 8192
        chunk = np.random.default_rng(1).standard_normal((n, 2)).astype(np.float32)
        move, _ = render.eq_envelopes(np, n, None)
        move.volume = np.ones(n, dtype=np.float32)
        self.assertTrue(np.allclose(render.apply_move(np, chunk, move), chunk, atol=1e-4))

    def test_zero_volume_mutes(self):
        n = 8192
        chunk = np.random.default_rng(2).standard_normal((n, 2)).astype(np.float32)
        move, _ = render.eq_envelopes(np, n, None)
        move.volume = np.zeros(n, dtype=np.float32)
        self.assertTrue(np.allclose(render.apply_move(np, chunk, move), 0.0, atol=1e-6))

    def test_killing_the_low_band_removes_bass(self):
        n = 44100
        t = np.arange(n) / 44100.0
        bass = (np.sin(2 * np.pi * 60 * t).astype(np.float32)[:, None]
                * np.ones((1, 2), np.float32))
        move, _ = render.eq_envelopes(np, n, None)
        move.volume = np.ones(n, dtype=np.float32)
        move.low = np.zeros(n, dtype=np.float32)
        quiet = render.apply_move(np, bass, move)
        self.assertLess(np.abs(quiet).mean(), np.abs(bass).mean() * 0.1)


class NormalizeTest(unittest.TestCase):
    def test_a_hot_mix_is_turned_down(self):
        mix = np.full((100, 2), 1.9, dtype=np.float32)
        out, peak = render.normalize(mix)
        self.assertAlmostEqual(peak, 1.9, places=5)
        self.assertLessEqual(float(np.abs(out).max()), 0.971)

    def test_a_quiet_mix_is_left_alone(self):
        mix = np.full((100, 2), 0.3, dtype=np.float32)
        out, peak = render.normalize(mix)
        self.assertAlmostEqual(float(np.abs(out).max()), 0.3, places=5)


if __name__ == "__main__":
    unittest.main()


class LimitTest(unittest.TestCase):
    RATE = 44100

    def signal(self):
        t = np.arange(self.RATE * 4) / self.RATE
        x = (0.4 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)[:, None] * np.ones(
            (1, 2), np.float32)
        x[self.RATE * 2:self.RATE * 2 + 2000] *= 4.0       # one hot moment
        return x

    def test_peaks_are_held_under_the_ceiling(self):
        y, peak = render.limit(self.signal(), rate=self.RATE, chunk_s=1.0)
        self.assertGreater(peak, 1.0)
        self.assertLessEqual(float(np.abs(y).max()), render.LIMIT_CEILING + 1e-6)

    def test_only_the_hot_moment_is_turned_down(self):
        """Scaling the whole mix by its peak cost the last render 8 dB."""
        x = self.signal()
        y, _ = render.limit(x.copy(), rate=self.RATE, chunk_s=1.0)
        self.assertTrue(np.allclose(y[:self.RATE], x[:self.RATE]))
        self.assertTrue(np.allclose(y[-self.RATE:], x[-self.RATE:]))

    def test_a_quiet_mix_is_left_alone(self):
        x = np.full((1000, 2), 0.3, dtype=np.float32)
        y, _ = render.limit(x.copy())
        self.assertTrue(np.allclose(y, 0.3))


class SplitTest(unittest.TestCase):
    """Cutting the mix into one file per song."""

    def segments(self):
        # Three songs: bodies 100/90/80 s, overlaps 5/4 s.
        segs = [
            render.Segment("A", Path("a.mp3"), 0, 100_000, 5_000),
            render.Segment("B", Path("b.mp3"), 0, 90_000, 4_000),
            render.Segment("C", Path("c.mp3"), 0, 80_000, 0),
        ]
        at = 0
        for s in segs:
            s.at_ms = at
            at += (s.end_ms - s.start_ms) - s.overlap_ms
        return segs

    def test_pieces_tile_the_mix_with_no_gap_or_overlap(self):
        """Played gapless in order they must reproduce the mix exactly."""
        pieces = render.split_points(self.segments())
        self.assertEqual(pieces[0].from_ms, 0)
        for earlier, later in zip(pieces, pieces[1:]):
            self.assertEqual(earlier.to_ms, later.from_ms)

    def test_a_piece_ends_after_its_blend_so_the_blend_survives(self):
        segs = self.segments()
        pieces = render.split_points(segs)
        # Song A's piece runs to the end of its 5 s overlap, i.e. its full body.
        self.assertEqual(pieces[0].to_ms, 100_000)
        # Which is 5 s past where song B started sounding.
        self.assertEqual(segs[1].at_ms, 95_000)

    def test_total_length_matches_the_mix(self):
        segs = self.segments()
        pieces = render.split_points(segs)
        end = max(s.at_ms + (s.end_ms - s.start_ms) for s in segs)
        self.assertEqual(pieces[-1].to_ms, end)
        self.assertEqual(sum(p.duration_ms for p in pieces), end)

    def test_solo_rendering_is_not_a_slice_of_the_mix(self):
        """Across an overlap the mix holds both songs summed and cannot be cut
        apart again, so solo files need their own render pass."""
        self.assertFalse(hasattr(render, "solo_points"))
        self.assertTrue(callable(render.render_solo))

    def test_every_piece_is_named_in_playing_order(self):
        pieces = render.split_points(self.segments())
        names = [render.piece_filename(p, ".wav") for p in pieces]
        self.assertEqual(names, sorted(names))
        self.assertTrue(names[0].startswith("01 - "))

    def test_filenames_drop_characters_windows_refuses(self):
        piece = render.Piece(index=3, title='Pike: Remix / "Mix" *?', from_ms=0, to_ms=1)
        name = render.piece_filename(piece, ".wav")
        for bad in '<>:"/|?*' + chr(92):
            self.assertNotIn(bad, name)
        self.assertTrue(name.endswith(".wav"))


class RollTest(unittest.TestCase):
    """The Looping ingredient: a beat repeat on the outgoing track."""

    def signal(self, seconds=6.0, rate=44100):
        t = np.arange(int(seconds * rate)) / rate
        mono = (np.sin(2 * np.pi * 220 * t) * np.exp(-(t % 1.0) * 4)).astype(np.float32)
        return mono[:, None] * np.ones((1, 2), np.float32)

    def test_the_period_is_exactly_the_loop_length(self):
        """An earlier version shortened the loop to hide the wrap, which made
        every repeat land early and walked the roll off the beat."""
        rate = 44100
        out = apply = render.apply_roll(np, self.signal(rate=rate), 1000, rate)
        first, second, third = out[:rate], out[rate:2 * rate], out[2 * rate:3 * rate]
        self.assertTrue(np.allclose(first, second, atol=1e-6))
        self.assertTrue(np.allclose(first, third, atol=1e-6))

    def test_it_repeats_rather_than_playing_on(self):
        rate = 44100
        src = self.signal(rate=rate)
        out = render.apply_roll(np, src, 500, rate)
        # The second half-second now matches the first, where the source did not.
        half = rate // 2
        self.assertTrue(np.allclose(out[:half], out[half:2 * half], atol=1e-6))
        self.assertFalse(np.allclose(src[:half], src[half:2 * half], atol=1e-3))

    def test_the_wrap_does_not_step(self):
        rate = 44100
        out = render.apply_roll(np, self.signal(rate=rate), 1000, rate)
        step = float(np.abs(out[rate] - out[rate - 1]).max())
        self.assertLess(step, 0.05)

    def test_a_loop_longer_than_the_region_is_left_alone(self):
        src = self.signal(seconds=1.0)
        self.assertIs(render.apply_roll(np, src, 5000, 44100), src)

    def test_length_is_unchanged(self):
        src = self.signal(seconds=3.0)
        self.assertEqual(len(render.apply_roll(np, src, 700, 44100)), len(src))

    def test_the_held_beat_is_the_one_before_the_out_point(self):
        """Suavemente's roll caught the beat after "mente" and repeated "eh"."""
        rate = 44100
        src = self.signal(seconds=6.0, rate=rate)
        before, chunk = src[:3 * rate], src[3 * rate:]
        out = render.apply_roll(np, chunk, 500, rate, before=before)
        self.assertEqual(len(out), len(chunk))
        held = before[-rate // 2:]
        self.assertTrue(np.allclose(out[rate // 2 + 400:rate], held[400:], atol=1e-6))

    def test_the_first_repeat_joins_the_out_point_without_a_step(self):
        rate = 44100
        src = self.signal(seconds=6.0, rate=rate)
        before, chunk = src[:3 * rate], src[3 * rate:]
        out = render.apply_roll(np, chunk, 500, rate, before=before)
        self.assertLess(float(np.abs(out[0] - before[-1]).max()), 0.05)


class RollLengthTest(unittest.TestCase):
    """How long the roll is, and which track's tempo sets it."""

    def transition(self, beats=2, from_bpm=65, to_bpm=130):
        return Transition(
            index=0, snapshot="", from_track=Track(title="A", bpm=from_bpm),
            to_track=Track(title="B", bpm=to_bpm), overlap_ms=7384,
            ingredients={"loop": {"raw": f"{beats}-beat loop", "value": f"{beats} beat loop",
                                  "off": False, "beats": beats}})

    def test_it_uses_the_outgoing_tempo(self):
        """The player reports this as fade_out_roll_time, so the roll is on the
        track that is leaving and must be measured in that track's tempo."""
        t = self.transition()
        self.assertEqual(render.roll_ms_from(None, t), 1846)     # 2 beats at 65
        self.assertNotEqual(render.roll_ms_from(None, t), 923)   # not 2 at 130

    def test_four_repeats_fill_that_overlap_exactly(self):
        """The check that settles which tempo: 1846 x 4 is the 7384 ms overlap."""
        t = self.transition()
        self.assertEqual(render.roll_ms_from(None, t) * 4, t.overlap_ms)

    def test_no_loop_setting_means_no_roll(self):
        t = self.transition()
        t.ingredients = {}
        self.assertIsNone(render.roll_ms_from(None, t))

    def test_beats_to_ms(self):
        self.assertEqual(render.loop_ms_from_beats(8, 170), 2824)
        self.assertIsNone(render.loop_ms_from_beats(None, 170))
        self.assertIsNone(render.loop_ms_from_beats(2, None))


class LoudnessTest(unittest.TestCase):
    """Bringing every track to a common loudness, the way Spotify plays them."""

    RATE = 44100

    def tone(self, amplitude, seconds=3.0, hz=1000.0):
        t = np.arange(int(seconds * self.RATE)) / self.RATE
        mono = (amplitude * np.sin(2 * np.pi * hz * t)).astype(np.float32)
        return mono[:, None] * np.ones((1, 2), np.float32)

    def test_a_quieter_signal_measures_quieter(self):
        loud = render.measure_loudness(self.tone(0.5), self.RATE)
        quiet = render.measure_loudness(self.tone(0.05), self.RATE)
        self.assertIsNotNone(loud)
        self.assertLess(quiet, loud)

    def test_halving_amplitude_costs_about_six_dB(self):
        a = render.measure_loudness(self.tone(0.4), self.RATE)
        b = render.measure_loudness(self.tone(0.2), self.RATE)
        self.assertAlmostEqual(a - b, 6.0, delta=0.5)

    def test_silence_has_no_measurable_loudness(self):
        silence = np.zeros((self.RATE * 2, 2), dtype=np.float32)
        self.assertIsNone(render.measure_loudness(silence, self.RATE))

    def test_too_short_to_measure(self):
        self.assertIsNone(render.measure_loudness(self.tone(0.5, seconds=0.1), self.RATE))

    def test_quiet_passages_do_not_drag_the_measurement_down(self):
        """A track with a long quiet intro plays as loud as its body."""
        body = self.tone(0.4, seconds=6.0)
        intro = self.tone(0.0005, seconds=6.0)
        with_intro = np.concatenate([intro, body])
        self.assertAlmostEqual(render.measure_loudness(body, self.RATE),
                               render.measure_loudness(with_intro, self.RATE),
                               delta=1.5)

    def test_the_standards_reference_tone_reads_right(self):
        """A full-scale 997 Hz sine in one channel is -3.01 LUFS by definition.
        The shelf used before read bass-heavy tracks 8 dB hot."""
        t = np.arange(self.RATE * 5) / self.RATE
        x = np.zeros((len(t), 2), dtype=np.float32)
        x[:, 0] = np.sin(2 * np.pi * 997 * t)
        self.assertAlmostEqual(render.measure_loudness(x, self.RATE), -3.01, delta=0.1)

    def test_bass_does_not_read_louder_than_it_is(self):
        mid = render.measure_loudness(self.tone(0.3, hz=1000.0), self.RATE)
        bass = render.measure_loudness(self.tone(0.3, hz=60.0), self.RATE)
        self.assertLess(bass, mid)

    def test_db_to_gain(self):
        self.assertAlmostEqual(render.db_to_gain(0.0), 1.0)
        self.assertAlmostEqual(render.db_to_gain(6.0), 2.0, places=2)
        self.assertAlmostEqual(render.db_to_gain(-6.0), 0.5, places=2)


class LoudnessTrimTest(unittest.TestCase):
    """Turning measurements into a gain per file."""

    def segments(self, paths):
        return [render.Segment(f"t{i}", Path(p), 0, 1000, 0) for i, p in enumerate(paths)]

    def trims(self, by_path):
        real_measure, real_load = render.measure_loudness, render.load_audio
        render.load_audio = lambda p, rate=render.TARGET_RATE: str(p)
        render.measure_loudness = lambda audio, rate=render.TARGET_RATE: by_path[audio]
        try:
            return render.loudness_trims(self.segments(by_path))
        finally:
            render.measure_loudness, render.load_audio = real_measure, real_load

    def test_the_target_is_the_median_so_moves_stay_small(self):
        trims = self.trims({"a.mp3": -17.0, "b.mp3": -12.0, "c.mp3": -7.0})
        self.assertAlmostEqual(trims["b.mp3"], 0.0)       # the median moves not at all
        self.assertAlmostEqual(trims["a.mp3"], 5.0)       # quiet one comes up
        self.assertAlmostEqual(trims["c.mp3"], -5.0)      # loud one comes down

    def test_a_loud_playlist_is_brought_down_to_leave_room_for_blends(self):
        trims = self.trims({"a.mp3": -7.0, "b.mp3": -6.0, "c.mp3": -5.0})
        self.assertAlmostEqual(trims["b.mp3"], render.MIX_LUFS + 6.0)

    def test_the_quiet_outlier_is_lifted(self):
        trims = self.trims({"temperature.mp3": -20.9, "b.mp3": -10.7, "c.mp3": -6.3})
        self.assertGreater(trims["temperature.mp3"], 9.0)

    def test_a_trim_is_capped(self):
        trims = self.trims({"a.mp3": -40.0, "b.mp3": 0.0, "c.mp3": 0.0})
        self.assertLessEqual(trims["a.mp3"], render.MAX_TRIM_DB)

    def test_one_file_used_twice_is_measured_once(self):
        calls = []
        real_measure, real_load = render.measure_loudness, render.load_audio
        render.load_audio = lambda p, rate=render.TARGET_RATE: str(p)
        render.measure_loudness = lambda audio, rate=render.TARGET_RATE: (
            calls.append(audio) or -5.0)
        try:
            segs = self.segments(["same.mp3", "same.mp3", "other.mp3"])
            render.loudness_trims(segs)
        finally:
            render.measure_loudness, render.load_audio = real_measure, real_load
        self.assertEqual(len(calls), 2)

    def test_nothing_measurable_yields_no_trims(self):
        real_measure, real_load = render.measure_loudness, render.load_audio
        render.load_audio = lambda p, rate=render.TARGET_RATE: str(p)
        render.measure_loudness = lambda audio, rate=render.TARGET_RATE: None
        try:
            self.assertEqual(render.loudness_trims(self.segments(["a.mp3"])), {})
        finally:
            render.measure_loudness, render.load_audio = real_measure, real_load


class RenderIntegrationTest(unittest.TestCase):
    """render() end to end on synthetic audio.

    Worth having because the unit tests above pass whether or not render()
    actually calls any of them. Twice now a patch to render() silently failed
    to apply - once leaving the loudness trims unused, which only surfaced as a
    NameError mid-render - and nothing in the suite noticed.
    """
    RATE = 44100

    def setUp(self):
        self.real_load = render.load_audio
        self.real_dur = render.load_duration_ms
        # Two tracks 30 s long, one much quieter than the other.
        t = np.arange(int(30 * self.RATE)) / self.RATE
        wave = np.sin(2 * np.pi * 220 * t).astype(np.float32)
        self.audio = {
            "loud.mp3": (wave * 0.5)[:, None] * np.ones((1, 2), np.float32),
            "quiet.mp3": (wave * 0.05)[:, None] * np.ones((1, 2), np.float32),
        }
        render.load_audio = lambda p, rate=render.TARGET_RATE: self.audio[Path(p).name].copy()
        render.load_duration_ms = lambda p: 30_000

    def tearDown(self):
        render.load_audio = self.real_load
        render.load_duration_ms = self.real_dur

    def transitions(self):
        t = Transition(index=0, snapshot="", from_track=Track(title="Loud", bpm=120),
                       to_track=Track(title="Quiet", bpm=120),
                       out_point_ms=20_000, in_point_ms=0, overlap_ms=4_000,
                       ingredients={"volume": {"raw": "Overlap", "value": "overlap",
                                               "off": False}})
        return [t]

    def entries(self):
        class E:
            def __init__(self, path):
                self.local_path = Path(path)
                self.offset_ms = 0

            def is_different_edit(self):
                return False
        return {"Loud": E("loud.mp3"), "Quiet": E("quiet.mp3")}

    def render(self, **kw):
        by = self.entries()
        return render.render(self.transitions(), lambda tr: by[tr.title], **kw)

    def test_it_produces_audio_of_the_expected_length(self):
        mix, report = self.render(match_loudness=False)
        # 24 s of Loud (out point + overlap) then Quiet's remaining 30 s.
        self.assertAlmostEqual(report.duration_ms, 50_000, delta=200)
        self.assertAlmostEqual(len(mix) / self.RATE, 50.0, delta=0.3)

    def test_loudness_matching_is_actually_applied(self):
        """The guard for the bug: trims computed but never used."""
        _, report = self.render()
        self.assertEqual(len(report.loudness_trims), 2)
        self.assertTrue(any(abs(v) > 1.0 for v in report.loudness_trims.values()))

    def test_matching_brings_the_two_tracks_closer_together(self):
        plain, _ = self.render(match_loudness=False)
        matched, _ = self.render()
        # Measure a solo second of each track, before and after.
        def solo(mix, at):
            return render.measure_loudness(mix[int(at * self.RATE):int((at + 3) * self.RATE)],
                                           self.RATE)
        gap_plain = abs(solo(plain, 5) - solo(plain, 40))
        gap_matched = abs(solo(matched, 5) - solo(matched, 40))
        self.assertLess(gap_matched, gap_plain)
        self.assertLess(gap_matched, 3.0)

    def test_it_does_not_clip(self):
        mix, _ = self.render()
        mix, _ = render.normalize(mix)
        self.assertLessEqual(float(np.abs(mix).max()), 0.9701)

    def test_the_overlap_carries_both_tracks(self):
        mix, _ = self.render(match_loudness=False)
        # Overlap sits at 20-24 s; both tracks sound there, so it is louder
        # than the stretch just after, where only the incoming one plays.
        during = float(np.abs(mix[int(21 * self.RATE):int(23 * self.RATE)]).mean())
        after = float(np.abs(mix[int(26 * self.RATE):int(28 * self.RATE)]).mean())
        self.assertGreater(during, after)


class FilterSweepReportTest(unittest.TestCase):
    """apply_filter_sweep has to say whether it actually did anything.

    It used to return the chunk unchanged when neutral and leave the caller to
    notice with `is not`, which never worked - `body[-n:]` builds a fresh slice
    object each time it is evaluated, so the check was always true and a run
    reported a filter sweep on all 26 transitions when only 16 had one set.
    """
    N = 4096

    def chunk(self):
        return np.zeros((self.N, 2), dtype=np.float32)

    def test_no_filter_set_returns_none(self):
        self.assertIsNone(render.apply_filter_sweep(np, self.chunk(), None, {}, "out"))

    def test_an_off_filter_returns_none(self):
        ing = {"filter": {"raw": "None", "value": None, "off": True}}
        self.assertIsNone(render.apply_filter_sweep(np, self.chunk(), None, ing, "out"))

    def test_a_named_sweep_returns_audio(self):
        ing = {"filter": {"raw": "High-pass filter out", "value": "high pass filter out",
                          "off": False}}
        out = render.apply_filter_sweep(np, self.chunk(), None, ing, "out")
        self.assertIsNotNone(out)
        self.assertEqual(len(out), self.N)

    def test_a_sweep_named_for_the_other_side_is_not_applied(self):
        ing = {"filter": {"raw": "High-pass filter in", "value": "high pass filter in",
                          "off": False}}
        self.assertIsNone(render.apply_filter_sweep(np, self.chunk(), None, ing, "out"))
        self.assertIsNotNone(render.apply_filter_sweep(np, self.chunk(), None, ing, "in"))

    def test_too_short_to_filter(self):
        tiny = np.zeros((8, 2), dtype=np.float32)
        ing = {"filter": {"value": "high pass filter out", "off": False}}
        self.assertIsNone(render.apply_filter_sweep(np, tiny, None, ing, "out"))
