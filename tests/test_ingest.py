import json
import pathlib
import tempfile
import unittest
from unittest import mock

import serve


class ItemKeyTest(unittest.TestCase):
    def test_plain_id(self):
        self.assertEqual(serve._item_key({"id": "123"}), "123")

    def test_rest_id_and_id_str(self):
        self.assertEqual(serve._item_key({"rest_id": "456"}), "456")
        self.assertEqual(serve._item_key({"id_str": "789"}), "789")

    def test_legacy_nested(self):
        self.assertEqual(serve._item_key({"legacy": {"id_str": "999"}}), "999")

    def test_url_fallback(self):
        self.assertEqual(serve._item_key({"url": "https://x.com/a/status/1"}), "@https://x.com/a/status/1")

    def test_garbage(self):
        self.assertEqual(serve._item_key("nope"), "")
        self.assertEqual(serve._item_key({"text": "no id"}), "")


class MergeTest(unittest.TestCase):
    def test_keeps_order_and_overrides(self):
        old = [{"id": "1", "text": "a"}, {"id": "2", "text": "b"}]
        new = [{"id": "2", "text": "b2"}, {"id": "3", "text": "c"}]
        got = serve.merge_items(old, new)
        self.assertEqual([g["id"] for g in got], ["1", "2", "3"])
        self.assertEqual(got[1]["text"], "b2")

    def test_dedupes_within_new(self):
        got = serve.merge_items([], [{"id": "1"}, {"id": "1", "text": "x"}])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["text"], "x")

    def test_skips_entries_without_id(self):
        got = serve.merge_items([{"id": "1"}], [{"text": "no id"}, "junk"])
        self.assertEqual([g["id"] for g in got], ["1"])

    def test_partial_capture_does_not_shrink(self):
        old = [{"id": str(i)} for i in range(100)]
        new = [{"id": "0"}, {"id": "500"}]
        self.assertEqual(len(serve.merge_items(old, new)), 101)


class IngestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="xbm-ingest-")
        self.dir = pathlib.Path(self.tmp)
        self.settings = {"data_dir": self.tmp}

    def _ingest(self, items, handle="someone"):
        with mock.patch.object(serve, "load_settings", return_value=self.settings), \
                mock.patch.object(serve, "reclassify", return_value=(True, "ok")):
            return serve.ingest_items(items, handle, "bookmark")

    def test_merges_with_existing_file(self):
        raw = self.dir / "raw_bookmarks.json"
        raw.write_text(json.dumps([{"id": "1", "text": "a"}, {"id": "2", "text": "b"}]), encoding="utf-8")
        ok, _msg, total = self._ingest([{"id": "2", "text": "b2"}, {"id": "3", "text": "c"}])
        self.assertTrue(ok)
        self.assertEqual(total, 3)
        got = json.loads(raw.read_text(encoding="utf-8"))
        self.assertEqual([g["id"] for g in got], ["1", "2", "3"])
        self.assertEqual(got[1]["text"], "b2")
        meta = json.loads((self.dir / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["count"], 3)
        self.assertEqual(meta["source"], "extension")
        self.assertEqual(meta["account"], "someone")

    def test_fresh_dir_no_crash(self):
        ok, _msg, total = self._ingest([{"id": "9"}])
        self.assertTrue(ok)
        self.assertEqual(total, 1)
        self.assertTrue((self.dir / "raw_bookmarks.json").exists())

    def test_corrupt_existing_file_is_ignored(self):
        (self.dir / "raw_bookmarks.json").write_text("{not json", encoding="utf-8")
        ok, _msg, total = self._ingest([{"id": "1"}])
        self.assertTrue(ok)
        self.assertEqual(total, 1)

    def test_likes_mode_writes_likes_file(self):
        with mock.patch.object(serve, "load_settings", return_value=self.settings), \
                mock.patch.object(serve, "reclassify", return_value=(True, "ok")):
            serve.ingest_items([{"id": "1"}], "someone", "like")
        self.assertTrue((self.dir / "raw_likes.json").exists())


if __name__ == "__main__":
    unittest.main()
