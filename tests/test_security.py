"""検索結果から来る URL・本文・フィードを、他人が細工したものとして扱えるかを検査する。

2026-10-20 に、3つの系統のモデル（Claude・GPT・DeepSeek）でレビューして見つかったもの。
"""

import contextlib
import gzip
import http.server
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
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


def dial_public_as_local(ip, port, timeout, source_address):
    """見せかけの PUBLIC_IP へのつなぎを手元のサーバーへ回す（_pinned_connection は引いた IP へじかにつなぐ）。"""
    import socket
    return socket.create_connection(("127.0.0.1" if ip == PUBLIC_IP else ip, port), timeout, source_address)


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


class _Proxy(_Server):
    def do_GET(self):
        body = b"via-proxy"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class HttpProxyTests(unittest.TestCase):
    def setUp(self):
        server = http.server.HTTPServer(("127.0.0.1", 0), _Proxy)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        proxy_url = f"http://127.0.0.1:{server.server_port}"
        env = patch.dict(os.environ, {"http_proxy": proxy_url, "no_proxy": "", "NO_PROXY": "",
                                    "IMOZURU_ALLOW_PRIVATE": ""})
        env.start()
        self.addCleanup(env.stop)
        for module in (fetch, watch):
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy_url}),
                module._Redirect308, module._PinnedHTTPHandler, module._PinnedHTTPSHandler)
            for patcher in (patch.object(module, "_OPENER", opener),
                            patch.object(module, "resolve", lambda host: {PUBLIC_IP} if host == "example.test"
                                         else REAL_RESOLVE(host))):
                patcher.start()
                self.addCleanup(patcher.stop)

    def test_fetch_download_through_http_proxy(self):
        self.assertEqual(fetch.download("http://example.test/x")[0], b"via-proxy")

    def test_watch_fetch_through_http_proxy(self):
        self.assertEqual(watch.fetch("http://example.test/x")[0], b"via-proxy")


class UnsafeUrlTests(unittest.TestCase):
    """手元や内側の宛先を取りに行かない。転送の先でも同じ。"""

    def setUp(self):
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _Server)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://localhost:{self.server.server_port}"
        for module in (fetch, watch):
            dial = patch.object(module, "_dial", dial_public_as_local)
            dial.start()
            self.addCleanup(dial.stop)

    def test_dns_rebinding_between_check_and_connect_is_refused(self):
        """検査の時は外の IP、つなぐ時は 127.0.0.1 を返す名前（DNS rebinding）を断る。"""
        for module in (fetch, watch):
            answers = iter([{PUBLIC_IP}, {"127.0.0.1"}])
            with patch.object(module, "resolve", lambda host: next(answers)), self.subTest(module=module.__name__):
                with self.assertRaises(module.UnsafeURL):
                    (module.download if module is fetch else module.fetch)(f"{self.base}/ok")

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

        with tempfile.TemporaryDirectory() as tmp, patch.object(fetch, "run_tool", side_effect=yt_dlp):
            fetch.youtube_to_md("https://www.youtube.com/watch?v=a", Path(tmp) / "t.md")
        self.assertEqual(asked[-1], "ja-orig,en-orig")


class DecodeTests(unittest.TestCase):
    def test_non_document_codecs_are_skipped(self):
        # 「\x81 」で UTF-8・cp932・EUC-JP を外し、名乗りの文字コードまで試させる
        for name in ("zlib_codec", "base64_codec", "hex_codec", "rot13", "idna", "punycode", "utf-7",
                     "unicode_escape"):
            with self.subTest(name=name):
                self.assertIsInstance(fetch.decode_text(b"\x81 plain", f"text/html; charset={name}"), str)

    def test_punycode_label_does_not_hang(self):
        # 「\x81 」は UTF-8・cp932・EUC-JP のどれでも読めない。読み直しで名乗りの punycode まで行かせる。
        # 後ろは punycode なら長さの2乗の時間がかかる形
        body = b"x\x81 -" + b"ba" * 60000
        started = time.monotonic()
        fetch.decode_text(body, "text/plain; charset=punycode")
        self.assertLess(time.monotonic() - started, 1)

    def test_latin1_label_on_utf8_body(self):
        text = "日本語の記事"
        self.assertEqual(fetch.decode_text(text.encode("utf-8"), "text/html; charset=iso-8859-1"), text)

    def test_bom_wins(self):
        text = "日本語の記事"
        self.assertEqual(fetch.decode_text(text.encode("utf-16"), "text/plain"), text)
        self.assertEqual(fetch.decode_text(b"\xef\xbb\xbf" + text.encode("utf-8"), "text/plain"), text)


class ToolLimitTests(unittest.TestCase):
    def test_run_tool_stops_grandchildren(self):
        """時間切れで、孫の処理（出力の管をつかんだまま眠る）まで止めて戻る。"""
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "grandchild.pid"
            script = ("import subprocess, sys, time\n"
                      "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                      f"open({str(pid_file)!r}, 'w').write(str(p.pid))\n"
                      "time.sleep(60)\n")
            started = time.monotonic()
            with patch.object(fetch, "TOOL_TIMEOUT", 1):
                result = fetch.run_tool([sys.executable, "-c", script], capture_output=True, encoding="utf-8")
            self.assertIsNone(result)
            self.assertLess(time.monotonic() - started, 15)
            grandchild = int(pid_file.read_text())
            time.sleep(0.2)
            if os.name != "nt":
                with self.assertRaises(ProcessLookupError):
                    os.kill(grandchild, 0)

    def test_callers_handle_timeouts(self):
        def timed_out(cmd, **kwargs):
            if cmd[0] == "pdftotext":  # 書きかけを残して時間切れになった
                Path(cmd[-1]).write_text("partial " * 500, encoding="utf-8")
            return None

        with tempfile.TemporaryDirectory() as tmp, patch.object(fetch, "run_tool", side_effect=timed_out), \
                patch.object(fetch, "missing", return_value=[]):
            pdf = Path(tmp) / "a.pdf"
            pdf.write_bytes(b"%PDF-1.4")
            self.assertEqual(fetch.pdf_to_text(pdf), "")
            self.assertFalse(pdf.with_suffix(".txt").exists())
            self.assertEqual(fetch.ocr_pdf(pdf)[0], "")
            self.assertEqual(fetch.youtube_to_md("https://youtu.be/a", Path(tmp) / "t.md"), "")
            with patch.object(fetch, "HAS_AX", True):
                self.assertEqual(fetch.html_to_md("https://example.test/", Path(tmp) / "h.md",
                                                  b"<p>x</p>", "text/html"), "")

    def test_failed_pdftotext_leaves_no_partial_text(self):
        """pdftotext が途中で失敗したら、書きかけの .txt を使わない。"""
        def failed(cmd, **kwargs):
            Path(cmd[-1]).write_text("first half " * 500, encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 1)

        with tempfile.TemporaryDirectory() as tmp, patch.object(fetch, "run_tool", side_effect=failed), \
                patch.object(fetch, "missing", return_value=[]):
            pdf = Path(tmp) / "a.pdf"
            pdf.write_bytes(b"%PDF-1.4")
            self.assertEqual(fetch.pdf_to_text(pdf), "")
            self.assertFalse(pdf.with_suffix(".txt").exists())

    def test_ocr_page_cap(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(fetch, "run_tool", return_value=subprocess.CompletedProcess([], 0, "", "")) as run, \
                patch.object(fetch, "missing", return_value=[]):
            fetch.ocr_pdf(Path(tmp) / "a.pdf")
        cmd = run.call_args_list[0].args[0]
        self.assertEqual(cmd[cmd.index("-l") + 1], str(fetch.OCR_MAX_PAGES))

    def test_failed_ocr_keeps_extracted_text(self):
        # 本物の ocr_pdf を通す。pdftoppm が1枚も書き出さなかったとき、.txt を空で上書きしない
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(fetch, "run_tool", return_value=subprocess.CompletedProcess([], 0, "", "")), \
                patch.object(fetch, "missing", return_value=[]):
            pdf = Path(tmp) / "a.pdf"
            pdf.with_suffix(".txt").write_text("short text layer", encoding="utf-8")
            self.assertEqual(fetch.ocr_if_longer(pdf, "short text layer")[0], "short text layer")
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


class SecondRoundTests(unittest.TestCase):
    """2026-10-20 の2回目のレビュー（Claude・Sol）で見つかったもの。"""

    def test_host_with_userinfo_or_percent_is_refused(self):
        # 検査は a.example を見るが、urllib は x@a.example や %61 を戻した名前を引く
        for url in ("http://x@a.example.com/", "http://%61.example.com/", "http://a.example.com%2f@b/"):
            for module in (fetch, watch):
                with self.subTest(url=url, module=module.__name__), \
                        patch.object(module, "resolve", lambda host: {PUBLIC_IP}):
                    self.assertIsNotNone(module.url_problem(url))

    def test_ipv4_inside_ipv6_is_checked(self):
        for addr in ("64:ff9b::7f00:1", "::127.0.0.1", "64:ff9b::a00:1", "2002:7f00:1::", "::ffff:10.0.0.1"):
            for module in (fetch, watch):
                with self.subTest(addr=addr, module=module.__name__), \
                        patch.object(module, "resolve", lambda host: {addr}):
                    self.assertIsNotNone(module.url_problem("https://looks-public.example/"))

    def test_broken_url_does_not_crash(self):
        for module in (fetch, watch):
            with self.subTest(module=module.__name__):
                self.assertIsNotNone(module.url_problem("http://[::1/"))
        self.assertEqual(watch.md_link("http://["), "")
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(fetch.fetch_source(Path(tmp), "p", "http://[evil]/", Path(tmp)), 1)

    def test_private_opt_out(self):
        with patch.dict(os.environ, {"IMOZURU_ALLOW_PRIVATE": "1"}):
            self.assertIsNone(fetch.url_problem("http://127.0.0.1:8080/"))
            self.assertIsNotNone(fetch.url_problem("file:///etc/passwd"))  # http(s) の検査は残る
        self.assertIn("IMOZURU_ALLOW_PRIVATE", fetch.url_problem("http://127.0.0.1:8080/"))

    def test_fail_line_has_no_control_characters(self):
        err = fetch.UnsafeURL("転送先 http://127.0.0.1/\x1b]52;c;ZXZpbA==\x07\tEXTRA\nFAKE")
        with tempfile.TemporaryDirectory() as tmp, patch.object(fetch, "resolve", lambda host: {PUBLIC_IP}), \
                patch.object(fetch, "download", side_effect=err), contextlib.redirect_stdout(io.StringIO()) as out:
            fetch.fetch_source(Path(tmp), "p", "https://example.test/", Path(tmp))
        line = out.getvalue()
        self.assertEqual(line.count("\n"), 1)
        self.assertEqual(line.count("\t"), 2)
        self.assertNotIn("\x1b", line)

    def test_arxiv_by_host(self):
        self.assertEqual(fetch.arxiv_id("https://arxiv.org/abs/2401.00001v2"), "2401.00001v2")
        for url in ("https://evil.example/?arxiv.org/abs/2401.00001", "https://arxiv.org.evil.example/abs/2401.00001"):
            with self.subTest(url=url):
                self.assertIsNone(fetch.arxiv_id(url))

    def test_youtube_embed_and_playlist(self):
        self.assertTrue(fetch.is_youtube("https://www.youtube-nocookie.com/embed/abc"))
        self.assertTrue(fetch.is_youtube("https://www.youtube.com./watch?v=abc"))
        with tempfile.TemporaryDirectory() as tmp, patch.object(
                fetch, "run_tool", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            fetch.youtube_to_md("https://www.youtube.com/watch?v=a&list=b", Path(tmp) / "t.md")
        self.assertIn("--no-playlist", run.call_args_list[0].args[0])

    def test_truncated_body_fails(self):
        res = MagicMock()
        res.__enter__.return_value = res
        res.read.return_value = b"half"
        res.length = 100  # Content-Length に届かずに接続が切れた
        res.headers = {}
        with patch.object(fetch, "resolve", lambda host: {PUBLIC_IP}), patch.object(fetch._OPENER, "open", return_value=res):
            with self.assertRaises(ValueError):
                fetch.download("https://example.test/")
        with patch.object(watch, "resolve", lambda host: {PUBLIC_IP}), patch.object(watch._OPENER, "open", return_value=res):
            with self.assertRaises(ValueError):
                watch.fetch("https://example.test/")

    def test_gzip_members_and_truncation(self):
        feed = b'<?xml version="1.0"?><rss><channel><item><title>t</title></item></channel></rss>'
        self.assertEqual(watch.gunzip(gzip.compress(feed[:30]) + gzip.compress(feed[30:])), feed)
        self.assertEqual(watch.gunzip(gzip.compress(feed) + b"\0\0\0"), feed)
        with self.assertRaises(ValueError):
            watch.gunzip(gzip.compress(feed)[:-10])
        with patch.object(watch, "MAX_BYTES", 200), self.assertRaises(ValueError):
            watch.gunzip(gzip.compress(feed) + gzip.compress(b" " * 1000))

    def test_markdown_escaping_holds(self):
        # 元からある「\」で、足した「\」を打ち消させない
        self.assertEqual(watch.md_text("\\](https://evil.example/)"), "\\\\\\](https://evil.example/)")
        parser = fetch._TextExtractor("https://example.test/")
        parser.feed('<a href="/ok">x\\](https://evil.example/)</a>')
        self.assertIn("[x\\\\\\](https://evil.example/)](https://example.test/ok)", parser.text())

    def test_watch_check_escapes_labels_and_errors(self):
        title = "&amp;lt;img src=x onerror=alert(1)&amp;gt;"
        with tempfile.TemporaryDirectory() as tmp:
            topic = Path(tmp)
            (topic / "watch.tsv").write_text(
                f"feed\t{watch.plain(title)}\thttps://a.example/feed\thttps://a.example/\n"
                "feed\tbroken\thttps://b.example/feed\thttps://b.example/\n", encoding="utf-8")

            def fetch_(url):
                if url.startswith("https://b."):
                    raise ValueError("<script>bad</script>\x1b[31m")
                return (b'<?xml version="1.0"?><rss><channel><item><title>t</title>'
                        b'<link>http://[</link></item></channel></rss>', "application/rss+xml")

            with patch.object(watch, "fetch", side_effect=fetch_), contextlib.redirect_stdout(io.StringIO()):
                watch.cmd_check(topic, 14, True)
            md = next((topic / "watch").glob("*.md")).read_text(encoding="utf-8")
        self.assertNotIn("<img", md)
        self.assertNotIn("<script", md)
        self.assertNotIn("\x1b", md)

    def test_tsv_keeps_rows_with_unicode_line_separators(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "watch.tsv").write_text("site\tgood\u2028site\thttps://a.example/feed\thttps://a.example/\n",
                                                encoding="utf-8")
            self.assertEqual(watch.load_watch(Path(tmp))[0][2], "https://a.example/feed")
        self.assertEqual(watch.clean("a\u2028b\x85c"), "a b c")

    def test_chain_redirects_stay_on_the_api(self):
        handler = chain._SameOrigin()
        req = chain.urllib.request.Request(chain.API + "/works/W1")
        with self.assertRaises(ValueError):
            handler.redirect_request(req, MagicMock(), 302, "Found", {}, "http://127.0.0.1:9222/json")
        handler.redirect_request(req, MagicMock(), 302, "Found", {}, chain.API + "/works/W2")

    def test_chain_doi_quoting(self):
        with patch.object(chain, "get", return_value={}) as get:
            chain.resolve("https://doi.org/10.1000/a%28b%29")
            self.assertEqual(get.call_args.args[0], "/works/doi:10.1000/a%28b%29")
            chain.resolve("10.1000/50%off")
            self.assertEqual(get.call_args.args[0], "/works/doi:10.1000/50%25off")
            with self.assertRaises(SystemExit):
                chain.resolve("10.1/../../authors")

    def test_chain_author_line_is_clean(self):
        author = {"id": "https://openalex.org/A1", "display_name": "Name\tX\n# fake", "works_count": 1,
                  "last_known_institutions": [{"display_name": "Inst\x1b]52;c;x\x07"}]}

        def get(path, **params):
            return {"results": [author]} if path == "/authors" else {"results": []}

        with patch.object(chain, "get", side_effect=get), patch.object(sys, "argv", ["chain.py", "author", "x"]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            chain.main()
        self.assertEqual(out.getvalue().count("\n"), 1)
        self.assertNotIn("\x1b", out.getvalue())

    def test_non_ascii_digits(self):
        for argv in (["watch.py", "check", "/tmp/x", "--days", "²"], ["chain.py", "refs", "W1", "-n", "²"]):
            module = watch if argv[0] == "watch.py" else chain
            with self.subTest(argv=argv), patch.object(sys, "argv", argv), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(module.main(), 2)

    def test_report_accepts_null_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "topic" / "sources"
            src.mkdir(parents=True)
            (src / "a.md").write_text("first line\n", encoding="utf-8")
            self.assertTrue(report.verify(src, {"file": "a.md", "line": 1, "end": None, "text": "first line"})[0])


class DeepSeekSplitReviewTests(unittest.TestCase):
    """2026-10-20、DeepSeek に1ファイルずつ読ませたレビューで当たったもの。"""

    def test_bidi_controls_are_removed(self):
        for module in (fetch, watch, chain):
            with self.subTest(module=module.__name__):
                self.assertEqual(module.clean("abc‮def​g﻿"), "abc def g")
                # 絵文字の並びや一部の文字に要る ZWJ・ZWNJ は残す
                self.assertEqual(module.clean("a‍b‌c"), "a‍b‌c")

    def test_short_title_is_queried_once(self):
        with patch.object(chain, "get", return_value={"results": []}) as get:
            self.assertEqual(chain.search_title("Exploratory search"), [])
        self.assertEqual(get.call_count, 1)

    def test_empty_title_is_not_queried(self):
        with patch.object(chain, "get") as get:
            self.assertEqual(chain.search_title(" : , "), [])
        get.assert_not_called()

    def test_author_lists_other_candidates_and_accepts_an_id(self):
        people = [{"id": f"https://openalex.org/A{i}", "display_name": "Same Name", "works_count": i,
                   "last_known_institutions": [{"display_name": f"Inst {i}"}]} for i in (1, 2)]
        calls = []

        def get(path, **params):
            calls.append(path)
            if path == "/authors":
                return {"results": people}
            if path == "/authors/A2":
                return people[1]
            return {"results": []}

        with patch.object(chain, "get", side_effect=get), \
                patch.object(sys, "argv", ["chain.py", "author", "Same Name"]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            chain.main()
        self.assertIn("# 別の候補 Same Name A2 Inst 2", out.getvalue())
        self.assertIn("chain.py author A2", out.getvalue())

        with patch.object(chain, "get", side_effect=get), \
                patch.object(sys, "argv", ["chain.py", "author", "A2"]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            chain.main()
        self.assertIn("# author Same Name A2", out.getvalue())
        self.assertIn("/authors/A2", calls)

    def test_anubis_page_is_a_wall(self):
        page = ("# Making sure you're not a bot!\n\nLoading...\n\n"
                + "You are seeing this because the administrator set up Anubis. " * 20)
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()) as out, \
                patch.object(fetch, "download", return_value=(b"<html>anubis</html>", "text/html")), \
                patch.object(fetch, "html_to_md", side_effect=lambda url, o, *a: (o.write_text(page), page)[1]), \
                patch.object(fetch, "pdf_save", return_value=False), \
                patch.object(fetch, "resolve", lambda host: {PUBLIC_IP}), \
                patch.object(sys, "argv", ["fetch.py", tmp, "paper", "https://example.test/x"]):
            self.assertEqual(fetch.main(), 1)
        self.assertIn("壁に当たった（you're not a bot）", out.getvalue())


class TestConnectOrder(unittest.TestCase):
    """IPv6 の経路が無い PC でも、IPv6 の宛先ごとに長く待たずに IPv4 へ進む。"""

    V6 = ["2606:50c0:8000::154", "2606:50c0:8001::154"]
    V4 = ["185.199.108.133", "185.199.109.133"]

    def connect(self, module, addrs, timeout, dial_result):
        tries = []
        sock = MagicMock()

        def dial(ip, port, wait, source_address):
            tries.append((ip, wait))
            error = dial_result(ip, wait)
            if error:
                raise error
            return sock

        with patch.object(module, "resolve", lambda host: addrs), patch.object(module, "_dial", dial):
            got = module._pinned_connection("example.test", 443, timeout, None)
        self.assertIs(got, sock)
        return tries, sock

    @staticmethod
    def no_ipv6_route(ip, wait):
        return TimeoutError("timed out") if ":" in ip else None

    def test_tries_ipv4_second_with_a_short_wait(self):
        for module in (fetch, watch):
            with self.subTest(module=module.__name__):
                tries, sock = self.connect(module, self.V6 + self.V4, 90, self.no_ipv6_route)
                self.assertEqual(tries, [(self.V6[0], 10), (self.V4[0], 10)])
                # つないだあとの読み込みは、呼び出し側の timeout で待つ
                sock.settimeout.assert_called_once_with(90)

    def test_ipv4_first_list_starts_with_ipv4(self):
        tries, _ = self.connect(fetch, self.V4[:1] + self.V6, 90,
                                lambda ip, wait: OSError("unreachable") if "." in ip else None)
        self.assertEqual([ip for ip, _ in tries], [self.V4[0], self.V6[0]])

    def test_last_address_keeps_the_callers_timeout(self):
        for module in (fetch, watch):
            with self.subTest(module=module.__name__):
                tries, sock = self.connect(module, self.V4[:1], 90, lambda ip, wait: None)
                self.assertEqual(tries, [(self.V4[0], 90)])
                sock.settimeout.assert_not_called()

    def test_short_caller_timeout_is_not_lengthened(self):
        tries, sock = self.connect(fetch, self.V6[:1] + self.V4[:1], 3, self.no_ipv6_route)
        self.assertEqual(tries, [(self.V6[0], 3), (self.V4[0], 3)])
        sock.settimeout.assert_not_called()

    def test_short_default_timeout_is_not_lengthened(self):
        default = fetch.socket._GLOBAL_DEFAULT_TIMEOUT
        with patch.object(fetch.socket, "getdefaulttimeout", return_value=0.25):
            tries, _ = self.connect(fetch, self.V6[:1] + self.V4[:1], default, self.no_ipv6_route)
        self.assertEqual(tries, [(self.V6[0], default), (self.V4[0], default)])

    def test_slow_address_is_retried_with_the_full_timeout(self):
        # IPv6 は10秒より遅くつながり、IPv4 には届かない。短く切った IPv6 を、最後に元の timeout で待ち直す
        def slow_ipv6(ip, wait):
            if ":" in ip:
                return TimeoutError("timed out") if wait == 10 else None
            return OSError("unreachable")

        for module in (fetch, watch):
            with self.subTest(module=module.__name__):
                tries, sock = self.connect(module, self.V6[:1] + self.V4[:1], 90, slow_ipv6)
                self.assertEqual(tries, [(self.V6[0], 10), (self.V4[0], 90), (self.V6[0], 90)])
                sock.settimeout.assert_not_called()

    def test_private_address_later_in_the_list_refuses_before_any_connection(self):
        for module in (fetch, watch):
            with self.subTest(module=module.__name__):
                dial = MagicMock()
                with patch.object(module, "resolve", lambda host: ["2606:4700::1", "1.1.1.1", "10.0.0.1"]), \
                        patch.object(module, "_dial", dial), \
                        patch.dict(os.environ, {"IMOZURU_ALLOW_PRIVATE": ""}):
                    with self.assertRaises(module.UnsafeURL):
                        module._pinned_connection("example.test", 443, 90, None)
                dial.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class OcrLanguageTests(unittest.TestCase):
    """#11: OCR が eng 固定で、日本語のスキャン PDF が意味のない英字の列のまま保存されうる。"""

    @staticmethod
    def listing(*names, stream="stdout"):
        text = 'List of available languages in "/x/tessdata/" (%d):\n' % len(names) + "\n".join(names) + "\n"
        out, err = (text, "") if stream == "stdout" else ("", text)
        return lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, out, err)

    def langs(self, run, env=None):
        with patch.dict(os.environ, env or {}, clear=False), patch.object(fetch, "run_tool", side_effect=run):
            if not env:
                os.environ.pop("IMOZURU_OCR_LANG", None)
            return fetch.ocr_langs()

    def test_adds_japanese_when_installed(self):
        self.assertEqual(self.langs(self.listing("eng", "jpn", "jpn_vert", "osd")), "jpn+jpn_vert+eng")

    def test_old_tesseract_lists_on_stderr(self):
        self.assertEqual(self.langs(self.listing("eng", "jpn", stream="stderr")), "jpn+eng")

    def test_vertical_alone_is_not_taken_for_jpn(self):
        self.assertEqual(self.langs(self.listing("eng", "jpn_vert")), "jpn_vert+eng")

    def test_listing_failure_falls_back_to_eng(self):
        self.assertEqual(self.langs(lambda cmd, **kw: None), "eng")

    def test_environment_overrides(self):
        never = MagicMock(side_effect=AssertionError("一覧は引かない"))
        self.assertEqual(self.langs(never, {"IMOZURU_OCR_LANG": "jpn+eng"}), "jpn+eng")

    def test_malformed_environment_is_ignored(self):
        for bad in ("eng --psm 0", "-l", "eng;x", "jpn+", "../eng"):
            with self.subTest(bad=bad), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(self.langs(self.listing("eng"), {"IMOZURU_OCR_LANG": bad}), "eng")

    def fake_tools(self, conf, fail_page=None):
        """pdftoppm は2ページの画像を、tesseract は txt と tsv を書く偽物。"""
        def run(cmd, **kw):
            if cmd[0] == "pdftoppm":
                for n in ("1", "2"):
                    Path(f"{cmd[-1]}-{n}.png").write_bytes(b"png")
                return subprocess.CompletedProcess(cmd, 0)
            if cmd[1] == "--list-langs":
                return subprocess.CompletedProcess(cmd, 0, "eng\n", "")
            if fail_page and cmd[1].endswith(f"-{fail_page}.png"):
                return None
            out = Path(cmd[2])
            out.with_suffix(".txt").write_text("HRIRROVRCA WASORRO " * 50, encoding="utf-8")
            out.with_suffix(".tsv").write_text(
                "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
                "4\t1\t1\t1\t1\t0\t0\t0\t9\t9\t-1\t\n"
                f"5\t1\t1\t1\t1\t1\t0\t0\t9\t9\t{conf}\tHRIRROVRCA\n", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return run

    def ocr(self, run):
        with tempfile.TemporaryDirectory() as tmp, patch.object(fetch, "run_tool", side_effect=run), \
                patch.object(fetch, "missing", return_value=[]), patch.dict(os.environ, {"IMOZURU_OCR_LANG": ""}):
            pdf = Path(tmp) / "a.pdf"
            result = fetch.ocr_pdf(pdf)
            self.assertFalse((Path(tmp) / ".a-ocr").exists())  # 画像の置き場は残さない
            return result

    def test_low_confidence_text_is_not_used(self):
        text, note = self.ocr(self.fake_tools(conf=18.4))
        self.assertEqual(text, "")
        self.assertIn("確信度が低い", note)

    def test_confident_text_is_used(self):
        text, note = self.ocr(self.fake_tools(conf=90))
        self.assertIn("=== page 2 ===", text)
        self.assertEqual(note, "")

    def test_a_timed_out_page_fails_the_whole_ocr(self):
        text, note = self.ocr(self.fake_tools(conf=90, fail_page="2"))
        self.assertEqual(text, "")
        self.assertIn("読めなかったページ", note)

    def test_pdftoppm_failing_midway_fails_the_whole_ocr(self):
        # #14: pdftoppm が1ページだけ書いて終了コード 1 で落ちても、残りの画像だけで成功扱いにしない
        tools = self.fake_tools(conf=95)

        def run(cmd, **kw):
            if cmd[0] == "pdftoppm":
                Path(f"{cmd[-1]}-1.png").write_bytes(b"png")
                return subprocess.CompletedProcess(cmd, 1)
            return tools(cmd, **kw)
        text, note = self.ocr(run)
        self.assertEqual(text, "")
        self.assertIn("終了コード 1", note)

    def test_fail_line_names_the_ocr_reason(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(fetch, "download", return_value=(b"%PDF-1.4\nscan", "application/pdf")), \
                patch.object(fetch, "pdf_to_text", return_value=""), \
                patch.object(fetch, "missing", return_value=[]), \
                patch.object(fetch, "ocr_pdf", return_value=("", "OCR の確信度が低い（平均 18・言語 eng）")), \
                patch.object(fetch, "resolve", lambda host: {PUBLIC_IP}), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            with patch.object(sys, "argv", ["fetch.py", tmp, "scan", "https://example.test/scan.pdf"]):
                self.assertEqual(fetch.main(), 1)
        self.assertIn("確信度が低い", out.getvalue())
