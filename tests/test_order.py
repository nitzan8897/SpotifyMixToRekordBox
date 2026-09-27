"""Putting the snapshots into the order the mix actually plays them."""
import unittest
from src.transitions import (Track, Transition, dedupe, order_into_chain,
                            verify_against_cluster)

def mk(from_title, to_title, out=1000, inp=0, overlap=2510, idx=0):
    return Transition(index=idx, snapshot=f"{from_title}.html",
                      from_track=Track(title=from_title), to_track=Track(title=to_title),
                      out_point_ms=out, in_point_ms=inp, overlap_ms=overlap)


class ChainTest(unittest.TestCase):
    def test_orders_into_a_chain(self):
        shuffled = [mk("C", "D"), mk("A", "B"), mk("B", "C")]
        chain, dropped = order_into_chain(shuffled)
        self.assertEqual([t.from_track.title for t in chain], ["A", "B", "C"])
        self.assertEqual([t.index for t in chain], [0, 1, 2])
        self.assertEqual(dropped, [])

    def test_leaves_ambiguous_order_alone(self):
        """Nothing links to anything, so there is no reading to prefer."""
        given = [mk("A", "B"), mk("X", "Y")]
        chain, dropped = order_into_chain(given)
        self.assertEqual([t.from_track.title for t in chain], ["A", "X"])
        self.assertEqual(dropped, [])

    def test_drops_a_stale_snapshot_that_breaks_the_chain(self):
        """The real failure: the editor's panel was stale when 's' was pressed.

        Taken from run 20260926-123843, where a snapshot recorded
        Saquarema -> QUE LOUCURA, a pair the mix never plays. It made the old
        greedy walk dead-end, which silently fell back to capture order.
        """
        phantom = mk("Saquarema", "QUE LOUCURA", out=193750)
        real = [mk("Ela Ta Farmando", "QUE LOUCURA"), mk("QUE LOUCURA", "CALA BOCA"),
                mk("CALA BOCA", "Saquarema"), mk("Saquarema", "Party Funk")]
        chain, dropped = order_into_chain([phantom] + real)
        self.assertEqual([t.from_track.title for t in chain],
                         ["Ela Ta Farmando", "QUE LOUCURA", "CALA BOCA", "Saquarema"])
        self.assertEqual([t.to_track.title for t in dropped], ["QUE LOUCURA"])
        self.assertEqual([t.out_point_ms for t in dropped], [193750])
        self.assertEqual([t.index for t in chain], [0, 1, 2, 3])

    def test_backtracks_past_a_dead_end(self):
        """A wrong first choice at a fork must not cost the tail of the chain."""
        # From A there are two ways out; only one continues.
        given = [mk("A", "DEAD"), mk("A", "B"), mk("B", "C"), mk("C", "D")]
        chain, dropped = order_into_chain(given)
        self.assertEqual([t.from_track.title for t in chain], ["A", "B", "C"])
        self.assertEqual([t.to_track.title for t in dropped], ["DEAD"])

    def test_dedupe_keeps_last_of_a_pair(self):
        first, second = mk("A", "B", out=1000), mk("A", "B", out=2000)
        out = dedupe([first, second])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].out_point_ms, 2000)


class PageOrderTest(unittest.TestCase):
    """Ordering from the page's own chip positions, not inferred from names."""

    @staticmethod
    def at(pos, frm, to):
        t = mk(frm, to)
        t.order_index = pos
        return t

    def test_page_positions_set_the_order(self):
        given = [self.at(2, "C", "D"), self.at(0, "A", "B"), self.at(1, "B", "C")]
        chain, dropped = order_into_chain(given)
        self.assertEqual([t.from_track.title for t in chain], ["A", "B", "C"])
        self.assertEqual([t.index for t in chain], [0, 1, 2])
        self.assertEqual(dropped, [])

    def test_snapshot_with_no_open_chip_is_dropped(self):
        """The real phantom: a stale panel, recorded with no chip checked."""
        phantom = mk("Saquarema", "QUE LOUCURA", out=193750)      # order_index None
        given = [phantom, self.at(0, "A", "B"), self.at(1, "B", "C")]
        chain, dropped = order_into_chain(given)
        self.assertEqual([t.from_track.title for t in chain], ["A", "B"])
        self.assertEqual(dropped, [phantom])

    def test_same_position_twice_keeps_the_later_snapshot(self):
        first, second = self.at(0, "A", "B"), self.at(0, "A", "B")
        second.out_point_ms = 4242
        chain, dropped = order_into_chain([first, second, self.at(1, "B", "C")])
        self.assertEqual(len(chain), 2)
        self.assertEqual(chain[0].out_point_ms, 4242)
        self.assertEqual(dropped, [first])

    def test_falls_back_to_names_when_positions_contradict_them(self):
        """Page indices that do not form a handover are not trusted."""
        given = [self.at(0, "A", "B"), self.at(1, "X", "Y")]
        chain, _ = order_into_chain(given)
        self.assertEqual(len(chain), 2)      # kept, but via the name-based path

    def test_falls_back_to_names_when_no_positions_at_all(self):
        given = [mk("C", "D"), mk("A", "B"), mk("B", "C")]
        chain, dropped = order_into_chain(given)
        self.assertEqual([t.from_track.title for t in chain], ["A", "B", "C"])
        self.assertEqual(dropped, [])


class VerificationTest(unittest.TestCase):
    TRUTH = {"out_point_ms": 55630, "in_point_ms": 0, "overlap_ms": 2510}

    def test_agrees(self):
        ts = [mk("A", "B", out=55630, inp=0, overlap=2510)]
        r = verify_against_cluster(ts, self.TRUTH)
        self.assertTrue(r["checked"])
        self.assertTrue(r["agrees"])

    def test_disagrees_on_overlap(self):
        ts = [mk("A", "B", out=55630, inp=0, overlap=9999)]
        r = verify_against_cluster(ts, self.TRUTH)
        self.assertTrue(r["checked"])
        self.assertFalse(r["agrees"])

    def test_no_matching_snapshot(self):
        r = verify_against_cluster([mk("A", "B", out=1)], self.TRUTH)
        self.assertFalse(r["agrees"])

    def test_no_cluster_means_unchecked(self):
        r = verify_against_cluster([mk("A", "B")], {})
        self.assertFalse(r["checked"])


if __name__ == "__main__":
    unittest.main()


class DedupeKeepsPositionTest(unittest.TestCase):
    """A snapshot that knows its place must not be replaced by one that does not.

    From the real run: the play-through snapshots the page repeatedly with the
    editor closed, so they carry no chip position. The editor is left showing
    the last transition, so one of them matched that pair, "later wins"
    replaced the good snapshot, and the transition was then dropped for having
    no place in the running order.
    """
    @staticmethod
    def at(pos, frm, to, snapshot):
        t = mk(frm, to)
        t.order_index = pos
        t.snapshot = snapshot
        return t

    def test_a_positionless_later_snapshot_does_not_win(self):
        real = self.at(25, "CORACAO", "Free Your Mind", "auto_26.html")
        stray = self.at(None, "CORACAO", "Free Your Mind", "final.html")
        out = dedupe([real, stray])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].snapshot, "auto_26.html")
        self.assertEqual(out[0].order_index, 25)

    def test_later_still_wins_between_two_positioned_snapshots(self):
        first = self.at(3, "A", "B", "auto_04.html")
        second = self.at(3, "A", "B", "auto_04b.html")
        out = dedupe([first, second])
        self.assertEqual([t.snapshot for t in out], ["auto_04b.html"])

    def test_later_still_wins_when_neither_has_a_position(self):
        first = self.at(None, "A", "B", "one.html")
        second = self.at(None, "A", "B", "two.html")
        out = dedupe([first, second])
        self.assertEqual([t.snapshot for t in out], ["two.html"])

    def test_the_last_transition_survives_a_play_through(self):
        """End to end: 3 real transitions plus play-through noise on the last."""
        given = [self.at(0, "A", "B", "a1"), self.at(1, "B", "C", "a2"),
                 self.at(2, "C", "D", "a3"),
                 self.at(None, "C", "D", "playing_00"),
                 self.at(None, "C", "D", "final")]
        chain, dropped = order_into_chain(dedupe(given))
        self.assertEqual([t.from_track.title for t in chain], ["A", "B", "C"])
        self.assertEqual(dropped, [])
