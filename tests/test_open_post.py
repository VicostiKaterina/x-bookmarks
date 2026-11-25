# -*- coding: utf-8 -*-
"""「用登录态打开原帖」的单元测试（v2.6.0）。"""
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
        for bad in ("", None, "   ", "x"/1 if False else "https://", "not a url"):
            self.assertEqual(serve.safe_post_url(bad), "", repr(bad))

    def test_query_kept_fragment_dropped(self):
        self.assertEqual(serve.safe_post_url("https://x.com/a/status/1?s=20#top"),
                         "https://x.com/a/status/1?s=20")


class OpenInLoggedBrowser(unittest.TestCase):
    def test_rejects_non_x_url(self):
        ok, msg = serve.open_in_logged_browser("https://evil.example/a", {})
        self.assertFalse(ok)
        self.assertIn("X", msg)

    def test_creates_visible_window_in_logged_profile(self):
        serve._OPEN_WIN["tid"] = ""      # 不受别的用例影响
        calls = []

        class FakeCDP:
            def __init__(self, ws, timeout=10):
                calls.append(("connect", ws))

            def send(self, method, params=None):
                calls.append((method, params or {}))
                if method == "Target.createTarget":
                    return {"targetId": "T1"}
                if method == "Browser.getWindowForTarget":
                    return {"windowId": 7}
                return {}

            def close(self):
                calls.append(("close", {}))

        with mock.patch.object(serve.cdp, "alive", return_value=True), \
             mock.patch.object(serve, "_browser_ws", return_value="ws://browser"), \
             mock.patch.object(serve.cdp, "CDP", FakeCDP):
            ok, msg = serve.open_in_logged_browser("https://x.com/a/status/1",
                                                   {"debug_port": 9222})

        self.assertTrue(ok, msg)
        ct = [c for c in calls if c[0] == "Target.createTarget"][0]
        self.assertTrue(ct[1]["newWindow"], "要开成新窗口，别占用采集用的那个隐藏窗口")
        self.assertEqual(ct[1]["url"], "https://x.com/a/status/1")
        bounds = [c for c in calls if c[0] == "Browser.setWindowBounds"][0][1]["bounds"]
        self.assertEqual(bounds["windowState"], "normal")
        self.assertGreater(bounds["width"], 600)
        self.assertGreater(bounds["left"], 0, "窗口不能被丢到屏幕外（采集窗口就在 -32000）")
        self.assertIn(("close", {}), calls)

    def test_reuses_previous_window(self):
        class FakeCDP:
            def __init__(self, ws, timeout=10):
                self.ws = ws
                self.sent = []

            def send(self, method, params=None):
                self.sent.append((method, params or {}))
                return {}

            def close(self):
                pass

        made = {}

        def fake_cdp(ws, timeout=10):
            made.setdefault("inst", FakeCDP(ws, timeout))
            return made["inst"]

        serve._OPEN_WIN["tid"] = "T9"
        with mock.patch.object(serve.cdp, "alive", return_value=True), \
             mock.patch.object(serve, "_browser_ws", return_value="ws://browser"), \
             mock.patch.object(serve, "_page_ws", return_value="ws://page-T9"), \
             mock.patch.object(serve.cdp, "CDP", fake_cdp):
            ok, msg = serve.open_in_logged_browser("https://x.com/a/status/77",
                                                   {"debug_port": 9222})
        serve._OPEN_WIN["tid"] = ""
        self.assertTrue(ok, msg)
        methods = [m for m, _ in made["inst"].sent]
        self.assertIn("Page.navigate", methods, "应该复用上次那个窗口")
        self.assertNotIn("Target.createTarget", methods)
        nav = [p for m, p in made["inst"].sent if m == "Page.navigate"][0]
        self.assertEqual(nav["url"], "https://x.com/a/status/77")

    def test_browser_down_launches_visible_chrome(self):
        started = []
        with mock.patch.object(serve.cdp, "alive", return_value=False), \
             mock.patch.object(serve, "ensure_chrome",
                               side_effect=lambda s, visible=False, url="": started.append((visible, url))):
            ok, msg = serve.open_in_logged_browser("https://x.com/a/status/9",
                                                   {"debug_port": 9222})
        self.assertTrue(ok, msg)
        self.assertEqual(started, [(True, "https://x.com/a/status/9")])


if __name__ == "__main__":
    unittest.main()
