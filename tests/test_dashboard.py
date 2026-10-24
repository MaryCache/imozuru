"""履歴のページ（dashboard.py）の数え方と、記録の無い調査の扱いを検査する。"""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


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

    def request(self, path="/?k=kk", headers=None, handler=None):
        import io
        from types import SimpleNamespace

        class Request:
            def __init__(self, data):
                self.data = data
                self.output = bytearray()

            def makefile(self, *a):
                return io.BytesIO(self.data)

            def sendall(self, data):
                self.output.extend(data)

        headers = {"Host": "127.0.0.1:8765", **(headers or {})}
        data = f"GET {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n"
        request = Request(data.encode("latin-1"))
        handler = handler or dash.make_handler(self.root, [], "kk")
        handler(request, ("127.0.0.1", 1), SimpleNamespace(server_port=8765))
        head, _, body = bytes(request.output).partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        return int(lines[0].split()[1]), dict(line.split(": ", 1) for line in lines[1:]), body

    def test_malformed_source_urls_still_render_and_serve(self):
        (self.root / "new-topic" / "map.md").write_text(
            "問い: 空\n\n## 原文\n| A | https://example.com＃frag |\n"
            "| B | https://[x |\n| C | https://example.com：443 |\n| D | https://good.test/a |\n",
            encoding="utf-8")
        self.assertEqual(self.collect()["new-topic"]["hosts"], ["good.test"])
        self.assertIn("<!doctype html>", dash.render(list(self.collect().values()), self.root))
        self.assertEqual(self.request()[0], 200)

    def test_symlinked_topics_and_documents_are_not_collected_served_or_bundled(self):
        import contextlib, io
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp)
            (outside / "map.md").write_text("問い: 外部の秘密\n", encoding="utf-8")
            (self.root / "linked-topic").symlink_to(outside, target_is_directory=True)
            topics = dash.collect(self.root, [])
            self.assertNotIn("linked-topic", [t["slug"] for t in topics])
            self.assertEqual(self.request("/linked-topic/map.md?k=kk")[0], 404)
            for name in ("map.md", "log.md", "digest.html", "report.html", "queries.jsonl", "digest.json"):
                path = self.root / "new-topic" / name
                path.unlink(missing_ok=True)
                path.symlink_to(outside / "map.md")
            topics = dash.collect(self.root, [])
            topic = next(t for t in topics if t["slug"] == "new-topic")
            self.assertEqual(topic["question"], "")
            self.assertEqual(topic["date"], "")
            self.assertEqual(topic["docs"], [])
            for name in ("map.md", "log.md", "digest.html", "report.html"):
                self.assertEqual(self.request(f"/new-topic/{name}?k=kk")[0], 404)
            dest = outside / "bundle"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(dash.write_bundle(topics, self.root, dest), 0)
            self.assertFalse((dest / "linked-topic").exists())
            self.assertFalse((dest / "new-topic").exists())
            self.assertNotIn("外部の秘密", (dest / "index.html").read_text(encoding="utf-8"))

    def test_bundle_rejects_destination_symlinks_before_writing(self):
        import contextlib, io
        (self.root / "new-topic" / "report.html").write_text("<p>x</p>", encoding="utf-8")
        for relative in ("index.html", "new-topic", "new-topic/map.md", "new-topic/log.md",
                         "new-topic/digest.html", "new-topic/report.html", dash.ICON.name, "."):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as tmp:
                outside = Path(tmp) / "outside"
                outside.mkdir()
                marker = outside / "secret"
                marker.write_text("変えない", encoding="utf-8")
                dest = Path(tmp) / "bundle"
                target = dest / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(outside if relative in (".", "new-topic") else marker)
                topics = dash.collect(self.root, [])
                with self.assertRaisesRegex(ValueError, "シンボリックリンク"):
                    dash.write_bundle(topics, self.root, dest)
                self.assertEqual(marker.read_text(encoding="utf-8"), "変えない")
                self.assertEqual(list(outside.iterdir()), [marker])
                if relative != "index.html":
                    self.assertFalse((dest / "index.html").exists())
                err = io.StringIO()
                with patch("sys.argv", ["dashboard.py", str(self.root), "--bundle", str(dest)]), contextlib.redirect_stderr(err):
                    self.assertEqual(dash.main(), 2)
                self.assertIn("シンボリックリンク", err.getvalue())

    def test_index_write_ignores_old_temp_symlink_and_cleans_up_on_error(self):
        import contextlib, io
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "secret"
            outside.write_text("変えない", encoding="utf-8")
            old_tmp = self.root / ".index.html.tmp"
            old_tmp.symlink_to(outside)
            with patch("sys.argv", ["dashboard.py", str(self.root)]), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(dash.main(), 0)
            index = self.root / "index.html"
            page = index.read_text(encoding="utf-8")
            self.assertEqual(page, dash.render(dash.collect(self.root, []), self.root))
            self.assertEqual(outside.read_text(encoding="utf-8"), "変えない")
            with patch("sys.argv", ["dashboard.py", str(self.root)]), patch.object(dash.os, "replace", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    dash.main()
            self.assertEqual(index.read_text(encoding="utf-8"), page)
            self.assertEqual(list(self.root.glob(".index.html*.tmp")), [old_tmp])

    def test_non_ascii_query_and_cookie_fail_authentication(self):
        self.assertEqual(self.request("/?k=%C3%A9")[0], 403)
        self.assertEqual(self.request("/", {"Cookie": "imozuru=é"})[0], 403)
        status, headers, _ = self.request()
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertEqual(self.request("/", {"Cookie": "imozuru=kk"})[0], 200)

    def test_logging_without_path_does_not_crash_or_log_token(self):
        import contextlib, io
        handler = dash.make_handler(self.root, [], "kk")
        request = handler.__new__(handler)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            request.log_message("bad request")
            request.path = "/?k=secret"
            request.log_message("request", "GET", "403")
        self.assertNotIn("secret", err.getvalue())

    def test_short_serve_tokens_are_rejected_before_server_startup(self):
        import contextlib, io
        for token in ("", "short", "x" * 15):
            with self.subTest(token=token), patch.dict("os.environ", {"IMOZURU_SERVE_TOKEN": token}):
                with patch("http.server.ThreadingHTTPServer") as server, contextlib.redirect_stderr(io.StringIO()) as err:
                    self.assertEqual(dash.serve(self.root, [], 8765), 2)
                server.assert_not_called()
                self.assertIn("IMOZURU_SERVE_TOKEN は16文字以上にしてください", err.getvalue())

    def test_generated_and_long_serve_tokens_work(self):
        import contextlib, io, os
        for token in (None, "x" * 16):
            with self.subTest(token=token), patch.dict("os.environ"):
                os.environ.pop("IMOZURU_SERVE_TOKEN", None)
                if token is not None:
                    os.environ["IMOZURU_SERVE_TOKEN"] = token
                with patch("http.server.ThreadingHTTPServer") as server, contextlib.redirect_stdout(io.StringIO()) as out:
                    server.return_value.serve_forever.side_effect = KeyboardInterrupt
                    self.assertEqual(dash.serve(self.root, [], 8765), 0)
                given = out.getvalue().split("?k=", 1)[1].splitlines()[0]
                self.assertGreaterEqual(len(given), 16)
                self.assertEqual(self.request(f"/?k={given}", handler=server.call_args.args[1])[0], 200)

    def test_host_allowlist_runs_before_token_check(self):
        with patch.dict("os.environ", {"IMOZURU_SERVE_HOSTS": " Custom.Example.com , Fixed.Example.com:9443, "}):
            handler = dash.make_handler(self.root, [], "kk")
        for host in ("127.0.0.1:8765", "localhost:8765", "something.tail0a1b2c.ts.net",
                     "SOMETHING.tail0a1b2c.TS.NET:443", "custom.example.com", "CUSTOM.EXAMPLE.COM:9443",
                     "fixed.example.com:9443"):
            with self.subTest(host=host):
                self.assertEqual(self.request(headers={"Host": host}, handler=handler)[0], 200)
        for host in ("evil.example.com:8765", "something.ts.net.evil.com", "127.0.0.1:9999",
                     "localhost", "fixed.example.com:443", "something.ts.net:bad", ""):
            with self.subTest(host=host):
                status, _, body = self.request(headers={"Host": host}, handler=handler)
                self.assertEqual(status, 403)
                self.assertIn("Host", body.decode())
        status, _, body = self.request("/?k=wrong", {"Host": "localhost:8765"}, handler)
        self.assertEqual(status, 403)
        self.assertIn("合言葉", body.decode())
        status, _, body = self.request("/?k=%C3%A9", {"Host": "evil.example.com:8765"}, handler)
        self.assertEqual(status, 403)
        self.assertIn("Host", body.decode())

    def test_unsafe_slugs_cannot_enter_current_item_css(self):
        import re
        for slug in ('bad"].on{color:red}', "<style>", "bad\\slug", "bad name"):
            (self.root / slug).mkdir()
        topics = dash.collect(self.root, [])
        self.assertEqual({t["slug"] for t in topics}, {"new-topic", "empty-topic"})
        malicious = 'x</style><script>alert(1)</script>"'
        page = dash.render(topics + [{**topics[0], "slug": malicious}], self.root)
        styles = "".join(re.findall(r"<style>(.*?)</style>", page, re.S))
        self.assertNotIn(malicious, page)
        self.assertNotIn("</style>", styles)
        self.assertNotIn("alert(1)", styles)
        self.assertNotIn('<script>alert(1)</script>', page)
        self.assertIn(dash.esc(malicious), page)

    def test_percent_encoded_japanese_documents_and_traversal(self):
        from urllib.parse import quote
        topic = self.root / "夕焼け"
        topic.mkdir()
        (topic / "map.md").write_text("問い: 赤い空\n", encoding="utf-8")
        self.assertIn("夕焼け", self.collect())
        status, _, body = self.request(f"/{quote(topic.name)}/map.md?k=kk")
        self.assertEqual(status, 200)
        self.assertIn("赤い空", body.decode())
        for path in ("/..%2Fnew-topic/map.md", "/%2e%2e/new-topic/map.md",
                     f"/{quote(topic.name)}/..%2Fnew-topic/map.md", "/%252e%252e/map.md"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path + "?k=kk")[0], 404)

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
        self.assertEqual(t["reason_class"], "論点充足")

    def test_topic_without_records_is_marked_not_zero(self):
        t = self.collect()["empty-topic"]
        self.assertIsNone(t["rounds"])
        self.assertIsNone(t["searches"])
        self.assertEqual(t["reason_class"], "記録欠落")
        page = dash.render(list(self.collect().values()), self.root)
        self.assertIn("記録なし", page)

    def test_reason_classes(self):
        self.assertEqual(dash.classify("約束で打ち切り（壁が多い）"), "期限到達")
        self.assertEqual(dash.classify("2周で区切った（要の原典5本が出版社のボット対策で取れない）"), "取得不可")
        self.assertEqual(dash.classify("1周目まで（論点3/5に原文）"), "一部充足")
        self.assertEqual(dash.classify("論点6/6に原文（道具の比較は一部だけ）"), "論点充足")

    def test_page_escapes_text_and_runs_only_the_hashed_script(self):
        import base64
        import hashlib
        (self.root / "new-topic" / "map.md").write_text("問い: <script>alert(1)</script>\n", encoding="utf-8")
        page = dash.render(list(self.collect().values()), self.root)
        self.assertNotIn("<script>alert", page)
        body = page.split("</head>", 1)[1]
        # 動くのは一覧の開閉（SIDE_JS）と、入れる・送る・PDF（FEATURE_JS）の2つだけで、どちらも中身のハッシュで許したもの
        self.assertEqual(body.count("<script"), 2)
        hashes = []
        for js in (dash.SIDE_JS, dash.FEATURE_JS):
            self.assertIn(f"<script>{js}</script>", body)
            hashes.append("'sha256-" + base64.b64encode(hashlib.sha256(js.encode()).digest()).decode() + "'")
        self.assertIn("script-src " + " ".join(hashes) + ";", page)
        self.assertIn("default-src 'none'", page)

    def test_explanations_live_on_the_guide_section(self):
        topics = list(self.collect().values())
        page = dash.render(topics, self.root)
        home = dash.home_html(topics)
        # ホームには数え方や分類の規則を書かず、同じページの説明の節へのリンクだけを置く
        self.assertNotIn("自動分類", home)
        self.assertNotIn("新しい順", page.split('id="guide"')[0])
        self.assertIn('href="#guide"', page)
        guide = page.split('id="guide"', 1)[1]
        for label, _ in dash.REASON_RULES:
            self.assertIn(label, guide)

    def test_bundle_copies_documents_but_not_sources(self):
        import contextlib, io
        dest = self.root.parent / (self.root.name + "-bundle")
        self.addCleanup(lambda: __import__("shutil").rmtree(dest, ignore_errors=True))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(dash.write_bundle(dash.collect(self.root, []), self.root, dest), 0)
        self.assertTrue((dest / "index.html").is_file())
        self.assertTrue((dest / "new-topic" / "map.md").is_file())
        self.assertFalse((dest / "new-topic" / "sources").exists())
        # 束を調査の置き場の中に作ると、次の走査で束が調査の1件に見える
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(dash.write_bundle([], self.root, self.root / "b"), 2)

    def test_serve_returns_only_listed_documents(self):
        import threading, urllib.error, urllib.request
        from http.server import ThreadingHTTPServer
        (self.root / "new-topic" / "sources").mkdir(exist_ok=True)
        (self.root / "new-topic" / "sources" / "a.md").write_text("原文", encoding="utf-8")
        server = ThreadingHTTPServer(("127.0.0.1", 0), dash.make_handler(self.root, [], "kk"))
        server.RequestHandlerClass.log_message = lambda *a: None
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"

        def status(path):
            try:
                return urllib.request.urlopen(base + path).status
            except urllib.error.HTTPError as e:
                return e.code
        self.assertEqual(status("/"), 403)
        self.assertEqual(status("/?k=kk"), 200)
        self.assertEqual(status("/new-topic/map.md?k=kk"), 200)
        page = urllib.request.urlopen(base + "/new-topic/map.md?k=kk").read().decode()
        self.assertIn('class="imz-hud" href="../index.html#t-new-topic"', page)
        for bad in ("/new-topic/sources/a.md", "/new-topic/../new-topic/map.md", "/../etc/passwd", "/new-topic/x.md"):
            self.assertEqual(status(bad + "?k=kk"), 404, bad)
        # ホーム画面のアイコンだけは合言葉なしで返す（iPhone は cookie を送らないことがある）。ほかは合言葉が要るまま
        self.assertEqual(status("/apple-touch-icon.png"), 200)
        self.assertEqual(status("/new-topic/map.md"), 403)
        self.assertIn('rel="apple-touch-icon" href="apple-touch-icon.png"', urllib.request.urlopen(base + "/?k=kk").read().decode())

    def test_totals_are_the_first_plate_not_the_header(self):
        page = dash.render(dash.collect(self.root, []), self.root)
        header = page[page.index("<header>"):page.index("</header>")]
        self.assertNotIn("原文", header)
        home = page[page.index('<section class="home">'):]
        self.assertTrue(home.split(">", 1)[1].lstrip().startswith('<div class="card tally"><h1 class="plate-name">'))

    def test_search_index_takes_words_from_map_and_escapes_them(self):
        (self.root / "new-topic" / "map.md").write_text(
            "問い: 夕焼け\n\n## 用語\n- Rayleigh 散乱（\"短い\" <波長>）— a.md:3\n- 薄明 — b.md\n\n"
            "## 人\n- Strutt（レイリー卿）— c.md\n\n## 場（学会・雑誌）\n- AMS — https://ams.example.test\n",
            encoding="utf-8")
        topics = dash.collect(self.root, [])
        info = next(t for t in topics if t["slug"] == "new-topic")
        self.assertEqual(info["words"], [("用語", 'Rayleigh 散乱（"短い" <波長>）'), ("用語", "薄明"),
                                         ("人", "Strutt（レイリー卿）"), ("媒体", "AMS")])
        page = dash.render(topics, self.root)
        self.assertNotIn("<波長>", page)
        self.assertIn("&lt;波長&gt;", page)
        self.assertIn('data-s="new-topic 夕焼け', page)

    def test_reading_line_goes_to_the_search_index(self):
        (self.root / "new-topic" / "map.md").write_text(
            "問い: 夕焼けはなぜ赤いか\n読み: ゆうやけ なぜ あかい\n", encoding="utf-8")
        topics = dash.collect(self.root, [])
        self.assertEqual(next(t for t in topics if t["slug"] == "new-topic")["reading"], "ゆうやけ なぜ あかい")
        self.assertIn('data-r="ゆうやけ なぜ あかい"', dash.render(topics, self.root))

    def test_short_title_heads_the_list_and_the_question_stays_in_full(self):
        (self.root / "new-topic" / "map.md").write_text(
            "問い: 夕方の空が赤く見えるのは、光のどんな性質によるのか\n題: 夕焼けはなぜ赤いか\n", encoding="utf-8")
        page = dash.render(dash.collect(self.root, []), self.root)
        self.assertIn('<span class="q">夕焼けはなぜ赤いか</span>', page)
        self.assertIn("<h2>夕焼けはなぜ赤いか</h2>", page)
        self.assertIn('<p class="question">夕方の空が赤く見えるのは、光のどんな性質によるのか</p>', page)

    def test_sidebar_has_search_and_both_pagers(self):
        page = dash.render(dash.collect(self.root, []), self.root)
        self.assertIn('id="find"', page)
        self.assertIn('class="pager sp"', page.split("</header>")[0])
        self.assertIn('class="pager pc"', page.split('<div class="side"')[1])

    def test_markdown_preview_escapes_and_links_only_http(self):
        import skin
        out = skin.md_html("# 題 <b>\n\n- [x] 済み\n- [ ] まだ <script>alert(1)</script>\n\n"
                           "| 列 | 値 |\n|---|---|\n| a | [x](javascript:alert(1)) |\n\n"
                           "見る https://example.test/a?b=1&c=2 と [論文](https://example.test/p)\n\n```\n<i>そのまま</i>\n```\n")
        self.assertIn("<h1>題 &lt;b&gt;</h1>", out)
        self.assertNotIn("<script>", out)
        self.assertNotIn('href="javascript', out)
        self.assertIn('<a href="https://example.test/p" target="_blank" rel="noopener noreferrer">論文</a>', out)
        self.assertIn('href="https://example.test/a?b=1&amp;c=2"', out)
        self.assertIn('class="box on"', out)
        self.assertIn("<table>", out)
        self.assertIn("&lt;i&gt;そのまま&lt;/i&gt;", out)
        # 字下げした項目は、直前の項目の中の入れ子の箇条書きになる
        self.assertIn("<ul><li>親<ul><li>子</li></ul></li><li>次</li></ul>", skin.md_html("- 親\n  - 子\n- 次"))
        page = dash.with_back("map.md", "# 地図\n- 項目".encode(), "new-topic").decode()
        self.assertIn('<article class="sheet md"><h1>地図</h1>', page)
        self.assertIn('class="imz-hud"', page)

    def test_pages_carry_favicon_and_quotes_name_their_language(self):
        import report
        import skin
        page = dash.render(dash.collect(self.root, []), self.root)
        doc = dash.with_back("map.md", "# 地図".encode(), "new-topic").decode()
        for p in (page, doc):
            self.assertIn('<html lang="ja">', p)
            self.assertIn('name="format-detection" content="telephone=no"', p)
            self.assertEqual('rel="icon" type="image/png" href="data:image/png;base64,' in p,
                             (skin.ASSETS / "favicon.png").is_file())
        en = report.quote_html(self.root, {"file": "a.txt", "line": 1, "text": "Rayleigh scattering"}, ["Rayleigh scattering"], 1)
        self.assertIn('<blockquote lang="en">', en)
        self.assertIn('<pre lang="en">', en)
        ja = report.quote_html(self.root, {"file": "a.txt", "line": 1, "text": "空が青い"}, ["空が青い"], 1)
        self.assertIn("<blockquote>", ja)

    def test_ledger_note_only_when_numbers_differ(self):
        t = self.collect()["new-topic"]
        t["run"] = {**t["run"], "周": "2周", "原文": "原文5本", "感覚と合った？": "「いいね」"}
        out = dash.topic_html(t)
        self.assertNotIn("台帳 2周", out)
        self.assertIn("台帳 原文5本", out)
        self.assertIn("<dt>ユーザーの反応</dt>", out)

    def test_guide_shows_screens_with_numbered_legend(self):
        guide = dash.guide_html(list(self.collect().values()))
        have = all(dash.LOGO.with_name(f"guide-{d}-{k}.webp").is_file() for d in ("sp", "pc") for k, _, _ in dash.GUIDE_SCREENS)
        # 図があれば図と番号の説明、無ければ文字の3行で画面の見方を書く。どちらでも「ホーム」の説明は出る
        self.assertEqual('<img src="data:image/webp;base64,' in guide, have)
        self.assertIn("ホーム", guide)
        if have:
            self.assertIn('<span class="n">9</span>', guide)
            # 両方の端末にある部品（数字）は、どちらの組にも同じ番号と説明で並ぶ。片方だけの部品は文字の印（.only）で、その組にだけ載る
            sp, pc = guide.split('data-dev="sp"')[1].split('data-dev="pc"')
            pc = pc.split("</details>")[0]
            num = lambda h: __import__("re").findall(r'<li><span class="n">(\d+)</span>([^<]*)</li>', h)
            self.assertEqual(num(sp), num(pc))
            self.assertIn("サイドバーを閉じる", sp)
            self.assertNotIn("サイドバーを閉じる", pc)
            self.assertIn("つまみ", pc)
            # 題 → 図 → 番号の説明の順。スマホとパソコンの組は、それぞれ畳みに入る
            self.assertIn('<figure class="shot"><figcaption>', guide)
            self.assertIn('data-dev="sp"', guide)
            self.assertNotIn('<ul class="parts">', guide)

    def test_guide_breaks_lines_per_sentence_but_not_inside_tags(self):
        guide = dash.guide_html(list(self.collect().values()))
        self.assertIn("ダッシュボードです。<br>", guide)
        self.assertEqual(dash.sentence_lines('<p>一。二。</p><img alt="a。b">'), '<p>一。<br>二。</p><img alt="a。b">')
        # 項目の最後に句点が無ければ付ける。図の番号の説明は、同じ番号の中では改行しない
        self.assertEqual(dash.sentence_lines("<li>一。二</li>"), "<li>一。<br>二。</li>")
        self.assertEqual(dash.sentence_lines('<ol class="legend"><li>一。二</li></ol><p>三。四。</p>'),
                         '<ol class="legend"><li>一。二。</li></ol><p>三。<br>四。</p>')

    def test_works_without_ledger(self):
        topics = dash.collect(self.root, dash.load_runs(None))
        self.assertTrue(dash.render(topics, self.root).startswith("<!doctype html>"))


if __name__ == "__main__":
    unittest.main()
