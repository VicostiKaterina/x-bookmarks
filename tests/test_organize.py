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
        # 另一个干净账号的纯图待定帖不该被连带
        for i in range(3):
            o = organize.norm(tweet("9%d" % i, handle="clean", text="",
                                    sensitive=False,
                                    media=[{"type": "photo", "media_url_https": "https://pbs.twimg.com/y.jpg"}]))
            o["cat"] = "misc"
            o["conf"] = 0.5
            rows.append(o)
        organize.media_only_smooth(rows)
        self.assertEqual(rows[2]["cat"], "private")
        self.assertEqual([r["cat"] for r in rows[4:]], ["misc", "misc", "misc"],
                         "不该按全站比例把别的账号也拖进私密")


class TestSafety(unittest.TestCase):
    """回归：隐私与数据安全（都是审计里真踩到的坑）。"""

    def test_rules_have_no_single_char_private_word(self):
        rules = json.loads((BASE / "rules.json").read_text(encoding="utf-8"))
        kws = rules["categories"]["private"]["keywords"]
        self.assertEqual([w for w in kws if len(w) == 1], [],
                         "私密类里有单字词，会把「乳制品」「喷子」这类日常帖误判")

    def test_daily_words_not_private(self):
        for text in ("乳制品真好吃，推荐这款酸奶", "今天喷子好多，评论区没法看"):
            r = organize.classify(organize.norm(tweet("1", "x", "X", text)))
            self.assertNotEqual(r["cat"], "private", "%r 被误判成私密（%s）" % (text, r.get("why")))

    def test_write_items_is_atomic_and_keeps_bak(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "items.json"
            a = organize.classify(organize.norm(tweet("1", "alice", "Alice", "第一篇")))
            b = organize.classify(organize.norm(tweet("2", "bob", "Bob", "第二篇")))
            organize.write_items([a], p)
            first = p.read_text(encoding="utf-8")
            organize.write_items([b], p)
            self.assertEqual(json.loads(first)["items"][0]["id"], "1")
            self.assertEqual(json.loads(p.read_text(encoding="utf-8"))["items"][0]["id"], "2")
            self.assertTrue(p.with_name("items.json.bak").exists(), "应该留一份 .bak")
            self.assertFalse(p.with_name("items.json.tmp").exists(), "临时文件要清掉")

    def test_data_dir_comes_from_settings(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "settings.json").write_text(json.dumps({"data_dir": "mydata"}), encoding="utf-8")
            data, exports = organize._settings_paths(root)
            self.assertEqual(data, root / "mydata")
            self.assertEqual(exports, root / "exports")

    def test_sanitize_settings_clamps_and_drops(self):
        try:
            import serve
        except Exception as e:  # 依赖不全时跳过，不假装通过
            self.skipTest("import serve 失败：%s" % e)
        clean = serve.sanitize_settings({
            "port": "abc", "semantic_threshold": 99, "auto_refresh_hours": -5,
            "bind": "8.8.8.8", "blur_private": "yes", "default_sort": "hack",
            "unknown_key": 123, "access_code": "  123456  ",
        })
        self.assertEqual(clean["port"], serve.DEFAULTS["port"])
        self.assertLessEqual(clean["semantic_threshold"], 0.95)
        self.assertGreaterEqual(clean["semantic_threshold"], 0.4)
        self.assertEqual(clean["auto_refresh_hours"], 0)
        self.assertEqual(clean["bind"], "127.0.0.1")
        self.assertIs(clean["blur_private"], True)
        self.assertEqual(clean["default_sort"], "time_desc")
        self.assertNotIn("unknown_key", clean)
        self.assertEqual(clean["access_code"], "123456")
        self.assertTrue(clean["proxy"] == "" and clean["llm_key"] == "")

    def test_sanitize_settings_keeps_added_desc_and_rotates_short_code(self):
        try:
            import serve
        except Exception as e:
            self.skipTest("import serve 失败：%s" % e)
        clean = serve.sanitize_settings({"default_sort": "added_desc", "access_code": "12"})
        self.assertEqual(clean["default_sort"], "added_desc")
        # 1~3 位的访问码等于没锁，必须被换成新的 6 位
        self.assertNotEqual(clean["access_code"], "12")
        self.assertEqual(len(clean["access_code"]), 6)
        self.assertTrue(clean["access_code"].isdigit())

    def test_code_token_is_stable_and_salted(self):
        try:
            import serve
        except Exception as e:
            self.skipTest("import serve 失败：%s" % e)
        a = serve._code_token({"cookie_salt": "aa", "access_code": "123456"})
        b = serve._code_token({"cookie_salt": "aa", "access_code": "123456"})
        c = serve._code_token({"cookie_salt": "bb", "access_code": "123456"})
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertNotIn("123456", a)
        self.assertTrue(serve._const_eq(a, b))
        self.assertFalse(serve._const_eq(a, c))


class TestKwHit(unittest.TestCase):
    def test_ascii_boundary(self):
        self.assertFalse(organize._kw_hit("waiting for godot", "ai"))
        self.assertTrue(organize._kw_hit("ai 是人工智能", "ai"))
        self.assertTrue(organize._kw_hit("我用 AI 画图", "AI", cs=True))

    def test_cjk_substring(self):
        self.assertTrue(organize._kw_hit("这是一篇技术文章", "技术"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
