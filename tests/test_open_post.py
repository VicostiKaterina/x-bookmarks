# -*- coding: utf-8 -*-
"""「用登录态打开原帖」的单元测试（v2.6.0 起，2.6.2 改成默认新标签页）。"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve


class SafePostUrl(unittest.TestCase):
    def test_plain_status(self):
        self.assertEqual(serve.safe_post_url("https://x.com/foo/status/123"),
                         "https://x.com/foo/status/123")

    def test_twitter_host_kept(self):
        self.assertEqual(serve.safe_post_url("https://twitter.com/a/status/1?s=20"),
                         "https://twitter.com/a/status/1?s=20")

    def test_i_web_status(self):
        self.assertEqual(serve.safe_post_url("https://x.com/i/web/status/42"),
                         "https://x.com/i/web/status/42")

    def test_script_scheme_rejected(self):
        self.assertEqual(serve.safe_post_url("javascript:alert(1)"), "")

    def test_local_file_rejected(self):
        self.assertEqual(serve.safe_post_url("file:///C:/Windows/win.ini"), "")

    def test_other_host_rejected(self):
        for bad in ("https://evil.example/x/status/1",
                    "http://x.com.evil.example/status/1",
                    "https://192.168.1.1/status/1",
                    "https://x.com@evil.example/status/1"):
            self.assertEqual(serve.safe_post_url(bad), "", bad)

    def test_empty_and_garbage(self):
        for bad in ("", None, "   ", "https://", "not a url"):
            self.assertEqual(serve.safe_post_url(bad), "", repr(bad))

    def test_query_kept_fragment_dropped(self):
        self.assertEqual(serve.safe_post_url("https://x.com/a/status/1?s=20#top"),
                         "https://x.com/a/status/1?s=20")


class PostKey(unittest.TestCase):
    def test_same_status_different_host(self):
        self.assertEqual(serve.post_key("https://twitter.com/foo/status/123?s=20"),
                         serve.post_key("https://x.com/bar/status/123"))
        self.assertEqual(serve.post_key("https://x.com/i/web/status/123"), "status:123")

    def test_different_status(self):
        self.assertNotEqual(serve.post_key("https://x.com/a/status/1"),
                            serve.post_key("https://x.com/a/status/2"))

    def test_bad_url(self):
        self.assertEqual(serve.post_key("javascript:void(0)"), "")


class FakeCDP:
    """记录调用的假 CDP 连接。bounds 决定窗口当前在哪。"""

    def __init__(self, *a, **kw):
        self.sent = []
        FakeCDP.last = self

    def send(self, method, params=None):
        self.sent.append((method, params or {}))
        if method == "Target.createTarget":
            return {"targetId": "T%d" % (len(self.sent))}
        if method == "Browser.getWindowForTarget":
            return {"windowId": 7}
        if method == "Browser.getWindowBounds":
            return {"bounds": FakeCDP.bounds}
        return {}

    def close(self):
        pass


class OpenInLoggedBrowser(unittest.TestCase):
    def setUp(self):
        serve._OPEN_WIN["tid"] = ""
        FakeCDP.bounds = {"state": "normal", "left": 100, "top": 100}
        self.made = []

    def _run(self, url, settings, pages=None):
        def fake_cdp(ws, timeout=10):
            c = FakeCDP(ws, timeout)
            self.made.append(c)
            return c

        with mock.patch.object(serve.cdp, "alive", return_value=True), \
             mock.patch.object(serve, "_browser_ws", return_value="ws://browser"), \
             mock.patch.object(serve, "_list_pages", return_value=pages or []), \
             mock.patch.object(serve, "_page_ws", return_value=""), \
             mock.patch.object(serve.cdp, "CDP", fake_cdp):
            return serve.open_in_logged_browser(url, settings)

    def test_rejects_non_x_url(self):
        ok, msg = serve.open_in_logged_browser("https://evil.example/a", {})
        self.assertFalse(ok)
        self.assertIn("X", msg)

    def test_default_opens_new_tab_not_new_window(self):
        ok, msg = self._run("https://x.com/a/status/1", {"debug_port": 9222})
        self.assertTrue(ok, msg)
        sent = self.made[0].sent
        ct = [p for m, p in sent if m == "Target.createTarget"][0]
        self.assertFalse(ct["newWindow"], "默认应该开新标签页，不要再占一个窗口")
        self.assertEqual(ct["url"], "https://x.com/a/status/1")
        self.assertIn("Target.activateTarget", [m for m, _ in sent], "开完要切到这个标签页")

    def test_hidden_window_is_moved_back_on_screen(self):
        FakeCDP.bounds = {"state": "normal", "left": -32000, "top": -32000}
        ok, msg = self._run("https://x.com/a/status/2", {"debug_port": 9222})
        self.assertTrue(ok, msg)
        bounds = [p for m, p in self.made[0].sent if m == "Browser.setWindowBounds"][0]["bounds"]
        self.assertEqual(bounds["windowState"], "normal")
        self.assertGreater(bounds["left"], 0, "采集窗口藏在 -32000，打开原帖时要挪回屏幕")
        self.assertGreater(bounds["width"], 600)

    def test_visible_window_is_left_alone(self):
        ok, msg = self._run("https://x.com/a/status/3", {"debug_port": 9222})
        self.assertTrue(ok, msg)
        self.assertNotIn("Browser.setWindowBounds", [m for m, _ in self.made[0].sent],
                         "窗口本来就在屏幕上，不该乱动")

    def test_window_mode_opens_separate_window(self):
        ok, msg = self._run("https://x.com/a/status/4",
                            {"debug_port": 9222, "open_post_mode": "window"})
        self.assertTrue(ok, msg)
        sent = self.made[0].sent
        ct = [p for m, p in sent if m == "Target.createTarget"][0]
        self.assertTrue(ct["newWindow"], "window 模式要单开新窗口（可以并排看两条）")
        self.assertIn("Browser.setWindowBounds", [m for m, _ in sent], "新窗口要摆到屏幕中间")

    def test_reuse_mode_navigates_previous_window(self):
        serve._OPEN_WIN["tid"] = "T9"
        with mock.patch.object(serve.cdp, "alive", return_value=True), \
             mock.patch.object(serve, "_browser_ws", return_value="ws://browser"), \
             mock.patch.object(serve, "_list_pages", return_value=[]), \
             mock.patch.object(serve, "_page_ws", return_value="ws://page-T9"), \
             mock.patch.object(serve.cdp, "CDP", FakeCDP):
            ok, msg = serve.open_in_logged_browser(
                "https://x.com/a/status/77", {"debug_port": 9222, "open_post_mode": "reuse"})
        self.assertTrue(ok, msg)
        methods = [m for m, _ in FakeCDP.last.sent]
        self.assertIn("Page.navigate", methods)
        self.assertNotIn("Target.createTarget", methods)

    def test_same_post_already_open_is_focused_not_duplicated(self):
        pages = [{"id": "P1", "url": "https://x.com/someone/status/555?s=20", "ws": "ws://page-P1"}]
        ok, msg = self._run("https://x.com/a/status/555", {"debug_port": 9222}, pages=pages)
        self.assertTrue(ok, msg)
        self.assertIn("已经开着", msg)
        self.assertNotIn("Target.createTarget", [m for m, _ in FakeCDP.last.sent])
        self.assertIn("Page.bringToFront", [m for m, _ in FakeCDP.last.sent])

    def test_browser_down_launches_visible_chrome(self):
        started = []
        with mock.patch.object(serve.cdp, "alive", return_value=False), \
             mock.patch.object(serve, "ensure_chrome",
                               side_effect=lambda s, visible=False, url="": started.append((visible, url))):
            ok, msg = serve.open_in_logged_browser("https://x.com/a/status/9",
                                                   {"debug_port": 9222})
        self.assertTrue(ok, msg)
        self.assertEqual(started, [(True, "https://x.com/a/status/9")])


class OpenPostModeSetting(unittest.TestCase):
    def test_default_is_tab(self):
        self.assertEqual(serve.DEFAULTS["open_post_mode"], "tab")

    def test_accepts_known_modes(self):
        for m in ("tab", "window", "reuse"):
            self.assertEqual(serve.sanitize_settings({"open_post_mode": m})["open_post_mode"], m)

    def test_case_and_garbage_fall_back_to_tab(self):
        self.assertEqual(serve.sanitize_settings({"open_post_mode": "WINDOW"})["open_post_mode"],
                         "window")
        for bad in ("nope", "", None, 5):
            self.assertEqual(serve.sanitize_settings({"open_post_mode": bad})["open_post_mode"],
                             "tab", repr(bad))


if __name__ == "__main__":
    unittest.main()
