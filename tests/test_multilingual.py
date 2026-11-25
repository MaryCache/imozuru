"""海外言語の一次資料への対応を検査する。

取得（fetch.py）・題名検索（chain.py）・引用の照合と表示（report.py・skin.py）・
一覧の検索（dashboard.py）が、中国語・韓国語・欧州の言語・キリル文字・アラビア文字・ヘブライ文字で
壊れないこと、そして宛先の検査（SSRF 対策）が国際化ドメイン名でも保たれることを見る。
"""

import codecs
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/imozuru/scripts"
sys.path.insert(0, str(SCRIPTS))


def load(name: str):
    """スクリプトを、ほかの試験のモジュールと状態（差し替えた resolve など）を共有しない別の実体として読む。"""
    spec = importlib.util.spec_from_file_location(f"multilingual_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fetch = load("fetch")
chain = load("chain")
report = load("report")
skin = load("skin")
dash = load("dashboard")

PUBLIC_IP = "93.184.215.14"


class HangulFloorTests(unittest.TestCase):
    """韓国語の PDF も、日本語・中国語と同じ 1,000 字の下限で判定する。"""

    KOREAN = "한국어 논문의 본문입니다. " * 90  # 空白を除いても 1,000 字を超え、3,000 字未満

    def test_hangul_counts_as_cjk(self):
        self.assertTrue(fetch.mostly_cjk(self.KOREAN))
        # 字母（U+1100 台）と互換字母（U+3130 台）も数える
        self.assertTrue(fetch.mostly_cjk("각" * 30))
        self.assertTrue(fetch.mostly_cjk("ㄱㅏ" * 30))

    def test_english_is_not_cjk(self):
        self.assertFalse(fetch.mostly_cjk("An English paper body. " * 90))

    def test_korean_pdf_passes_with_html_floor(self):
        self.assertGreaterEqual(len(self.KOREAN), fetch.MIN_HTML_CHARS)
        self.assertLess(len(self.KOREAN), fetch.MIN_TEXT_CHARS)
        self.assertEqual(fetch.content_problem(self.KOREAN, is_pdf=True), (None, None))

    def test_english_pdf_of_the_same_length_is_still_short(self):
        english = ("This is an English paper body. " * 60)[:len(self.KOREAN)]
        self.assertEqual(fetch.content_problem(english, is_pdf=True)[0], "short")

    def test_short_korean_pdf_is_still_short(self):
        self.assertEqual(fetch.content_problem("한국어 " * 20, is_pdf=True)[0], "short")


class DeclaredLanguageTests(unittest.TestCase):
    """_declared_lang_codecs: html の lang と Content-Language から文字コードの候補を引く。"""

    def codecs(self, head: str) -> list[str]:
        return fetch._declared_lang_codecs(head.encode("ascii"))

    def test_traditional_chinese_regions(self):
        for tag in ("zh-TW", "zh-HK", "zh-Hant", "zh_tw"):
            with self.subTest(tag=tag):
                self.assertEqual(self.codecs(f'<html lang="{tag}">'), ["big5hkscs", "big5", "gb18030"])

    def test_simplified_chinese(self):
        for tag in ("zh", "zh-CN", "zh-Hans", "zh-SG"):
            with self.subTest(tag=tag):
                self.assertEqual(self.codecs(f"<html lang='{tag}'>"), ["gb18030", "gbk"])

    def test_other_languages(self):
        expected = {"ko": ["cp949", "euc_kr"], "ru": ["cp1251", "koi8_r", "koi8_u"], "uk": ["cp1251", "koi8_r", "koi8_u"],
                    "bg": ["cp1251", "koi8_r", "koi8_u"], "ar": ["cp1256"], "he": ["cp1255"], "th": ["cp874"],
                    "tr": ["cp1254"], "vi": ["cp1258"], "pl": ["cp1250"], "cs": ["cp1250"], "hu": ["cp1250"],
                    "ro": ["cp1250"], "de": ["cp1252", "latin-1"], "fr": ["cp1252", "latin-1"],
                    "es": ["cp1252", "latin-1"], "pt": ["cp1252", "latin-1"], "it": ["cp1252", "latin-1"],
                    "nl": ["cp1252", "latin-1"], "ja": ["cp932", "euc_jp"]}
        for tag, names in expected.items():
            with self.subTest(tag=tag):
                self.assertEqual(self.codecs(f'<html lang="{tag}">'), names)

    def test_region_falls_back_to_primary_subtag(self):
        self.assertEqual(self.codecs('<html lang="de-AT">'), ["cp1252", "latin-1"])
        self.assertEqual(self.codecs("<html lang=ru-RU>"), ["cp1251", "koi8_r", "koi8_u"])

    def test_xml_lang_and_uppercase_tag(self):
        self.assertEqual(self.codecs("<HTML xml:lang='ko'>"), ["cp949", "euc_kr"])

    def test_content_language_meta_in_both_attribute_orders(self):
        self.assertEqual(self.codecs('<meta http-equiv="Content-Language" content="ru">'),
                         ["cp1251", "koi8_r", "koi8_u"])
        self.assertEqual(self.codecs('<meta content="ar" http-equiv="content-language">'), ["cp1256"])

    def test_unrelated_meta_and_attributes_are_ignored(self):
        self.assertEqual(self.codecs('<meta name="description" content="ru">'), [])
        self.assertEqual(self.codecs('<html data-lang="ru">'), [])
        self.assertEqual(self.codecs('<html lang="xx">'), [])
        self.assertEqual(self.codecs("<p>lang=ru</p>"), [])

    def test_hint_beyond_the_head_is_ignored(self):
        late = '<html>' + " " * 4100 + '<html lang="ru">'
        self.assertEqual(self.codecs(late), [])

    def test_html_and_meta_candidates_are_merged_without_duplicates(self):
        head = '<html lang="ru"><meta http-equiv="Content-Language" content="uk">'
        self.assertEqual(self.codecs(head), ["cp1251", "koi8_r", "koi8_u"])


class DecodeByLanguageTests(unittest.TestCase):
    """文字コードを名乗らない古いページを、言語の名乗りから正しく読む。"""

    def page(self, lang: str, text: str, codec: str) -> bytes:
        return f'<html lang="{lang}"><body>{text}</body></html>'.encode(codec)

    def test_big5_page_without_charset(self):
        text = "繁體中文的測試文章"
        self.assertIn(text, fetch.decode_text(self.page("zh-TW", text, "big5")))

    def test_gbk_page_without_charset(self):
        text = "简体中文的测试文章"
        self.assertIn(text, fetch.decode_text(self.page("zh-CN", text, "gbk")))

    def test_big5_page_with_three_subtag_language(self):
        text = "繁體中文的測試文章"
        for tag in ("zh-Hant-TW", "zh-Hant-HK"):
            with self.subTest(tag=tag):
                self.assertIn(text, fetch.decode_text(self.page(tag, text, "big5")))
        self.assertEqual(fetch._declared_lang_codecs(b'<html lang="zh-Hant-TW">'), ["big5hkscs", "big5", "gb18030"])
        self.assertEqual(fetch._declared_lang_codecs(b'<html lang="zh-Hans-CN">'), ["gb18030", "gbk"])

    def test_english_cp1252_page_is_not_read_as_cp932(self):
        self.assertEqual(fetch._declared_lang_codecs(b'<html lang="en">'), ["cp1252", "latin-1"])
        self.assertIn("It\u2019s", fetch.decode_text(b'<html lang="en"><body>It\x92s</body></html>'))
        self.assertEqual(fetch._declared_lang_codecs(b'<html lang="el-GR">'), ["cp1253"])
        self.assertIn("Ελλάδα", fetch.decode_text('<html lang="el"><body>Ελλάδα</body></html>'.encode("cp1253")))

    def test_korean_cp949_page_without_charset(self):
        text = "한국어 시험 문장"
        self.assertIn(text, fetch.decode_text(self.page("ko", text, "cp949")))

    def test_russian_cp1251_page_without_charset(self):
        text = "Тестовая статья на русском языке"
        self.assertIn(text, fetch.decode_text(self.page("ru", text, "cp1251")))

    def test_arabic_cp1256_page_without_charset(self):
        text = "مقالة اختبار"
        self.assertIn(text, fetch.decode_text(self.page("ar", text, "cp1256")))

    def test_hebrew_cp1255_page_without_charset(self):
        text = "מאמר בדיקה"
        self.assertIn(text, fetch.decode_text(self.page("he", text, "cp1255")))

    def test_content_language_meta_alone_is_enough(self):
        text = "Тестовая статья"
        body = f'<meta http-equiv="Content-Language" content="ru"><p>{text}</p>'.encode("cp1251")
        self.assertIn(text, fetch.decode_text(body))

    def test_declared_charset_wins_over_language_hint(self):
        # lang は ru でも、実際の文字コードは名乗りどおり koi8-r。cp1251 では別の字に化ける
        text = "Тестовая статья"
        body = f'<html lang="ru"><body>{text}</body></html>'.encode("koi8_r")
        self.assertIn(text, fetch.decode_text(body, "text/html; charset=koi8-r"))

    def test_utf8_still_wins_over_language_hint(self):
        text = "한국어 시험 문장"
        self.assertIn(text, fetch.decode_text(self.page("ru", text, "utf-8")))

    def test_japanese_pages_are_unchanged(self):
        text = "日本語の記事です。髙橋①"
        self.assertEqual(fetch.decode_text(text.encode("cp932"), "text/html; charset=Shift_JIS"), text)
        self.assertEqual(fetch.decode_text(text[:7].encode("euc_jp")), text[:7])

    def test_generic_fallback_reads_gb18030_four_byte_sequences(self):
        # 名乗りが何も無く、cp932 と euc_jp では読めない並び（0x81 0x30 は両方とも不正）
        self.assertEqual(fetch.decode_text("\u0080".encode("gb18030")), "\u0080")

    def test_generic_fallback_reads_cp1252(self):
        self.assertEqual(fetch.decode_text(b"caf\xe9"), "café")

    def test_undecodable_bytes_still_return_text(self):
        # どの候補でも読めない並びは、前と同じく置換文字のいちばん少ない読みで返す（落ちない）
        self.assertIsInstance(fetch.decode_text(b"\x81 plain \x8d\x90"), str)

    def test_language_hint_cannot_introduce_non_document_codecs(self):
        for names in fetch._LANG_CODECS.values():
            for name in names:
                self.assertIn(codecs.lookup(name).name, fetch._DOCUMENT_CODECS)


class AsciiHostTests(unittest.TestCase):
    def test_ascii_is_unchanged(self):
        self.assertEqual(fetch._ascii_host("example.com"), "example.com")
        self.assertEqual(fetch._ascii_host("xn--bcher-kva.example"), "xn--bcher-kva.example")

    def test_non_ascii_becomes_punycode(self):
        self.assertEqual(fetch._ascii_host("bücher.example"), "xn--bcher-kva.example")
        self.assertEqual(fetch._ascii_host("日本語.jp"), "xn--wgv71a119e.jp")
        self.assertEqual(fetch._ascii_host("пример.рф"), "xn--e1afmkfd.xn--p1ai")

    def test_ideographic_full_stop_separates_labels(self):
        # 「。」は IDNA ではラベルの区切り。127。0。0。1 は 127.0.0.1 になる
        self.assertEqual(fetch._ascii_host("127。0。0。1"), "127.0.0.1")
        self.assertEqual(fetch._ascii_host("１２７．０．０．１"), "127.0.0.1")

    def test_invalid_hosts_raise_value_error(self):
        for bad in ("", "a..b.例", "exa\x07mple.例", "exa\x07mple.com", "a\x7f.例", "例" * 70 + ".jp"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                fetch._ascii_host(bad)


class IdnUrlTests(unittest.TestCase):
    """国際化ドメイン名でも、検査（url_problem）と接続（ascii_url）が同じ ASCII のホストを見る。"""

    def setUp(self):
        self.seen: list[str] = []

        def resolver(host):
            self.seen.append(host)
            return {"127.0.0.1"} if host == "127.0.0.1" else {PUBLIC_IP}

        patcher = patch.object(fetch, "resolve", resolver)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("IMOZURU_ALLOW_PRIVATE", None)

    def test_idn_host_is_resolved_as_punycode(self):
        self.assertIsNone(fetch.url_problem("https://bücher.example/書籍"))
        self.assertEqual(self.seen, ["xn--bcher-kva.example"])

    def test_ascii_host_is_resolved_unchanged(self):
        self.assertIsNone(fetch.url_problem("https://Example.com/a"))
        self.assertEqual(self.seen, ["example.com"])

    def test_fullwidth_loopback_is_blocked(self):
        for url in ("http://127。0。0。1/", "http://１２７．０．０．１/", "http://127．0．0．1:8080/x"):
            with self.subTest(url=url):
                self.seen.clear()
                problem = fetch.url_problem(url)
                self.assertIsNotNone(problem)
                self.assertIn("127.0.0.1", problem)
                self.assertEqual(self.seen, ["127.0.0.1"])

    def test_malformed_idn_is_rejected_without_resolving(self):
        for url in ("http://a..b.例/", "http://exa\x07mple.例/", "http://" + "例" * 70 + ".jp/"):
            with self.subTest(url=url):
                self.assertEqual(fetch.url_problem(url), "URL の形が壊れている")
        self.assertEqual(self.seen, [])

    def test_malformed_idn_is_rejected_even_when_private_hosts_are_allowed(self):
        os.environ["IMOZURU_ALLOW_PRIVATE"] = "1"
        self.assertEqual(fetch.url_problem("http://a..b.例/"), "URL の形が壊れている")
        self.assertIsNone(fetch.url_problem("http://bücher.example/"))

    def test_userinfo_and_percent_are_still_rejected(self):
        for url in ("http://user@bücher.example/", "http://bücher.example%2f@evil.example/",
                    "http://b%C3%BCcher.example/", "http://bücher.example@127.0.0.1/"):
            with self.subTest(url=url):
                self.assertIn("「@」か「%」", fetch.url_problem(url) or "")
        self.assertEqual(self.seen, [])

    def test_scheme_check_remains(self):
        self.assertIsNotNone(fetch.url_problem("file://bücher.example/etc/passwd"))

    def test_non_ldh_hosts_after_idna_are_rejected(self):
        """全角の「％」（NFKC で「%」）や NBSP は IDNA を通っても ASCII の記号・空白で残る。ホストとして通さない。"""
        for url in ("http://％61.evil.example/", "http://ev\u00a0il.example/", "http://exa mple.example/",
                    "http://a\uff05b.example/", "http://a_b.example/", "http://-a.example/", "http://a-.example/",
                    "http://a.example.."):
            with self.subTest(url=url):
                self.seen.clear()
                self.assertEqual(fetch.url_problem(url), "URL の形が壊れている")
                self.assertEqual(self.seen, [])
        for bad in ("％61.evil.example", "ev\u00a0il.example", "a b.example", "a%b.example", "a.b..", "."):
            with self.subTest(host=bad), self.assertRaises(ValueError):
                fetch._ascii_host(bad)

    def test_ldh_hosts_and_ip_literals_still_pass(self):
        for host in ("example.com", "example.com.", "a-b.example", "xn--bcher-kva.example", "1.example", "127.0.0.1",
                     "::1", "2001:db8::1"):
            with self.subTest(host=host):
                self.assertEqual(fetch._ascii_host(host), host)
        self.assertIsNone(fetch.url_problem("https://example.com./a"))

    def test_invalid_ports_are_rejected(self):
        for url in ("http://bücher.example:99999/", "http://bücher.example:abc/", "http://bücher.example:８０/",
                    "http://example.com:99999/", "http://example.com:abc/", "http://example.com:８０/",
                    "http://[::1]:abc/"):
            with self.subTest(url=url):
                self.seen.clear()
                self.assertEqual(fetch.url_problem(url), "URL の形が壊れている")
                self.assertEqual(self.seen, [])
        self.assertIsNone(fetch.url_problem("https://bücher.example:8443/a"))
        self.assertIsNone(fetch.url_problem("https://example.com:/a"))

    def test_checked_host_is_the_connected_host(self):
        """検査で引いた名前と、ascii_url が組んだ URL の名前が同じ（検査と接続のずれが無い）。"""
        for url in ("https://bücher.example/a", "https://日本語.jp:8443/パス?q=値", "http://127。0。0。1/",
                    "https://пример.рф/", "https://Example.com/"):
            with self.subTest(url=url):
                self.seen.clear()
                fetch.url_problem(url)
                connected = fetch.urllib.parse.urlsplit(fetch.ascii_url(url)).hostname
                self.assertEqual(self.seen, [connected])
                self.assertTrue(connected.isascii())


class AsciiUrlTests(unittest.TestCase):
    def test_idn_host_path_and_query(self):
        url = fetch.ascii_url("https://bücher.example/パス?q=値")
        self.assertTrue(url.isascii())
        self.assertTrue(url.startswith("https://xn--bcher-kva.example/"))
        self.assertIn("%E3%83%91%E3%82%B9", url)
        self.assertIn("q=%E5%80%A4", url)

    def test_port_is_kept(self):
        self.assertEqual(fetch.ascii_url("https://bücher.example:8443/x"), "https://xn--bcher-kva.example:8443/x")

    def test_ascii_urls_are_unchanged(self):
        for url in ("https://Example.com/a?b=c#d", "https://xn--bcher-kva.example/", "http://[2001:db8::1]:80/x"):
            with self.subTest(url=url):
                self.assertEqual(fetch.ascii_url(url), url)

    def test_userinfo_or_percent_in_netloc_is_not_rewritten(self):
        for url in ("https://u@bücher.example/", "https://bücher.example%2f/"):
            with self.subTest(url=url):
                self.assertIn("bücher", fetch.ascii_url(url))

    def test_unconvertible_host_raises(self):
        with self.assertRaises(ValueError):
            fetch.ascii_url("https://a..b.例/")

    def test_encoded_url_is_stable(self):
        once = fetch.ascii_url("https://bücher.example/パス")
        self.assertEqual(fetch.ascii_url(once), once)


class WallPatternTests(unittest.TestCase):
    """各国語のログイン壁・404・確認画面を壁と判定し、普通の本文は壁にしない。"""

    def wall(self, phrase: str) -> tuple:
        # 本文として足りる長さはあるが、先頭に壁の文言がある
        return fetch.content_problem(phrase + "\n" + "あ" * 1500, is_pdf=False)

    def test_walls_in_many_languages(self):
        phrases = ("您访问的页面不存在", "您訪問的頁面不存在", "请登录后查看", "請先登入", "访问被拒绝",
                   "페이지를 찾을 수 없습니다", "로그인이 필요합니다", "접근이 거부되었습니다",
                   "Seite nicht gefunden", "Zugriff verweigert", "Page introuvable", "Accès refusé",
                   "Veuillez vous connecter pour continuer", "Página no encontrada", "Acceso denegado",
                   "Inicia sesión para continuar", "Página não encontrada", "Acesso negado",
                   "Страница не найдена", "Доступ запрещён", "Войдите, чтобы продолжить",
                   "الصفحة غير موجودة", "تم رفض الوصول")
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                kind, reason = self.wall(phrase)
                self.assertEqual(kind, "wall")
                self.assertIn("壁に当たった", reason)

    def test_challenges_in_many_languages(self):
        phrases = ("确认您是真人", "請完成安全驗證", "Bestätigen Sie, dass Sie ein Mensch sind",
                   "Vérifiez que vous êtes humain", "Verifica que eres humano", "Подтвердите, что вы человек")
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                self.assertEqual(self.wall(phrase)[0], "wall")

    def test_short_phrases_common_in_articles_are_not_a_wall(self):
        # 記事の本文にも出る短い句は確認画面の判定に使わない（誤検知で原文を捨てない）
        for phrase in ("사람인지 확인", "로봇이 아닙니다", "Sind Sie ein Roboter", "No eres un robot"):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.wall(phrase), (None, None))

    def test_ordinary_foreign_text_is_not_a_wall(self):
        body = ("Die Forschung zeigt, dass die Ergebnisse stabil sind. " * 30
                + "La recherche montre que les résultats sont stables. " * 30
                + "研究表明，这些结果是稳定的。" * 60 + "연구 결과는 안정적이다. " * 60)
        self.assertEqual(fetch.content_problem(body, is_pdf=False), (None, None))

    def test_patterns_are_lowercase_so_they_match_the_lowered_head(self):
        for pattern in (*fetch.WALL_PATTERNS, *fetch.CHALLENGE_PATTERNS):
            self.assertEqual(pattern, pattern.lower(), pattern)


class OcrHintTests(unittest.TestCase):
    def run_ocr(self, conf: float) -> tuple[str, str]:
        def run(cmd, **kw):
            if cmd[0] == "pdftoppm":
                Path(f"{cmd[-1]}-1.png").write_bytes(b"png")
                return subprocess.CompletedProcess(cmd, 0)
            if cmd[1] == "--list-langs":
                return subprocess.CompletedProcess(cmd, 0, "eng\n", "")
            out = Path(cmd[2])
            out.with_suffix(".txt").write_text("XQZKV WPLMR " * 50, encoding="utf-8")
            out.with_suffix(".tsv").write_text(
                "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
                f"5\t1\t1\t1\t1\t1\t0\t0\t9\t9\t{conf}\tXQZKV\n", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with tempfile.TemporaryDirectory() as tmp, patch.object(fetch, "run_tool", side_effect=run), \
                patch.object(fetch, "missing", return_value=[]), patch.dict(os.environ, {"IMOZURU_OCR_LANG": ""}):
            return fetch.ocr_pdf(Path(tmp) / "a.pdf")

    def test_low_confidence_note_names_other_languages(self):
        text, note = self.run_ocr(conf=20)
        self.assertEqual(text, "")
        self.assertIn("確信度が低い", note)
        for hint in ("jpn_vert", "IMOZURU_OCR_LANG", "chi_sim+eng", "kor+eng", "deu+eng"):
            self.assertIn(hint, note)

    def test_confident_text_has_no_note(self):
        self.assertEqual(self.run_ocr(conf=90)[1], "")

    def test_foreign_language_pairs_are_accepted_from_environment(self):
        for lang in ("chi_sim+eng", "kor+eng", "deu+eng", "fra+eng", "rus+eng", "ara+eng", "heb+eng"):
            with self.subTest(lang=lang), patch.dict(os.environ, {"IMOZURU_OCR_LANG": lang}):
                self.assertEqual(fetch.ocr_langs(), lang)


class YoutubeLanguageTests(unittest.TestCase):
    """動画の元の言語（中国語・韓国語など）の手動字幕を、自動字幕より先に試す。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name) / "talk.md"
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("IMOZURU_YT_LANGS", None)

    def run_youtube(self, language, manual=None, auto=None):
        """manual と auto は動画が持つ字幕（言語 → 文）。指定されて、かつ有るものだけを .vtt に出力する。"""
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

    def test_spoken_language_manual_track_is_tried_before_auto(self):
        asked, body = self.run_youtube("zh-Hans", manual={"zh": "手动字幕"}, auto={"zh-orig": "自动字幕"})
        # 完全なタグ（zh-Hans）を先に、基底の言語（zh）を続けて依頼する
        self.assertEqual(asked, [("--write-subs", "ja,en"), ("--write-subs", "zh-Hans,zh")])
        self.assertEqual(body, "[00:01] 手动字幕")
        self.assertIn("字幕: 手動 v.zh.vtt", self.out.read_text(encoding="utf-8"))

    def test_falls_through_to_spoken_auto_track(self):
        asked, body = self.run_youtube("ko", auto={"ko-orig": "자동 자막"})
        self.assertEqual(asked, [("--write-subs", "ja,en"), ("--write-subs", "ko"), ("--write-auto-subs", "ko-orig")])
        self.assertEqual(body, "[00:01] 자동 자막")
        self.assertIn("字幕: 自動", self.out.read_text(encoding="utf-8"))

    def test_japanese_and_english_do_not_get_the_extra_attempt(self):
        asked, _ = self.run_youtube("en-US", auto={"en-orig": "English"})
        self.assertEqual(asked, [("--write-subs", "ja,en"), ("--write-auto-subs", "en-orig")])
        asked, _ = self.run_youtube("ja", auto={"ja-orig": "日本語"})
        self.assertEqual(asked, [("--write-subs", "ja,en"), ("--write-auto-subs", "ja-orig")])

    def test_ja_en_manual_found_on_first_call_stops_there(self):
        asked, body = self.run_youtube("de", manual={"en": "English subtitles"})
        self.assertEqual(asked, [("--write-subs", "ja,en")])
        self.assertEqual(body, "[00:01] English subtitles")

    def test_invalid_spoken_language_is_not_requested(self):
        for language in ("", "en,all", "all", "zh;rm", "toolong", "Z"):
            with self.subTest(language=language):
                asked, _ = self.run_youtube(language)
                self.assertEqual(asked, [("--write-subs", "ja,en"), ("--write-auto-subs", "ja-orig,en-orig")])

    def test_environment_languages_replace_the_manual_request(self):
        os.environ["IMOZURU_YT_LANGS"] = "zh-Hans,en"
        asked, body = self.run_youtube("zh", manual={"zh-Hans": "简体字幕"})
        self.assertEqual(asked, [("--write-subs", "zh-Hans,en")])
        self.assertEqual(body, "[00:01] 简体字幕")

    def test_environment_languages_skip_the_extra_spoken_attempt(self):
        os.environ["IMOZURU_YT_LANGS"] = "zh-Hans,en"
        asked, _ = self.run_youtube("zh", auto={"zh-orig": "自动"})
        self.assertEqual(asked, [("--write-subs", "zh-Hans,en"), ("--write-auto-subs", "zh-orig")])

    def test_unsafe_environment_values_are_ignored(self):
        for value in ("all", "ALL", "ja,all", "all,en", "en;rm -rf", "ja,,en", ",en", "en,", "ja en", "--x", "en.*"):
            with self.subTest(value=value):
                os.environ["IMOZURU_YT_LANGS"] = value
                asked, _ = self.run_youtube("en")
                self.assertEqual(asked[0], ("--write-subs", "ja,en"))

    def test_environment_order_is_chosen_first(self):
        os.environ["IMOZURU_YT_LANGS"] = "ja,ko,en"
        _, body = self.run_youtube("ko", manual={"ja": "日本語", "ko": "한국어", "en": "English"})
        self.assertEqual(body, "[00:01] 日本語")
        os.environ["IMOZURU_YT_LANGS"] = "ko,ja,en"
        _, body = self.run_youtube("ko", manual={"ja": "日本語", "ko": "한국어", "en": "English"})
        self.assertEqual(body, "[00:01] 한국어")

    def test_environment_order_wins_over_name_order(self):
        os.environ["IMOZURU_YT_LANGS"] = "de,en,ja,fr"
        _, body = self.run_youtube("ko", manual={"de": "Deutsch", "en": "English", "ja": "日本語", "fr": "Français"})
        self.assertEqual(body, "[00:01] Deutsch")
        _, body = self.run_youtube("ko", manual={"en": "English", "ja": "日本語", "fr": "Français"})
        self.assertEqual(body, "[00:01] English")
        _, body = self.run_youtube("ko", manual={"ja": "日本語", "fr": "Français"})
        self.assertEqual(body, "[00:01] 日本語")
        _, body = self.run_youtube("ko", manual={"fr": "Français"})
        self.assertEqual(body, "[00:01] Français")

    def test_environment_order_applies_without_a_spoken_language(self):
        os.environ["IMOZURU_YT_LANGS"] = "de,en"
        asked, body = self.run_youtube("", manual={"en": "English", "de": "Deutsch"})
        self.assertEqual(asked, [("--write-subs", "de,en")])
        self.assertEqual(body, "[00:01] Deutsch")

    def test_live_chat_is_never_requested(self):
        for value in ("live_chat", "LIVE_CHAT", "en,live_chat", "live_chat,en"):
            with self.subTest(value=value):
                os.environ["IMOZURU_YT_LANGS"] = value
                asked, _ = self.run_youtube("en")
                self.assertEqual(asked[0], ("--write-subs", "ja,en"))
        for language in ("live_chat", "all-x", "ALL"):
            with self.subTest(language=language):
                asked, _ = self.run_youtube(language)
                self.assertEqual(asked, [("--write-subs", "ja,en"), ("--write-auto-subs", "ja-orig,en-orig")])

    def test_spoken_language_keeps_the_full_tag(self):
        for printed, expected in (("zh-Hans", "zh-Hans"), ("zh-TW", "zh-TW"), ("pt-BR", "pt-BR"), ("es-419", "es-419"),
                                  ("ko", "ko"), ("en-US", "en-US"), ("all", ""), ("all-x", ""), ("zh-", ""),
                                  ("zh;rm", ""), ("zh-Hans-extra-long-tag", ""), ("", "")):
            with self.subTest(printed=printed):
                self.assertEqual(fetch._spoken_language(f"Talk\tSpeaker\t20220820\t{printed}\n"), expected)

    def test_subtag_language_manual_track_is_found(self):
        asked, body = self.run_youtube("zh-Hans", manual={"zh-Hans": "简体字幕"})
        self.assertEqual(asked, [("--write-subs", "ja,en"), ("--write-subs", "zh-Hans,zh")])
        self.assertEqual(body, "[00:01] 简体字幕")

    def test_subtag_language_auto_track_is_found(self):
        asked, body = self.run_youtube("zh-Hans", auto={"zh-Hans-orig": "自动字幕"})
        self.assertEqual(asked, [("--write-subs", "ja,en"), ("--write-subs", "zh-Hans,zh"),
                                 ("--write-auto-subs", "zh-Hans-orig,zh-orig")])
        self.assertEqual(body, "[00:01] 自动字幕")
        self.assertIn("v.zh-Hans-orig.vtt", self.out.read_text(encoding="utf-8"))

    def test_spoken_prefix_beats_dubbed_japanese_and_english(self):
        # 吹き替えの ja-orig や en-orig より、動画の元の言語（zh-Hans-orig）を先に選ぶ。前方一致は「タグ + -」だけ
        _, body = self.run_youtube("zh-Hans", auto={"ja-orig": "吹き替え", "zh-Hans-orig": "原声"})
        self.assertEqual(body, "[00:01] 原声")
        order = fetch._vtt_order([Path("v.enx.vtt"), Path("v.en-orig.vtt")], ["en"])
        self.assertEqual([p.name for p in order], ["v.en-orig.vtt", "v.enx.vtt"])

    def test_timeout_on_the_extra_attempt_leaves_no_subtitles(self):
        calls = []

        def yt_dlp(cmd, **kwargs):
            calls.append(cmd[cmd.index("--sub-langs") + 1])
            if len(calls) == 1:
                return subprocess.CompletedProcess(cmd, 0, "Talk\tSpeaker\t20220820\tzh\n", "")
            return None

        with patch.object(fetch, "run_tool", side_effect=yt_dlp):
            body = fetch.youtube_to_md("https://www.youtube.com/watch?v=example", self.out)
        self.assertEqual(calls, ["ja,en", "zh"])
        self.assertEqual(body, "")
        self.assertIn("字幕: なし", self.out.read_text(encoding="utf-8"))


class SearchTitleTests(unittest.TestCase):
    def queries(self, key: str, hits_by_call=None) -> list[str]:
        """search_title が OpenAlex に送った絞り込みを、送った順に返す。"""
        seen = []
        responses = list(hits_by_call or [])

        def get(path, **params):
            seen.append(params["filter"])
            return {"results": responses.pop(0) if responses else []}

        with patch.object(chain, "get", side_effect=get):
            chain.search_title(key)
        return seen

    def test_fullwidth_separators_split_words(self):
        for key in ("机器学习：综述", "机器学习、综述", "机器学习，综述", "机器学习・综述", "机器学习—综述", "机器学习–综述"):
            with self.subTest(key=key):
                self.assertEqual(self.queries(key, [[1]]), ["title.search:机器学习 综述"])

    def test_ascii_separators_still_split(self):
        self.assertEqual(self.queries("Exploratory search: a survey, 2006", [[1]]),
                         ["title.search:Exploratory search a survey 2006"])

    def test_long_single_word_cjk_title_retries_with_first_ten_characters(self):
        key = "基于深度学习的中文自然语言处理研究综述"
        self.assertEqual(self.queries(key), [f"title.search:{key}", f"title.search:{key[:10]}"])

    def test_retry_is_skipped_when_the_first_query_hits(self):
        key = "基于深度学习的中文自然语言处理研究综述"
        self.assertEqual(self.queries(key, [[1]]), [f"title.search:{key}"])

    def test_boundary_of_twelve_characters(self):
        self.assertEqual(len(self.queries("あ" * 12)), 1)
        self.assertEqual(len(self.queries("あ" * 13)), 2)

    def test_long_ascii_single_word_has_no_retry(self):
        self.assertEqual(len(self.queries("Electroencephalographically")), 1)

    def test_three_or_fewer_words_have_no_prefix_retry(self):
        self.assertEqual(len(self.queries("機械 学習 入門")), 1)

    def test_more_than_three_words_still_retry_with_first_three(self):
        self.assertEqual(self.queries("a b c d"), ["title.search:a b c d", "title.search:a b c"])

    def test_empty_or_separator_only_titles_do_not_query(self):
        for key in ("", "  ", "：、，", "・—–"):
            with self.subTest(key=key):
                self.assertEqual(self.queries(key), [])


class NormTests(unittest.TestCase):
    def test_composed_and_decomposed_forms_match(self):
        composed = "Café résumé über año 한국어"
        decomposed = ("Café résumé über año "
                      "한국어")
        self.assertEqual(report.norm(composed), report.norm(decomposed))

    def test_nfkc_is_not_applied(self):
        # 全角と半角、上付きの数字、合字は別の字のまま
        self.assertNotEqual(report.norm("ＡＢＣ"), report.norm("ABC"))
        self.assertNotEqual(report.norm("x²"), report.norm("x2"))
        self.assertNotEqual(report.norm("ﬁ"), report.norm("fi"))

    def test_existing_behavior_is_kept(self):
        self.assertEqual(report.norm("  a \n b\t\tc "), "a b c")
        self.assertEqual(report.norm("- 5 mg"), "- 5 mg")
        self.assertEqual(report.norm("* item\n- other"), "item other")

    def test_word_char_covers_accented_greek_and_cyrillic(self):
        for ch in ("é", "ü", "ñ", "ő", "ɏ", "α", "ω", "Я", "ж", "ё", "A", "9"):
            with self.subTest(ch=ch):
                self.assertTrue(report.WORD_CHAR.fullmatch(ch))
        for ch in ("あ", "漢", "한", "-", " ", "؀"):
            with self.subTest(ch=ch):
                self.assertFalse(report.WORD_CHAR.fullmatch(ch))

    def test_hyphen_after_accented_word_is_not_a_minus(self):
        self.assertFalse(report._after_minus("el año-5", len("el año-")))
        self.assertFalse(report._after_minus("модель-5", len("модель-")))
        self.assertTrue(report._after_minus("気温は-5度", len("気温は-")))

    def test_word_char_excludes_multiplication_and_division_signs(self):
        for ch in ("×", "÷"):
            with self.subTest(ch=ch):
                self.assertFalse(report.WORD_CHAR.fullmatch(ch))
        for ch in ("ế", "ạ", "ẞ", "Ẅ", "\u1eff", "\u1e00", "Ö", "ö", "Ø", "ø"):
            with self.subTest(ch=ch):
                self.assertTrue(report.WORD_CHAR.fullmatch(ch))

    def _verify_quote(self, source: str, quote: str) -> bool:
        with tempfile.TemporaryDirectory() as tmp:
            sources = Path(tmp) / "topic" / "sources"
            sources.mkdir(parents=True)
            (sources / "a.md").write_text(f"head\n{source}\nfoot\n", encoding="utf-8")
            return report.verify(sources, {"file": "a.md", "line": 2, "text": quote})[0]

    def test_number_after_multiplication_and_minus_is_still_signed(self):
        # 「2×−5」の「−」は負号。「5」だけ抜いた引用は符号を落としているため通さない
        self.assertFalse(self._verify_quote("結果は 2×−5 だった。", "5 だった。"))
        self.assertTrue(self._verify_quote("結果は 2×−5 だった。", "−5 だった。"))
        self.assertTrue(report._after_minus("2×−5", len("2×−")))

    def test_hyphen_after_vietnamese_word_is_not_a_minus(self):
        self.assertFalse(report._after_minus("thế-5", len("thế-")))
        self.assertTrue(self._verify_quote("Phiên bản thế-5 mới.", "5 mới."))

    def test_quote_with_decomposed_source_is_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            sources = Path(tmp) / "topic" / "sources"
            sources.mkdir(parents=True)
            (sources / "fr.md").write_text(
                "Intro\nLe rôle de l'élève est central.\nFin\n", encoding="utf-8")
            q = {"file": "fr.md", "line": 2, "text": "Le rôle de l'élève est central."}
            self.assertTrue(report.verify(sources, q)[0])
            q["text"] = "Le rôle de l'élève est marginal."
            self.assertFalse(report.verify(sources, q)[0])


class LangAttrTests(unittest.TestCase):
    def test_script_detection(self):
        cases = {
            "مرحبا بالعالم": ' lang="ar" dir="auto"',
            "שלום עולם": ' lang="he" dir="auto"',
            "한국어 문장": ' lang="ko"',
            "Привет, мир": ' lang="ru"',
            "这是一个测试": ' lang="zh"',
            "これは日本語の文です": "",
            "日本語と English": "",
            "Plain English text": ' lang="en"',
            "Le rôle de l'élève": ' lang="en"',
            "Über uns": ' lang="en"',
            "12345 ---": "",
            "": "",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(skin.lang_attr(text), expected)

    def test_hanzi_with_hangul_is_korean(self):
        self.assertEqual(skin.lang_attr("한국어 漢字"), ' lang="ko"')

    def test_explicit_lang_wins(self):
        self.assertEqual(skin.lang_attr("Le rôle", "fr"), ' lang="fr"')
        self.assertEqual(skin.lang_attr("这是测试", "zh-Hant-TW"), ' lang="zh-Hant-TW"')
        self.assertEqual(skin.lang_attr("Text", " de-AT "), ' lang="de-AT"')

    def test_rtl_languages_get_dir_auto(self):
        for tag in ("ar", "he", "fa", "ur", "AR", "ar-EG", "fa-IR"):
            with self.subTest(tag=tag):
                self.assertEqual(skin.lang_attr("x", tag), f' lang="{tag}" dir="auto"')

    def test_unsafe_lang_is_ignored_and_text_decides(self):
        for bad in ('"><script>', 'en" onclick="x', "e", "english", "x-y", "en_US", "en-", "en--US", "ja-" + "a" * 9,
                    "", " ", "1a", None, 5, ["en"]):
            with self.subTest(bad=bad):
                attr = skin.lang_attr("Привет", bad)
                self.assertEqual(attr, ' lang="ru"')
                self.assertNotIn("<", attr)

    def test_render_passes_the_quote_language(self):
        with tempfile.TemporaryDirectory() as tmp:
            sources = Path(tmp) / "topic" / "sources"
            sources.mkdir(parents=True)
            (sources / "ar.md").write_text("مرحبا بالعالم\nثانيا\n", encoding="utf-8")
            q = {"file": "ar.md", "line": 1, "text": "مرحبا بالعالم", "lang": "ar"}
            ok, ctx, c0, _ = report.verify(sources, q)
            self.assertTrue(ok)
            html = report.quote_html(sources, q, ctx, c0)
            self.assertEqual(html.count(' lang="ar" dir="auto"'), 2)  # 引用と前後の文脈
            q["lang"] = '"><img src=x onerror=alert(1)>'
            html = report.quote_html(sources, q, ctx, c0)
            self.assertNotIn("<img", html)
            self.assertNotIn("onerror", html)
            del q["lang"]
            self.assertIn(' lang="ar" dir="auto"', report.quote_html(sources, q, ctx, c0))

    def test_css_font_stack_has_multilingual_fallbacks(self):
        self.assertIn('"Noto Serif SC"', skin.CSS)
        self.assertIn('"Noto Serif KR"', skin.CSS)
        # 本文の書体（--serif）の中で、日本語の書体が先、多言語の書体はシステムの明朝より前
        serif = skin.CSS[skin.CSS.index("--serif:"):].split(";", 1)[0]
        self.assertLess(serif.index('"Noto Serif JP"'), serif.index('"Noto Serif SC"'))
        self.assertLess(serif.index('"Noto Serif KR"'), serif.index('"Hiragino Mincho ProN"'))


@unittest.skipUnless(shutil.which("node"), "node が無いので一覧の検索の JS は確かめない")
class DashboardFoldTests(unittest.TestCase):
    """SIDE_JS の fold() を node で動かし、アクセントの畳みと1文字→1文字の保証を見る。"""

    @classmethod
    def setUpClass(cls):
        js = dash.SIDE_JS
        start = js.index("function fold(v)")
        end = js.index("\n// ローマ字の表")
        cls.fold_src = js[start:end]

    def fold_all(self, values: list[str]) -> list[str]:
        script = f"{self.fold_src}\nconsole.log(JSON.stringify({json.dumps(values, ensure_ascii=False)}.map(fold)));"
        done = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_latin_accents_are_folded(self):
        self.assertEqual(self.fold_all(["É", "é", "ü", "Ñ", "ñ", "ç", "ő", "Ž", "ế", "Ǻ"]),
                         ["e", "e", "u", "n", "n", "c", "o", "z", "e", "a"])
        self.assertEqual(self.fold_all(["Café Über Año"]), ["cafe uber ano"])

    def test_japanese_voiced_marks_are_kept(self):
        # 濁点・半濁点つきのかな（NFD で2文字になる）は変えない。カタカナはひらがなに、全角英数は半角になる
        self.assertEqual(self.fold_all(["がぎぐ", "ガギグ", "ぱぴ", "ＡＢＣ１２３"]), ["がぎぐ", "がぎぐ", "ぱぴ", "abc123"])
        self.assertEqual(self.fold_all(["が"]), ["が"])

    def test_hangul_syllables_are_kept(self):
        self.assertEqual(self.fold_all(["한국어", "각"]), ["한국어", "각"])

    def test_other_scripts_pass_through(self):
        self.assertEqual(self.fold_all(["Привет", "中文", "مرحبا", "שלום", "ß", "×÷"]),
                         ["привет", "中文", "مرحبا", "שלום", "ß", "×÷"])

    def test_length_is_preserved_for_precomposed_text(self):
        samples = ["Café Über Año Ñandú", "Ǻ ế ő ž", "がぎぐ ガギグ ぱぴ", "한국어 각", "ＡＢＣ １２３", "Привет"]
        for original, folded in zip(samples, self.fold_all(samples)):
            with self.subTest(original=original):
                self.assertEqual(len(folded), len(original))

    def test_ascii_query_matches_accented_title(self):
        title = self.fold_all(["Résumé der Übersicht über Año"])[0]
        for query in self.fold_all(["resume", "uber", "ano"]):
            self.assertGreaterEqual(title.find(query), 0)

    def test_length_is_preserved_even_when_lowercase_changes_the_length(self):
        # İ（U+0130）は toLowerCase() で「i + 結合の点」の2文字になる。先に全体を小文字にすると位置がずれる
        samples = ["İSTANBUL Café", "İİİ", "Iİ ẞ ǅ", "İ\u0307", "ǈ ǋ"]
        for original, folded in zip(samples, self.fold_all(samples)):
            with self.subTest(original=original):
                self.assertEqual(len(folded), len(original))
        self.assertEqual(self.fold_all(["İSTANBUL Café"]), ["istanbul cafe"])

    def test_highlight_positions_stay_aligned_for_istanbul(self):
        title = "İSTANBUL Café"
        script = (f"{self.fold_src}\nvar t={json.dumps(title, ensure_ascii=False)},f=fold(t),out=[];"
                  "['istanbul','cafe'].forEach(function(k){var at=f.indexOf(fold(k));out.push([at,t.slice(at,at+k.length)])});"
                  "console.log(JSON.stringify(out));")
        done = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), [[0, "İSTANBUL"], [9, "Café"]])

    def test_decomposed_title_is_composed_before_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            topic = Path(tmp) / "cafe"
            topic.mkdir()
            decomposed = "Cafe\u0301 de Sao Paulo"
            (topic / "map.md").write_text(f"問い: {decomposed}\n題: {decomposed}\n読み: Cafe\u0301\n", encoding="utf-8")
            info = dash.topic_info(topic, [])
        self.assertEqual(info["title"], "Café de Sao Paulo")
        self.assertEqual(info["question"], "Café de Sao Paulo")
        self.assertEqual(info["reading"], "Café")

    def test_regex_is_written_as_an_escape_in_the_script(self):
        # SIDE_JS は CSP のハッシュで許すため、中身を変えても整合する。結合アクセントそのものの字は入れない
        self.assertIn("/[\\u0300-\\u036f]/g", dash.SIDE_JS)
        self.assertNotIn("̀", dash.SIDE_JS)


if __name__ == "__main__":
    unittest.main()
