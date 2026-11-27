# -*- coding: utf-8 -*-
"""「应用内读原帖」的单元测试（v2.7.1 起）。

真的抓取要开浏览器，不放单测里；这里测那些纯逻辑：图片白名单、正文规整、
缓存过期，以及「没登录 / 错误页 / 没渲染出来」三种提示。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve


class PostImages(unittest.TestCase):
    def test_https_kept(self):
        got = serve._post_imgs(["https://pbs.twimg.com/media/abc.jpg"])
        self.assertEqual(got, ["https://pbs.twimg.com/media/abc.jpg"])

    def test_bad_scheme_dropped(self):
        got = serve._post_imgs([
            "javascript:alert(1)",
            "data:image/png;base64,AAAA",
            "http://pbs.twimg.com/media/x.jpg",   # 明文也丢掉
            "https://pbs.twimg.com/media/ok.jpg",
        ])
        self.assertEqual(got, ["https://pbs.twimg.com/media/ok.jpg"])

    def test_none_and_garbage(self):
        self.assertEqual(serve._post_imgs(None), [])
        self.assertEqual(serve._post_imgs([None, 5, ""]), [])

    def test_capped_at_eight(self):
        urls = ["https://pbs.twimg.com/media/%d.jpg" % i for i in range(12)]
        self.assertEqual(len(serve._post_imgs(urls)), 8)


class FetchGuards(unittest.TestCase):
    def test_non_x_url_rejected_without_browser(self):
        ok, msg, data = serve.fetch_post_detail("https://evil.example/status/1")
        self.assertFalse(ok)
        self.assertIn("只能读", msg)
        self.assertIsNone(data)

    def test_cache_hit_skips_browser(self):
        key = "status:987654321"
        serve._POST_CACHE[key] = (serve.time.time(), {"url": "u", "main": {}, "replies": []})
        try:
            with mock.patch.object(serve.cdp, "alive") as alive:
                ok, msg, data = serve.fetch_post_detail("https://x.com/a/status/987654321")
            self.assertTrue(ok)
            self.assertEqual(data["url"], "u")
            alive.assert_not_called()
        finally:
            serve._POST_CACHE.pop(key, None)

    def test_expired_cache_is_not_used(self):
        key = "status:123123123"
        serve._POST_CACHE[key] = (serve.time.time() - serve.POST_CACHE_TTL - 5,
                                 {"url": "old", "main": {}, "replies": []})
        try:
            with mock.patch.object(serve.cdp, "alive", return_value=False), \
                 mock.patch.object(serve, "ensure_chrome", side_effect=RuntimeError("boom")):
                ok, msg, data = serve.fetch_post_detail("https://x.com/a/status/123123123")
            self.assertFalse(ok)
            self.assertIn("采集浏览器没起来", msg)
        finally:
            serve._POST_CACHE.pop(key, None)


class ReaderResponseShape(unittest.TestCase):
    """结果里该有的字段一个都不能少，前端直接拿来渲染。"""

    def test_shape(self):
        with mock.patch.object(serve, "_POST_CACHE", {}):
            fake = {"url": "https://x.com/a/status/5", "main": {"name": "N", "handle": "@a"},
                    "replies": [], "at": "2026-11-28 03:00:00"}
            serve._POST_CACHE["status:5"] = (serve.time.time(), fake)
            ok, msg, data = serve.fetch_post_detail("https://x.com/a/status/5")
        self.assertTrue(ok)
        for k in ("url", "main", "replies", "at"):
            self.assertIn(k, data)


if __name__ == "__main__":
    unittest.main()
