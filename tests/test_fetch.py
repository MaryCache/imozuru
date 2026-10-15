"""再取得が失敗したときの原文保全と、成功時の保存先を検査する。"""

import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "skills/imozuru/scripts/fetch.py"
SPEC = importlib.util.spec_from_file_location("imozuru_fetch", SCRIPT)
fetch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fetch)


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


if __name__ == "__main__":
    unittest.main()
