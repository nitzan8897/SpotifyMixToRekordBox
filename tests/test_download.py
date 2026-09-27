"""Choosing which YouTube upload is really the Spotify recording."""
import unittest

import phase0_download as p0


class TitleScoreTest(unittest.TestCase):
    """Scoring a search result's title against the track.

    Every case here is a real failure from this project's own downloads: the
    wrong version arrived, the cue positions then landed on the wrong bar, and
    the mix sounded out of time.
    """
    TRACK = {"title": "Ela ké Leitada",
             "artists": ["Prod.Nifour", "Mc Gw", "CACAU CHUU", "Storys Funk"],
             "duration_ms": 156708}

    def score(self, title, track=None):
        return p0._title_score(track or self.TRACK, title)

    def test_the_real_thing_scores_well(self):
        self.assertGreater(
            self.score("ELA KÉ LEITADA - Prod.Nifour, MC GW, Cacau Chuu"), 0.8)

    def test_accents_and_case_do_not_matter(self):
        plain = self.score("ela ke leitada prod nifour mc gw cacau chuu")
        fancy = self.score("ELA KÉ LEITADA - Prod.Nifour, MC GW, Cacau Chuu")
        self.assertAlmostEqual(plain, fancy, places=2)

    def test_a_slowed_rework_scores_below_the_original(self):
        original = self.score("ELA KÉ LEITADA - Prod.Nifour, MC GW, Cacau Chuu")
        slowed = self.score("Ela kè Leitada - Slowed X Naoya Zenin")
        self.assertLess(slowed, original)

    def test_rework_words_are_only_penalised_when_the_track_lacks_them(self):
        """A track genuinely called "SLOWED + REVERB" must not be penalised."""
        track = {"title": "Automotivo da Turbulencia - SLOWED + REVERB",
                 "artists": ["Mc Oliver"], "duration_ms": 168000}
        self.assertGreater(
            self.score("Automotivo da Turbulencia - SLOWED + REVERB - Mc Oliver", track),
            0.8)

    def test_a_different_song_by_the_same_artists_loses(self):
        """The near-miss that fuzzy matching alone gets wrong."""
        right = self.score("ELA KÉ LEITADA - Mc Gw, Cacau Chuu")
        wrong = self.score("ELA KÉ CAVUCADINHA - Mc Gw, Cacau Chuu")
        self.assertGreater(right, wrong)
        self.assertLess(wrong, 0.7)

    def test_a_tiktok_cut_loses(self):
        full = self.score("ELA KÉ LEITADA - Prod.Nifour, MC GW")
        cut = self.score("Ela Ké Leitada (tiktok version) - MC GW")
        self.assertLess(cut, full)

    def test_an_unrelated_title_scores_near_zero(self):
        self.assertLess(self.score("Lean Into The Sunshine - Sparrow & Barbossa"), 0.3)

    def test_empty_input_is_not_a_match(self):
        self.assertEqual(self.score(""), 0.0)
        self.assertEqual(p0._title_score({"title": None}, "anything"), 0.0)


class SourceChoiceTest(unittest.TestCase):
    """Weighing length against title, without going near the network."""

    TRACK = {"title": "Ela ké Leitada", "artists": ["Mc Gw"],
             "duration_ms": 156708, "spotify_id": "161dJFOgyuYcgKg0AzYuAh"}

    def search(self, entries):
        """Run find_source against a canned result list."""
        class FakeYDL:
            def __init__(self, opts):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def extract_info(self, query, download=False):
                return {"entries": entries}

        import sys
        import types
        fake = types.ModuleType("yt_dlp")
        fake.YoutubeDL = FakeYDL
        saved = sys.modules.get("yt_dlp")
        sys.modules["yt_dlp"] = fake
        try:
            return p0.find_source(self.TRACK)
        finally:
            if saved is None:
                sys.modules.pop("yt_dlp", None)
            else:
                sys.modules["yt_dlp"] = saved

    @staticmethod
    def entry(vid, title, seconds):
        return {"id": vid, "title": title, "duration": seconds}

    def test_the_closest_length_with_a_good_title_wins(self):
        got = self.search([
            self.entry("short", "ELA KÉ LEITADA - Mc Gw", 140),
            self.entry("right", "ELA KÉ LEITADA - Mc Gw", 157),
            self.entry("long", "ELA KÉ LEITADA - Mc Gw", 160),
        ])
        self.assertIsNotNone(got)
        self.assertIn("right", got[0])

    def test_a_right_length_on_the_wrong_song_does_not_win(self):
        """This is why length alone is not enough."""
        got = self.search([
            self.entry("wrongsong", "ELA KÉ CAVUCADINHA - Mc Gw", 157),
            self.entry("rightsong", "ELA KÉ LEITADA - Mc Gw", 159),
        ])
        self.assertIn("rightsong", got[0])

    def test_a_perfect_title_at_the_wrong_length_is_rejected(self):
        """A different arrangement is a different length, whatever it is called."""
        got = self.search([
            self.entry("slow", "ELA KÉ LEITADA - Prod.Nifour, Mc Gw", 210),
        ])
        self.assertIsNone(got)

    def test_nothing_usable_returns_none(self):
        self.assertIsNone(self.search([]))
        self.assertIsNone(self.search([self.entry("x", "Some Other Song", 157)]))

    def test_entries_without_a_duration_are_skipped(self):
        got = self.search([
            {"id": "nodur", "title": "ELA KÉ LEITADA - Mc Gw"},
            self.entry("ok", "ELA KÉ LEITADA - Mc Gw", 157),
        ])
        self.assertIn("ok", got[0])

    def test_a_track_with_no_spotify_duration_cannot_be_searched(self):
        saved = self.TRACK
        self.TRACK = dict(saved, duration_ms=None)
        try:
            self.assertIsNone(self.search([self.entry("x", "ELA KÉ LEITADA", 157)]))
        finally:
            self.TRACK = saved


if __name__ == "__main__":
    unittest.main()


class ModuleNamesTest(unittest.TestCase):
    """Names the download path reaches for at run time.

    A missing import here does not fail at import time - it fails deep inside
    a refetch, after the download has already succeeded, which is how
    AUDIO_SUFFIXES went unnoticed until a real repair crashed on it.
    """
    REQUIRED = ("AUDIO_SUFFIXES", "CONTENT_TOLERANCE_MS", "align", "fold",
                "shutil", "tempfile", "subprocess", "SequenceMatcher",
                "FORMAT", "BITRATE", "LENGTH_REJECT_MS", "SEARCH_RESULTS",
                "REWORK")

    def test_every_name_the_functions_use_is_present(self):
        for name in self.REQUIRED:
            self.assertTrue(hasattr(p0, name), f"phase0_download is missing {name}")

    def test_audio_suffixes_is_the_shared_set(self):
        from src.rekordbox import AUDIO_SUFFIXES
        self.assertIs(p0.AUDIO_SUFFIXES, AUDIO_SUFFIXES)
        self.assertIn(".mp3", p0.AUDIO_SUFFIXES)

    def test_fix_does_not_bulk_download(self):
        """--fix is a repair; re-downloading the good files risked undoing
        earlier verified replacements."""
        import inspect
        src = inspect.getsource(p0.main)
        self.assertIn("not args.check_only and not args.fix", src)
