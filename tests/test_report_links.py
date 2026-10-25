"""引用に元のページへのリンクを付ける（#16）検査。map.md の「原文（sources/）」の表の「ファイル」の列の書き方を読み分けること、
リンクにするのが http(s) の URL だけであること、数の合わない行や「同上」で別の原文へリンクしないことを見る。"""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/imozuru/scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("report_links", SCRIPTS / "report.py")
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)

# 手元の map.md にあった書き方を1行ずつ並べた表。区切りの行と最初の行のあいだの空行も、手元にあった形
MAP = """# 調査
## 原文（sources/）
| 論文・資料 | ファイル | 元の URL | どこから来たか |
|---|---|---|---|

| 論文 A | a-paper.pdf/.txt | https://example.org/a.pdf | 1周目 |
| 論文 B | b-paper.pdf / .txt | https://example.org/b.pdf | 1周目 |
| 論文 C | c-paper.pdf・.txt | https://example.org/c.pdf | 1周目 |
| 記事 D と E | d-post.md・e-post.md | https://example.org/d https://example.org/e | 1周目 |
| 記事 F | `sources/f-post.md`（未読） | https://example.org/f | 1周目 |
| 記事 G | g-post(-r).md | https://example.org/g | 1周目 |
| 記事 H と I | h.md, i.md, j.md | https://example.org/h https://example.org/i | 数が合わない |
| 記事 K | k-post.md | 同上 | 前の行の注記の繰り返し |
| 記事 L | l-post.md | learn.example.org/…/l | 省略した URL |
| 記事 M | m-post.md | javascript:alert(1) | 悪い URL |
| 資料 N | n-note.md | https://example.org/n | 表は .md、実物は .txt |
| 資料 O | ms-learn-*.md ほか | https://example.org/o | ワイルドカード |

## 次の手がかり
| 語 | ファイル | 元の URL | 備考 |
|---|---|---|---|
"""


class FileGroupTests(unittest.TestCase):
    def test_issue_examples_split_on_separators_and_drop_notes(self):
        # Issue に書かれた形: 読点・カンマで分け、括弧の注記を外す
        self.assertEqual(report.file_groups("a.md, a.txt（未読）"), [["a.md"], ["a.txt"]])
        self.assertEqual(report.file_groups("a.md、b.md"), [["a.md"], ["b.md"]])

    def test_extension_alternates_are_one_source_with_two_files(self):
        for cell in ("a.pdf/.txt", "a.pdf / .txt", "a.pdf・.txt", "`sources/a.pdf/.txt`"):
            self.assertEqual(report.file_groups(cell), [["a.pdf", "a.txt"]], cell)

    def test_other_separators_and_kept_parentheses(self):
        self.assertEqual(report.file_groups("a.md・b.md"), [["a.md"], ["b.md"]])
        self.assertEqual(report.file_groups("a.md／b.md"), [["a.md"], ["b.md"]])
        # 半角の丸括弧はファイル名の一部
        self.assertEqual(report.file_groups("g-post(-r).md"), [["g-post(-r).md"]])
        # 下のフォルダの名前は全体も残す
        self.assertEqual(report.file_groups("deepseek/x.md"), [["deepseek/x.md", "x.md"]])
        # 名前でない語（ほか）は捨てる
        self.assertEqual(report.file_groups("ms-learn-*.md ほか"), [["ms-learn-*.md"]])


class LinkUrlTests(unittest.TestCase):
    def test_only_http_urls_without_spaces_or_quotes(self):
        self.assertEqual(report.link_url("https://example.org/a?b=1"), "https://example.org/a?b=1")
        for bad in ("javascript:alert(1)", "data:text/html,x", "https://", "https://a b", 'https://a"b', "https://a<b", "", None, 3):
            self.assertEqual(report.link_url(bad), "", bad)


class SourceUrlTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.topic = Path(tmp.name)
        (self.topic / "sources").mkdir()
        (self.topic / "map.md").write_text(MAP, encoding="utf-8")
        self.src = self.topic / "sources"

    def url(self, file):
        return report.source_url(self.src, file)

    def test_quotes_point_at_the_text_file_of_a_pdf(self):
        self.assertEqual(self.url("a-paper.txt"), "https://example.org/a.pdf")
        self.assertEqual(self.url("b-paper.txt"), "https://example.org/b.pdf")
        self.assertEqual(self.url("c-paper.txt"), "https://example.org/c.pdf")

    def test_urls_pair_with_sources_in_order_only_when_counts_match(self):
        self.assertEqual(self.url("d-post.md"), "https://example.org/d")
        self.assertEqual(self.url("e-post.md"), "https://example.org/e")
        # 3本に URL が2つ。どれがどれか決まらないので付けない
        for f in ("h.md", "i.md", "j.md"):
            self.assertEqual(self.url(f), "", f)

    def test_notes_prefix_parentheses_and_wildcards(self):
        self.assertEqual(self.url("f-post.md"), "https://example.org/f")
        self.assertEqual(self.url("g-post(-r).md"), "https://example.org/g")
        self.assertEqual(self.url("ms-learn-adds-overview.md"), "https://example.org/o")

    def test_no_link_for_same_as_above_shortened_or_unsafe_urls(self):
        self.assertEqual(self.url("k-post.md"), "")
        self.assertEqual(self.url("l-post.md"), "")
        self.assertEqual(self.url("m-post.md"), "")

    def test_extension_only_mismatch_falls_back_to_the_stem(self):
        self.assertEqual(self.url("n-note.txt"), "https://example.org/n")

    def test_unknown_source_or_missing_map_gives_no_link(self):
        self.assertEqual(self.url("nowhere.md"), "")
        (self.topic / "map.md").unlink()
        self.assertEqual(self.url("a-paper.txt"), "")


class FetchLogTests(unittest.TestCase):
    """表から引けない原文は、取得の記録（fetch-log.jsonl）の URL を使う"""

    LOG = [
        {"url": "https://example.org/old", "name": "p-post", "status": "OK", "file": "p-post.md"},
        {"url": "https://example.org/p", "name": "p-post", "status": "OK", "file": "p-post.md"},
        {"url": "https://example.org/q", "name": "q-post", "status": "FAIL", "reason": "403"},
        {"time": "2026-10-24T22:21:13", "name": "r-post", "url": "https://example.org/r", "result": "OK", "file": "sources/r-post.md"},
        {"url": "https://example.org/s.pdf", "name": "s-paper", "status": "OK", "file": "s-paper.pdf"},
        {"url": "javascript:alert(1)", "name": "t-post", "status": "OK", "file": "t-post.md"},
        {"url": "https://example.org/log-a", "name": "a-paper", "status": "OK", "file": "a-paper.txt"},
    ]

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.topic = Path(tmp.name)
        (self.topic / "sources").mkdir()
        (self.topic / "map.md").write_text(MAP, encoding="utf-8")
        (self.topic / "fetch-log.jsonl").write_text(
            "\n".join(report.json.dumps(r) for r in self.LOG) + "\n壊れた行\n", encoding="utf-8")
        self.src = self.topic / "sources"

    def url(self, file):
        return report.source_url(self.src, file)

    def test_latest_successful_fetch_is_used(self):
        self.assertEqual(self.url("p-post.md"), "https://example.org/p")
        self.assertEqual(self.url("r-post.md"), "https://example.org/r")

    def test_failed_or_unsafe_fetches_are_not_linked(self):
        self.assertEqual(self.url("q-post.md"), "")
        self.assertEqual(self.url("t-post.md"), "")

    def test_text_file_of_a_fetched_pdf_matches_by_stem(self):
        self.assertEqual(self.url("s-paper.txt"), "https://example.org/s.pdf")

    def test_table_wins_over_the_log(self):
        self.assertEqual(self.url("a-paper.txt"), "https://example.org/a.pdf")

    def test_without_map_the_log_is_still_used(self):
        (self.topic / "map.md").unlink()
        self.assertEqual(self.url("a-paper.txt"), "https://example.org/log-a")


class QuoteHtmlTests(SourceUrlTests):
    def quote(self, **q):
        q = {"file": "a-paper.txt", "line": 1, "text": "x", **q}
        return report.quote_html(self.src, q, ["x"], 1)

    def test_link_opens_a_new_tab_without_referrer(self):
        page = self.quote()
        self.assertIn('<a class="src" href="https://example.org/a.pdf" target="_blank" rel="noopener noreferrer">', page)

    def test_url_in_the_quote_wins_and_unsafe_ones_fall_back_to_the_map(self):
        self.assertIn('href="https://example.org/own"', self.quote(url="https://example.org/own"))
        page = self.quote(url="javascript:alert(1)")
        self.assertNotIn("javascript:", page)
        self.assertIn('href="https://example.org/a.pdf"', page)

    def test_no_link_when_nothing_is_found(self):
        self.assertNotIn('class="src"', self.quote(file="nowhere.md"))


if __name__ == "__main__":
    unittest.main()
