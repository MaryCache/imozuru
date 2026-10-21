"""取得名と抜き書きの参照先が sources/ の境界を守ることを検査する。"""

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "skills/imozuru/scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(f"boundary_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fetch = load("fetch")
report = load("report")
# テストの宛先（example.test）は名前を引けない。インターネット上の宛先に引けたことにする
fetch.resolve = lambda host: {"93.184.215.14"}


class FileBoundaryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.topic = Path(tmp.name) / "topic"
        self.sources = self.topic / "sources"
        self.sources.mkdir(parents=True)

    def quote(self, file, text="Original sentence"):
        return {"file": file, "line": 1, "text": text}

    def test_fetch_rejects_unsafe_names_before_side_effects(self):
        destination = self.topic / "saved"
        destination.mkdir()
        original = destination / "outside.md"
        original.write_text("Keep this source", encoding="utf-8")
        for name in ("", ".", "..", "...", "   ", "../outside", "a/b", "a\\b",
                     "/outside", "C:\\outside", "C:outside", "CON", "a\x00b", "a\nb"):
            with self.subTest(name=name), patch.object(fetch, "download") as download:
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(fetch.fetch_source(self.sources, name, "https://example.test/a", destination), 1)
                self.assertIn("FAIL\t", output.getvalue())
                download.assert_not_called()
                self.assertEqual(original.read_text(encoding="utf-8"), "Keep this source")
                self.assertEqual(list(self.sources.iterdir()), [])

    def test_fetch_accepts_author_year_unicode_name(self):
        name = "髙橋-2024-色彩の研究"
        body = b"Verified article text. " * 80
        with patch.object(fetch, "download", return_value=(body, "text/plain")):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(fetch.fetch_source(self.sources, name, "https://example.test/a", self.sources), 0)
        self.assertEqual((self.sources / f"{name}.md").read_bytes(), body)

    def test_report_rejects_outside_paths(self):
        outside = self.topic / "outside.md"
        outside.write_text("Original sentence", encoding="utf-8")
        for file in ("../outside.md", str(outside), "C:\\outside.md", "\x00"):
            with self.subTest(file=file):
                self.assertEqual(report.verify(self.sources, self.quote(file)), (False, [], 0, 0))

    def test_report_rejects_symlink_escapes(self):
        outside = self.topic / "outside.md"
        outside.write_text("Original sentence", encoding="utf-8")
        external = self.topic.parent / "external"
        external.mkdir()
        (external / "paper.md").write_text("Original sentence", encoding="utf-8")
        try:
            (self.sources / "link.md").symlink_to(outside)
            (self.topic / "linked-sources").symlink_to(external, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Symlinks unavailable: {exc}")
        self.assertEqual(report.verify(self.sources, self.quote("link.md")), (False, [], 0, 0))
        self.assertEqual(report.verify(self.topic / "linked-sources", self.quote("paper.md")),
                         (False, [], 0, 0))

    def test_report_rejects_empty_extracts_and_accepts_nested_source(self):
        nested = self.sources / "archive"
        nested.mkdir()
        (nested / "paper.md").write_text("Original sentence", encoding="utf-8")
        for text in ("", " \t\n ", "…", " …  … "):
            with self.subTest(text=text):
                self.assertEqual(report.verify(self.sources, self.quote("archive/paper.md", text))[0], False)
        ok, context, _, _ = report.verify(self.sources, self.quote("archive/paper.md"))
        self.assertTrue(ok)
        self.assertIn("Original sentence", context)

    def test_failed_report_keeps_prior_content(self):
        (self.sources / "paper.md").write_text("Original sentence", encoding="utf-8")
        data = {"title": "Test", "question": "Q", "stopped": "Done", "claims": [
            {"claim": "Claim", "status": "根拠あり", "quotes": [self.quote("paper.md", "…")]}]}
        (self.topic / "extracts.json").write_text(json.dumps(data), encoding="utf-8")
        prior = self.topic / "report.html"
        prior.write_text("Previous report", encoding="utf-8")
        with patch.object(sys, "argv", ["report.py", str(self.topic)]), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(report.main(), 1)
        self.assertEqual(prior.read_text(encoding="utf-8"), "Previous report")


if __name__ == "__main__":
    unittest.main()
