"""Lining a downloaded file up with Spotify's timeline."""
import unittest
from pathlib import Path

from src import align as align_mod

class AlignTest(unittest.TestCase):
    """Deciding whether a download needs shifting onto Spotify's timeline."""

    def probe(self, duration, lead, tail):
        return align_mod.Probe(duration_ms=duration, lead_in_ms=lead, tail_ms=tail)

    def test_content_length_excludes_both_silences(self):
        self.assertEqual(self.probe(10000, 500, 1500).content_ms, 8000)

    def _align(self, monkey_probe, spotify_ms):
        real = align_mod.probe
        align_mod.probe = lambda *a, **k: monkey_probe
        try:
            return align_mod.align(Path("x.mp3"), spotify_ms)
        finally:
            align_mod.probe = real

    def test_leading_silence_becomes_the_offset(self):
        """Ela Ke Cavucadinha: 2028 ms longer, 2060 ms of it silence up front."""
        a = self._align(self.probe(140980, 2060, 1900), 138996)
        self.assertTrue(a.same_recording)
        self.assertEqual(a.offset_ms, 1984)      # capped at the length difference
        self.assertIn("0:00 sits at", a.reason)

    def test_extra_length_at_the_end_is_not_shifted(self):
        a = self._align(self.probe(150000, 0, 5000), 148000)
        self.assertTrue(a.same_recording)
        self.assertEqual(a.offset_ms, 0)
        self.assertIn("at the end", a.reason)

    def test_same_length_needs_no_shift(self):
        a = self._align(self.probe(129115, 660, 1420), 129141)
        self.assertEqual(a.offset_ms, 0)
        self.assertTrue(a.same_recording)

    def test_more_music_than_spotify_has_room_for_is_a_different_edit(self):
        """EU BEM QUE TE AVISEI: 9.6 s more music than Spotify's whole track."""
        a = self._align(self.probe(168763, 20, 360), 158769)
        self.assertFalse(a.same_recording)
        self.assertEqual(a.offset_ms, 0)
        self.assertIn("different edit", a.reason)

    def test_undecodable_file_is_reported_not_guessed(self):
        a = self._align(None, 100000)
        self.assertEqual(a.offset_ms, 0)
        self.assertFalse(a.same_recording)
        self.assertIsNone(a.delta_ms)


if __name__ == "__main__":
    unittest.main()
