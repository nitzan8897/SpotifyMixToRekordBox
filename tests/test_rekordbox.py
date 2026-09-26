"""Building the rekordbox collection, its cues and the XML around them."""
import unittest
import logging
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from src import align as align_mod
from src import rekordbox as rb
from src.transitions import Track, Transition

def mk(from_title, to_title, out=1000, inp=0, overlap=2510, idx=0):
    return Transition(index=idx, snapshot=f"{from_title}.html",
                      from_track=Track(title=from_title), to_track=Track(title=to_title),
                      out_point_ms=out, in_point_ms=inp, overlap_ms=overlap)


class RekordboxTest(unittest.TestCase):
    def test_camelot_maps_to_rekordbox_keys(self):
        self.assertEqual(rb.CAMELOT_TO_KEY["12A"], "Dbm")
        self.assertEqual(rb.CAMELOT_TO_KEY["8B"], "C")
        self.assertEqual(len(rb.CAMELOT_TO_KEY), 24)

    def test_location_uri_encodes_windows_path(self):
        uri = rb.location_uri(Path("D:/Music/a b.mp3"))
        self.assertTrue(uri.startswith("file://localhost/D:/Music/"))
        self.assertIn("a%20b.mp3", uri)

    def test_cues_named_and_placed(self):
        # overlap defaults to 2510ms in mk(), so each side also gets a loop cue
        # over the crossfade window alongside its point cue.
        ts = [mk("A", "B", out=90315, inp=299, idx=0)]
        entries = rb.build_entries(ts, [])
        a = next(e for e in entries if e.title == "A")
        b = next(e for e in entries if e.title == "B")
        def rounded(cues):
            return [(c.name, c.seconds, None if c.loop_end is None else round(c.loop_end, 6))
                    for c in cues]
        self.assertEqual(rounded(a.cues),
                         [("\u2192 01 out", 90.315, None), ("\u2192 01 loop", 90.315, 92.825)])
        self.assertEqual(rounded(b.cues),
                         [("01 in \u2192", 0.299, None), ("01 in loop", 0.299, 2.809)])

    def test_no_loop_cue_without_overlap(self):
        ts = [mk("A", "B", out=1000, inp=0, overlap=None)]
        entries = rb.build_entries(ts, [])
        a = next(e for e in entries if e.title == "A")
        self.assertEqual([c.name for c in a.cues], ["\u2192 01 out"])

    def test_repeated_track_gets_a_cue_per_appearance(self):
        # 2 cues (point + loop) per appearance; B appears as the incoming
        # track once and the outgoing track once, so 4 in total.
        ts = [mk("A", "B", idx=0), mk("B", "C", idx=1)]
        entries = rb.build_entries(ts, [])
        b = next(e for e in entries if e.title == "B")
        self.assertEqual(len(b.cues), 4)

    def test_playlist_order_appends_final_track(self):
        ts = [mk("A", "B", idx=0), mk("B", "C", idx=1)]
        entries = rb.build_entries(ts, [])
        order = rb.playlist_order(ts, entries)
        self.assertEqual([e.title for e in order], ["A", "B", "C"])

    def test_unmatched_track_points_at_missing_dir(self):
        entries = rb.build_entries([mk("A", "B")], [])
        self.assertIn(rb.MISSING_DIR, entries[0].location())

    def test_xml_structure(self):
        ts = [mk("A", "B", idx=0)]
        entries = rb.build_entries(ts, [])
        for e in entries:
            e.bpm, e.camelot, e.duration_ms = 170, "12A", 200000
        tree = rb.build_xml(entries, rb.playlist_order(ts, entries), "My Mix")
        root = tree.getroot()
        self.assertEqual(root.tag, "DJ_PLAYLISTS")
        col = root.find("COLLECTION")
        self.assertEqual(col.get("Entries"), "2")
        track = col.find("TRACK")
        self.assertEqual(track.get("AverageBpm"), "170.00")
        self.assertEqual(track.get("Comments", ""), "")  # no spotify id on this fixture
        self.assertEqual(track.get("Tonality"), "Dbm")
        self.assertEqual(track.get("TotalTime"), "200")
        node = root.find("PLAYLISTS").find("NODE").find("NODE")
        self.assertEqual(node.get("Name"), "My Mix")
        self.assertEqual(node.get("Entries"), "2")

    def test_no_beatgrid_unless_asked(self):
        """An imported grid overrides rekordbox's own analysis, so it is opt-in."""
        ts = [mk("A", "B", idx=0)]
        entries = rb.build_entries(ts, [])
        for e in entries:
            e.bpm = 170
        self.assertIsNone(rb.build_xml(entries, [], "x").getroot().find("COLLECTION/TRACK/TEMPO"))
        grid = rb.build_xml(entries, [], "x", write_grid=True).getroot().find(
            "COLLECTION/TRACK/TEMPO")
        self.assertIsNotNone(grid)
        self.assertEqual(grid.get("Bpm"), "170.00")

    def test_memory_cues_by_default_hot_cues_on_request(self):
        ts = [mk("A", "B", idx=0)]
        entries = rb.build_entries(ts, [])
        mem = rb.build_xml(entries, [], "x").getroot().find("COLLECTION/TRACK/POSITION_MARK")
        self.assertEqual(mem.get("Num"), "-1")
        hot = rb.build_xml(entries, [], "x", hot_cues=True).getroot().find(
            "COLLECTION/TRACK/POSITION_MARK")
        self.assertEqual(hot.get("Num"), "0")

    def setUp(self):
        # The stub files below are empty, so their duration is unreadable and
        # scan_music_dirs rightly says so. That warning is expected here.
        logging.getLogger("src.rekordbox").setLevel(logging.ERROR)

    def tearDown(self):
        logging.getLogger("src.rekordbox").setLevel(logging.NOTSET)

    def test_local_file_matching(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "MC Roge - Saquarema.mp3").write_bytes(b"")
            (root / "Something Unrelated.mp3").write_bytes(b"")
            files = rb.scan_music_dirs([root])
            hit, delta = rb.match_local_file("Saquarema", ["MC Rogê"], files)
            self.assertIsNotNone(hit)
            self.assertEqual(hit.name, "MC Roge - Saquarema.mp3")
            self.assertIsNone(delta)        # no readable duration on an empty file

    def test_local_file_no_false_positive(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "Totally Different Song.mp3").write_bytes(b"")
            files = rb.scan_music_dirs([root])
            hit, _ = rb.match_local_file("Saquarema", ["MC Rogê"], files)
            self.assertIsNone(hit)

    def test_duration_breaks_the_tie_between_two_edits(self):
        """The real failure: spotdl fetched the right song in the wrong edit."""
        files = [
            rb.LocalFile(path=Path("/m/Young Madz - Party Funk.mp3"),
                         stem_tokens=rb.tokens("Young Madz - Party Funk"), duration_ms=119352),
            rb.LocalFile(path=Path("/m/other/Young Madz - Party Funk.mp3"),
                         stem_tokens=rb.tokens("Young Madz - Party Funk"), duration_ms=132990),
        ]
        hit, delta = rb.match_local_file("Party Funk", ["Young Madz"], files, duration_ms=132990)
        self.assertEqual(str(hit).replace("\\", "/"), "/m/other/Young Madz - Party Funk.mp3")
        self.assertEqual(delta, 0)

    def test_reports_delta_when_only_a_wrong_edit_exists(self):
        files = [rb.LocalFile(path=Path("/m/Party Funk.mp3"),
                              stem_tokens=rb.tokens("Party Funk"), duration_ms=119352)]
        hit, delta = rb.match_local_file("Party Funk", [], files, duration_ms=132990)
        self.assertIsNotNone(hit)           # still matched, but flagged
        self.assertEqual(delta, -13638)

    def test_entry_flags_a_different_edit(self):
        e = rb.Entry(track_id=1, title="Party Funk", artists=[], bpm=None, camelot=None,
                     duration_ms=132990, spotify_id=None, local_path=Path("/m/x.mp3"),
                     duration_delta_ms=-13638)
        self.assertTrue(e.is_different_edit())
        e.duration_delta_ms = -400
        self.assertFalse(e.is_different_edit())
        e.duration_delta_ms = None
        self.assertFalse(e.is_different_edit())

    def test_alignment_offset_shifts_every_cue(self):
        """A file that starts with silence gets its cues moved onto the music."""
        ts = [mk("A", "B", out=90315, inp=299, overlap=None, idx=0)]
        entries = rb.build_entries(ts, [], auto_align=False)
        a = next(e for e in entries if e.title == "A")
        a.alignment = align_mod.Alignment(offset_ms=2000, same_recording=True)
        tree = rb.build_xml(entries, rb.playlist_order(ts, entries), "M")
        track = next(t for t in tree.getroot().find("COLLECTION") if t.get("Name") == "A")
        marks = {m.get("Name"): m.get("Start") for m in track.findall("POSITION_MARK")}
        self.assertEqual(marks["\u2192 01 out"], "92.315")   # 90.315 + 2.000
        self.assertEqual(marks["spotify 0:00"], "2.000")     # marker, not doubled

if __name__ == "__main__":
    unittest.main()
