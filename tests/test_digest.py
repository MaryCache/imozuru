"""詳細レポート（digest.py）と、その記録を作る fetch.py・chain.py の口を検査する。"""

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "skills/imozuru/scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(f"digest_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


digest = load("digest")
fetch = load("fetch")
chain = load("chain")

SOURCE = "Sunlight is scattered by air molecules.\nBlue light scatters more than red light.\n"


def sentence(text, line=2, quote="Blue light scatters more than red light."):
    return {"text": text, "quotes": [{"file": "paper.txt", "line": line, "text": quote}]}


def level():
    return {"summary": [sentence("青い光ほど強く散らばる。")],
            "terms": [{"term": "散乱", "sentences": [sentence("光が粒に当たって向きを変えること。", 1,
                                                              "Sunlight is scattered by air molecules.")]}],
            "answers": [{"claim": "なぜ赤いか", "sentences": [sentence("青が先に散って赤が残る。")]}]}


class DigestTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.topic = Path(tmp.name) / "sunset"
        (self.topic / "sources").mkdir(parents=True)
        (self.topic / "sources" / "paper.txt").write_text(SOURCE, encoding="utf-8")
        self.data = {"title": "夕焼け", "question": "なぜ赤いか", "stopped": "主張に原文が付いた",
                     "levels": {"lv1": level(), "lv2": level(), "lv3": level()},
                     "people_places": [sentence("散乱の論文がある。")],
                     "unresolved": ["雲の色は調べていない"]}

    def build(self, *extra):
        (self.topic / "digest.json").write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        out = io.StringIO()
        with patch.object(sys, "argv", ["digest.py", str(self.topic), *extra]), contextlib.redirect_stdout(out):
            code = digest.main()
        return code, out.getvalue()

    def add(self, payload, *flags):
        out = io.StringIO()
        with patch.object(sys, "argv", ["digest.py", "add", str(self.topic), *flags]), \
                patch.object(sys, "stdin", io.StringIO(json.dumps(payload, ensure_ascii=False))), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            return digest.main()

    def test_cited_digest_builds_page_with_both_levels(self):
        code, _ = self.build()
        self.assertEqual(code, 0)
        page = (self.topic / "digest.html").read_text(encoding="utf-8")
        for name in ("Lv1", "Lv2", "Lv3", "調べたこと", "調査を区切った理由"):
            self.assertIn(name, page)
        self.assertIn("Blue light scatters more than red light.", page)

    def test_sentence_without_quote_is_rejected(self):
        self.data["levels"]["lv1"]["summary"].append({"text": "根拠の無い文。", "quotes": []})
        code, out = self.build()
        self.assertEqual(code, 1)
        self.assertIn("根拠の引用が無い", out)
        self.assertFalse((self.topic / "digest.html").exists())

    def test_paraphrased_quote_is_rejected(self):
        self.data["levels"]["lv2"]["answers"][0]["sentences"] = [
            sentence("言い換え。", 2, "Blue light is scattered more strongly.")]
        code, out = self.build()
        self.assertEqual(code, 1)
        self.assertIn("原文に見つからない引用", out)

    def test_quote_outside_sources_is_rejected(self):
        self.data["people_places"] = [sentence("外。", 1, "x") | {"quotes": [{"file": "../digest.json", "line": 1, "text": "{"}]}]
        code, _ = self.build()
        self.assertEqual(code, 1)

    def test_both_levels_are_required(self):
        del self.data["levels"]["lv3"]
        code, out = self.build()
        self.assertEqual(code, 1)
        self.assertIn("Lv3が無い", out)

    def test_failed_build_keeps_prior_page(self):
        (self.topic / "digest.html").write_text("Previous", encoding="utf-8")
        self.data["levels"]["lv1"]["summary"] = []
        code, _ = self.build()
        self.assertEqual(code, 1)
        self.assertEqual((self.topic / "digest.html").read_text(encoding="utf-8"), "Previous")

    def test_records_list_every_hit_empty_queries_and_fetch_status(self):
        self.assertEqual(self.add([
            {"query": "why is the sunset red", "hits": [
                {"title": "Paper", "url": "http://www.example.test/paper/"},
                {"title": "Blog", "url": "https://blog.example.test/a"},
                {"title": "Gone", "url": "https://gone.example.test/"}]},
            {"query": "夕焼け 赤い 理由 論文", "hits": []}], "--round", "0", "--by", "検索役A"), 0)
        (self.topic / "fetch-log.jsonl").write_text(
            json.dumps({"url": "https://example.test/paper", "name": "paper", "status": "OK", "file": "paper.txt"}) + "\n"
            + json.dumps({"url": "https://gone.example.test", "name": "gone", "status": "FAIL", "reason": "404"}) + "\n"
            + json.dumps({"url": "https://other.example.test/x", "name": "x", "status": "OK", "file": "x.md"}) + "\n",
            encoding="utf-8")
        code, _ = self.build()
        self.assertEqual(code, 0)
        page = (self.topic / "digest.html").read_text(encoding="utf-8")
        self.assertIn("検索 2本・ヒット 3件・空振り 1本", page)
        self.assertIn("ヒット0", page)
        self.assertIn("未取得", page)
        self.assertIn("取得失敗", page)
        self.assertIn("paper.txt", page)
        # 検索で挙がっていない取得も隠さず出す
        self.assertIn("other.example.test/x", page)

    def test_non_http_hit_is_not_linked(self):
        self.add([{"query": "q", "hits": [{"title": "bad", "url": "javascript:alert(1)"}]}], "--round", "1")
        self.build()
        page = (self.topic / "digest.html").read_text(encoding="utf-8")
        self.assertNotIn('href="javascript:', page)

    def test_add_rejects_record_without_hits_array(self):
        self.assertEqual(self.add([{"query": "q"}], "--round", "1"), 1)
        self.assertEqual(self.add([{"query": "q", "hits": []}]), 1)  # 周が無い
        self.assertFalse((self.topic / "queries.jsonl").exists())


class FetchLogTests(unittest.TestCase):
    def test_fetch_result_is_recorded_beside_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            sources = Path(tmp) / "topic" / "sources"
            with patch.object(sys, "argv", ["fetch.py", str(sources), "x", "file:///etc/passwd"]), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(fetch.main(), 1)
            rows = [json.loads(l) for l in (sources.parent / "fetch-log.jsonl").read_text().splitlines()]
            self.assertEqual(rows[0]["status"], "FAIL")
            self.assertEqual(rows[0]["url"], "file:///etc/passwd")
            self.assertEqual(list(sources.iterdir()), [])

    def test_fetch_log_is_skipped_outside_a_sources_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "elsewhere"
            with patch.object(sys, "argv", ["fetch.py", str(target), "x", "file:///etc/passwd"]), \
                    contextlib.redirect_stdout(io.StringIO()):
                fetch.main()
            self.assertFalse((Path(tmp) / "fetch-log.jsonl").exists())


class ChainLogTests(unittest.TestCase):
    def test_chain_log_records_listed_works(self):
        works = {"meta": {"count": 1}, "results": [
            {"id": "https://openalex.org/W1", "display_name": "Rayleigh", "publication_year": 1871,
             "cited_by_count": 9, "doi": "https://doi.org/10.1/x",
             "best_oa_location": {"pdf_url": "https://example.test/x.pdf"}}]}
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(chain, "resolve", return_value={"id": "https://openalex.org/W9"}), \
                    patch.object(chain, "get", return_value=works), \
                    patch.object(sys, "argv", ["chain.py", "cites", "W9", "--log", tmp, "--round", "2"]), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(chain.main(), 0)
            row = json.loads((Path(tmp) / "queries.jsonl").read_text())
            self.assertEqual((row["round"], row["method"], row["by"]), (2, "cites", "chain.py"))
            self.assertEqual(row["hits"][0]["url"], "https://doi.org/10.1/x")
            self.assertEqual(row["hits"][0]["also"], ["https://example.test/x.pdf"])

    def test_chain_log_requires_round(self):
        with patch.object(sys, "argv", ["chain.py", "cites", "W9", "--log", "/tmp/x"]), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(chain.main(), 2)


if __name__ == "__main__":
    unittest.main()
