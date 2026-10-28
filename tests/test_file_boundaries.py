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

    def test_report_matches_extracts_across_bullet_lines(self):
        """箇条書きの何行かにまたがる抜き書きを、印を除いても、印ごと写しても通す。言い換えは通さない。"""
        (self.sources / "list.md").write_text(
            "# 手順\n\n- 一つ目の項目です\n- 二つ目の項目です\n* 三つ目の項目です\n", encoding="utf-8")
        cases = {
            "一つ目の項目です 二つ目の項目です": True,
            "一つ目の項目です\n二つ目の項目です\n三つ目の項目です": True,
            "一つ目の項目です - 二つ目の項目です": True,
            "一つ目の項目です 三つ目の項目です": False,
            "一つ目の項目でした 二つ目の項目です": False,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                q = {"file": "list.md", "line": 3, "end": 5, "text": text}
                self.assertEqual(report.verify(self.sources, q)[0], expected)

    def test_report_keeps_line_start_minus_in_text_sources(self):
        """PDF から起こした .txt の行頭の「- 」は負号のことがある。負号を落とした抜き書きは通さない。"""
        (self.sources / "paper.txt").write_text("変化量は\n- 5 mgです。\n", encoding="utf-8")
        for text, expected in (("変化量は 5 mgです。", False), ("変化量は - 5 mgです。", True)):
            with self.subTest(text=text):
                q = {"file": "paper.txt", "line": 1, "end": 2, "text": text}
                self.assertEqual(report.verify(self.sources, q)[0], expected)

    def test_report_keeps_line_start_minus_in_markdown_sources(self):
        """text/plain も .md で保存される。.md でも、2行目の行頭の負号を落とした抜き書きは通さない（#19）。"""
        (self.sources / "measurement.md").write_text("Change is\n- 5 mg.\n", encoding="utf-8")
        for text, expected in (("Change is 5 mg.", False), ("Change is - 5 mg.", True)):
            with self.subTest(text=text):
                q = {"file": "measurement.md", "line": 1, "end": 2, "text": text}
                self.assertEqual(report.verify(self.sources, q)[0], expected)

    def test_report_keeps_minus_on_first_line(self):
        """原文の1行目が「- 5 mg.」のとき、負号の後ろから写した抜き書きは .txt でも .md でも通さない（#19）。"""
        for name in ("first.txt", "first.md"):
            (self.sources / name).write_text("- 5 mg.\n", encoding="utf-8")
            for text, expected in (("5 mg.", False), ("- 5 mg.", True)):
                with self.subTest(name=name, text=text):
                    q = {"file": name, "line": 1, "text": text}
                    self.assertEqual(report.verify(self.sources, q)[0], expected)
        # PDF の負号は U+2212 のことがある。「…」で区切った後ろの区切りも同じ扱い
        (self.sources / "unicode-minus.txt").write_text("Change is −5 mg.\n", encoding="utf-8")
        q = {"file": "unicode-minus.txt", "line": 1, "text": "Change is … 5 mg."}
        self.assertFalse(report.verify(self.sources, q)[0])

    def test_report_needs_marker_for_list_items_starting_with_digits(self):
        """数字で始まる Markdown の項目は、負号と見分けられないので「- 」ごと写したときだけ通す。"""
        (self.sources / "reasons.md").write_text("# Why\n\n- 5 reasons to try\n- Easy setup\n", encoding="utf-8")
        cases = {"- 5 reasons to try Easy setup": True, "5 reasons to try Easy setup": False,
                 "Easy setup": True}
        for text, expected in cases.items():
            with self.subTest(text=text):
                q = {"file": "reasons.md", "line": 3, "end": 4, "text": text}
                self.assertEqual(report.verify(self.sources, q)[0], expected)

    def test_report_accepts_numbers_after_hyphens_inside_words(self):
        """英数字の直後のハイフンは語の一部。その後ろから写した抜き書きは通す（2.1.0 で通っていた）。
        2段組の PDF では、行末で切った語「period-」の直後に左の段の数が来る"""
        (self.sources / "words.txt").write_text(
            "COVID-19 patients were enrolled.\nAged 18-65 years.\n"
            "over a 40-day period-            0% to 30% of runs\n", encoding="utf-8")
        for line, text in ((1, "19 patients were enrolled."), (2, "65 years."), (3, "0% to 30% of runs")):
            with self.subTest(text=text):
                self.assertTrue(report.verify(self.sources, {"file": "words.txt", "line": line, "text": text})[0])

    def test_report_minus_needs_a_number_after_it(self):
        """「.」だけでは数にしない。「- .NET 8」は箇条書きの印を落として写しても通し、「- .5 mg」は通さない。"""
        (self.sources / "dotnet.md").write_text("# Notes\n\n- .NET 8 adds native AOT\n- .5 mg daily\n",
                                                 encoding="utf-8")
        cases = {(3, ".NET 8 adds native AOT"): True, (4, ".5 mg daily"): False, (4, "- .5 mg daily"): True}
        for (line, text), expected in cases.items():
            with self.subTest(text=text):
                q = {"file": "dotnet.md", "line": line, "text": text}
                self.assertEqual(report.verify(self.sources, q)[0], expected)

    def test_report_keeps_fullwidth_minus_and_en_dash(self):
        """全角の負号と en dash も負号として扱う。和文の「は－5」は語の中のハイフンにしない。"""
        (self.sources / "signs.txt").write_text(
            "気温は－5度だった。\nChange was –3 points.\nSeasons 2019–2020 ended.\n", encoding="utf-8")
        cases = {(1, "5度だった。"): False, (1, "気温は－5度だった。"): True,
                 (2, "3 points."): False, (3, "2020 ended."): True}
        # 空白をはさむ en dash は範囲か挿入のダッシュ。% の後ろも範囲
        (self.sources / "dashes.txt").write_text(
            "Seasons 2019 \u2013 2020 ended.\nThe rate \u2013 30% of patients \u2013 rose.\nRates rose 50%\u201360% overall.\n",
            encoding="utf-8")
        for line, text in ((1, "2020 ended."), (2, "30% of patients \u2013 rose."), (3, "60% overall.")):
            with self.subTest(text=text):
                self.assertTrue(report.verify(self.sources, {"file": "dashes.txt", "line": line, "text": text})[0])
        for (line, text), expected in cases.items():
            with self.subTest(text=text):
                q = {"file": "signs.txt", "line": line, "text": text}
                self.assertEqual(report.verify(self.sources, q)[0], expected)

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
