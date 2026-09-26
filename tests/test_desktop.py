import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import desktop
from src.config import ConfigError, load_config

CONFIG_TEMPLATE = """
playlist_url: "https://open.spotify.com/playlist/0jkL0VgonmSBWLS4PwONrx"
browser:
{browser}
discovery:
  keywords: [transition]
"""


def write_config(tmp: Path, browser_lines: str) -> Path:
    p = tmp / "config.yaml"
    p.write_text(CONFIG_TEMPLATE.format(browser=browser_lines), encoding="utf-8")
    return p


class ConfigTargetTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_defaults_to_web(self):
        cfg = load_config(write_config(self.tmp, "  slow_mo_ms: 10"))
        self.assertEqual(cfg.browser_target, "web")
        self.assertFalse(cfg.is_desktop)
        self.assertEqual(cfg.cdp_port, 9222)
        self.assertIsNone(cfg.spotify_exe)

    def test_desktop_target(self):
        cfg = load_config(write_config(
            self.tmp, '  target: Desktop\n  cdp_port: 9333\n  spotify_exe: "C:/x/Spotify.exe"'))
        self.assertEqual(cfg.browser_target, "desktop")
        self.assertTrue(cfg.is_desktop)
        self.assertEqual(cfg.cdp_port, 9333)
        self.assertEqual(cfg.spotify_exe, "C:/x/Spotify.exe")

    def test_unknown_target_rejected(self):
        with self.assertRaises(ConfigError):
            load_config(write_config(self.tmp, "  target: phone"))

    def test_bad_port_rejected(self):
        for bad in ("  cdp_port: nope", "  cdp_port: 0", "  cdp_port: 99999"):
            with self.assertRaises(ConfigError, msg=bad):
                load_config(write_config(self.tmp, "  target: desktop\n" + bad))


class FindExeTest(unittest.TestCase):
    def test_explicit_path_used(self):
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d) / "Spotify.exe"
            exe.write_text("", encoding="utf-8")
            self.assertEqual(desktop.find_spotify_exe(str(exe)), exe)

    def test_explicit_missing_path_raises(self):
        with self.assertRaises(desktop.DesktopError):
            desktop.find_spotify_exe("/definitely/not/here/Spotify.exe")

    def test_no_install_raises_with_search_list(self):
        with mock.patch.object(desktop, "_windows_candidates", return_value=[Path("/nope/Spotify.exe")]), \
             mock.patch.object(desktop, "_mac_candidates", return_value=[Path("/nope/Spotify")]), \
             mock.patch.object(desktop, "_linux_candidates", return_value=[Path("/nope/spotify")]):
            with self.assertRaises(desktop.DesktopError) as ctx:
                desktop.find_spotify_exe()
        self.assertIn("nope", str(ctx.exception))


class ProcessListingTest(unittest.TestCase):
    def test_parses_tasklist_csv(self):
        out = ('"Spotify.exe","904","Console","1","120 K"\n'
               '"Spotify.exe","12720","Console","1","98 K"\n')
        with mock.patch.object(desktop.platform, "system", return_value="Windows"), \
             mock.patch.object(desktop.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, stdout=out)):
            self.assertEqual(desktop.running_spotify_pids(), [904, 12720])

    def test_no_processes_when_tasklist_says_none(self):
        out = "INFO: No tasks are running which match the specified criteria.\n"
        with mock.patch.object(desktop.platform, "system", return_value="Windows"), \
             mock.patch.object(desktop.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, stdout=out)):
            self.assertEqual(desktop.running_spotify_pids(), [])

    def test_listing_failure_is_not_fatal(self):
        with mock.patch.object(desktop.subprocess, "run", side_effect=OSError("boom")):
            self.assertEqual(desktop.running_spotify_pids(), [])


class PreflightTest(unittest.TestCase):
    """The flag is only read on a cold start, so a warm Spotify must be refused."""

    def test_attach_without_listener_raises(self):
        with mock.patch.object(desktop, "cdp_version", return_value=None):
            with self.assertRaises(desktop.DesktopError) as ctx:
                desktop.start_and_attach_preflight(None, 9222, close_running=False, attach_only=True)
        self.assertIn("--remote-debugging-port", str(ctx.exception))

    def test_running_spotify_without_debugging_refused(self):
        with mock.patch.object(desktop, "cdp_version", return_value=None), \
             mock.patch.object(desktop, "running_spotify_pids", return_value=[1, 2]), \
             mock.patch.object(desktop, "launch") as launched:
            with self.assertRaises(desktop.DesktopError) as ctx:
                desktop.start_and_attach_preflight(None, 9222, close_running=False, attach_only=False)
        launched.assert_not_called()
        self.assertIn("--close-spotify", str(ctx.exception))

    def test_close_spotify_then_launches(self):
        with mock.patch.object(desktop, "cdp_version", return_value=None), \
             mock.patch.object(desktop, "running_spotify_pids", return_value=[1]), \
             mock.patch.object(desktop, "close_spotify") as closed, \
             mock.patch.object(desktop, "find_spotify_exe", return_value=Path("Spotify.exe")), \
             mock.patch.object(desktop, "launch") as launched, \
             mock.patch.object(desktop, "wait_for_cdp", return_value={}), \
             mock.patch.object(desktop, "wait_for_app_page", return_value={}):
            desktop.start_and_attach_preflight(None, 9222, close_running=True, attach_only=False)
        closed.assert_called_once()
        launched.assert_called_once()

    def test_existing_listener_is_reused(self):
        with mock.patch.object(desktop, "cdp_version", return_value={"Browser": "Chrome/146"}), \
             mock.patch.object(desktop, "wait_for_app_page", return_value={}) as waited, \
             mock.patch.object(desktop, "launch") as launched:
            desktop.start_and_attach_preflight(None, 9222, close_running=False, attach_only=False)
        launched.assert_not_called()
        waited.assert_called_once()


class LaunchFlagsTest(unittest.TestCase):
    def test_port_is_substituted(self):
        flags = [f.format(port=9333) for f in desktop.LAUNCH_FLAGS]
        self.assertIn("--remote-debugging-port=9333", flags)
        self.assertIn("--remote-allow-origins=*", flags)


class AppPageTest(unittest.TestCase):
    def test_prefers_xpui_page(self):
        other = mock.Mock(url="https://accounts.spotify.com/login")
        app = mock.Mock(url="https://xpui.app.spotify.com/index.html")
        ctx = mock.Mock(pages=[other, app])
        self.assertIs(desktop.pick_app_page(ctx), app)

    def test_none_when_no_pages(self):
        self.assertIsNone(desktop.pick_app_page(mock.Mock(pages=[])))


if __name__ == "__main__":
    unittest.main()
