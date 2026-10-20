"""検索結果から来る URL・本文・フィードを、他人が細工したものとして扱えるかを検査する。

2026-10-20 に、3つの系統のモデル（Claude・GPT・DeepSeek）でレビューして見つかったもの。
"""

import contextlib
import http.server
import importlib.util
import io
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


SCRIPTS = Path(__file__).resolve().parents[1] / "skills/imozuru/scripts"
PUBLIC_IP = "93.184.215.14"


def load(name):
    spec = importlib.util.spec_from_file_location(f"security_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fetch = load("fetch")
watch = load("watch")
chain = load("chain")
report = load("report")
REAL_RESOLVE = fetch.resolve


def public_localhost(host):
    """「localhost」だけをインターネット上の宛先に引けたことにする。ほかは本当に引く。

    手元の小さなサーバーへは localhost でつなぎ、転送の先の 127.0.0.1 は本当の引き先で止まるかを見る。
    """
    return {PUBLIC_IP} if host == "localhost" else REAL_RESOLVE(host)


class _Server(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/to-private":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/secret")
            self.end_headers()
        elif self.path == "/to-ftp":
            self.send_response(302)
            self.send_header("Location", "ftp://127.0.0.1:9/x")
            self.end_headers()
        else:
            body = b"x" * 20
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *args):
        pass


class UnsafeUrlTests(unittest.TestCase):
    """手元や内側の宛先を取りに行かない。転送の先でも同じ。"""

    def setUp(self):
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _Server)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://localhost:{self.server.server_port}"

    def test_private_addresses_are_refused(self):
        for url in ("http://127.0.0.1:8080/", "http://[::1]/", "http://[::ffff:127.0.0.1]/",
                    "http://169.254.169.254/latest/meta-data/", "http://10.0.0.5/", "http://192.168.1.1/",
                    "http://2130706433/", "http://0x7f000001/", "http://0.0.0.0/"):
            for module in (fetch, watch):
                with self.subTest(url=url, module=module.__name__):
                    self.assertIsNotNone(module.url_problem(url))

    def test_public_address_and_non_http_schemes(self):
        self.assertIsNone(fetch.url_problem(f"https://{PUBLIC_IP}/paper"))
        for url in ("file:///etc/passwd", "ftp://example.com/x", "-o/tmp/x", "http:///nohost"):
            with self.subTest(url=url):
                self.assertIsNotNone(fetch.url_problem(url))

    def test_fetch_source_refuses_private_url_before_downloading(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(fetch, "download") as download, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(fetch.fetch_source(Path(tmp), "paper", "http://127.0.0.1:9222/json", Path(tmp)), 1)
        download.assert_not_called()
        self.assertIn("手元や内側の宛先", out.getvalue())

    def test_redirect_into_private_address_is_refused(self):
        with patch.object(fetch, "resolve", public_localhost), patch.object(watch, "resolve", public_localhost):
            self.assertEqual(fetch.download(f"{self.base}/ok")[0], b"x" * 20)  # つながること自体の確かめ
            for path in ("/to-private", "/to-ftp"):
                with self.subTest(path=path):
                    with self.assertRaises(fetch.UnsafeURL):
                        fetch.download(f"{self.base}{path}")
                    with self.assertRaises(watch.UnsafeURL):
                        watch.fetch(f"{self.base}{path}")

    def test_redirect_body_is_closed_without_reading(self):
        for module in (fetch, watch):
            fp = MagicMock()
            with patch.object(module, "resolve", lambda host: {PUBLIC_IP}), self.subTest(module=module.__name__):
                req = module.urllib.request.Request("https://example.test/a")
                module._Redirect308().redirect_request(req, fp, 302, "Found", {}, "https://example.test/b")
            fp.close.assert_called_once()
            fp.read.assert_not_called()

    def test_oversized_body_fails_instead_of_truncating(self):
        with patch.object(fetch, "resolve", public_localhost), patch.object(fetch, "MAX_BYTES", 10):
            with self.assertRaises(ValueError):
                fetch.download(f"{self.base}/ok")


class YoutubeHostTests(unittest.TestCase):
    def test_only_youtube_hosts(self):
        for url in ("https://www.youtube.com/watch?v=abc", "https://youtu.be/abc",
                    "https://m.youtube.com/shorts/abc", "https://www.youtube.com/live/abc"):
            with self.subTest(url=url):
                self.assertTrue(fetch.is_youtube(url))
        for url in ("https://evil.example/x?youtu.be/", "https://evil.example/youtube.com/watch",
                    "https://youtube.com.evil.example/watch?v=a", "https://www.youtube.com/about"):
            with self.subTest(url=url):
                self.assertFalse(fetch.is_youtube(url))

    def test_lookalike_url_is_not_sent_to_ytdlp(self):
        body = b"<article>" + b"Article text. " * 200 + b"</article>"
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(fetch, "resolve", lambda host: {PUBLIC_IP}), \
                patch.object(fetch, "HAS_AX", False), \
                patch.object(fetch, "download", return_value=(body, "text/html")), \
                patch.object(fetch, "youtube_to_md") as yt, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(fetch.fetch_source(Path(tmp), "page", "https://evil.example/?next=youtu.be/x", Path(tmp)), 0)
        yt.assert_not_called()

    def test_language_from_metadata_must_look_like_a_language(self):
        asked = []

        def yt_dlp(cmd, **kwargs):
            asked.append(cmd[cmd.index("--sub-langs") + 1])
            return subprocess.CompletedProcess(cmd, 0, "Talk\tSpeaker\t20220820\ten,all\n", "")

        with tempfile.TemporaryDirectory() as tmp, patch.object(fetch.subprocess, "run", side_effect=yt_dlp):
            fetch.youtube_to_md("https://www.youtube.com/watch?v=a", Path(tmp) / "t.md")
        self.assertEqual(asked[-1], "ja-orig,en-orig")


class DecodeTests(unittest.TestCase):
    def test_non_text_codec_does_not_crash(self):
        for name in ("zlib_codec", "base64_codec", "hex_codec", "rot13", "idna"):
            with self.subTest(name=name):
                self.assertEqual(fetch.decode_text(b"plain text", f"text/html; charset={name}"), "plain text")

    def test_latin1_label_on_utf8_body(self):
        text = "日本語の記事"
        self.assertEqual(fetch.decode_text(text.encode("utf-8"), "text/html; charset=iso-8859-1"), text)

    def test_bom_wins(self):
        text = "日本語の記事"
        self.assertEqual(fetch.decode_text(text.encode("utf-16"), "text/plain"), text)
        self.assertEqual(fetch.decode_text(b"\xef\xbb\xbf" + text.encode("utf-8"), "text/plain"), text)


class ToolLimitTests(unittest.TestCase):
    def test_timeout_is_passed_and_handled(self):
        def slow(cmd, **kwargs):
            self.assertEqual(kwargs["timeout"], fetch.TOOL_TIMEOUT)
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

        with tempfile.TemporaryDirectory() as tmp, patch.object(fetch.subprocess, "run", side_effect=slow), \
                patch.object(fetch, "missing", return_value=[]):
            pdf = Path(tmp) / "a.pdf"
            pdf.write_bytes(b"%PDF-1.4")
            self.assertEqual(fetch.pdf_to_text(pdf), "")
            self.assertEqual(fetch.ocr_pdf(pdf), "")
            self.assertEqual(fetch.youtube_to_md("https://youtu.be/a", Path(tmp) / "t.md"), "")
            with patch.object(fetch, "HAS_AX", True):
                self.assertEqual(fetch.html_to_md("https://example.test/", Path(tmp) / "h.md"), "")

    def test_ocr_page_cap(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(fetch.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run, \
                patch.object(fetch, "missing", return_value=[]):
            fetch.ocr_pdf(Path(tmp) / "a.pdf")
        cmd = run.call_args_list[0].args[0]
        self.assertEqual(cmd[cmd.index("-l") + 1], str(fetch.OCR_MAX_PAGES))

    def test_failed_ocr_keeps_extracted_text(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(fetch, "ocr_pdf", return_value=""):
            pdf = Path(tmp) / "a.pdf"
            pdf.with_suffix(".txt").write_text("short text layer", encoding="utf-8")
            self.assertEqual(fetch.ocr_if_longer(pdf, "short text layer"), "short text layer")
            self.assertEqual(pdf.with_suffix(".txt").read_text(encoding="utf-8"), "short text layer")


class OutputSafetyTests(unittest.TestCase):
    def test_extractor_keeps_only_http_links(self):
        page = ('<a href="&#74;avascript:alert(1)">a</a> <a href=" javascript:x">b</a>'
                '<a href="data:text/html,x">c</a> <a href="/ok (1)">d [e]</a>')
        parser = fetch._TextExtractor("https://example.test/dir/")
        parser.feed(page)
        text = parser.text()
        self.assertNotIn("avascript", text.lower())
        self.assertNotIn("data:", text)
        self.assertIn(r"[d \[e\]](https://example.test/ok%20%281%29)", text)

    def test_source_names(self):
        for name in ("a?b", "a*b", "a<b", 'a"b', "a|b", "report‮gpj.md"):
            with self.subTest(name=name):
                self.assertFalse(fetch.valid_source_name(name))

    def test_first_line_drops_terminal_escapes(self):
        line = fetch.first_line("\x1b]52;c;ZXZpbA==\x07 title text here\n")
        self.assertNotIn("\x1b", line)
        self.assertNotIn("\x07", line)

    def test_watch_markdown_is_escaped(self):
        self.assertEqual(watch.md_text(watch.plain("&amp;lt;img src=x onerror=alert(1)&amp;gt;")),
                         "&lt;img src=x onerror=alert(1)&gt;")
        self.assertEqual(watch.md_link("javascript:alert(1)"), "")
        self.assertEqual(watch.md_link("https://a.example/x (1)"), "https://a.example/x%20%281%29")

    def test_chain_row_has_six_columns(self):
        w = {"id": "https://openalex.org/W1", "display_name": "A\ttitle\nW999\x1b[31m",
             "publication_year": 2020, "cited_by_count": 1, "doi": None, "best_oa_location": None}
        line = chain.row(w)
        self.assertEqual(len(line.split("\t")), 6)
        self.assertNotIn("\n", line)
        self.assertNotIn("\x1b", line)


class FeedTests(unittest.TestCase):
    def test_doctype_tricks_are_refused(self):
        laughs = '<!DOCTYPE rss [<!ENTITY a "AAAA">]><rss><channel><item><title>&a;</title></item></channel></rss>'
        for body in (('<?xml version="1.0"?><!-- <rss -->' + laughs).encode(),
                     ('<?xml version="1.0" encoding="UTF-16"?>' + laughs).encode("utf-16"),
                     laughs.encode()):
            with self.subTest(body=body[:30]):
                with self.assertRaises(ValueError):
                    watch.items(body)

    def test_html_doctype_inside_cdata_is_fine(self):
        feed = (b'<?xml version="1.0"?><rss><channel><item><title>t</title><link>https://a.example/</link>'
                b'<description><![CDATA[<!DOCTYPE html><p>x</p>]]></description></item></channel></rss>')
        self.assertEqual(watch.items(feed)[0]["title"], "t")

    def test_feed_links_are_resolved_and_checked(self):
        page = b'<html><link rel="alternate" type="application/rss+xml" href="feed.xml"></html>'
        with patch.object(watch, "fetch_final", return_value=(page, "text/html", "https://site.example/blog/index.html")):
            self.assertEqual(watch.discover("https://site.example/blog/"), ("site", "https://site.example/blog/feed.xml"))
        with patch.object(watch, "resolve", lambda host: {PUBLIC_IP}):
            with self.assertRaises(watch.UnsafeURL):
                watch.fetch("file:///etc/passwd")

    def test_tsv_rows_cannot_be_injected(self):
        page = (b'<link rel="alternate" type="application/rss+xml" '
                b'href="https://evil.example/feed&#10;site\tx\thttp://127.0.0.1/\tx">')
        feed = b'<?xml version="1.0"?><rss><channel></channel></rss>'
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(watch, "fetch_final", return_value=(page, "text/html", "https://evil.example/")), \
                patch.object(watch, "fetch", return_value=(feed, "application/rss+xml")), \
                contextlib.redirect_stdout(io.StringIO()):
            watch.cmd_add(Path(tmp), "https://evil.example/", None)
            with patch.object(watch, "discover", return_value=("site", "https://other.example/feed")):
                watch.cmd_add(Path(tmp), "https://other.example/", "name\twith\ntabs")
            # 見出しの1行と、足した2行だけ。どの行も4列
            rows = (Path(tmp) / "watch.tsv").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(len(r.split("\t")) == 4 for r in rows))
        self.assertFalse(any(r.split("\t")[2].startswith("http://127.") for r in rows[1:]))

    def test_control_characters_in_urls_are_refused(self):
        feed = b'<?xml version="1.0"?><rss><channel></channel></rss>'
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(watch, "discover", return_value=("site", "https://a.example/feed\x0bx")), \
                patch.object(watch, "fetch", return_value=(feed, "application/rss+xml")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(watch.cmd_add(Path(tmp), "https://a.example/", None), 1)
            self.assertFalse((Path(tmp) / "watch.tsv").exists())

    def gzip_response(self, body):
        res = MagicMock()
        res.read.return_value = body
        res.headers = {"Content-Encoding": "gzip"}
        res.geturl.return_value = "https://a.example/feed"
        res.__enter__.return_value = res
        return res

    def test_gzip_feed_is_inflated(self):
        import gzip
        feed = b'<?xml version="1.0"?><rss><channel><item><title>t</title></item></channel></rss>'
        with patch.object(watch, "resolve", lambda host: {PUBLIC_IP}), \
                patch.object(watch._OPENER, "open", return_value=self.gzip_response(gzip.compress(feed))):
            self.assertEqual(watch.fetch("https://a.example/feed")[0], feed)

    def test_gzip_bomb_is_refused(self):
        import gzip
        bomb = gzip.compress(b"\0" * (watch.MAX_BYTES * 10))
        with patch.object(watch, "resolve", lambda host: {PUBLIC_IP}), \
                patch.object(watch._OPENER, "open", return_value=self.gzip_response(bomb)):
            with self.assertRaises(ValueError):
                watch.fetch("https://a.example/feed")

    def test_short_rows_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "watch.tsv").write_text("site\tonly-two\nsite\tok\thttps://a.example/feed\n", encoding="utf-8")
            self.assertEqual(watch.load_watch(Path(tmp)), [["site", "ok", "https://a.example/feed"]])

    def test_bad_days_argument(self):
        for argv in (["check", "/tmp/x", "--days"], ["check", "/tmp/x", "--days", "abc"]):
            with self.subTest(argv=argv), patch.object(sys, "argv", ["watch.py", *argv]), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(watch.main(), 2)


class ChainKeyTests(unittest.TestCase):
    def test_id_and_doi_cannot_rewrite_the_request(self):
        with patch.object(chain, "get", return_value={}) as get:
            with self.assertRaises(SystemExit):
                chain.resolve("https://openalex.org/W1?filter=x")
            chain.resolve("10.1000/a?b#c")
        self.assertEqual(get.call_args.args[0], "/works/doi:10.1000/a%3Fb%23c")

    def test_bad_n(self):
        for n in ("", "abc", "0", "999"):
            argv = ["chain.py", "refs", "W1", "-n"] + ([n] if n else [])
            with self.subTest(n=n), patch.object(sys, "argv", argv), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(chain.main(), 2)


class ReportLineTests(unittest.TestCase):
    def test_line_range_must_be_inside_the_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "topic" / "sources"
            src.mkdir(parents=True)
            (src / "a.md").write_text("first line\nsecond line\n", encoding="utf-8")
            self.assertTrue(report.verify(src, {"file": "a.md", "line": 1, "text": "first line"})[0])
            for q in ({"line": 0}, {"line": -1}, {"line": 2, "end": 1}, {"line": 99}, {"line": "1"}):
                with self.subTest(q=q):
                    self.assertFalse(report.verify(src, {"file": "a.md", "text": "first line", **q})[0])


if __name__ == "__main__":
    unittest.main()
