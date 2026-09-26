"""The editor's five transition ingredients, and the mode chip."""
import unittest
from src import ingredients

class IngredientsTest(unittest.TestCase):
    """The five ingredient slots, as the editor renders them.

    The HTML shape and every option string here are taken verbatim from run
    20260926-123843, whose editor was in Hebrew.
    """
    @staticmethod
    def panel(*pairs):
        buttons = "".join(
            f'<button><span>{name}</span><span>{value}</span></button>' for name, value in pairs)
        return f'<div data-curve-editing-ingredient-controls="">{buttons}</div>'

    HE = ("עוצמת השמע", "EQ", "מסנן", "אפקטים", "בלופ")

    def test_reads_all_five_slots(self):
        html = self.panel(
            ("עוצמת השמע", "קרוספייד חלק"),
            ("EQ", "החלפת בס באמצע"),
            ("מסנן", "פילטר מעביר תדרים גבוהים אין פילטר מעביר תדרים גבוהים אאוט"),
            ("אפקטים", "אקו ½ אאוט בסוף"),
            ("בלופ", "לופ של 8 ביטים"))
        ing = ingredients.parse_ingredients(html)
        self.assertEqual(ing["volume"]["value"], "smooth crossfade")
        self.assertEqual(ing["eq"]["value"], "centre bass swap")
        self.assertEqual(ing["filter"]["value"],
                         "high pass filter in + high pass filter out")
        self.assertEqual(ing["effects"]["value"], "echo 1/2 out end")
        self.assertEqual(ing["loop"]["value"], "8 beat loop")
        self.assertEqual(ing["loop"]["beats"], 8)
        self.assertEqual(ingredients.unmapped(ing), [])

    def test_no_option_is_off_not_unknown(self):
        """"No option" must read as switched off, not as a parse failure."""
        ing = ingredients.parse_ingredients(
            self.panel(("מסנן", "אף אפשרות"), ("בלופ", "אף אפשרות")))
        self.assertTrue(ing["filter"]["off"])
        self.assertIsNone(ing["filter"]["value"])
        self.assertEqual(ingredients.unmapped(ing), [])
        self.assertNotIn("filter", ingredients.summarize(ing))

    def test_every_volume_option_seen_in_the_capture(self):
        cases = {
            "קרוספייד חלק": "smooth crossfade",
            "קרוספייד": "crossfade",
            "פייד אין פייד אאוט": "fade in fade out",
            "פייד אין קאט אאוט": "fade in cut out",
            "חפיפה": "overlap",
            "Unknown": "custom",
        }
        for raw, want in cases.items():
            ing = ingredients.parse_ingredients(self.panel(("עוצמת השמע", raw)))
            self.assertEqual(ing["volume"]["value"], want, raw)

    def test_english_ui_is_the_reference_wording(self):
        """Verbatim strings from the English editor in run 20260926-194809."""
        ing = ingredients.parse_ingredients(self.panel(
            ("Volume", "Smooth crossfade"),
            ("EQ", "Centre bass swap"),
            ("Filter", "Low-pass filter in low-pass filter out"),
            ("Effects", "Reverb cut end"),
            ("Looping", "8-beat loop")))
        self.assertEqual(ing["volume"]["value"], "smooth crossfade")
        self.assertEqual(ing["eq"]["value"], "centre bass swap")
        self.assertEqual(ing["filter"]["value"],
                         "low pass filter in + low pass filter out")
        self.assertEqual(ing["effects"]["value"], "reverb cut end")
        self.assertEqual(ing["loop"]["beats"], 8)
        self.assertEqual(ingredients.unmapped(ing), [])

    def test_slot_is_called_looping_in_english(self):
        """It is "Looping", not "Loop" - reading only "Loop" lost every loop."""
        ing = ingredients.parse_ingredients(self.panel(("Looping", "2-beat loop")))
        self.assertEqual(ing["loop"]["beats"], 2)

    def test_hyphens_do_not_block_a_match(self):
        for raw in ("3-band fade", "3 band fade"):
            ing = ingredients.parse_ingredients(self.panel(("EQ", raw)))
            self.assertEqual(ing["eq"]["value"], "3 band fade", raw)

    def test_english_none_switches_a_slot_off(self):
        ing = ingredients.parse_ingredients(self.panel(("Effects", "None")))
        self.assertTrue(ing["effects"]["off"])
        self.assertEqual(ingredients.unmapped(ing), [])

    def test_beat_count_after_the_word(self):
        """Hebrew puts the number last: "loop of beat 1"."""
        ing = ingredients.parse_ingredients(self.panel(("בלופ", "לופ של ביט 1")))
        self.assertEqual(ing["loop"]["beats"], 1)

    def test_unknown_option_is_kept_not_dropped(self):
        ing = ingredients.parse_ingredients(self.panel(("EQ", "some future preset")))
        self.assertEqual(ing["eq"]["raw"], "some future preset")
        self.assertIsNone(ing["eq"]["value"])
        self.assertEqual(ingredients.unmapped(ing), [("eq", "some future preset")])

    def test_no_panel_gives_empty_slots(self):
        ing = ingredients.parse_ingredients("<div>nothing</div>")
        self.assertEqual(set(ing), set(ingredients.SLOTS))
        self.assertTrue(all(v["raw"] is None for v in ing.values()))

    def test_loop_length_from_beats_and_bpm(self):
        # 8 beats at 170 BPM is the 2824 ms overlap the editor showed.
        self.assertEqual(ingredients.loop_ms(8, 170), 2824)
        self.assertEqual(ingredients.loop_ms(2, 118), 1017)
        self.assertIsNone(ingredients.loop_ms(None, 170))
        self.assertIsNone(ingredients.loop_ms(8, None))


class ChipModeTest(unittest.TestCase):
    """The chip above each track: its mode, and which editor is open.

    The attribute shapes are taken verbatim from run 20260926-123843. The
    transition strips' chips carry aria-pressed; the chips inside the editor
    (the overlap length, "2 bars") carry aria-label instead and must not be
    counted, or the indices stop matching the running order.
    """
    STRIP = ('<button class="c" role="checkbox" aria-checked="{checked}" '
             'data-encore-id="chip" aria-pressed="{checked}" aria-disabled="false">'
             '<span>{text}</span></button>')
    EDITOR_CHIP = ('<button class="c" role="checkbox" aria-checked="false" '
                   'data-encore-id="chip" aria-label="2 bars"><span>2 bars</span></button>')

    def page(self, labels, open_at=None):
        html = "".join(
            self.STRIP.format(checked="true" if i == open_at else "false", text=t)
            for i, t in enumerate(labels))
        return html + self.EDITOR_CHIP        # the editor's own chip, always present

    def test_finds_the_open_chip_and_its_position(self):
        m = ingredients.parse_mode(self.page(["בהתאמה אישית"] * 5, open_at=3))
        self.assertEqual(m["index"], 3)
        self.assertEqual(m["value"], "custom")
        self.assertEqual(m["chips"], 5)       # the editor's "2 bars" chip excluded

    def test_automatic_mode(self):
        m = ingredients.parse_mode(self.page(["אוטומטי", "בהתאמה אישית"], open_at=0))
        self.assertEqual(m["value"], "automatic")

    def test_english_labels(self):
        m = ingredients.parse_mode(self.page(["Custom", "Automatic"], open_at=1))
        self.assertEqual(m["value"], "automatic")
        self.assertEqual(m["index"], 1)

    def test_no_chip_open_means_a_stale_panel(self):
        """The phantom snapshot in the real run had no chip checked."""
        m = ingredients.parse_mode(self.page(["בהתאמה אישית"] * 4, open_at=None))
        self.assertIsNone(m["index"])
        self.assertIsNone(m["value"])
        self.assertEqual(m["chips"], 4)

    def test_chip_count_is_how_many_transitions_the_mix_has(self):
        """Used as the completeness check: captured N of N chips means done."""
        m = ingredients.parse_mode(self.page(["Custom"] * 24, open_at=7))
        self.assertEqual(m["chips"], 24)

    def test_editor_chip_alone_is_not_a_transition(self):
        m = ingredients.parse_mode(self.EDITOR_CHIP)
        self.assertEqual(m["chips"], 0)
        self.assertIsNone(m["index"])


if __name__ == "__main__":
    unittest.main()
