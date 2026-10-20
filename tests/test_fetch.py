"""再取得が失敗したときの原文保全と、成功時の保存先を検査する。"""

import contextlib
import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "skills/imozuru/scripts/fetch.py"
SPEC = importlib.util.spec_from_file_location("imozuru_fetch", SCRIPT)
fetch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fetch)
# テストの宛先（example.test）は名前を引けない。インターネット上の宛先に引けたことにする。
# 宛先の検査そのものは UnsafeUrlTests が本物に近い引き先を渡して確かめる
PUBLIC_IP = "93.184.215.14"
fetch.resolve = lambda host: {PUBLIC_IP}


class FetchPreservationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sources = Path(tmp.name) / "sources"
        self.sources.mkdir()
        self.stdout = io.StringIO()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(self.stdout))
        self.stack.enter_context(patch.object(fetch, "HAS_AX", False))
        # 外部コマンドの有無で結果が変わらないように、既定では全部あることにする
        self.stack.enter_context(patch.object(fetch, "missing", return_value=[]))

    def seed(self, *suffixes):
        for suffix in suffixes:
            (self.sources / f"paper{suffix}").write_bytes(
                f"Original verified {suffix} source\n".encode() * 150)
        (self.sources / "unrelated.md").write_bytes(b"Unrelated source\n")

    def snapshot(self):
        return {p.name: p.read_bytes() for p in self.sources.iterdir() if p.is_file()}

    def assert_preserved(self, before):
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(any(p.is_dir() for p in self.sources.iterdir()))

    def run_fetch(self, url="https://example.test/source"):
        with patch.object(sys, "argv", [str(SCRIPT), str(self.sources), "paper", url]):
            return fetch.main()

    @staticmethod
    def pdf_text(text):
        def convert(pdf):
            pdf.with_suffix(".txt").write_text(text, encoding="utf-8")
            return text
        return convert

    @staticmethod
    def markdown(text):
        def convert(url, out, body=None, ctype=""):
            out.write_text(text, encoding="utf-8")
            return text
        return convert

    def test_short_text_retry_preserves_existing_source(self):
        for ctype, suffix in (("text/plain", ".md"), ("text/markdown", ".md"),
                              ("application/json", ".json"), ("text/csv", ".csv")):
            with self.subTest(ctype=ctype):
                self.seed(suffix)
                before = self.snapshot()
                with patch.object(fetch, "download", return_value=(b"Short response", ctype)):
                    self.assertEqual(self.run_fetch(), 1)
                self.assert_preserved(before)
        self.assertIn("FAIL\tpaper\t", self.stdout.getvalue())

    def test_html_wall_retry_preserves_existing_source(self):
        self.seed(".md")
        before = self.snapshot()
        body = b"<article>captcha " + b"Wall text " * 150 + b"</article>"
        with patch.object(fetch, "download", return_value=(body, "text/html")):
            self.assertEqual(self.run_fetch(), 1)
        self.assert_preserved(before)

    def test_short_pdf_retry_preserves_both_files(self):
        self.seed(".pdf", ".txt")
        before = self.snapshot()
        with patch.object(fetch, "download", return_value=(b"%PDF-1.4\nNew PDF", "application/pdf")), \
                patch.object(fetch, "pdf_to_text", side_effect=self.pdf_text("Short text " * 30)):
            self.assertEqual(self.run_fetch(), 1)
        self.assert_preserved(before)

    def test_short_youtube_retry_preserves_existing_transcript(self):
        self.seed(".md")
        before = self.snapshot()
        with patch.object(fetch, "youtube_to_md", side_effect=self.markdown("Short transcript")):
            self.assertEqual(self.run_fetch("https://www.youtube.com/watch?v=example"), 1)
        self.assert_preserved(before)

    def test_download_error_preserves_existing_source(self):
        self.seed(".md")
        before = self.snapshot()
        with patch.object(fetch, "download", side_effect=OSError("Connection failed")):
            self.assertEqual(self.run_fetch(), 1)
        self.assert_preserved(before)

    def test_conversion_exception_preserves_both_files(self):
        self.seed(".pdf", ".txt")
        before = self.snapshot()
        with patch.object(fetch, "download", return_value=(b"%PDF-1.4\nNew PDF", "application/pdf")), \
                patch.object(fetch, "pdf_to_text", side_effect=FileNotFoundError("pdftotext")):
            with self.assertRaises(FileNotFoundError):
                self.run_fetch()
        self.assert_preserved(before)

    def test_failed_arxiv_fallback_preserves_existing_files(self):
        self.seed(".pdf", ".txt", ".md")
        before = self.snapshot()
        with patch.object(fetch, "download", return_value=(b"%PDF-1.4\nNew PDF", "application/pdf")), \
                patch.object(fetch, "pdf_to_text", side_effect=self.pdf_text("")), \
                patch.object(fetch, "html_to_md", side_effect=self.markdown("Short HTML")) as html:
            self.assertEqual(self.run_fetch("https://arxiv.org/pdf/2601.12345v2"), 1)
        self.assertEqual(html.call_args.args[0], "https://arxiv.org/html/2601.12345v2")
        self.assert_preserved(before)

    def test_failed_redirected_pdf_preserves_existing_files(self):
        self.seed(".md", ".pdf", ".txt")
        before = self.snapshot()

        def invalid_pdf(url, out):
            out.write_bytes(b"Not a PDF")
            return True

        with patch.object(fetch, "download", return_value=(b"<html>Redirect</html>", "text/html")), \
                patch.object(fetch, "html_to_md", side_effect=self.markdown("%PDF")), \
                patch.object(fetch, "ax_save", side_effect=invalid_pdf):
            self.assertEqual(self.run_fetch(), 1)
        self.assert_preserved(before)

    def test_successful_text_retry_replaces_source_and_reports_final_path(self):
        for ctype, suffix in (("text/plain", ".md"), ("text/markdown", ".md"),
                              ("application/json", ".json"), ("text/csv", ".csv")):
            with self.subTest(ctype=ctype):
                self.seed(suffix)
                body = b"New verified text\n" * 100
                with patch.object(fetch, "download", return_value=(body, ctype)):
                    self.assertEqual(self.run_fetch(), 0)
                target = self.sources / f"paper{suffix}"
                self.assertEqual(target.read_text(encoding="utf-8"), body.decode())
                self.assertIn(f"OK\tpaper\t{target}\t", self.stdout.getvalue())
                self.assertFalse(any(p.is_dir() for p in self.sources.iterdir()))
                self.assertEqual((self.sources / "unrelated.md").read_bytes(), b"Unrelated source\n")

    def test_successful_html_retry_saves_verified_text(self):
        self.seed(".md")
        body = b"<article>" + b"New article text " * 100 + b"</article>"
        with patch.object(fetch, "download", return_value=(body, "text/html")):
            self.assertEqual(self.run_fetch(), 0)
        target = self.sources / "paper.md"
        self.assertIn("New article text", target.read_text(encoding="utf-8"))
        self.assertIn(f"OK\tpaper\t{target}\t", self.stdout.getvalue())
        self.assertFalse(any(p.is_dir() for p in self.sources.iterdir()))

    def test_successful_pdf_retry_replaces_both_files(self):
        self.seed(".pdf", ".txt")
        pdf = b"%PDF-1.4\nNew verified PDF"
        text = "New verified PDF text\n" * 200
        with patch.object(fetch, "download", return_value=(pdf, "application/pdf")), \
                patch.object(fetch, "pdf_to_text", side_effect=self.pdf_text(text)):
            self.assertEqual(self.run_fetch(), 0)
        self.assertEqual((self.sources / "paper.pdf").read_bytes(), pdf)
        target = self.sources / "paper.txt"
        self.assertEqual(target.read_text(encoding="utf-8"), text)
        self.assertIn(f"OK\tpaper\t{target}\t", self.stdout.getvalue())
        self.assertFalse(any(p.is_dir() for p in self.sources.iterdir()))

    def test_successful_youtube_retry_replaces_transcript(self):
        self.seed(".md")
        text = "[00:00] New transcript\n" * 30
        with patch.object(fetch, "youtube_to_md", side_effect=self.markdown(text)):
            self.assertEqual(self.run_fetch("https://www.youtube.com/watch?v=example"), 0)
        target = self.sources / "paper.md"
        self.assertEqual(target.read_text(encoding="utf-8"), text)
        self.assertIn(f"OK\tpaper\t{target}\t", self.stdout.getvalue())
        self.assertFalse(any(p.is_dir() for p in self.sources.iterdir()))

    def test_failed_first_fetch_leaves_no_partial_source(self):
        with patch.object(fetch, "download", return_value=(b"Short response", "text/plain")):
            self.assertEqual(self.run_fetch(), 1)
        self.assertEqual(list(self.sources.iterdir()), [])

    def test_non_http_url_is_rejected_without_reading(self):
        secret = Path(self.sources.parent) / "secret.txt"
        secret.write_text("local secret\n" * 200, encoding="utf-8")
        for url in (secret.as_uri(), "FILE://" + secret.as_posix(),
                    "ftp://example.test/paper.txt", "-o/tmp/x"):
            with self.subTest(url=url):
                with patch.object(fetch, "download") as download:
                    self.assertEqual(self.run_fetch(url), 1)
                download.assert_not_called()
                self.assertEqual(list(self.sources.iterdir()), [])
        self.assertIn("http(s) の URL だけを取りに行く", self.stdout.getvalue())


class OptionInjectionTests(unittest.TestCase):
    """外のコマンドへ渡す URL は「--」の直後に置く。「-」で始まる URL をオプションとして読ませない。"""

    URL = "--exec=touch /tmp/pwned"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name) / "page.md"

    def capture(self, call):
        with patch.object(fetch.subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            call()
        return [c.args[0] for c in run.call_args_list]

    def assert_url_after_separator(self, cmds):
        self.assertTrue(cmds)
        for cmd in cmds:
            with self.subTest(cmd=cmd):
                self.assertEqual(cmd[-2:], ["--", self.URL])

    def test_ax_markdown(self):
        with patch.object(fetch, "HAS_AX", True):
            self.assert_url_after_separator(self.capture(lambda: fetch.html_to_md(self.URL, self.out)))

    def test_ax_save(self):
        with patch.object(fetch, "HAS_AX", True):
            self.assert_url_after_separator(self.capture(lambda: fetch.ax_save(self.URL, self.out)))

    def test_yt_dlp(self):
        self.assert_url_after_separator(self.capture(lambda: fetch.youtube_to_md(self.URL, self.out)))


class YoutubeCaptionTests(unittest.TestCase):
    """yt-dlp の呼び出しを差し替えて、どの字幕を指定するかを検査する。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name) / "talk.md"

    def run_youtube(self, language, manual=None, auto=None):
        """manual と auto は動画が持つ字幕（言語 → 文）。指定されて、かつ有るものだけを .vtt に書く。"""
        asked = []

        def yt_dlp(cmd, **kwargs):
            flag = next(c for c in cmd if c.startswith("--write-"))
            langs = cmd[cmd.index("--sub-langs") + 1]
            asked.append((flag, langs))
            tracks = (auto if flag == "--write-auto-subs" else manual) or {}
            folder = Path(cmd[cmd.index("-o") + 1]).parent
            for lang in langs.split(","):
                if lang in tracks:
                    (folder / f"v.{lang}.vtt").write_text(
                        f"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n{tracks[lang]}\n", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, f"Talk\tSpeaker\t20220820\t{language}\n", "")

        with patch.object(fetch.subprocess, "run", side_effect=yt_dlp):
            body = fetch.youtube_to_md("https://www.youtube.com/watch?v=example", self.out)
        return asked, body

    def test_auto_captions_ask_only_the_spoken_language(self):
        # 吹き替えの音声にも ja-orig が付く。中身は翻訳なので取らない
        asked, body = self.run_youtube("en-US", auto={"en-orig": "English speech", "ja-orig": "吹き替え"})
        self.assertEqual(asked, [("--write-subs", "ja,en"), ("--write-auto-subs", "en-orig")])
        self.assertEqual(body, "[00:01] English speech")
        text = self.out.read_text(encoding="utf-8")
        self.assertIn("\nSpeaker / 20220820\n字幕: 自動", text)
        self.assertIn("v.en-orig.vtt", text)

    def test_japanese_auto_captions(self):
        asked, body = self.run_youtube("ja", auto={"ja-orig": "日本語の音声"})
        self.assertEqual(asked[-1], ("--write-auto-subs", "ja-orig"))
        self.assertEqual(body, "[00:01] 日本語の音声")

    def test_unknown_language_asks_original_japanese_or_english(self):
        asked, body = self.run_youtube("", auto={"en-orig": "English speech"})
        self.assertEqual(asked[-1], ("--write-auto-subs", "ja-orig,en-orig"))
        self.assertEqual(body, "[00:01] English speech")

    def test_manual_captions_skip_auto_captions(self):
        asked, body = self.run_youtube("en", manual={"ja": "手動の日本語"}, auto={"en-orig": "English"})
        self.assertEqual(asked, [("--write-subs", "ja,en")])
        self.assertEqual(body, "[00:01] 手動の日本語")
        self.assertIn("字幕: 手動 v.ja.vtt", self.out.read_text(encoding="utf-8"))


class CjkPdfFloorTests(unittest.TestCase):
    """和文の PDF は 3,000 字に届かなくても、HTML と同じ下限を超えていれば残す。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sources = Path(tmp.name) / "sources"
        self.sources.mkdir()
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        stack.enter_context(patch.object(fetch, "missing", return_value=[]))

    def fetch_pdf(self, text):
        def convert(pdf):
            pdf.with_suffix(".txt").write_text(text, encoding="utf-8")
            return text
        argv = [str(SCRIPT), str(self.sources), "paper", "https://example.test/paper.pdf"]
        with patch.object(sys, "argv", argv), \
                patch.object(fetch, "download", return_value=(b"%PDF-1.4\nshort", "application/pdf")), \
                patch.object(fetch, "pdf_to_text", side_effect=convert):
            return fetch.main()

    def test_short_japanese_pdf_is_kept(self):
        text = "日本語の本文です。\n" * 150  # 1,500字
        self.assertLess(len(text), fetch.MIN_TEXT_CHARS)
        self.assertEqual(self.fetch_pdf(text), 0)
        self.assertEqual((self.sources / "paper.txt").read_text(encoding="utf-8"), text)

    def test_short_english_pdf_still_fails(self):
        text = "English body text.\n" * 120  # 2,280字
        self.assertGreater(len(text), fetch.MIN_HTML_CHARS)
        self.assertEqual(self.fetch_pdf(text), 1)
        self.assertEqual(list(self.sources.iterdir()), [])

    def test_very_short_japanese_pdf_still_fails(self):
        text = "日本語。\n" * 150  # 750字
        self.assertEqual(self.fetch_pdf(text), 1)
        self.assertEqual(list(self.sources.iterdir()), [])

    def test_english_pdf_with_japanese_heading_is_not_cjk(self):
        text = "日本語の見出し\n" + "English body text.\n" * 100
        self.assertFalse(fetch.mostly_cjk(text))
        self.assertTrue(fetch.mostly_cjk("全角の１２３"))


class NonAsciiUrlTests(unittest.TestCase):
    """日本語の字をそのまま含む URL も、符号化して取りに行く（イシュー #9）。"""

    def requested(self, url):
        seen = []

        def open_(req, timeout):
            seen.append(req.full_url)
            res = unittest.mock.MagicMock()
            res.__enter__.return_value.read.return_value = b"body"
            return res

        with patch.object(fetch._OPENER, "open", side_effect=open_):
            fetch.download(url)
        return seen[0]

    def test_japanese_path_and_query_are_encoded(self):
        self.assertEqual(self.requested("https://kotobank.jp/word/漆工芸"),
                         "https://kotobank.jp/word/%E6%BC%86%E5%B7%A5%E8%8A%B8")
        self.assertEqual(self.requested("https://example.test/search?q=和竿&page=2"),
                         "https://example.test/search?q=%E5%92%8C%E7%AB%BF&page=2")

    def test_encoded_url_is_unchanged(self):
        for url in ("https://kotobank.jp/word/%E6%BC%86%E5%B7%A5%E8%8A%B8",
                    "https://www.youtube.com/watch?v=jPCV4GKX9Dw&t=30s",
                    "https://example.test/a%20b/c?x=1+2&y=%2F"):
            with self.subTest(url=url):
                self.assertEqual(self.requested(url), url)


if __name__ == "__main__":
    unittest.main()
