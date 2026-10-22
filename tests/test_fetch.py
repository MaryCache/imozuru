"""再取得が失敗したときの原文保全と、成功時の保存先を検査する。"""

import contextlib
import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "skills/imozuru/scripts/fetch.py"
SPEC = importlib.util.spec_from_file_location("imozuru_fetch", SCRIPT)
fetch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fetch)
# テストの宛先（example.test）は名前を引けない。インターネット上の宛先に引けたことにする。
# 宛先の検査そのものは UnsafeUrlTests が本物に近い引き先を渡して確かめる
PUBLIC_IP = "93.184.215.14"
fetch.resolve = lambda host: {PUBLIC_IP}


class FetchPreservationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sources = Path(tmp.name) / "sources"
        self.sources.mkdir()
        self.stdout = io.StringIO()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(self.stdout))
        self.stack.enter_context(patch.object(fetch, "HAS_AX", False))
        # 外部コマンドの有無で結果が変わらないように、既定では全部あることにする
        self.stack.enter_context(patch.object(fetch, "missing", return_value=[]))

    def seed(self, *suffixes):
        for suffix in suffixes:
            (self.sources / f"paper{suffix}").write_bytes(
                f"Original verified {suffix} source\n".encode() * 150)
        (self.sources / "unrelated.md").write_bytes(b"Unrelated source\n")

    def snapshot(self):
        return {p.name: p.read_bytes() for p in self.sources.iterdir() if p.is_file()}

    def assert_preserved(self, before):
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(any(p.is_dir() for p in self.sources.iterdir()))

    def run_fetch(self, url="https://example.test/source"):
        with patch.object(sys, "argv", [str(SCRIPT), str(self.sources), "paper", url]):
            return fetch.main()

    @staticmethod
    def pdf_text(text):
        def convert(pdf):
            pdf.with_suffix(".txt").write_text(text, encoding="utf-8")
            return text
        return convert

    @staticmethod
    def markdown(text):
        def convert(url, out, body=None, ctype=""):
            out.write_text(text, encoding="utf-8")
            return text
        return convert

    def test_short_text_retry_preserves_existing_source(self):
        for ctype, suffix in (("text/plain", ".md"), ("text/markdown", ".md"),
                              ("application/json", ".json"), ("text/csv", ".csv")):
            with self.subTest(ctype=ctype):
                self.seed(suffix)
                before = self.snapshot()
                with patch.object(fetch, "download", return_value=(b"Short response", ctype)):
                    self.assertEqual(self.run_fetch(), 1)
                self.assert_preserved(before)
        self.assertIn("FAIL\tpaper\t", self.stdout.getvalue())

    def test_html_wall_retry_preserves_existing_source(self):
        self.seed(".md")
        before = self.snapshot()
        body = b"<article>captcha " + b"Wall text " * 150 + b"</article>"
        with patch.object(fetch, "download", return_value=(body, "text/html")):
            self.assertEqual(self.run_fetch(), 1)
        self.assert_preserved(before)

    def test_short_pdf_retry_preserves_both_files(self):
        self.seed(".pdf", ".txt")
        before = self.snapshot()
        with patch.object(fetch, "download", return_value=(b"%PDF-1.4\nNew PDF", "application/pdf")), \
                patch.object(fetch, "pdf_to_text", side_effect=self.pdf_text("Short text " * 30)):
            self.assertEqual(self.run_fetch(), 1)
        self.assert_preserved(before)

    def test_short_youtube_retry_preserves_existing_transcript(self):
        self.seed(".md")
        before = self.snapshot()
        with patch.object(fetch, "youtube_to_md", side_effect=self.markdown("Short transcript")):
            self.assertEqual(self.run_fetch("https://www.youtube.com/watch?v=example"), 1)
        self.assert_preserved(before)

    def test_download_error_preserves_existing_source(self):
        self.seed(".md")
        before = self.snapshot()
        with patch.object(fetch, "download", side_effect=OSError("Connection failed")):
            self.assertEqual(self.run_fetch(), 1)
        self.assert_preserved(before)

    def test_conversion_exception_preserves_both_files(self):
        self.seed(".pdf", ".txt")
        before = self.snapshot()
        with patch.object(fetch, "download", return_value=(b"%PDF-1.4\nNew PDF", "application/pdf")), \
                patch.object(fetch, "pdf_to_text", side_effect=FileNotFoundError("pdftotext")):
            with self.assertRaises(FileNotFoundError):
                self.run_fetch()
        self.assert_preserved(before)

    def test_failed_arxiv_fallback_preserves_existing_files(self):
        self.seed(".pdf", ".txt", ".md")
        before = self.snapshot()
        with patch.object(fetch, "download", return_value=(b"%PDF-1.4\nNew PDF", "application/pdf")), \
                patch.object(fetch, "pdf_to_text", side_effect=self.pdf_text("")), \
                patch.object(fetch, "html_to_md", side_effect=self.markdown("Short HTML")) as html:
            self.assertEqual(self.run_fetch("https://arxiv.org/pdf/2601.12345v2"), 1)
        self.assertEqual(html.call_args.args[0], "https://arxiv.org/html/2601.12345v2")
        self.assert_preserved(before)

    def test_failed_redirected_pdf_preserves_existing_files(self):
        self.seed(".md", ".pdf", ".txt")
        before = self.snapshot()

        def invalid_pdf(url, out):
            out.write_bytes(b"Not a PDF")
            return True

        with patch.object(fetch, "download", return_value=(b"<html>Redirect</html>", "text/html")), \
                patch.object(fetch, "html_to_md", side_effect=self.markdown("%PDF")), \
                patch.object(fetch, "pdf_save", side_effect=invalid_pdf):
            self.assertEqual(self.run_fetch(), 1)
        self.assert_preserved(before)

    def test_hal_style_pdf_is_fetched_with_plain_user_agent(self):
        """HAL はブラウザの名乗りに HTML を、それ以外に PDF を返す。名乗りを変えて頼み直し、PDF を残す。"""
        pdf = b"%PDF-1.4\nHAL paper"

        def download(url, user_agent=fetch.BROWSER_UA):
            return (pdf, "application/pdf") if user_agent == fetch.PLAIN_UA else (b"<html>viewer</html>", "text/html")

        with patch.object(fetch, "download", side_effect=download), \
                patch.object(fetch, "html_to_md", side_effect=self.markdown("Viewer page " * 200)), \
                patch.object(fetch, "pdf_to_text", side_effect=self.pdf_text("Verified paper text. " * 200)):
            self.assertEqual(self.run_fetch(), 0)
        self.assertEqual((self.sources / "paper.pdf").read_bytes(), pdf)
        self.assertFalse((self.sources / "paper.md").exists())

    def test_image_only_pdf_keeps_the_html_text(self):
        """名乗りを変えて取れた PDF が文字の層の無い画像なら、HTML の本文を残す。"""
        def download(url, user_agent=fetch.BROWSER_UA):
            return (b"%PDF-1.4\nscan", "application/pdf") if user_agent == fetch.PLAIN_UA \
                else (b"<html>page</html>", "text/html")

        with patch.object(fetch, "download", side_effect=download), \
                patch.object(fetch, "html_to_md", side_effect=self.markdown("Article body. " * 200)), \
                patch.object(fetch, "pdf_to_text", side_effect=self.pdf_text("")), \
                patch.object(fetch, "ocr_pdf", return_value=("", "")):
            self.assertEqual(self.run_fetch(), 0)
        self.assertIn("Article body.", (self.sources / "paper.md").read_text(encoding="utf-8"))
        self.assertFalse((self.sources / "paper.pdf").exists())

    def test_arxiv_html_error_keeps_the_pdf_text(self):
        """arXiv の HTML 版が取れなくても、先に取れた PDF の本文は残す。"""
        text = "Short but real PDF text. " * 80  # 2000字。下限の3000字には届かない
        with patch.object(fetch, "download", return_value=(b"%PDF-1.4\npaper", "application/pdf")), \
                patch.object(fetch, "pdf_to_text", side_effect=self.pdf_text(text)), \
                patch.object(fetch, "html_to_md", side_effect=OSError("404")):
            self.assertEqual(self.run_fetch("https://arxiv.org/pdf/2601.12345"), 1)
        self.assertIn("本文が短すぎる（2000文字）", self.stdout.getvalue())
        self.assertFalse(any(p.name.startswith(".") for p in self.sources.iterdir()))

    def test_media_type_is_case_insensitive(self):
        body = b"Plain text line\n" * 100
        with patch.object(fetch, "download", return_value=(body, "Text/Plain; Charset=UTF-8")):
            self.assertEqual(self.run_fetch(), 0)
        self.assertEqual((self.sources / "paper.md").read_bytes(), body)

    def test_successful_text_retry_replaces_source_and_reports_final_path(self):
        for ctype, suffix in (("text/plain", ".md"), ("text/markdown", ".md"),
                              ("application/json", ".json"), ("text/csv", ".csv")):
            with self.subTest(ctype=ctype):
                self.seed(suffix)
                body = b"New verified text\n" * 100
                with patch.object(fetch, "download", return_value=(body, ctype)):
                    self.assertEqual(self.run_fetch(), 0)
                target = self.sources / f"paper{suffix}"
                self.assertEqual(target.read_text(encoding="utf-8"), body.decode())
                self.assertIn(f"OK\tpaper\t{target}\t", self.stdout.getvalue())
                self.assertFalse(any(p.is_dir() for p in self.sources.iterdir()))
                self.assertEqual((self.sources / "unrelated.md").read_bytes(), b"Unrelated source\n")

    def test_successful_html_retry_saves_verified_text(self):
        self.seed(".md")
        body = b"<article>" + b"New article text " * 100 + b"</article>"
        with patch.object(fetch, "download", return_value=(body, "text/html")):
            self.assertEqual(self.run_fetch(), 0)
        target = self.sources / "paper.md"
        self.assertIn("New article text", target.read_text(encoding="utf-8"))
        self.assertIn(f"OK\tpaper\t{target}\t", self.stdout.getvalue())
        self.assertFalse(any(p.is_dir() for p in self.sources.iterdir()))

    def test_successful_pdf_retry_replaces_both_files(self):
        self.seed(".pdf", ".txt")
        pdf = b"%PDF-1.4\nNew verified PDF"
        text = "New verified PDF text\n" * 200
        with patch.object(fetch, "download", return_value=(pdf, "application/pdf")), \
                patch.object(fetch, "pdf_to_text", side_effect=self.pdf_text(text)):
            self.assertEqual(self.run_fetch(), 0)
        self.assertEqual((self.sources / "paper.pdf").read_bytes(), pdf)
        target = self.sources / "paper.txt"
        self.assertEqual(target.read_text(encoding="utf-8"), text)
        self.assertIn(f"OK\tpaper\t{target}\t", self.stdout.getvalue())
        self.assertFalse(any(p.is_dir() for p in self.sources.iterdir()))
        self.assertEqual(set(self.snapshot()), {"paper.pdf", "paper.txt", "unrelated.md"})

    def test_failed_second_replace_restores_both_files(self):
        for existing in (True, False):
            with self.subTest(existing=existing):
                if existing:
                    self.seed(".pdf", ".txt")
                before = self.snapshot()
                replace = Path.replace

                def fail_text(path, target):
                    if path.name == "paper.txt" and path.parent != self.sources:
                        raise OSError("Cannot replace text")
                    return replace(path, target)

                with patch.object(fetch, "download", return_value=(b"%PDF-1.4\nNew PDF", "application/pdf")), \
                        patch.object(fetch, "pdf_to_text", side_effect=self.pdf_text("New PDF text\n" * 300)), \
                        patch.object(Path, "replace", fail_text):
                    with self.assertRaisesRegex(OSError, "Cannot replace text"):
                        self.run_fetch()
                self.assert_preserved(before)
                for suffix in (".pdf", ".txt"):
                    (self.sources / f"paper{suffix}").unlink(missing_ok=True)

    def test_failed_backup_preserves_pdf_and_destination_directory(self):
        self.seed(".pdf")
        before = self.snapshot()
        target = self.sources / "paper.txt"
        target.mkdir()
        (target / "original.txt").write_bytes(b"Keep directory contents")
        with patch.object(fetch, "download", return_value=(b"%PDF-1.4\nNew PDF", "application/pdf")), \
                patch.object(fetch, "pdf_to_text", side_effect=self.pdf_text("New PDF text\n" * 300)):
            with self.assertRaises(OSError):
                self.run_fetch()
        self.assertEqual((target / "original.txt").read_bytes(), b"Keep directory contents")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual({p.name for p in self.sources.iterdir()}, {*before, "paper.txt"})

    def test_successful_youtube_retry_replaces_transcript(self):
        self.seed(".md")
        text = "[00:00] New transcript\n" * 30
        with patch.object(fetch, "youtube_to_md", side_effect=self.markdown(text)):
            self.assertEqual(self.run_fetch("https://www.youtube.com/watch?v=example"), 0)
        target = self.sources / "paper.md"
        self.assertEqual(target.read_text(encoding="utf-8"), text)
        self.assertIn(f"OK\tpaper\t{target}\t", self.stdout.getvalue())
        self.assertFalse(any(p.is_dir() for p in self.sources.iterdir()))

    def test_bot_check_page_is_a_wall(self):
        # HAL（hal.science）の Anubis が返した 1,297 字のページ。壁の文に掛かることを確かめる
        self.seed(".md")
        before = self.snapshot()
        body = (b"<html><head><title>Making sure you&#39;re not a bot!</title></head><body>"
                b"<h1>Making sure you're not a bot!</h1><p>Loading...</p><p>You are seeing this because "
                b"the administrator of this website has set up Anubis to protect the server.</p>"
                + b"<p>Anubis is a compromise. Anubis uses a Proof-of-Work scheme.</p>" * 20
                + b"</body></html>")
        with patch.object(fetch, "download", return_value=(body, "text/html")):
            self.assertEqual(self.run_fetch("https://hal.science/hal-01577896"), 1)
        self.assertIn("FAIL\tpaper\t壁に当たった（you're not a bot）", self.stdout.getvalue())
        self.assert_preserved(before)

    JATS = """<?xml version="1.0"?>
<!DOCTYPE article PUBLIC "-//NLM//DTD JATS (Z39.96) Journal Archiving and Interchange DTD v1.4//EN" "JATS.dtd">
<article xmlns:xlink="http://www.w3.org/1999/xlink"><front>
<journal-meta><journal-title-group><journal-title>Mol Ecol</journal-title></journal-title-group></journal-meta>
<article-meta><article-id pub-id-type="pmcid">PMC1234567</article-id>
<article-id pub-id-type="doi">10.1111/mec.1</article-id>
<title-group><article-title>Environmental <italic>DNA</italic> for biomonitoring</article-title></title-group>
<contrib-group><contrib contrib-type="author"><name><surname>Pawlowski</surname><given-names>Jan</given-names></name></contrib></contrib-group>
<pub-date><year>2021</year></pub-date>
<abstract><p>Short abstract text.</p></abstract></article-meta></front>
<body><sec><title>Introduction</title><p>First paragraph (<xref ref-type="bibr" rid="r1">Smith, 2020</xref>).</p>
<p>Second paragraph with a <ext-link xlink:href="https://example.org/data">dataset</ext-link>.</p>
<sec><title>Sub section</title><p>Nested paragraph.</p></sec></sec>
<sec><title>Results</title><p>{filler}</p>
<fig><label>Figure 1</label><caption><p>Caption of the figure.</p></caption><graphic xlink:href="f1.jpg"/></fig></sec></body>
<back><ref-list><title>References</title><ref><label>1</label><element-citation><person-group>
<name><surname>Smith</surname><given-names>J</given-names></name></person-group>
<article-title>A paper</article-title><source>Nature</source><year>2020</year>
<pub-id pub-id-type="doi">10.1038/x</pub-id></element-citation></ref></ref-list></back></article>
""".replace("{filler}", "Results text. " * 100)

    def test_pmc_url_is_fetched_from_europe_pmc_as_lines(self):
        self.seed(".md")
        with patch.object(fetch, "download", return_value=(self.JATS.encode(), "application/xml")) as dl:
            self.assertEqual(self.run_fetch("https://pmc.ncbi.nlm.nih.gov/articles/PMC1234567/"), 0)
        self.assertEqual(dl.call_args.args[0],
                         "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC1234567/fullTextXML")
        lines = (self.sources / "paper.md").read_text(encoding="utf-8").splitlines()
        # 1行目は題名だけ。雑誌名や ID がつながらない
        self.assertEqual(lines[0], "# Environmental DNA for biomonitoring")
        self.assertIn("Mol Ecol / 2021 / https://doi.org/10.1111/mec.1 / PMC1234567", lines)
        self.assertIn("著者: Jan Pawlowski", lines)
        for line in ("## Abstract", "Short abstract text.", "## Introduction",
                     "First paragraph (Smith, 2020).",
                     "Second paragraph with a [dataset](https://example.org/data).",
                     "### Sub section", "Nested paragraph.", "## Results",
                     "[Figure 1] Caption of the figure.", "## References",
                     "[1] Smith J A paper Nature 2020 10.1038/x https://doi.org/10.1038/x"):
            self.assertIn(line, lines)
        self.assertLess(lines.index("## Introduction"), lines.index("### Sub section"))
        self.assertIn("OK\tpaper\t", self.stdout.getvalue())

    def test_pmc_without_europe_pmc_full_text_fails_and_preserves(self):
        self.seed(".md")
        before = self.snapshot()
        error = fetch.urllib.error.HTTPError("https://www.ebi.ac.uk/", 404, "Not Found", {}, None)
        with patch.object(fetch, "download", side_effect=error):
            self.assertEqual(self.run_fetch("https://www.ncbi.nlm.nih.gov/pmc/articles/PMC7654321/"), 1)
        self.assertIn("FAIL\tpaper\tEurope PMC に全文 XML が無い", self.stdout.getvalue())
        self.assert_preserved(before)

    def test_pmc_is_recognized_by_host_not_anywhere_in_url(self):
        for base in ("https://pmc.ncbi.nlm.nih.gov/articles/",
                     "https://www.ncbi.nlm.nih.gov/pmc/articles/",
                     "https://europepmc.org/article/PMC/"):
            for suffix in ("PMC1234567", "pmc1234567/"):
                with self.subTest(url=base + suffix):
                    self.assertEqual(fetch.pmc_id(base + suffix), "PMC1234567")
            for suffix in ("PMC1234567fake", "PMC1234567/../../articles/PMC7654321/",
                           "../PMC1234567/", "PMC1234567/./", "PMC1234567//"):
                with self.subTest(url=base + suffix):
                    self.assertIsNone(fetch.pmc_id(base + suffix))
        # 他人のページの URL に書き込まれた PMCID の本文を、そのページの原文として保存しない
        self.assertIsNone(fetch.pmc_id("https://evil.example/?next=pmc.ncbi.nlm.nih.gov/articles/PMC1234567/"))
        self.assertIsNone(fetch.pmc_id("https://pmc.ncbi.nlm.nih.gov.evil.example/articles/PMC1234567/"))

    def test_jats_with_entity_declaration_is_refused(self):
        self.seed(".md")
        before = self.snapshot()
        for encoding, bom in (("utf-8", b""), ("utf-16le", b"\xff\xfe"), ("utf-16be", b"\xfe\xff")):
            for declaration in ('<!ENTITY x "xx">', '<!ENTITY x SYSTEM "file:///etc/passwd">'):
                with self.subTest(encoding=encoding, declaration=declaration):
                    xml = bom + (f'<?xml version="1.0"?><!DOCTYPE article [{declaration}]>'
                                 '<article>&x;</article>').encode(encoding)
                    with self.assertRaisesRegex(ValueError, "ENTITY"):
                        fetch.jats_to_md(xml, "https://example.org/", "https://example.org/fullTextXML")
                    with patch.object(fetch, "download", return_value=(xml, "application/xml")):
                        self.assertEqual(self.run_fetch("https://pmc.ncbi.nlm.nih.gov/articles/PMC1234567/"), 1)
                    self.assert_preserved(before)
        self.assertIn("ENTITY", self.stdout.getvalue())

    def test_jats_pmcid_mismatch_fails_and_preserves(self):
        self.seed(".md")
        before = self.snapshot()
        xml = self.JATS.replace('>PMC1234567</article-id>', '>PMC7654321</article-id>').encode()
        with patch.object(fetch, "download", return_value=(xml, "application/xml")):
            self.assertEqual(self.run_fetch("https://pmc.ncbi.nlm.nih.gov/articles/PMC1234567/"), 1)
        self.assertIn("PMCID", self.stdout.getvalue())
        self.assert_preserved(before)

    def test_jats_pmcid_accepts_optional_prefix_and_missing_id(self):
        for article_id in ('<article-id pub-id-type="pmcid">1234567</article-id>',
                           '<article-id pub-id-type="pmcid">pMc1234567</article-id>', ""):
            with self.subTest(article_id=article_id):
                xml = self.JATS.replace('<article-id pub-id-type="pmcid">PMC1234567</article-id>', article_id)
                with patch.object(fetch, "download", return_value=(xml.encode(), "application/xml")):
                    self.assertEqual(self.run_fetch("https://pmc.ncbi.nlm.nih.gov/articles/PMC1234567/"), 0)

    def test_jats_links_follow_html_safety_rules(self):
        for tag in ("ext-link", "uri"):
            for href, expected in (("javascript:alert(1)", "label [x]\\y"),
                                   ("data:text/html,x", "label [x]\\y"),
                                   ("https://example.org/a (1)",
                                    r"[label \[x\]\\y](https://example.org/a%20%281%29)")):
                with self.subTest(tag=tag, href=href):
                    xml = (f'<article xmlns:xlink="http://www.w3.org/1999/xlink"><body><p>'
                           f'<{tag} xlink:href="{href}">label [x]\\y</{tag}>'
                           '</p></body></article>').encode()
                    text = fetch.jats_to_md(xml, "https://example.org/", "https://example.org/fullTextXML")
                    self.assertEqual(text.splitlines()[-1], expected)

    def test_malformed_hrefs_keep_text_without_failing(self):
        # 全角の「＃」や閉じていない「[」は urlsplit が ValueError を出す。リンク1本で変換全体を落とさない
        for href in ("https://example.org＃frag", "https://[x"):
            with self.subTest(href=href):
                self.assertEqual(fetch.md_link("label", href), "label")
                parser = fetch._TextExtractor("https://example.org/")
                parser.feed(f'<p><a href="{href}">bad</a> <a href="https://ok.example/">ok</a></p>')
                self.assertIn("[ok](https://ok.example/)", parser.text())

    def test_jats_tree_limits_fail_and_preserve(self):
        self.seed(".md")
        before = self.snapshot()
        for limit in ("JATS_MAX_ELEMENTS", "JATS_MAX_DEPTH"):
            with self.subTest(limit=limit), patch.object(fetch, limit, 3):
                with patch.object(fetch, "download", return_value=(self.JATS.encode(), "application/xml")):
                    self.assertEqual(self.run_fetch("https://pmc.ncbi.nlm.nih.gov/articles/PMC1234567/"), 1)
                self.assert_preserved(before)
                self.assertIn("ValueError", self.stdout.getvalue())

    def test_jats_tree_limits_allow_boundary(self):
        xml = b'<article><body><p>Body</p><p>Text</p></body></article>'
        with patch.object(fetch, "JATS_MAX_ELEMENTS", 4), patch.object(fetch, "JATS_MAX_DEPTH", 3):
            text = fetch.jats_to_md(xml, "https://example.org/", "https://example.org/fullTextXML")
        self.assertTrue(text.endswith("Body\nText\n"))

    def test_failed_first_fetch_leaves_no_partial_source(self):
        with patch.object(fetch, "download", return_value=(b"Short response", "text/plain")):
            self.assertEqual(self.run_fetch(), 1)
        self.assertEqual(list(self.sources.iterdir()), [])

    def test_non_http_url_is_rejected_without_reading(self):
        secret = Path(self.sources.parent) / "secret.txt"
        secret.write_text("local secret\n" * 200, encoding="utf-8")
        for url in (secret.as_uri(), "FILE://" + secret.as_posix(),
                    "ftp://example.test/paper.txt", "-o/tmp/x"):
            with self.subTest(url=url):
                with patch.object(fetch, "download") as download:
                    self.assertEqual(self.run_fetch(url), 1)
                download.assert_not_called()
                self.assertEqual(list(self.sources.iterdir()), [])
        self.assertIn("http(s) の URL だけを取りに行く", self.stdout.getvalue())


class OptionInjectionTests(unittest.TestCase):
    """外のコマンドへ渡す URL は「--」の直後に置く。「-」で始まる URL をオプションとして読ませない。"""

    URL = "--exec=touch /tmp/pwned"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name) / "page.md"

    def capture(self, call):
        with patch.object(fetch, "run_tool",
                          return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            call()
        return [c.args[0] for c in run.call_args_list]

    def assert_url_after_separator(self, cmds):
        self.assertTrue(cmds)
        for cmd in cmds:
            with self.subTest(cmd=cmd):
                self.assertEqual(cmd[-2:], ["--", self.URL])

    def test_ax_gets_a_file_never_the_url(self):
        """ax には取ってきた HTML のファイルだけを渡す。URL を渡すと ax が自分で通信する。"""
        with patch.object(fetch, "HAS_AX", True):
            cmds = self.capture(lambda: fetch.html_to_md(self.URL, self.out, b"<p>body</p>", "text/html"))
        self.assertEqual(len(cmds), 1)
        self.assertNotIn(self.URL, cmds[0])
        self.assertEqual(cmds[0][-2], "--")
        self.assertTrue(Path(cmds[0][-1]).is_absolute())

    def test_yt_dlp(self):
        self.assert_url_after_separator(self.capture(lambda: fetch.youtube_to_md(self.URL, self.out)))


class YoutubeCaptionTests(unittest.TestCase):
    """yt-dlp の呼び出しを差し替えて、どの字幕を指定するかを検査する。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name) / "talk.md"

    def run_youtube(self, language, manual=None, auto=None):
        """manual と auto は動画が持つ字幕（言語 → 文）。指定されて、かつ有るものだけを .vtt に書く。"""
        asked = []

        def yt_dlp(cmd, **kwargs):
            flag = next(c for c in cmd if c.startswith("--write-"))
            langs = cmd[cmd.index("--sub-langs") + 1]
            asked.append((flag, langs))
            tracks = (auto if flag == "--write-auto-subs" else manual) or {}
            folder = Path(cmd[cmd.index("-o") + 1]).parent
            for lang in langs.split(","):
                if lang in tracks:
                    (folder / f"v.{lang}.vtt").write_text(
                        f"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n{tracks[lang]}\n", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, f"Talk\tSpeaker\t20220820\t{language}\n", "")

        with patch.object(fetch, "run_tool", side_effect=yt_dlp):
            body = fetch.youtube_to_md("https://www.youtube.com/watch?v=example", self.out)
        return asked, body

    def test_auto_captions_ask_only_the_spoken_language(self):
        # 吹き替えの音声にも ja-orig が付く。中身は翻訳なので取らない
        asked, body = self.run_youtube("en-US", auto={"en-orig": "English speech", "ja-orig": "吹き替え"})
        self.assertEqual(asked, [("--write-subs", "ja,en"), ("--write-auto-subs", "en-orig")])
        self.assertEqual(body, "[00:01] English speech")
        text = self.out.read_text(encoding="utf-8")
        self.assertIn("\nSpeaker / 20220820\n字幕: 自動", text)
        self.assertIn("v.en-orig.vtt", text)

    def test_japanese_auto_captions(self):
        asked, body = self.run_youtube("ja", auto={"ja-orig": "日本語の音声"})
        self.assertEqual(asked[-1], ("--write-auto-subs", "ja-orig"))
        self.assertEqual(body, "[00:01] 日本語の音声")

    def test_unknown_language_asks_original_japanese_or_english(self):
        asked, body = self.run_youtube("", auto={"en-orig": "English speech"})
        self.assertEqual(asked[-1], ("--write-auto-subs", "ja-orig,en-orig"))
        self.assertEqual(body, "[00:01] English speech")

    def test_manual_captions_skip_auto_captions(self):
        asked, body = self.run_youtube("en", manual={"ja": "手動の日本語"}, auto={"en-orig": "English"})
        self.assertEqual(asked, [("--write-subs", "ja,en")])
        self.assertEqual(body, "[00:01] 手動の日本語")
        self.assertIn("字幕: 手動 v.ja.vtt", self.out.read_text(encoding="utf-8"))


class CjkPdfFloorTests(unittest.TestCase):
    """和文の PDF は 3,000 字に届かなくても、HTML と同じ下限を超えていれば残す。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sources = Path(tmp.name) / "sources"
        self.sources.mkdir()
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        stack.enter_context(patch.object(fetch, "missing", return_value=[]))

    def fetch_pdf(self, text):
        def convert(pdf):
            pdf.with_suffix(".txt").write_text(text, encoding="utf-8")
            return text
        argv = [str(SCRIPT), str(self.sources), "paper", "https://example.test/paper.pdf"]
        with patch.object(sys, "argv", argv), \
                patch.object(fetch, "download", return_value=(b"%PDF-1.4\nshort", "application/pdf")), \
                patch.object(fetch, "pdf_to_text", side_effect=convert):
            return fetch.main()

    def test_short_japanese_pdf_is_kept(self):
        text = "日本語の本文です。\n" * 150  # 1,500字
        self.assertLess(len(text), fetch.MIN_TEXT_CHARS)
        self.assertEqual(self.fetch_pdf(text), 0)
        self.assertEqual((self.sources / "paper.txt").read_text(encoding="utf-8"), text)

    def test_short_english_pdf_still_fails(self):
        text = "English body text.\n" * 120  # 2,280字
        self.assertGreater(len(text), fetch.MIN_HTML_CHARS)
        self.assertEqual(self.fetch_pdf(text), 1)
        self.assertEqual(list(self.sources.iterdir()), [])

    def test_very_short_japanese_pdf_still_fails(self):
        text = "日本語。\n" * 150  # 750字
        self.assertEqual(self.fetch_pdf(text), 1)
        self.assertEqual(list(self.sources.iterdir()), [])

    def test_english_pdf_with_japanese_heading_is_not_cjk(self):
        text = "日本語の見出し\n" + "English body text.\n" * 100
        self.assertFalse(fetch.mostly_cjk(text))
        self.assertTrue(fetch.mostly_cjk("全角の１２３"))


class NonAsciiUrlTests(unittest.TestCase):
    """日本語の字をそのまま含む URL も、符号化して取りに行く（イシュー #9）。"""

    def requested(self, url):
        seen = []

        def open_(req, timeout):
            seen.append(req.full_url)
            res = unittest.mock.MagicMock()
            res.__enter__.return_value.read.return_value = b"body"
            return res

        with patch.object(fetch._OPENER, "open", side_effect=open_):
            fetch.download(url)
        return seen[0]

    def test_japanese_path_and_query_are_encoded(self):
        self.assertEqual(self.requested("https://kotobank.jp/word/漆工芸"),
                         "https://kotobank.jp/word/%E6%BC%86%E5%B7%A5%E8%8A%B8")
        self.assertEqual(self.requested("https://example.test/search?q=和竿&page=2"),
                         "https://example.test/search?q=%E5%92%8C%E7%AB%BF&page=2")

    def test_encoded_url_is_unchanged(self):
        for url in ("https://kotobank.jp/word/%E6%BC%86%E5%B7%A5%E8%8A%B8",
                    "https://www.youtube.com/watch?v=jPCV4GKX9Dw&t=30s",
                    "https://example.test/a%20b/c?x=1+2&y=%2F"):
            with self.subTest(url=url):
                self.assertEqual(self.requested(url), url)


if __name__ == "__main__":
    unittest.main()
