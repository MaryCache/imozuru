"""fetch.py の再試行・OA API フォールバックと、ingest.py の取り込み検証を検査する。"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/imozuru/scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"imozuru_fb_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fetch = _load("fetch")
sys.modules["fetch"] = fetch
ingest = _load("ingest")


class _FakeResponse:
    def __init__(self, body: bytes, ctype: str = "text/html; charset=utf-8", length: int = 0):
        self._buf = io.BytesIO(body)
        self.headers = {"Content-Type": ctype}
        self.length = length

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _http_err(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    hdrs = Message()
    if retry_after is not None:
        hdrs["Retry-After"] = retry_after
    return urllib.error.HTTPError("https://example.test/a", code, f"Err {code}", hdrs, io.BytesIO(b""))


class RetryDownloadTests(unittest.TestCase):
    def test_retries_503_then_succeeds_and_marks_retried(self):
        calls = []
        sleeps = []

        def fake_open(req, timeout=90):
            calls.append(timeout)
            if len(calls) < 3:
                raise _http_err(503)
            return _FakeResponse(b"hello world")

        with mock.patch.object(fetch, "url_problem", return_value=None), \
                mock.patch.object(fetch._OPENER, "open", side_effect=fake_open), \
                mock.patch.object(fetch, "_sleep", side_effect=sleeps.append):
            body, ctype = fetch.download("https://example.test/a")

        self.assertEqual(body, b"hello world")
        self.assertIn("text/html", ctype)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(sleeps), 2)
        self.assertEqual(fetch._LAST_RETRIED, 2)

    def test_respects_retry_after_numeric_and_aborts_when_too_long(self):
        sleeps = []

        def fake_open_short(req, timeout=90):
            if not sleeps:
                raise _http_err(429, retry_after="3")
            return _FakeResponse(b"ok")

        with mock.patch.object(fetch, "url_problem", return_value=None), \
                mock.patch.object(fetch._OPENER, "open", side_effect=fake_open_short), \
                mock.patch.object(fetch, "_sleep", side_effect=sleeps.append):
            body, _ = fetch.download("https://example.test/rate")
        self.assertEqual(body, b"ok")
        self.assertEqual(sleeps, [3.0])

        # 上限を超える Retry-After は待たずに即失敗する。
        sleeps.clear()
        with mock.patch.object(fetch, "url_problem", return_value=None), \
                mock.patch.object(fetch._OPENER, "open", side_effect=_http_err(429, retry_after="3600")), \
                mock.patch.object(fetch, "_sleep", side_effect=sleeps.append):
            with self.assertRaises(urllib.error.HTTPError):
                fetch.download("https://example.test/rate")
        self.assertEqual(sleeps, [])

    def test_never_retries_permanent_4xx(self):
        for code in (400, 401, 403, 404):
            calls = []

            def fake_open(req, timeout=90, c=code):
                calls.append(c)
                raise _http_err(c)

            with mock.patch.object(fetch, "url_problem", return_value=None), \
                    mock.patch.object(fetch._OPENER, "open", side_effect=fake_open), \
                    mock.patch.object(fetch, "_sleep") as sleep_mock:
                with self.assertRaises(urllib.error.HTTPError):
                    fetch.download("https://example.test/perm")
            self.assertEqual(len(calls), 1)
            sleep_mock.assert_not_called()


class DoiAndOaFallbackTests(unittest.TestCase):
    def test_extract_doi_recognizes_known_patterns_and_ignores_non_doi(self):
        self.assertEqual(
            fetch.extract_doi("https://journals.sagepub.com/doi/pdf/10.1177/2041669517715474"),
            "10.1177/2041669517715474",
        )
        self.assertEqual(
            fetch.extract_doi("https://dl.acm.org/doi/pdf/10.1145/3843779.3844639"),
            "10.1145/3843779.3844639",
        )
        self.assertEqual(
            fetch.extract_doi("https://doi.org/10.1037/A0029333"),
            "10.1037/a0029333",
        )
        self.assertEqual(
            fetch.extract_doi("https://link.springer.com/content/pdf/10.1007/s00426-020-01334-z.pdf"),
            "10.1007/s00426-020-01334-z",
        )
        self.assertIsNone(fetch.extract_doi("https://example.test/blog/10.1234/not-a-doi-path"))

    def test_oa_rejects_mismatched_doi_zero_false_positives(self):
        wrong_epmc = json.dumps({
            "resultList": {"result": [{
                "doi": "10.1177/9999999999999999",
                "pmcid": "PMC9999999",
                "isOpenAccess": "Y",
            }]}
        }).encode()
        wrong_openalex = json.dumps({
            "doi": "https://doi.org/10.1177/9999999999999999",
            "best_oa_location": {"is_oa": True, "pdf_url": "https://repo.example.test/wrong.pdf"},
        }).encode()

        def fake_download(url, **kwargs):
            if "ebi.ac.uk" in url:
                return wrong_epmc, "application/json"
            if "openalex.org" in url:
                return wrong_openalex, "application/json"
            raise AssertionError(f"unexpected download of mismatched candidate: {url}")

        with tempfile.TemporaryDirectory() as tmp:
            dst = Path(tmp) / "sources"
            dst.mkdir()
            work = Path(tmp) / "work"
            work.mkdir()
            with mock.patch.object(fetch, "download", side_effect=fake_download), \
                    mock.patch.object(fetch, "url_problem", return_value=None):
                res = fetch._try_oa_fallback(
                    work, "paper", "https://journals.sagepub.com/doi/pdf/10.1177/2041669517715474",
                    dst, doi="10.1177/2041669517715474",
                )
        self.assertIsNone(res)

    def test_oa_accepts_exact_doi_jats_xml_and_rejects_mismatched_xml_doi(self):
        good_jats = (
            '<?xml version="1.0"?><article><front><article-meta>'
            '<article-id pub-id-type="doi">10.1177/2041669517715474</article-id>'
            '<title-group><article-title>Beauty in Paintings</article-title></title-group>'
            '</article-meta></front><body><p>' + ("Composition study sentence. " * 60) + '</p></body></article>'
        ).encode()
        bad_jats = good_jats.replace(b"2041669517715474", b"0000000000000000")

        with self.assertRaises(ValueError):
            fetch.jats_to_md(bad_jats, "https://journals.sagepub.com/doi/pdf/10.1177/2041669517715474",
                             "https://www.ebi.ac.uk/x", expected_doi="10.1177/2041669517715474")

        epmc_search = json.dumps({
            "resultList": {"result": [{
                "doi": "10.1177/2041669517715474",
                "pmcid": "PMC5536243",
                "isOpenAccess": "Y",
            }]}
        }).encode()

        def fake_download(url, **kwargs):
            if "journals.sagepub.com" in url:
                raise _http_err(403)
            if "rest/search" in url:
                return epmc_search, "application/json"
            if "PMC5536243/fullTextXML" in url:
                return good_jats, "application/xml"
            raise AssertionError(f"unexpected url: {url}")

        with tempfile.TemporaryDirectory() as tmp:
            dst = Path(tmp) / "sources"
            dst.mkdir()
            work = Path(tmp) / "work"
            work.mkdir()
            meta = {}
            buf = io.StringIO()
            with mock.patch.object(fetch, "download", side_effect=fake_download), \
                    mock.patch.object(fetch, "url_problem", return_value=None), \
                    contextlib.redirect_stdout(buf):
                rc = fetch.fetch_source(
                    work, "hayn-2017", "https://journals.sagepub.com/doi/pdf/10.1177/2041669517715474",
                    dst, meta=meta,
                )
            self.assertEqual(rc, 0)
            self.assertEqual(meta["via"], "oa-europepmc-xml")
            self.assertTrue((dst / "hayn-2017.md").is_file())
            self.assertIn("Beauty in Paintings", (dst / "hayn-2017.md").read_text(encoding="utf-8"))

    def test_challenge_page_rejected_as_wall(self):
        cf_html = b"<html><head><title>Just a moment...</title></head><body>Checking your browser before accessing.</body></html>"
        with tempfile.TemporaryDirectory() as tmp:
            dst = Path(tmp) / "sources"
            dst.mkdir()
            work = Path(tmp) / "work"
            work.mkdir()
            buf = io.StringIO()
            with mock.patch.object(fetch, "url_problem", return_value=None), \
                    mock.patch.object(fetch, "download", return_value=(cf_html, "text/html")), \
                    mock.patch.object(fetch, "pdf_save", return_value=False), \
                    contextlib.redirect_stdout(buf):
                rc = fetch.fetch_source(work, "cf-page", "https://example.test/cf", dst)
            self.assertEqual(rc, 1)
            self.assertIn("壁に当たった", buf.getvalue())


class IngestTests(unittest.TestCase):
    def test_inbox_ingests_html_md_with_url_and_rejects_wall_even_with_short_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            topic = Path(tmp) / "topic"
            sources = topic / "sources"
            inbox = Path(tmp) / "inbox"
            sources.mkdir(parents=True)
            inbox.mkdir()

            html_file = inbox / "article.html"
            html_file.write_text(
                '<!-- saved from url=(0035)https://example.test/lesson.htm -->\n'
                '<html><body><main><h1>Composition</h1><p>' + ("Visual rhythm and balance. " * 60) + '</p></main></body></html>',
                encoding="utf-8",
            )
            note_file = inbox / "short-note.md"
            note_file.write_text(
                "URL: https://example.test/note/1\n\n"
                "構成のポイント: 1. リズム 2. 重なり 3. 四隅のバリエーション\r\n",
                encoding="utf-8",
            )
            wall_file = inbox / "wall.html"
            wall_file.write_text(
                "<html><head><title>Just a moment...</title></head><body>Verify you are human</body></html>",
                encoding="utf-8",
            )

            buf = io.StringIO()
            err = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
                rc = ingest.ingest_inbox(inbox, sources, short_ok=True, move_processed=True)

            # wall.html があるため終了コード 1、正当な 2 件は保存され _processed へ移動済み。
            self.assertEqual(rc, 1)
            self.assertTrue((sources / "article.md").is_file())
            self.assertTrue((sources / "short-note.md").is_file())
            self.assertFalse((sources / "wall.md").exists())
            self.assertNotIn(b"\r\n", (sources / "short-note.md").read_bytes())
            self.assertTrue((inbox / "_processed" / "article.html").is_file())
            self.assertTrue((inbox / "_processed" / "short-note.md").is_file())
            self.assertTrue((inbox / "wall.html").is_file())

            log_lines = [json.loads(l) for l in (topic / "fetch-log.jsonl").read_text(encoding="utf-8").splitlines()]
            by_name = {r["name"]: r for r in log_lines}
            self.assertEqual(by_name["article"]["status"], "OK")
            self.assertEqual(by_name["article"]["via"], "user-browser")
            self.assertEqual(by_name["article"]["url"], "https://example.test/lesson.htm")
            self.assertEqual(by_name["short-note"]["status"], "OK")
            self.assertEqual(by_name["short-note"]["url"], "https://example.test/note/1")
            self.assertEqual(by_name["wall"]["status"], "FAIL")
            self.assertIn("壁に当たった", by_name["wall"]["reason"])

    def test_rejects_symlink_escape_and_overlapping_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret = root / "secret.txt"
            secret.write_text("secret data " * 200, encoding="utf-8")
            inbox = root / "inbox"
            sources = root / "topic" / "sources"
            inbox.mkdir()
            sources.mkdir(parents=True)
            try:
                (inbox / "escape.txt").symlink_to(secret)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"Symlinks unavailable: {exc}")

            buf = io.StringIO()
            err = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
                rc = ingest.ingest_inbox(inbox, sources)
            self.assertEqual(rc, 1)
            self.assertIn("外を指すリンク", buf.getvalue())
            self.assertFalse((sources / "escape.md").exists())

            with contextlib.redirect_stderr(err):
                rc_overlap = ingest.ingest_inbox(sources, sources)
            self.assertEqual(rc_overlap, 1)

    def test_rejects_fake_pdf_and_oversized_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sources = root / "topic" / "sources"
            sources.mkdir(parents=True)
            fake_pdf = root / "fake.pdf"
            fake_pdf.write_bytes(b"<html>not a pdf</html>")

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = ingest.ingest_file(fake_pdf, sources, name="fake")
            self.assertEqual(rc, 1)
            self.assertIn("PDF ではない", buf.getvalue())

            big = root / "big.txt"
            big.write_bytes(b"a" * 200)
            buf = io.StringIO()
            with mock.patch.object(fetch, "MAX_BYTES", 100), contextlib.redirect_stdout(buf):
                rc2 = ingest.ingest_file(big, sources, name="big")
            self.assertEqual(rc2, 1)
            self.assertIn("超える", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
