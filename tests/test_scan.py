import re
import unittest

from phase1_discover import js_keyword_pattern
from spotimix.scan import (compile_keywords, endpoint_template, get_at, match_key, parent_path,
                           redact, scan_json, tokenize)

KW = compile_keywords(["transition", "crossfade", "fade", "start_ms", "eq", "mix", "filter"])


class ScanTest(unittest.TestCase):
    def test_tokenize(self):
        self.assertEqual(tokenize("crossfadeStartMs"), ("crossfade", "start", "ms"))
        self.assertEqual(tokenize("fade_in-ms"), ("fade", "in", "ms"))
        self.assertEqual(tokenize("EQLow"), ("eq", "low"))

    def test_match_key(self):
        self.assertIn("start_ms", match_key("startMs", KW))
        self.assertIn("transition", match_key("transitions", KW))
        self.assertIn("eq", match_key("eqLow", KW))
        self.assertEqual(match_key("sequence", KW), [])
        self.assertEqual(match_key("remixer", KW), [])

    def test_scan_and_paths(self):
        doc = {"data": {"items": [{"transition": {"fadeMs": 8000, "filter": "hp"}}], "accessToken": "Bearer abc"}}
        hits = scan_json(doc, KW)
        paths = {h.path for h in hits}
        self.assertIn("$.data.items[0].transition", paths)
        self.assertIn("$.data.items[0].transition.fadeMs", paths)
        self.assertEqual(get_at(doc, "$.data.items[0].transition.fadeMs"), 8000)
        self.assertEqual(parent_path("$.data.items[0].transition.fadeMs"), "$.data.items[0].transition")

    def test_redact(self):
        r = redact({"accessToken": "x", "list": list(range(10)), "nested": {"email": "a@b"}})
        self.assertEqual(r["accessToken"], "<redacted>")
        self.assertEqual(r["nested"]["email"], "<redacted>")
        self.assertEqual(len(r["list"]), 4)

    def test_endpoint_template(self):
        self.assertEqual(endpoint_template("https://spclient.wg.spotify.com/playlist/v2/playlist/37i9dQZF1DXcBWIGoYBM5M/diff?x=1"),
                         "spclient.wg.spotify.com/playlist/v2/playlist/{id}/diff")

    def test_js_pattern(self):
        rx = re.compile(js_keyword_pattern(["eq", "transition", "start_ms"]))
        self.assertTrue(rx.search("EQ"))
        self.assertTrue(rx.search("eqLow"))
        self.assertFalse(rx.search("sequence"))
        self.assertTrue(rx.search("TransitionEditor"))
        self.assertTrue(rx.search("start-ms"))


if __name__ == "__main__":
    unittest.main()
