import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def make_run(tmp: Path, with_transitions: bool) -> Path:
    run = tmp / "output" / "discovery" / "20260101-000000"
    (run / "bodies").mkdir(parents=True)
    (run / "dom").mkdir()
    playlist = {"data": {"playlistV2": {"name": "My Mix", "content": {"items": [
        {"itemV2": {"data": {"uri": "spotify:track:aaaaaaaaaaaaaaaaaaaaaa", "name": "Song (Extended Mix)"}}}]}}}}
    if with_transitions:
        playlist["data"]["playlistV2"]["transitions"] = [{
            "fromUri": "spotify:track:aaaaaaaaaaaaaaaaaaaaaa", "toUri": "spotify:track:bbbbbbbbbbbbbbbbbbbbbb",
            "fadeOutStartMs": 201000, "fadeInStartMs": 12000, "durationMs": 8000, "curve": "linear",
            "eq": {"low": -12, "mid": 0, "high": 0}, "accessToken": "secret"}]
    (run / "bodies" / "00001_fetchPlaylist.json").write_text(json.dumps(playlist))
    rows = [{"seq": 1, "method": "POST", "status": 200, "endpoint": "api-partner.spotify.com/pathfinder/v2/query",
             "url": "https://api-partner.spotify.com/pathfinder/v2/query", "content_type": "application/json",
             "graphql_operation": "fetchPlaylist", "body_file": "bodies/00001_fetchPlaylist.json"}]
    (run / "index.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    (run / "dom" / "000000_snap.json").write_text(json.dumps([{"matches": [], "sliders": [], "candidates": []}]))
    cfg = (ROOT / "config.yaml").read_text().replace("REPLACE_WITH_YOUR_PLAYLIST_ID", "37i9dQZF1DXcBWIGoYBM5M")
    (tmp / "config.yaml").write_text(cfg)
    return run


class AnalyzeTest(unittest.TestCase):
    def run_analyze(self, with_transitions):
        tmp = Path(tempfile.mkdtemp())
        run = make_run(tmp, with_transitions)
        proc = subprocess.run([sys.executable, str(ROOT / "phase1_analyze.py"), "--config", str(tmp / "config.yaml")],
                              cwd=ROOT, capture_output=True, text=True)
        return proc, run

    def test_found(self):
        proc, run = self.run_analyze(True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        report = (run / "report.md").read_text()
        self.assertIn("Transition-like data found", report)
        self.assertIn("fadeOutStartMs", report)
        self.assertNotIn("secret", report)
        result = json.loads((run / "report.json").read_text())
        cov = {c["parameter"]: c["network_candidates"] for c in result["coverage"]}
        self.assertTrue(cov["transition length"])
        self.assertTrue(cov["EQ (low/mid/high)"])
        self.assertTrue(cov["from/to track identity"])

    def test_not_found(self):
        proc, run = self.run_analyze(False)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("No transition data observed", (run / "report.md").read_text())


if __name__ == "__main__":
    unittest.main()
