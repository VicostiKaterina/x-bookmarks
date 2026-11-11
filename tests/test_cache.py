# -*- coding: utf-8 -*-
"""/api/state 的 items 文本缓存：文件没变就复用，文件变了必须失效。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import serve  # noqa: E402


class ItemsCacheTest(unittest.TestCase):
    def setUp(self):
        serve.invalidate_items_cache()

    def test_cache_hits_and_invalidates_on_change(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "data"
            d.mkdir(parents=True)
            p = d / "items.json"
            p.write_text(json.dumps({"items": [{"id": "1", "text": "a", "cat": "ai"}]},
                                    ensure_ascii=False), encoding="utf-8")
            s = {"data_dir": str(d)}
            first = serve.items_json_cached(s)
            self.assertIn('"1"', first)
            second = serve.items_json_cached(s)
            self.assertEqual(first, second)

            p.write_text(json.dumps({"items": [{"id": "1", "text": "a", "cat": "ai"},
                                               {"id": "2", "text": "b", "cat": "tech"}]},
                                    ensure_ascii=False), encoding="utf-8")
            third = serve.items_json_cached(s)
            self.assertIn('"2"', third)
            self.assertNotEqual(first, third)

    def test_missing_file_returns_empty_array(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(serve.items_json_cached({"data_dir": str(Path(t) / "nope")}), "[]")

    def test_cached_text_is_valid_json(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "data"
            d.mkdir(parents=True)
            (d / "items.json").write_text(json.dumps({"items": [
                {"id": "9", "text": "x" * 1200, "cat": "misc", "url": "javascript:alert(1)"}]}),
                encoding="utf-8")
            text = serve.items_json_cached({"data_dir": str(d)})
            doc = json.loads(text)
            self.assertEqual(len(doc), 1)
            self.assertEqual(doc[0]["url"], "")          # 仍然过 _safe_url
            self.assertTrue(doc[0]["trunc"])             # 仍然按 900 字截断


if __name__ == "__main__":
    unittest.main()
