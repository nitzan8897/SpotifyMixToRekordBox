import json
import unittest

from spotimix.guard import write_block_reason


class GuardTest(unittest.TestCase):
    def test_reads_pass(self):
        self.assertIsNone(write_block_reason("GET", "https://spclient.wg.spotify.com/playlist/v2/playlist/37i9dQZF1DXcBWIGoYBM5M", None))
        body = json.dumps({"operationName": "fetchPlaylist", "variables": {}})
        self.assertIsNone(write_block_reason("POST", "https://api-partner.spotify.com/pathfinder/v2/query", body))

    def test_graphql_mutations_blocked(self):
        for op in ("addToPlaylist", "removeFromPlaylist", "moveItemsInPlaylist", "editPlaylist", "setTransition"):
            body = json.dumps({"operationName": op})
            self.assertIsNotNone(write_block_reason("POST", "https://api-partner.spotify.com/pathfinder/v2/query", body), op)
        self.assertIsNotNone(write_block_reason(
            "GET", "https://api-partner.spotify.com/pathfinder/v1/query?operationName=updateMix&variables=%7B%7D", None))

    def test_playlist_changes_blocked(self):
        self.assertIsNotNone(write_block_reason(
            "POST", "https://spclient.wg.spotify.com/playlist/v2/playlist/37i9dQZF1DXcBWIGoYBM5M/changes", "x"))
        self.assertIsNotNone(write_block_reason("POST", "https://api.spotify.com/v1/playlists/abc/tracks", "{}"))
        self.assertIsNotNone(write_block_reason("PUT", "https://spclient.wg.spotify.com/mix-editor/v1/transitions/abc", "{}"))

    def test_login_and_other_hosts_untouched(self):
        self.assertIsNone(write_block_reason("POST", "https://accounts.spotify.com/login/password", "u=x"))
        self.assertIsNone(write_block_reason("POST", "https://example.com/v1/playlists/x", "{}"))


if __name__ == "__main__":
    unittest.main()
