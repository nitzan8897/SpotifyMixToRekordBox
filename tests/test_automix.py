"""Decoding the volume and EQ automation the player reports."""
import unittest
import json

from src import automix

class AutomixTest(unittest.TestCase):
    """The volume/EQ automation Spotify reports for a transition.

    Values are the ones the player sent for CALA BOCA PXTA -> Saquarema in
    run 20260926-123843.
    """
    OUT = {
        "audio.fade_out_start_time": 55630,
        "audio.fade_out_duration": 2510,
        "audio.fade_out_curves": json.dumps([
            {"start_point": 0, "end_point": 0.5, "fade_curve": [{"x": 0, "y": 1}, {"x": 1, "y": 1}]},
            {"start_point": 0.5, "end_point": 0.50195,
             "fade_curve": [{"x": 0, "y": 1}, {"x": 1, "y": 0}]},
            {"start_point": 0.50195, "end_point": 1,
             "fade_curve": [{"x": 0, "y": 0}, {"x": 1, "y": 0}]}]),
        "audio.fade_out_eq_low_gain_curves": json.dumps([
            {"start_point": 0, "end_point": 0.46875,
             "fade_curve": [{"x": 0, "y": 0.5}, {"x": 1, "y": 0.5}]},
            {"start_point": 0.46875, "end_point": 0.5,
             "fade_curve": [{"x": 0, "y": 0.5}, {"x": 0.5, "y": 0.5},
                            {"x": 0.5, "y": 0}, {"x": 1, "y": 0}]},
            {"start_point": 0.5, "end_point": 1,
             "fade_curve": [{"x": 0, "y": 0}, {"x": 1, "y": 0}]}]),
    }
    IN = {
        "audio.fade_in_start_time": 0,
        "audio.fade_in_duration": 2510,
        "audio.fade_overlap": 2510,
        "automix.mode": "auto",
        "audio.fade_in_curves": json.dumps([
            {"start_point": 0, "end_point": 0.5, "fade_curve": [{"x": 0, "y": 0}, {"x": 1, "y": 1}]},
            {"start_point": 0.5, "end_point": 1,
             "fade_curve": [{"x": 0, "y": 1}, {"x": 1, "y": 1}]}]),
        "audio.fade_in_eq_low_gain_curves": json.dumps([
            {"start_point": 0, "end_point": 0.46875,
             "fade_curve": [{"x": 0, "y": 0}, {"x": 1, "y": 0}]},
            {"start_point": 0.46875, "end_point": 0.5,
             "fade_curve": [{"x": 0, "y": 0}, {"x": 0.5, "y": 0},
                            {"x": 0.5, "y": 0.5}, {"x": 1, "y": 0.5}]},
            {"start_point": 0.5, "end_point": 1,
             "fade_curve": [{"x": 0, "y": 0.5}, {"x": 1, "y": 0.5}]}]),
    }

    def setUp(self):
        self.a = automix.parse_automation(self.OUT, self.IN)

    def test_reads_the_headline_numbers(self):
        self.assertEqual(self.a.overlap_ms, 2510)
        self.assertEqual(self.a.mode, "auto")
        self.assertEqual(self.a.outgoing.start_time_ms, 55630)
        self.assertEqual(self.a.incoming.start_time_ms, 0)

    def test_incoming_volume_ramps_over_the_first_half(self):
        v = self.a.incoming.volume
        self.assertAlmostEqual(automix.value_at(v, 0.0), 0.0)
        self.assertAlmostEqual(automix.value_at(v, 0.25), 0.5)
        self.assertAlmostEqual(automix.value_at(v, 0.5), 1.0)
        self.assertAlmostEqual(automix.value_at(v, 1.0), 1.0)

    def test_outgoing_volume_holds_then_cuts(self):
        v = self.a.outgoing.volume
        self.assertAlmostEqual(automix.value_at(v, 0.25), 1.0)
        self.assertAlmostEqual(automix.value_at(v, 0.5), 1.0)
        self.assertAlmostEqual(automix.value_at(v, 0.6), 0.0)

    def test_bass_is_swapped_not_blended(self):
        """Both lows step at the same instant: a swap, not a crossfade."""
        lo_out = self.a.outgoing.eq["low"]
        lo_in = self.a.incoming.eq["low"]
        self.assertAlmostEqual(automix.value_at(lo_out, 0.2), 0.5)   # outgoing at unity
        self.assertAlmostEqual(automix.value_at(lo_in, 0.2), 0.0)    # incoming bass killed
        self.assertAlmostEqual(automix.value_at(lo_out, 0.8), 0.0)   # and afterwards
        self.assertAlmostEqual(automix.value_at(lo_in, 0.8), 0.5)

    def test_swap_point_is_the_midpoint(self):
        self.assertAlmostEqual(self.a.swap_point(), 0.484375)
        self.assertEqual(self.a.swap_ms(), 1216)

    def test_describe_mentions_the_swap(self):
        text = "\n".join(automix.describe(self.a))
        self.assertIn("Bass swap", text)
        self.assertIn("2510", text)

    def test_missing_curves_do_not_crash(self):
        a = automix.parse_automation({}, {})
        self.assertIsNone(a.overlap_ms)
        self.assertIsNone(a.swap_point())
        self.assertEqual(a.incoming.sample(0.5)["volume"], None)
        automix.describe(a)         # must not raise


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
