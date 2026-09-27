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
        self.assertAlmostEqual(self.at(inc, "high", 0.25), cut)
        self.assertAlmostEqual(self.at(inc, "high", 0.75), 1.0)

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
    N = 500

    def moves(self):
        return render.eq_envelopes(np, self.N, None)

    def test_high_pass_out_sweeps_the_low_end_away(self):
        out, inc = self.moves()
        render.apply_filter_setting(np, self.N, "high pass filter out", out, inc)
        self.assertAlmostEqual(float(out.low[0]), 1.0)
        self.assertAlmostEqual(float(out.low[-1]), 0.0)

    def test_low_pass_in_sweeps_the_top_back_in(self):
        out, inc = self.moves()
        render.apply_filter_setting(np, self.N, "low pass filter in", out, inc)
        self.assertAlmostEqual(float(inc.high[0]), 0.0)
        self.assertAlmostEqual(float(inc.high[-1]), 1.0)

    def test_a_combined_setting_touches_both_sides(self):
        out, inc = self.moves()
        render.apply_filter_setting(
            np, self.N, "low pass filter in + high pass filter out", out, inc)
        self.assertAlmostEqual(float(inc.high[0]), 0.0)
        self.assertAlmostEqual(float(out.low[-1]), 0.0)

    def test_no_filter_changes_nothing(self):
        out, inc = self.moves()
        render.apply_filter_setting(np, self.N, None, out, inc)
        self.assertTrue(np.allclose(out.low, 1.0))
        self.assertTrue(np.allclose(inc.high, 1.0))


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
