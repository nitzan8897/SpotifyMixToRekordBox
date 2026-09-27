"""Reading one transition out of a DOM snapshot: ids, track sides, tempo."""
import unittest
from pathlib import Path

from src.transitions import (Track, Transition, gid_to_base62, match_track,
                            normalize, parse_side)

# URIs the player reported for the same two tracks.
REAL_IDS = [
    ("cd1a4240ab2f4ba4ae9ed8e3ae836205", "6f1quGoa3yMxvvhBWg5LF3"),
    ("225ee338abc440c8895189c946a37a0c", "12R6npsSMx8g0k5GN19poM"),
]

EDITOR_HTML = """
<div style="--view-transition-name-title: mixing-metadata-track-a-title;
            --view-transition-name-trailing: mixing-metadata-track-a-trailing;">
  <img src="spotify:image:ab67616d000011eb49248c0bd4b98457fce00a1a">
  <span>QUE LOUCURA</span><span>•</span><a>DJ EXE</a>,<a>CACAU CHUU</a>
  <span>‏‏BPM 170‏‏</span><span>12A</span>
</div>
<div style="--view-transition-name-trailing: mixing-metadata-track-b-trailing;">
  <span>Saquarema</span><span>•</span><a>MC Rogê</a>
  <span>BPM 150</span><span>11A</span>
</div>
"""

# The serializer can also emit numeric entities; the parser must survive both.
EDITOR_HTML_ENTITIES = (EDITOR_HTML.replace("•", "&#8226;")
                                   .replace("ê", "&#234;"))

class Base62Test(unittest.TestCase):
    def test_known_gids(self):
        for gid, expected in REAL_IDS:
            self.assertEqual(gid_to_base62(gid), expected)

    def test_case_is_not_swapped(self):
        """A swapped alphabet yields the right letters in the wrong case."""
        gid, expected = REAL_IDS[0]
        self.assertNotEqual(gid_to_base62(gid), expected.swapcase())

    def test_always_22_chars(self):
        self.assertEqual(len(gid_to_base62("0" * 32)), 22)
        self.assertEqual(len(gid_to_base62("f" * 32)), 22)

    def test_bad_input(self):
        self.assertIsNone(gid_to_base62("not-hex"))
        self.assertIsNone(gid_to_base62(None))


class ParseSideTest(unittest.TestCase):
    def test_reads_track_a(self):
        t = parse_side(EDITOR_HTML, "a")
        self.assertEqual(t.title, "QUE LOUCURA")
        self.assertEqual(t.artists, ["DJ EXE", "CACAU CHUU"])
        self.assertEqual(t.bpm, 170)
        self.assertEqual(t.camelot, "12A")
        self.assertEqual(t.image_id, "ab67616d000011eb49248c0bd4b98457fce00a1a")

    def test_reads_track_b(self):
        t = parse_side(EDITOR_HTML, "b")
        self.assertEqual(t.title, "Saquarema")
        self.assertEqual(t.artists, ["MC Rogê"])
        self.assertEqual(t.bpm, 150)
        self.assertEqual(t.camelot, "11A")

    def test_missing_side(self):
        self.assertIsNone(parse_side("<div>nothing here</div>", "a"))

    def test_html_entities_are_decoded(self):
        t = parse_side(EDITOR_HTML_ENTITIES, "b")
        self.assertEqual(t.title, "Saquarema")
        self.assertEqual(t.artists, ["MC Rogê"])


class NormalizeTest(unittest.TestCase):
    def test_folds_case_accents_punctuation(self):
        self.assertEqual(normalize("MC Rogê!"), normalize("mc roge"))
        self.assertEqual(normalize("Ela Tá  Farmando"), "ela ta farmando")

    def test_empty(self):
        self.assertEqual(normalize(None), "")


class MatchTrackTest(unittest.TestCase):
    CATALOG = [
        {"spotify_id": "aaa", "title": "Saquarema", "artists": ["MC Rogê"],
         "duration_ms": 201600, "isrc": "X", "image_suffixes": ["deadbeef"]},
        {"spotify_id": "bbb", "title": "Saquarema", "artists": ["Someone Else"],
         "duration_ms": 1000, "isrc": "Y", "image_suffixes": ["cafe"]},
    ]

    def test_artist_breaks_the_tie(self):
        hit = match_track(Track(title="Saquarema", artists=["MC Rogê"]), self.CATALOG)
        self.assertEqual(hit["spotify_id"], "aaa")

    def test_unresolvable_tie_returns_none(self):
        self.assertIsNone(match_track(Track(title="Saquarema", artists=["Nobody"]), self.CATALOG))

    def test_no_title_match(self):
        self.assertIsNone(match_track(Track(title="Not Here"), self.CATALOG))


class BpmLabelTest(unittest.TestCase):
    """The tempo label, which the editor writes either way round.

    A left-to-right build renders "BPM 170"; the right-to-left build in run
    20260926-194809 renders "170 BPM". Reading only one order loses every BPM,
    and a lost BPM silently loses the loop lengths too, since a loop's length
    is its beat count against the track's tempo.
    """
    TEMPLATE = """
    <div style="--view-transition-name-trailing: mixing-metadata-track-a-trailing;">
      <span>A Title</span><span>&#8226;</span><a>An Artist</a>
      <span>{bpm}</span><span>11A</span>
    </div>
    """

    def side(self, bpm_label):
        return parse_side(self.TEMPLATE.format(bpm=bpm_label), "a")

    def test_word_first(self):
        t = self.side("BPM 170")
        self.assertEqual(t.bpm, 170)
        self.assertEqual(t.artists, ["An Artist"])

    def test_number_first(self):
        t = self.side("170 BPM")
        self.assertEqual(t.bpm, 170)
        self.assertEqual(t.artists, ["An Artist"])

    def test_tempo_never_becomes_an_artist(self):
        """The bug this guards: "170 BPM" was ending up in the artist list."""
        for label in ("170 BPM", "BPM 170", "65 BPM"):
            t = self.side(label)
            self.assertNotIn(label, t.artists, label)
            self.assertTrue(all("BPM" not in a for a in t.artists), label)

    def test_bare_numbers_are_not_artists(self):
        t = parse_side(self.TEMPLATE.format(bpm="130"), "a")
        self.assertEqual(t.artists, ["An Artist"])

    def test_loop_length_needs_the_bpm(self):
        """Why the BPM matters downstream, not just cosmetically."""
        t = Transition(index=0, snapshot="", from_track=Track(title="A", bpm=170),
                       to_track=Track(title="B", bpm=170), overlap_ms=2824,
                       ingredients={"loop": {"raw": "8 beat loop", "value": "8 beat loop",
                                             "off": False, "beats": 8}})
        self.assertEqual(t.loop_length_ms(), 2824)
        t.from_track.bpm = None
        self.assertIsNone(t.loop_length_ms())


if __name__ == "__main__":
    unittest.main()
