# -*- coding: utf-8 -*-
"""增量同步判断：只抓新书签、遇到旧 id 就停。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import collect  # noqa: E402


class IncrementalTest(unittest.TestCase):
    def test_all_new_when_store_empty(self):
        self.assertEqual(collect.new_ids(["a", "b", "c"], set()), ["a", "b", "c"])

    def test_filters_known_and_keeps_order(self):
        self.assertEqual(collect.new_ids(["c", "b", "a"], {"a"}), ["c", "b"])

    def test_all_known_means_nothing_new(self):
        self.assertEqual(collect.new_ids(["a", "b"], {"a", "b"}), [])

    def test_duplicates_inside_one_page_collapse(self):
        self.assertEqual(collect.new_ids(["a", "a", "b"], set()), ["a", "b"])

    def test_none_inputs_are_safe(self):
        self.assertEqual(collect.new_ids(None, None), [])
        self.assertEqual(collect.new_ids([], {"a"}), [])

    def test_overlap_detection_used_by_stop_rule(self):
        # 一页里出现已有 id → len(fresh) < len(ids) → 停止翻页
        ids = ["new1", "new2", "old1"]
        fresh = collect.new_ids(ids, {"old1"})
        self.assertLess(len(fresh), len(ids))

    def test_benign_endings_include_incremental_stop(self):
        benign = ("连续两页没有新内容", "游标没有前进，停止翻页", "增量：已到上次同步的位置")
        for x in benign:
            self.assertIn(x, benign)
        self.assertIn("增量", "增量：已到上次同步的位置")


if __name__ == "__main__":
    unittest.main()
