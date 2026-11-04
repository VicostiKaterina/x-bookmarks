#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""基础自测：跑 `python -m unittest discover -s tests -v`

不依赖第三方库，也不碰真实数据目录（用临时文件）。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import organize  # noqa: E402


def tweet(rest_id="1", handle="someone", name="Someone", text="hello",
          sensitive=False, media=None, views=0, likes=0, created="Wed Nov 04 05:00:00 +0000 2026"):
    """构造一条 X GraphQL 形状的推文（和 collect.py 落盘的结构一致）。"""
    legacy = {
        "full_text": text,
        "created_at": created,
        "lang": "zh",
        "favorite_count": likes,
        "retweet_count": 0,
        "reply_count": 0,
        "quote_count": 0,
        "bookmark_count": 0,
        "possibly_sensitive": sensitive,
        "entities": {"urls": [], "hashtags": []},
    }
    if media:
        legacy["extended_entities"] = {"media": media}
    return {
        "rest_id": rest_id,
        "views": {"count": str(views)},
        "core": {
            "user_results": {
                "result": {"core": {"screen_name": handle, "name": name}}
            }
        },
        "legacy": legacy,
        "possibly_sensitive": sensitive,
    }


class TestNorm(unittest.TestCase):
    def test_basic_fields(self):
        r = organize.norm(tweet("123", "alice", "Alice", "你好 世界"))
        self.assertIsNotNone(r)
        self.assertEqual(r["id"], "123")
        self.assertEqual(r["handle"], "alice")
        self.assertEqual(r["name"], "Alice")
        self.assertEqual(r["text"], "你好 世界")
        self.assertEqual(r["url"], "https://x.com/alice/status/123")

    def test_counts_and_time(self):
        r = organize.norm(tweet("9", "bob", "Bob", "hi", views=1234, likes=56))
        self.assertEqual(r["views"], 1234)
        self.assertEqual(r["likes"], 56)
        self.assertTrue(r["created"].startswith("2026-11-04"))

    def test_media_normalized(self):
        media = [{"type": "photo", "media_url_https": "https://pbs.twimg.com/a.jpg", "ext_alt_text": "alt"}]
        r = organize.norm(tweet("7", media=media))
        self.assertEqual(len(r["media"]), 1)
        self.assertTrue(r["media"][0]["url"].startswith("https://pbs.twimg.com/a.jpg"))
        self.assertEqual(r["media"][0]["alt"], "alt")

    def test_no_id_returns_none(self):
        self.assertIsNone(organize.norm({"legacy": {}}))
        self.assertIsNone(organize.norm("nope"))


class TestClassify(unittest.TestCase):
    def cat(self, text, **kw):
        r = organize.norm(tweet("1", text=text, **kw))
        return organize.classify(r)["cat"]

    def test_tech(self):
        self.assertEqual(self.cat("Chrome DevTools 调试 network 面板的技巧"), "tech")

    def test_ai(self):
        self.assertEqual(self.cat("把 prompt 再改一下，模型输出会好很多"), "ai")

    def test_life(self):
        self.assertEqual(self.cat("今天去爬山了，天气很好，顺便做了一顿火锅"), "life")

    def test_sensitive_forced_private(self):
        self.assertEqual(self.cat("hello there", sensitive=True), "private")

    def test_unknown_goes_misc(self):
        self.assertEqual(self.cat("zzz qqq"), "misc")

    def test_manual_override_wins(self):
        rows = [organize.norm(tweet("42", text="今天去爬山"))]
        rows[0]["cat"] = "life"
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "overrides.json"
            p.write_text(json.dumps({"42": "ai"}), encoding="utf-8")
            n = organize.apply_overrides(rows, p)
        self.assertEqual(n, 1)
        self.assertEqual(rows[0]["cat"], "ai")
        self.assertTrue(rows[0]["manual"])
        self.assertEqual(rows[0]["why"], "手动指定")


class TestSmooth(unittest.TestCase):
    def test_author_smoothing(self):
        rows = []
        for i in range(4):
            r = organize.norm(tweet(str(i), handle="spammer", text="纯图" if i < 3 else "一句模糊的话",
                                    media=[{"type": "photo", "media_url_https": "https://pbs.twimg.com/x.jpg"}]))
            r["cat"] = "private" if i < 3 else "misc"
            r["conf"] = 0.8
            rows.append(r)
        organize.author_smooth(rows)
        self.assertEqual(rows[3]["cat"], "private")

    def test_media_only_smoothing_requires_sensitive_account(self):
        rows = []
        for i in range(4):
            r = organize.norm(tweet(str(i), handle="acct", text="",
                                    sensitive=(i < 2),
                                    media=[{"type": "photo", "media_url_https": "https://pbs.twimg.com/x.jpg"}]))
            r["cat"] = "private" if i < 2 else "misc"
            r["conf"] = 0.5
            rows.append(r)
        organize.media_only_smooth(rows)
        self.assertEqual(rows[2]["cat"], "private")


class TestKwHit(unittest.TestCase):
    def test_ascii_boundary(self):
        self.assertFalse(organize._kw_hit("waiting for godot", "ai"))
        self.assertTrue(organize._kw_hit("ai 是人工智能", "ai"))
        self.assertTrue(organize._kw_hit("我用 AI 画图", "AI", cs=True))

    def test_cjk_substring(self):
        self.assertTrue(organize._kw_hit("这是一篇技术文章", "技术"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
