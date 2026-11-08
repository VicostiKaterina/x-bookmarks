# -*- coding: utf-8 -*-
"""账号隔离：换账号要归档旧数据，同账号 / 读不到账号名时绝不动数据。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import serve  # noqa: E402


def make_lib(tmp, account="", n=3):
    d = Path(tmp) / "data"
    d.mkdir(parents=True, exist_ok=True)
    items = [{"id": str(1000 + i), "text": "x%d" % i, "cat": "ai"} for i in range(n)]
    (d / "items.json").write_text(json.dumps({"count": n, "items": items}), encoding="utf-8")
    (d / "raw_bookmarks.json").write_text(json.dumps([{"rest_id": it["id"]} for it in items]),
                                          encoding="utf-8")
    (d / "overrides.json").write_text("{}", encoding="utf-8")
    meta = {"total": n}
    if account:
        meta["account"] = account
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return {"data_dir": str(d)}


class AccountSwitchTest(unittest.TestCase):
    def test_other_account_archives(self):
        with tempfile.TemporaryDirectory() as t:
            s = make_lib(t, account="OldUser")
            dest = serve.switch_account_if_needed(s, "NewUser")
            self.assertTrue(dest and Path(dest).exists())
            self.assertTrue((Path(dest) / "items.json").exists())
            self.assertEqual(serve.load_items(s), [])
            meta = json.loads((Path(s["data_dir"]) / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["owner"], "NewUser")
            self.assertEqual(meta["total"], 0)
            self.assertEqual(meta["switched_from"], "OldUser")

    def test_same_account_keeps_data(self):
        with tempfile.TemporaryDirectory() as t:
            s = make_lib(t, account="SameUser")
            self.assertEqual(serve.switch_account_if_needed(s, "@sameuser"), "")
            self.assertEqual(len(serve.load_items(s)), 3)

    def test_unknown_handle_never_clears(self):
        with tempfile.TemporaryDirectory() as t:
            s = make_lib(t, account="SomeOne")
            self.assertEqual(serve.switch_account_if_needed(s, ""), "")
            self.assertEqual(len(serve.load_items(s)), 3)

    def test_owner_survives_logout(self):
        with tempfile.TemporaryDirectory() as t:
            s = make_lib(t, account="A")
            serve.switch_account_if_needed(s, "A")
            mp = Path(s["data_dir"]) / "meta.json"
            meta = json.loads(mp.read_text(encoding="utf-8"))
            meta.pop("account", None)          # 等价于 /api/logout
            mp.write_text(json.dumps(meta), encoding="utf-8")
            self.assertEqual(serve.data_owner(s), "A")
            self.assertTrue(serve.switch_account_if_needed(s, "B"))

    def test_empty_library_no_error(self):
        with tempfile.TemporaryDirectory() as t:
            s = {"data_dir": str(Path(t) / "data")}
            Path(s["data_dir"]).mkdir(parents=True)
            self.assertEqual(serve.switch_account_if_needed(s, "X"), "")

    def test_broken_items_recovered_from_bak(self):
        with tempfile.TemporaryDirectory() as t:
            s = make_lib(t, account="A", n=2)
            d = Path(s["data_dir"])
            good = (d / "items.json").read_text(encoding="utf-8")
            (d / "items.json.bak").write_text(good, encoding="utf-8")
            (d / "items.json").write_text('{"count": 2, "items": [', encoding="utf-8")
            self.assertEqual(len(serve.load_items(s)), 2)

    def test_non_http_urls_dropped(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "data"
            d.mkdir(parents=True)
            (d / "items.json").write_text(json.dumps({"items": [{
                "id": "1", "text": "x", "url": "javascript:alert(1)",
                "media": [{"url": "javascript:alert(2)", "thumb": "https://pbs.twimg.com/a.jpg"}]}]}),
                encoding="utf-8")
            it = serve.load_items({"data_dir": str(d)})[0]
            self.assertEqual(it["url"], "")
            self.assertEqual(it["media"][0]["url"], "")
            self.assertTrue(it["media"][0]["thumb"].startswith("https://"))


if __name__ == "__main__":
    unittest.main()
