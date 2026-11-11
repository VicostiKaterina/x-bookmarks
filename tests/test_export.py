import csv
import io
import unittest
import zipfile
from unittest import mock

import serve


def item(**kw):
    base = dict(
        id="2117489539115577835",
        url="https://x.com/someone/status/2117489539115577835",
        handle="someone",
        name="某人",
        created="2026-11-02 18:41",
        added_at="2026-11-05 14:57",
        cat="ai",
        text="用 RAG 给团队文档做问答，chunk 512 + 重排之后准确率从 61% 提到 84%",
        media=[],
        sensitive=False,
    )
    base.update(kw)
    return base


class SlugTest(unittest.TestCase):
    def test_illegal_chars_replaced(self):
        self.assertEqual(serve._slug('a/b:c*d?"e"'), "a-b-c-d-e")

    def test_truncate(self):
        self.assertEqual(len(serve._slug("x" * 200, n=10)), 10)

    def test_empty_falls_back(self):
        self.assertEqual(serve._slug(""), "untitled")

    def test_yaml_str_quotes_and_keeps_unicode(self):
        out = serve._yaml_str('他说 "好"')
        self.assertTrue(out.startswith('"') and out.endswith('"'))
        self.assertIn("好", out)


class ExportItemsTest(unittest.TestCase):
    def test_private_excluded_by_default(self):
        fake = [item(id="1", cat="ai"), item(id="2", cat="private")]
        with mock.patch.object(serve, "load_items", return_value=fake) as m:
            got = serve.export_items()
        self.assertEqual([x["id"] for x in got], ["1"])
        m.assert_called_once()

    def test_private_included_on_request(self):
        fake = [item(id="1", cat="ai"), item(id="2", cat="private")]
        with mock.patch.object(serve, "load_items", return_value=fake):
            got = serve.export_items(include_private=True)
        self.assertEqual(len(got), 2)


class CsvTest(unittest.TestCase):
    def _rows(self, data):
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"), "应当带 utf-8 BOM")
        return list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))

    def test_raindrop_header_and_row(self):
        with mock.patch.object(serve, "export_items", return_value=[item()]):
            data = serve.build_table_csv("raindrop")
        rows = self._rows(data)
        self.assertEqual(rows[0], ["url", "title", "folder", "tags", "created", "added", "notes"])
        self.assertEqual(rows[1][0], item()["url"])
        self.assertEqual(rows[1][2], "AI")
        self.assertEqual(rows[1][4], "2026-11-02")
        self.assertEqual(rows[1][5], "2026-11-05 14:57", "应当导出首次收录时间")

    def test_notion_header_and_row(self):
        with mock.patch.object(serve, "export_items", return_value=[item(cat="tech")]):
            rows = self._rows(serve.build_table_csv("notion"))
        self.assertEqual(rows[0], ["Name", "URL", "Category", "Author", "Date", "Added", "Text"])
        self.assertEqual(rows[1][2], "技术")
        self.assertEqual(rows[1][3], "@someone")
        self.assertEqual(rows[1][5], "2026-11-05 14:57")


class MarkdownZipTest(unittest.TestCase):
    def test_structure_and_frontmatter(self):
        with mock.patch.object(serve, "export_items", return_value=[item()]):
            data = serve.build_markdown_zip()
        z = zipfile.ZipFile(io.BytesIO(data))
        names = z.namelist()
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].startswith("AI/"), names[0])
        self.assertTrue(names[0].endswith("-" + item()["id"][-8:] + ".md"), names[0])
        md = z.read(names[0]).decode("utf-8")
        self.assertTrue(md.startswith("---\n"))
        self.assertIn('url: "https://x.com/someone/status/2117489539115577835"', md)
        self.assertIn('category: "AI"', md)
        self.assertIn("来源：https://x.com/", md)
        self.assertIn("准确率", md)

    def test_media_links_and_duplicate_names(self):
        two = [item(id="2"), item(id="2")]  # 同 id 同正文 → 文件名要去重
        with mock.patch.object(serve, "export_items", return_value=two):
            z = zipfile.ZipFile(io.BytesIO(serve.build_markdown_zip()))
        self.assertEqual(len(z.namelist()), 2)
        self.assertEqual(len(set(z.namelist())), 2)

    def test_media_appended(self):
        it = item(media=[{"url": "https://pbs.twimg.com/media/a.jpg"}])
        with mock.patch.object(serve, "export_items", return_value=[it]):
            z = zipfile.ZipFile(io.BytesIO(serve.build_markdown_zip()))
        md = z.read(z.namelist()[0]).decode("utf-8")
        self.assertIn("![](https://pbs.twimg.com/media/a.jpg)", md)


if __name__ == "__main__":
    unittest.main()
