"""Windows（cp932）・Python 3.10・外部コマンドの欠けで止まらないことを検査する（#3）。"""

import ast
import contextlib
import http.server
import importlib.util
import io
import locale
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/imozuru/scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(f"imozuru_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fetch = load("fetch")
watch = load("watch")
# テストの宛先（example.test と手元の小さなサーバー）を、インターネット上の宛先に引けたことにする。
# 接続時もこの resolve で引いた IP を確かめ、下の _dial で手元のサーバーにつなぐ
fetch.resolve = watch.resolve = lambda host: {"93.184.215.14"}
# _pinned_connection は引いた IP へじかにつなぐので、見せかけの IP は手元のサーバーへ回す
import socket as _socket
fetch._dial = watch._dial = lambda ip, port, timeout, src: _socket.create_connection(
    ("127.0.0.1" if ip == "93.184.215.14" else ip, port), timeout, src)


class EncodingTests(unittest.TestCase):
    def test_text_io_names_its_encoding(self):
        """encoding を省くと Windows では cp932 で読み書きして落ちる。省いた呼び出しを1つも残さない。"""
        omitted = []
        for script in sorted(SCRIPTS.glob("*.py")):
            for node in ast.walk(ast.parse(script.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                keywords = {k.arg: k.value for k in node.keywords}
                # open は組み込みか Path.open（モードを文字列で渡す）だけ。URL の opener.open(req) は除く
                file_open = name == "open" and (
                    isinstance(func, ast.Name) or not node.args
                    or isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str))
                text_io = name in ("read_text", "write_text") or file_open
                text_run = name == "run" and isinstance(keywords.get("text"), ast.Constant) \
                    and keywords["text"].value is True
                if (text_io or text_run) and "encoding" not in keywords:
                    omitted.append(f"{script.name}:{node.lineno} {name}")
        self.assertEqual(omitted, [])

    def test_report_check_runs_without_default_encoding(self):
        """既定の文字コードに頼る箇所があれば、EncodingWarning をエラーにして落とす。"""
        # 子のプロセスは、パイプへの出力を手元の文字コード（Windows では cp932）で書き、出せない字だけを置き換える。
        # utf-8 で読むと Windows では読めずに stdout が None になっていた（2026-10-26）
        res = subprocess.run(
            [sys.executable, "-I", "-X", "warn_default_encoding", "-W", "error::EncodingWarning",
             str(SCRIPTS / "report.py"), str(ROOT / "examples/demo"), "--check"],
            capture_output=True, encoding=locale.getpreferredencoding(False), errors="replace")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("照合 OK", res.stdout)


class PdfToTextTests(unittest.TestCase):
    def test_pdftotext_writes_utf8(self):
        """Xpdf の pdftotext は既定で Latin-1 を出すので、-enc UTF-8 を渡す。"""
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4")

            def fake_run(args, **kwargs):
                pdf.with_suffix(".txt").write_bytes("Schizophrénie\n".encode("utf-8"))
                return subprocess.CompletedProcess(args, 0)  # None は時間切れの意味になる

            with patch.object(fetch, "missing", return_value=[]), \
                    patch.object(fetch, "run_tool", side_effect=fake_run) as run:
                text = fetch.pdf_to_text(pdf)
        args = run.call_args.args[0]
        self.assertEqual(args[args.index("-enc") + 1], "UTF-8")
        self.assertEqual(text, "Schizophrénie\n")

    def test_pdfminer_is_used_without_pdftotext(self):
        """pdftotext が無く pdfminer.six があれば、別の処理で pdfminer.six を動かす（#15）。
        本物の代わりに extract_text だけの pdfminer を置き、子の処理が .txt を UTF-8 で書くところまで見る"""
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "lib" / "pdfminer"
            fake.mkdir(parents=True)
            (fake / "__init__.py").write_text("", encoding="utf-8")
            (fake / "high_level.py").write_text(
                "def extract_text(path):\n    return 'Schizophrénie\\n' + open(path, 'rb').read()[:8].decode()\n",
                encoding="utf-8")
            pdf = Path(tmp) / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4")
            seen = []

            def spy(cmd, **kwargs):
                seen.append((cmd, kwargs.get("cwd")))
                return real_run_tool(cmd, **kwargs)

            real_run_tool = fetch.run_tool
            with patch.object(fetch, "missing", side_effect=lambda *c: list(c)), \
                    patch.object(fetch, "has_pdfminer", return_value=True), \
                    patch.object(fetch, "run_tool", side_effect=spy), \
                    patch.dict(os.environ, {"PYTHONPATH": str(fake.parent)}):
                text = fetch.pdf_to_text(pdf)
        self.assertEqual(text, "Schizophrénie\n%PDF-1.4")
        cmd, cwd = seen[0]
        self.assertEqual(cmd[:2], [sys.executable, "-c"])
        self.assertEqual(Path(cwd), SCRIPTS)  # 取ってきたファイルの隣では動かさない

    def test_has_pdfminer_probes_in_child(self):
        """pdfminer.six の有無は子の処理で調べる。-I で動かした親からは見えない場所に入っていても見つける。"""
        with tempfile.TemporaryDirectory() as tmp:
            fetch.has_pdfminer.cache_clear()
            self.addCleanup(fetch.has_pdfminer.cache_clear)
            with patch.dict(os.environ, {"PYTHONPATH": tmp}):
                self.assertFalse(fetch.has_pdfminer())
                fake = Path(tmp) / "pdfminer"
                fake.mkdir()
                (fake / "__init__.py").write_text("", encoding="utf-8")
                (fake / "high_level.py").write_text("", encoding="utf-8")
                fetch.has_pdfminer.cache_clear()
                self.assertTrue(fetch.has_pdfminer())

    def test_pdf2txt_command_and_no_tool(self):
        """pdfminer.six を読み込めなくても PATH に pdf2txt.py があれば使う。何も無ければ動かさない。"""
        pdf, out = Path("/x/a.pdf"), Path("/x/a.txt")
        expected = None if os.name == "nt" else ["pdf2txt.py", "-o", str(out), str(pdf)]  # Windows は .py を動かせない
        with patch.object(fetch, "has_pdfminer", return_value=False):
            with patch.object(fetch, "missing", side_effect=lambda *c: [x for x in c if x != "pdf2txt.py"]):
                self.assertEqual(fetch.pdf_text_command(pdf, out), expected)
            with patch.object(fetch, "missing", side_effect=lambda *c: list(c)), \
                    patch.object(fetch, "run_tool") as run:
                self.assertEqual(fetch.pdf_to_text(Path("a.pdf")), "")
                run.assert_not_called()


class _Redirects(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/old":
            self.send_response(308)
            self.send_header("Location", "/new")
            self.end_headers()
        else:
            body = b"moved here"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *args):
        pass


def _redirect_request_310(original):
    """Python 3.10 の redirect_request と同じく、308 をはねる版を作る（3.11 で 308 が足された）。"""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if code == 308:
            raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)
        return original(self, req, fp, code, msg, headers, newurl)
    return redirect_request


class Redirect308Tests(unittest.TestCase):
    def setUp(self):
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _Redirects)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}/old"
        handler = urllib.request.HTTPRedirectHandler
        # 3.10 の動きを再現する。3.10 では http_error_308 も無いが、こちらは自前の handler が持つ
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(
            handler, "redirect_request", _redirect_request_310(handler.redirect_request)))

    def test_stock_opener_rejects_308_under_310_behaviour(self):
        """再現が効いていることの確かめ。素の urlopen は 3.10 と同じく 308 で止まる。"""
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.url, timeout=10)
        self.assertEqual(caught.exception.code, 308)

    def test_fetch_download_follows_308(self):
        body, ctype = fetch.download(self.url)
        self.assertEqual(body, b"moved here")
        self.assertEqual(ctype, "text/plain")

    def test_watch_fetch_follows_308(self):
        body, _ = watch.fetch(self.url)
        self.assertEqual(body, b"moved here")


class DecodeTextTests(unittest.TestCase):
    """Shift_JIS の記事を UTF-8 として読んで中身が U+FFFD になった（pju-hoge さんのフォーク）。"""

    TEXT = "日本語の記事。①髙橋さんの話"  # ①・髙 は Shift_JIS に無く cp932 にある

    def test_content_type_charset(self):
        body = self.TEXT.encode("cp932")
        self.assertEqual(fetch.decode_text(body, "text/html; charset=Shift_JIS"), self.TEXT)

    def test_meta_charset(self):
        body = '<meta charset="euc-jp"><p>日本語の記事</p>'.encode("euc_jp")
        self.assertIn("日本語の記事", fetch.decode_text(body, "text/html"))

    def test_http_equiv_charset(self):
        body = ('<meta http-equiv="Content-Type" content="text/html; charset=shift_jis">'
                + self.TEXT).encode("cp932")
        self.assertIn(self.TEXT, fetch.decode_text(body, "text/html"))

    def test_undeclared_falls_back_to_japanese(self):
        self.assertEqual(fetch.decode_text(self.TEXT.encode("cp932"), "text/html"), self.TEXT)

    def test_wrong_declaration_does_not_garble_utf8(self):
        body = self.TEXT.encode("utf-8")
        self.assertEqual(fetch.decode_text(body, "text/html; charset=shift_jis"), self.TEXT)

    def test_unknown_declaration_is_skipped(self):
        body = self.TEXT.encode("utf-8")
        self.assertEqual(fetch.decode_text(body, "text/html; charset=x-unknown-thing"), self.TEXT)

    def test_shift_jis_html_is_saved_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = ("<article>" + self.TEXT * 100 + "</article>").encode("cp932")
            out = io.StringIO()
            with contextlib.redirect_stdout(out), \
                    patch.object(fetch, "HAS_AX", False), \
                    patch.object(fetch, "download", return_value=(body, "text/html; charset=Shift_JIS")), \
                    patch.object(sys, "argv", ["fetch.py", tmp, "article", "https://example.test/a"]):
                self.assertEqual(fetch.main(), 0)
            saved = (Path(tmp) / "article.md").read_text(encoding="utf-8")
        self.assertIn(self.TEXT, saved)
        self.assertNotIn("�", saved)


class MissingCommandTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sources = Path(tmp.name)
        self.stdout = io.StringIO()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(self.stdout))
        # どのコマンドも無い環境（pdfminer.six も無い）
        self.stack.enter_context(patch.object(fetch, "missing", side_effect=lambda *c: list(c)))
        self.stack.enter_context(patch.object(fetch, "has_pdfminer", return_value=False))

    def run_fetch(self, url):
        with patch.object(sys, "argv", ["fetch.py", str(self.sources), "paper", url]):
            return fetch.main()

    def test_pdf_without_tools_fails_with_reason(self):
        with patch.object(fetch, "download", return_value=(b"%PDF-1.4\nscan", "application/pdf")):
            self.assertEqual(self.run_fetch("https://example.test/scan.pdf"), 1)
        self.assertIn("pdftotext・pdftoppm・tesseract が無い", self.stdout.getvalue())
        self.assertEqual(list(self.sources.iterdir()), [])

    def test_pdf_with_pdfminer_names_only_ocr_tools(self):
        """pdfminer.six で文字の層を読めたなら、FAIL の理由に pdftotext を挙げない（#15）。"""
        with patch.object(fetch, "has_pdfminer", return_value=True), \
                patch.object(fetch, "run_tool", return_value=subprocess.CompletedProcess([], 0)), \
                patch.object(fetch, "download", return_value=(b"%PDF-1.4\nscan", "application/pdf")):
            self.assertEqual(self.run_fetch("https://example.test/scan.pdf"), 1)
        self.assertIn("。pdftoppm・tesseract が無い", self.stdout.getvalue())

    def test_youtube_without_ytdlp_fails_with_reason(self):
        self.assertEqual(self.run_fetch("https://www.youtube.com/watch?v=example"), 1)
        self.assertIn("yt-dlp が無い", self.stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
