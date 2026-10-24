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
        # 送る中身に組むのは、書いた内容・版・ブラウザの名乗りだけ。ページの URL（合言葉を含む）を拾う処理を置かない
        self.assertNotIn("location.href", dash.FEATURE_JS)
        dialog = self.page(form=FORM).split('id="fb-dlg"', 1)[1].split("</dialog>", 1)[0]
        self.assertNotIn("秘密の題", dialog)
        self.assertNotIn(TOKEN, dialog)

    # --- PDF で保存 ---

    def test_print_css_is_added_to_served_documents(self):
        code, _, body = self.request(f"/a-topic/digest.html?k={TOKEN}")
        self.assertEqual(code, 200)
        self.assertIn(skin.PRINT_CSS.encode(), body)
        self.assertIn("@media print", skin.CSS)

    def test_print_button_is_in_the_page(self):
        page = self.page()
        self.assertIn('class="imz-hud docprint"', page)
        self.assertIn(json.dumps(skin.PRINT_CSS), dash.FEATURE_JS)


if __name__ == "__main__":
    unittest.main()
