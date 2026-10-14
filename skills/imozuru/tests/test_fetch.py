"""fetch.py が、取り直しに失敗しても保存済みの原文を残すかを確かめる（issue #1）。

ネットワークと外部コマンドは使わない。download・pdf_to_text・youtube_to_md などを差し替える。
実行: python3 -m unittest discover -s skills/imozuru/tests
"""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "fetch.py"
spec = importlib.util.spec_from_file_location("imozuru_fetch", SCRIPT)
fetch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch)

OLD = b"Original verified source\n" * 100
LONG = "Long enough body line.\n" * 200  # 下限（PDF 3,000 字・HTML 1,000 字）を超える長さ


def fake_pdf_to_text(text):
    """pdftotext の代わりに、決まった本文を .txt に書く。"""
    def run(pdf):
        out = pdf.with_suffix(".txt")
        out.write_text(text)
        return text
    return run


class FetchKeepsOriginal(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.sources = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def run_fetch(self, url="https://example.test/source", **patches):
        argv = [str(SCRIPT), str(self.sources), "paper", url]
        with patch.object(sys, "argv", argv), patch.multiple(fetch, **patches), \
                patch("builtins.print"):
            return fetch.main()

    def assert_no_work_dir(self):
        self.assertEqual([p.name for p in self.sources.iterdir() if p.name.startswith(".")], [])

    def test_short_text_keeps_old_md(self):
        (self.sources / "paper.md").write_bytes(OLD)
        code = self.run_fetch(download=lambda url: (b"Short response", "text/plain"))
        self.assertEqual(code, 1)
        self.assertEqual((self.sources / "paper.md").read_bytes(), OLD)
        self.assert_no_work_dir()

    def test_success_replaces_old_md(self):
        (self.sources / "paper.md").write_bytes(OLD)
        code = self.run_fetch(download=lambda url: (LONG.encode(), "text/plain"))
        self.assertEqual(code, 0)
        self.assertEqual((self.sources / "paper.md").read_text(), LONG)
        self.assert_no_work_dir()

    def test_short_pdf_keeps_old_pdf_and_txt(self):
        (self.sources / "paper.pdf").write_bytes(b"%PDF-old")
        (self.sources / "paper.txt").write_bytes(OLD)
        code = self.run_fetch(download=lambda url: (b"%PDF-new", "application/pdf"),
                              pdf_to_text=fake_pdf_to_text("x" * 500))
        self.assertEqual(code, 1)
        self.assertEqual((self.sources / "paper.pdf").read_bytes(), b"%PDF-old")
        self.assertEqual((self.sources / "paper.txt").read_bytes(), OLD)
        self.assert_no_work_dir()

    def test_pdf_success_moves_pdf_and_txt(self):
        code = self.run_fetch(download=lambda url: (b"%PDF-new", "application/pdf"),
                              pdf_to_text=fake_pdf_to_text(LONG))
        self.assertEqual(code, 0)
        self.assertEqual((self.sources / "paper.pdf").read_bytes(), b"%PDF-new")
        self.assertEqual((self.sources / "paper.txt").read_text(), LONG)
        self.assert_no_work_dir()

    def test_short_transcript_keeps_old_md(self):
        (self.sources / "paper.md").write_bytes(OLD)

        def short_subs(url, out):
            out.write_text("# title\n\nshort")
            return "short"
        code = self.run_fetch(url="https://www.youtube.com/watch?v=abc", youtube_to_md=short_subs)
        self.assertEqual(code, 1)
        self.assertEqual((self.sources / "paper.md").read_bytes(), OLD)
        self.assert_no_work_dir()

    def test_failed_arxiv_fallback_keeps_old_files(self):
        (self.sources / "paper.pdf").write_bytes(b"%PDF-old")
        (self.sources / "paper.txt").write_bytes(OLD)
        (self.sources / "paper.md").write_bytes(OLD)

        def short_html(url, out, body=None):
            out.write_text("short")
            return "short"
        code = self.run_fetch(url="https://arxiv.org/pdf/2401.00001",
                              download=lambda url: (b"%PDF-new", "application/pdf"),
                              pdf_to_text=fake_pdf_to_text(""), ocr_pdf=lambda pdf: "",
                              html_to_md=short_html)
        self.assertEqual(code, 1)
        for f in ("paper.txt", "paper.md"):
            self.assertEqual((self.sources / f).read_bytes(), OLD)
        self.assertEqual((self.sources / "paper.pdf").read_bytes(), b"%PDF-old")
        self.assert_no_work_dir()

    def test_failed_pdf_redirect_keeps_old_md(self):
        (self.sources / "paper.md").write_bytes(OLD)
        code = self.run_fetch(download=lambda url: (b"<html></html>", "text/html"),
                              html_to_md=lambda url, out, body=None: (out.write_text("%PDF"), "%PDF")[1],
                              ax_save=lambda url, out: False)
        self.assertEqual(code, 1)
        self.assertEqual((self.sources / "paper.md").read_bytes(), OLD)
        self.assert_no_work_dir()


if __name__ == "__main__":
    unittest.main()
