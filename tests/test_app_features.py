"""アプリとして入れる・感想と要望を送る・PDF で保存する（2026-10-25）の検査。
合言葉が漏れないこと、送り先がフォームだけに絞られること、印刷の見た目が配る文書に入ることを見る。"""

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/imozuru/scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("dash_app", SCRIPTS / "dashboard.py")
dash = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dash)
import palette  # noqa: E402
import skin  # noqa: E402

FORM = ("https://docs.google.com/forms/d/e/1FAIpQLSexample/viewform?usp=pp_url"
        "&entry.111=不具合&entry.222=5&entry.333=BODY&entry.444=VERSION&entry.555=ENV")
TOKEN = "kk-0123456789abcdef"


class AppFeatureTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "a-topic").mkdir()
        (self.root / "a-topic" / "map.md").write_text("問い: 秘密の題\n", encoding="utf-8")
        (self.root / "a-topic" / "digest.html").write_text("<html><body><p>x</p></body></html>", encoding="utf-8")

    def request(self, path, headers=None):
        class Request:
            def __init__(self, data):
                self.data, self.output = data, bytearray()

            def makefile(self, *a):
                return io.BytesIO(self.data)

            def sendall(self, data):
                self.output.extend(data)

        headers = {"Host": "127.0.0.1:8765", **(headers or {})}
        req = Request((f"GET {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n").encode("latin-1"))
        dash.make_handler(self.root, [], TOKEN)(req, ("127.0.0.1", 1), SimpleNamespace(server_port=8765))
        head, _, body = bytes(req.output).partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        return int(lines[0].split()[1]), dict(line.split(": ", 1) for line in lines[1:]), body

    def page(self, served=False, form=""):
        with patch.dict(os.environ, {"IMOZURU_FEEDBACK_FORM": form}):
            return dash.render(dash.collect(self.root, []), self.root, served=served)

    # --- アプリとして入れる ---

    def test_manifest_needs_the_token_and_starts_with_it(self):
        self.assertEqual(self.request("/manifest.webmanifest")[0], 403)
        code, head, body = self.request(f"/manifest.webmanifest?k={TOKEN}")
        self.assertEqual(code, 200)
        self.assertEqual(head["Content-Type"], "application/manifest+json")
        m = json.loads(body)
        # iPhone のホーム画面のアプリは cookie を Safari と分けて持つので、起動の URL に合言葉が要る。id には入れない
        self.assertEqual(m["start_url"], f"/?k={TOKEN}")
        self.assertEqual(m["id"], "/")
        self.assertEqual(m["display"], "standalone")
        self.assertNotIn("theme_color", m)
        self.assertTrue(m["background_color"].startswith("#"))
        self.assertEqual({i["sizes"] for i in m["icons"]}, {"192x192", "512x512"})
        # cookie で通ったときも同じものを返す
        self.assertEqual(self.request("/manifest.webmanifest", {"Cookie": f"imozuru={TOKEN}"})[0], 200)

    def test_icons_are_served_without_the_token_and_carry_no_secret(self):
        for path in dash.APP_ICONS:
            code, head, body = self.request(path)
            self.assertEqual(code, 200, path)
            self.assertEqual(head["Content-Type"], "image/png")
            self.assertTrue(body.startswith(b"\x89PNG"))
            self.assertNotIn(TOKEN.encode(), body)

    def test_manifest_link_and_csp_only_when_served(self):
        served, static = self.page(served=True), self.page(served=False)
        self.assertIn('<link rel="manifest" href="manifest.webmanifest" crossorigin="use-credentials">', served)
        self.assertIn("manifest-src 'self'", served)
        self.assertNotIn('rel="manifest"', static)
        self.assertNotIn("manifest-src", static)
        # 入れるボタンは書き出しておき、窓口で開いたときだけスクリプトが出す
        self.assertIn('id="inst" type="button" hidden', static)

    def test_install_prompt_is_hidden_in_minimal_ui_windows(self):
        # manifest が無いうちに PC のブラウザで入れた窓は minimal-ui で開く。standalone だけを見ると窓の中に案内が残る
        self.assertIn("matchMedia('(display-mode: minimal-ui)')", self.page(served=True))

    # --- 文書の待ちと失敗の札 ---

    def test_doc_wait_card_is_in_the_page_and_starts_hidden(self):
        for page in (self.page(served=True), self.page(served=False)):
            self.assertIn('<div class="docwait" id="docwait" hidden><div class="in" role="status">', page)
        # 待ちの線は動きを減らす設定で止める
        self.assertIn(".docwait .bar::before{animation:none", dash.CSS)

    def test_status_probe_is_allowed_only_when_served(self):
        # 文書が開けなかった理由は窓口へ fetch で問い合わせて分ける。ファイルのページでは問い合わせない
        self.assertIn("connect-src 'self'", self.page(served=True))
        self.assertNotIn("connect-src", self.page(served=False))

    # --- サーバーに届かないときの代わりの画面（service worker） ---

    def test_service_worker_is_served_without_the_token_and_carries_no_secret(self):
        code, head, body = self.request("/sw.js")
        self.assertEqual(code, 200)
        self.assertEqual(head["Content-Type"], "text/javascript; charset=utf-8")
        self.assertNotIn(TOKEN.encode(), body)
        self.assertNotIn("秘密の題".encode(), body)
        # 許していない Host には返さない
        self.assertEqual(self.request("/sw.js", {"Host": "evil.example"})[0], 403)

    def test_service_worker_only_takes_over_top_level_pages(self):
        # 枠（iframe）の文書まで代わりの画面にすると、本体の札が「文書が読めた」と取り違える
        self.assertIn("r.destination!=='document'", dash.sw_js())

    def test_offline_page_allows_only_its_own_script(self):
        page = dash.offline_html()
        js_hash = dash.base64.b64encode(dash.hashlib.sha256(dash.OFFLINE_JS.encode()).digest()).decode()
        self.assertIn(f"script-src 'sha256-{js_hash}'", page)
        self.assertIn(f"<script>{dash.OFFLINE_JS}</script>", page)
        self.assertIn("@@H@@", page)
        # 戻ったかの問い合わせは、文書のページで出たときも窓口の manifest へ向ける（service worker が置き場所を入れる）
        self.assertIn('<body data-scope="@@SCOPE@@">', page)
        self.assertIn("replace('@@SCOPE@@',self.registration.scope)", dash.sw_js())

    def test_worker_is_registered_and_allowed_only_when_served(self):
        self.assertIn("worker-src 'self'", self.page(served=True))
        self.assertNotIn("worker-src", self.page(served=False))

    def test_page_html_never_contains_the_token(self):
        code, _, body = self.request(f"/?k={TOKEN}")
        self.assertEqual(code, 200)
        self.assertNotIn(TOKEN.encode(), body)

    def test_hex_of_matches_known_colors(self):
        self.assertEqual(palette.hex_of("oklch(1 0 0)"), "#ffffff")
        self.assertEqual(palette.hex_of("oklch(0 0 0)"), "#000000")
        self.assertEqual(palette.hex_of("oklch(0.6279 0.2577 29.23)"), "#ff0000")

    # --- 感想・要望を送る ---

    def test_feedback_form_link_is_parsed_into_fields(self):
        with patch.dict(os.environ, {"IMOZURU_FEEDBACK_FORM": FORM}):
            f = dash.feedback_form()
        self.assertEqual(f, {"action": "https://docs.google.com/forms/d/e/1FAIpQLSexample/formResponse",
                             "kind": "entry.111", "rating": "entry.222", "body": "entry.333",
                             "version": "entry.444", "env": "entry.555"})

    def test_feedback_form_rejects_other_hosts_and_off(self):
        for link in ("", "off", FORM.replace("docs.google.com", "evil.example"), FORM.replace("https://", "http://"),
                     "https://docs.google.com/forms/d/e/x/viewform?entry.1=不具合"):
            with patch.dict(os.environ, {"IMOZURU_FEEDBACK_FORM": link}):
                self.assertIsNone(dash.feedback_form(), link)

    def test_feedback_button_and_form_action_follow_the_setting(self):
        none = self.page(form="off")
        self.assertNotIn('id="fb"', none)
        self.assertNotIn('id="fb-dlg"', none)
        self.assertIn("form-action 'none'", none)
        page = self.page(form=FORM)
        self.assertIn('id="fb"', page)
        self.assertIn('action="https://docs.google.com/forms/d/e/1FAIpQLSexample/formResponse"', page)
        self.assertRegex(page, r"form-action https://docs.google.com(;|\")")
        for kind in dash.FEEDBACK_KINDS:
            self.assertIn(f'name="entry.111" value="{kind}"', page)

    def test_feedback_sends_no_page_url_or_research_title(self):
        # 送る中身に組むのは、書いた内容・版・ブラウザの名乗りだけ。送る処理の中にページの URL（合言葉を含む）を拾う所を置かない
        send = dash.FEATURE_JS.split("// 感想・要望を送る", 1)[1].split("// 文書を PDF で保存", 1)[0]
        self.assertNotIn("location", send)
        self.assertNotIn("document.title", send)
        dialog = self.page(form=FORM).split('id="fb-dlg"', 1)[1].split("</dialog>", 1)[0]
        self.assertNotIn("秘密の題", dialog)
        self.assertNotIn(TOKEN, dialog)

    # --- PDF で保存 ---

    def test_print_css_is_added_to_served_documents(self):
        code, _, body = self.request(f"/a-topic/digest.html?k={TOKEN}")
        self.assertEqual(code, 200)
        self.assertIn(skin.PRINT_CSS.encode(), body)
        self.assertIn("@media print", skin.CSS)

    def test_print_now_opens_the_document_alone_and_allows_its_script(self):
        # iPhone と Safari は枠の文書だけを刷れないので、文書を1枚のページとして開き、そこで印刷の画面を出す
        (self.root / "a-topic" / "digest.html").write_text(
            '<html><head><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-src \'sha256-AAA\'">'
            "</head><body><p>x</p></body></html>", encoding="utf-8")
        import base64, hashlib
        h = "'sha256-" + base64.b64encode(hashlib.sha256(dash.PRINT_NOW_JS.encode()).digest()).decode() + "'"
        code, _, body = self.request(f"/a-topic/digest.html?print=1&k={TOKEN}")
        self.assertEqual(code, 200)
        text = body.decode()
        self.assertIn(f"<script>{dash.PRINT_NOW_JS}</script>", text)
        self.assertIn(f"script-src {h} 'sha256-AAA'", text)
        # ?print=1 の無いときは差し込まない
        self.assertNotIn(dash.PRINT_NOW_JS, self.request(f"/a-topic/digest.html?k={TOKEN}")[2].decode())
        # map.md も同じ
        (self.root / "a-topic" / "map.md").write_text("# map\n", encoding="utf-8")
        self.assertIn(dash.PRINT_NOW_JS, self.request(f"/a-topic/map.md?print=1&k={TOKEN}")[2].decode())

    def test_dashboard_page_hides_controls_when_printed(self):
        block = self.page().split("@media print{", 1)[1].split("\n}", 1)[0]
        hidden = [line for line in block.splitlines() if "display:none" in line][0]
        for sel in (".side", "header", ".tools", ".inst-bar", ".imz-hud", ".pager"):
            self.assertIn(sel, hidden.split("{", 1)[0])

    def test_print_button_is_in_the_page(self):
        page = self.page()
        self.assertIn('class="imz-hud docprint"', page)
        self.assertIn(json.dumps(skin.PRINT_CSS), dash.FEATURE_JS)


if __name__ == "__main__":
    unittest.main()
