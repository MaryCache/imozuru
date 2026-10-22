"""書き出すページの色が palette.py の1か所から来ているかを検査する。

ページの側に色の値を直接書くと、同じ役割の色が場所ごとにずれていく（2026-10-21 かしぇ「色管理徹底しよう」）。"""

import importlib.util
import re
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/imozuru/scripts"
sys.path.insert(0, str(SCRIPTS))


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


palette = load("palette")
dash = load("dashboard")
report = load("report")
digest = load("digest")

OKLCH = re.compile(r"oklch\([^)]*\)")
HEX_COLOR = re.compile(r"[:\s,(]#[0-9a-fA-F]{3,8}\b")
NAMED_COLOR = re.compile(r":\s*(white|black|gray|grey|red|blue|green)\b")


class PaletteTests(unittest.TestCase):
    def assert_from_palette(self, text, where):
        allowed = palette.all_values()
        stray = sorted(set(OKLCH.findall(text)) - allowed)
        self.assertEqual(stray, [], f"{where}: palette.py に無い色")
        self.assertIsNone(HEX_COLOR.search(text), f"{where}: 16進の色")
        self.assertIsNone(NAMED_COLOR.search(text), f"{where}: 名前の色")

    def test_document_css(self):
        self.assert_from_palette(report.CSS, "report.CSS")
        self.assert_from_palette(digest.CSS, "digest.CSS")

    def test_dashboard_page(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a-topic").mkdir()
            self.assert_from_palette(dash.render(dash.collect(Path(d), []), Path(d)), "dashboard")

    def test_back_button(self):
        self.assert_from_palette(dash.with_back("map.md", b"# map", "a").decode(), "map.md")
        self.assert_from_palette(dash.with_back("digest.html", b"<body></body>", "a").decode(), "digest.html")

    def test_roles_point_at_swatches(self):
        for roles in (palette.DASH, palette.DOC_LIGHT, palette.DOC_DARK, palette.HUD_LIGHT, palette.HUD_DARK):
            for k, v in roles.items():
                self.assertIn(v, palette.SWATCH, k)


if __name__ == "__main__":
    unittest.main()
