"""履歴のページ（dashboard.py）の数え方と、記録の無い調査の扱いを検査する。"""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "skills/imozuru/scripts"
spec = importlib.util.spec_from_file_location("dash", SCRIPTS / "dashboard.py")
dash = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dash)

RUNS = """| 日付 | 話題 | 型 | 回した人 | 調査を区切った理由 | 周 | 原文 | 感覚と合った？ |
|---|---|---|---|---|---|---|---|
| 2026-10-09 | 古い形（old-topic） | 掘り | モネ | 論点の被覆 | 2 | 3 | 「いいね」 |
| 2026-10-20 | new-topic | 掘り | 論点6/6に原文 | 2 | 4 | 未 |
| 2026-10-21 | new-topic | 見張り | 1回目の check | — | 新着9 | 未 |
"""


class DashboardTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        t = self.root / "new-topic"
        (t / "sources").mkdir(parents=True)
        for name in ("a.pdf", "a.txt", "b.md"):
            (t / "sources" / name).write_text("x", encoding="utf-8")
        (t / "map.md").write_text(
            "# map: new-topic\n\n問い: 夕焼けはなぜ赤いか\n\n## 調査の論点\n- [x] 仕組み\n- [ ] 原典\n\n"
            "## 原文（sources/）\n| 資料 | ファイル | 元の URL | 周 |\n|---|---|---|---|\n"
            "| A | a.pdf | https://www.example.test/a.pdf | 1周 |\n| B | b.md | https://blog.example.test/b | 1周 |\n",
            encoding="utf-8")
        (t / "log.md").write_text("# log\n\n## 0周目（2026-10-20）\n\n## 1周目\n\n## 2周目\n", encoding="utf-8")
        (t / "digest.html").write_text("<p>x</p>", encoding="utf-8")
        (self.root / "empty-topic").mkdir()
        self.runs_file = self.root / "runs.md"
        self.runs_file.write_text(RUNS, encoding="utf-8")

    def collect(self):
        return {t["slug"]: t for t in dash.collect(self.root, dash.load_runs(self.runs_file))}

    def test_counts_rounds_sources_and_hosts(self):
        t = self.collect()["new-topic"]
        self.assertEqual(t["rounds"], 2)          # 0周目は数えない
        self.assertEqual(t["sources"], 2)         # a.pdf と a.txt は1本
        self.assertEqual(sorted(t["hosts"]), ["blog.example.test", "example.test"])
        self.assertEqual(t["claims"], [("仕組み", "根拠あり"), ("原典", "未確認")])
        self.assertEqual(t["date"], "2026-10-20")

    def test_ledger_row_without_runner_column_and_watch_rows(self):
        t = self.collect()["new-topic"]
        # 「回した人」の列が無い行も列をずらさずに読み、見張りの行より掘った行を使う
        self.assertEqual(t["run"]["周"], "2")
        self.assertEqual(t["reason_class"], "論点に根拠がそろった")

    def test_topic_without_records_is_marked_not_zero(self):
        t = self.collect()["empty-topic"]
        self.assertIsNone(t["rounds"])
        self.assertIsNone(t["searches"])
        self.assertEqual(t["reason_class"], "記録なし")
        page = dash.render(list(self.collect().values()), self.root)
        self.assertIn("記録なし", page)

    def test_reason_classes(self):
        self.assertEqual(dash.classify("約束で打ち切り（壁が多い）"), "約束で打ち切り")
        self.assertEqual(dash.classify("2周で区切った（要の原典5本が出版社のボット対策で取れない）"), "取得の壁")
        self.assertEqual(dash.classify("1周目まで（論点3/5に原文）"), "一部の論点を残して区切った")
        self.assertEqual(dash.classify("論点6/6に原文（道具の比較は一部だけ）"), "論点に根拠がそろった")

    def test_page_escapes_text_and_runs_only_the_hashed_script(self):
        import base64
        import hashlib
        (self.root / "new-topic" / "map.md").write_text("問い: <script>alert(1)</script>\n", encoding="utf-8")
        page = dash.render(list(self.collect().values()), self.root)
        self.assertNotIn("<script>alert", page)
        body = page.split("</head>", 1)[1]
        self.assertEqual(body.count("<script"), 1)
        self.assertIn(f"<script>{dash.SIDE_JS}</script>", body)
        h = base64.b64encode(hashlib.sha256(dash.SIDE_JS.encode()).digest()).decode()
        self.assertIn(f"script-src 'sha256-{h}'", page)
        self.assertIn("default-src 'none'", page)

    def test_works_without_ledger(self):
        topics = dash.collect(self.root, dash.load_runs(None))
        self.assertTrue(dash.render(topics, self.root).startswith("<!doctype html>"))


if __name__ == "__main__":
    unittest.main()
