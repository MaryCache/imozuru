"""外部の文や URL が混ざっても markdown を安全に表示する。"""

import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "skills/imozuru/scripts/skin.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("test_skin", SCRIPT)
skin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(skin)


class MarkdownTests(unittest.TestCase):
    def test_malformed_urls_in_map_render_as_text(self):
        for url in ("https://example.test＃evil/", "https://example.test：443/", "https://[example.test/"):
            for text in (f"# 地図\n\n[資料]({url})", f"# 地図\n\n{url}"):
                with self.subTest(text=text):
                    page = skin.md_html(text)
                    self.assertIn(url, page)
                    self.assertNotIn("<a ", page)

    def test_nuls_cannot_forge_code_placeholders(self):
        for text, expected in (("\x000\x00", "<p>0</p>"),
                               ("`code` \x0099\x00", "<p><code>code</code> 99</p>")):
            with self.subTest(text=text):
                self.assertEqual(skin.md_html(text), expected)

    def test_external_links_open_outside_frame(self):
        for text in ("[資料](https://example.test/a?b=1&c=2)", "https://example.test/a?b=1&c=2"):
            with self.subTest(text=text):
                page = skin.md_html(text)
                self.assertIn('href="https://example.test/a?b=1&amp;c=2" target="_blank" rel="noopener noreferrer"', page)
        self.assertNotIn("<a ", skin.md_html("[危険](javascript:alert(1)) `https://example.test/code`"))


if __name__ == "__main__":
    unittest.main()
