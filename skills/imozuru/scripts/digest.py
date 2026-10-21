#!/usr/bin/env python3
"""調べ終えたあとの詳細レポート（digest.html）を作る。解説と、検索の全記録の2層。

抜き書き（report.py）は原文を切り出して並べるだけで、その分野を知らない人には
読む順も言葉の意味も分からない。詳細レポートは、止めたあとに初めて解説を書く場所で、
前提知識ゼロ向けの「入門編」と、隣の分野を知っている人向けの「隣接分野編」を分けて置く。
要約を止めていたのは早すぎる閉じ（Kuhlthau 1991）を避けるためなので、調べを止めた後なら書いてよい。

解説の1文ごとに、原文からそのまま切り出した文（ファイル・行）を根拠として付ける。
根拠は report.py と同じ照合にかけ、1つでも合わない・根拠の無い文があれば HTML は作らない。
記録の層は queries.jsonl（検索語ごとのヒットの全件）と fetch-log.jsonl（fetch.py の
取得の記録）から機械で組み、エージェントは書かない。空振りの検索語もそのまま載る。

使い方:
    digest.py add <topic_dir> [--round N] [--by 誰]  … 標準入力の JSON を queries.jsonl に足す
        JSON は記録の配列: [{"query", "method"?, "round"?, "by"?, "hits": [{"title", "url", "also"?}]}]
        --round・--by は、記録に無いときの既定値
    digest.py <topic_dir>          … digest.json と queries.jsonl → digest.html
    digest.py <topic_dir> --check  … 照合だけして HTML は作らない

digest.json の形:
    {"title", "question", "stopped",
     "levels": {"intro": 編, "adjacent": 編},
     "people_places": [文], "unresolved": ["..."]}
    編 = {"summary": [文], "terms": [{"term", "sentences": [文]}], "answers": [{"claim", "sentences": [文]}]}
    文 = {"text", "quotes": [{"file", "line", "end"?, "text"}]}
"""

import html
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import report  # noqa: E402  照合と引用の描画は抜き書きと同じものを使う

QUERIES = "queries.jsonl"
FETCH_LOG = "fetch-log.jsonl"
LEVELS = (("intro", "入門編", "前提知識が無い人向け"),
          ("adjacent", "隣接分野編", "近い分野を知っている人向け"))


def append_records(topic: Path, records: list[dict]) -> int:
    """検索の記録を queries.jsonl の末尾に足す。足した件数を返す。"""
    topic.mkdir(parents=True, exist_ok=True)
    with (topic / QUERIES).open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(records)


def normalize_record(r: dict, round_: int | None, by: str | None) -> dict:
    """形を確かめてそろえる。query と hits が無い記録は受け付けない（空振りは hits を空の配列で書く）。"""
    if not isinstance(r, dict) or not isinstance(r.get("query"), str) or not r["query"].strip():
        raise ValueError(f"query の無い記録: {json.dumps(r, ensure_ascii=False)[:80]}")
    hits = r.get("hits")
    if not isinstance(hits, list):
        raise ValueError(f"hits が配列でない（空振りは []）: {r['query']}")
    out_hits = []
    for h in hits:
        if not isinstance(h, dict) or not isinstance(h.get("url"), str):
            raise ValueError(f"url の無いヒット: {r['query']}")
        hit = {"title": str(h.get("title") or ""), "url": h["url"]}
        if h.get("also"):
            hit["also"] = [str(u) for u in h["also"]]
        out_hits.append(hit)
    rnd = r.get("round", round_)
    if type(rnd) is not int:
        raise ValueError(f"周（round）が整数でない: {r['query']}")
    return {"round": rnd, "by": str(r.get("by") or by or "不明"),
            "method": str(r.get("method") or "WebSearch"), "query": r["query"], "hits": out_hits}


def url_key(url: str) -> str:
    """照合用に URL をそろえる。http と https、www、末尾の /、#以降の違いは同じものとして扱う。"""
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    host = (p.hostname or "").lower().removeprefix("www.")
    path = p.path.rstrip("/")
    return f"{host}{path}" + (f"?{p.query}" if p.query else "")


def load_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def fetch_status(fetches: list[dict]) -> dict[str, dict]:
    """URL ごとの最後の取得の結果。同じ URL を取り直したら新しいほうを使う。"""
    return {url_key(f["url"]): f for f in fetches if isinstance(f.get("url"), str)}


def sentence_problems(src: Path, where: str, sentences) -> list[str]:
    """文の並びを照合する。根拠の無い文・原文に無い引用を、場所つきで返す。"""
    bad = []
    if not isinstance(sentences, list) or not sentences:
        return [f"{where}: 文が無い"]
    for i, s in enumerate(sentences, 1):
        if not isinstance(s, dict) or not isinstance(s.get("text"), str) or not s["text"].strip():
            bad.append(f"{where} {i}文目: text が無い")
            continue
        quotes = s.get("quotes")
        if not isinstance(quotes, list) or not quotes:
            bad.append(f"{where} {i}文目「{s['text'][:30]}」: 根拠の引用が無い")
            continue
        for q in quotes:
            if not isinstance(q, dict) or not report.verify(src, q)[0]:
                loc = f"{q.get('file')}:{q.get('line')}" if isinstance(q, dict) else "?"
                bad.append(f"{where} {i}文目「{s['text'][:30]}」: 原文に見つからない引用 {loc}")
    return bad


def check(topic: Path, data: dict) -> list[str]:
    src = topic / "sources"
    bad = []
    levels = data.get("levels") or {}
    for key, name, _ in LEVELS:
        lv = levels.get(key)
        if not isinstance(lv, dict):
            bad.append(f"{name}が無い")
            continue
        bad += sentence_problems(src, f"{name}の要旨", lv.get("summary"))
        for t in lv.get("terms") or []:
            bad += sentence_problems(src, f"{name}の用語「{t.get('term')}」", t.get("sentences"))
        for a in lv.get("answers") or []:
            bad += sentence_problems(src, f"{name}の主張「{a.get('claim')}」", a.get("sentences"))
    if data.get("people_places"):
        bad += sentence_problems(src, "人と場", data["people_places"])
    return bad


esc = report.esc


def link(url: str, text: str) -> str:
    """http(s) だけをリンクにする。javascript: などはそのまま文字で出す。"""
    if urlsplit(url).scheme in ("http", "https"):
        return f'<a href="{esc(url)}" rel="noreferrer noopener">{esc(text or url)}</a>'
    return esc(text or url)


class Notes:
    """編ごとの脚注。文の後ろに [n] を付け、引用は節の末尾に並べる。"""

    def __init__(self, src: Path, prefix: str):
        self.src, self.prefix, self.items = src, prefix, []

    def sentence(self, s: dict) -> str:
        marks = []
        for q in s["quotes"]:
            self.items.append(q)
            n = len(self.items)
            marks.append(f'<a class="fn" href="#{self.prefix}{n}">[{n}]</a>')
        return esc(s["text"]) + "".join(marks)

    def paragraph(self, sentences: list[dict]) -> str:
        # 1文ごとに改行する。続けて組むと、どの番号がどの文の根拠かを目で追いにくい
        return '<p class="lines">' + "".join(f"<span>{self.sentence(s)}</span>" for s in sentences) + "</p>"

    def render(self) -> str:
        if not self.items:
            return ""
        out = ['<details class="notes"><summary>根拠の原文（' + str(len(self.items)) + '件）</summary><ol>']
        for n, q in enumerate(self.items, 1):
            _, ctx, c0, _ = report.verify(self.src, q)
            out.append(f'<li id="{self.prefix}{n}">' + report.quote_html(self.src, q, ctx, c0) + "</li>")
        out.append("</ol></details>")
        return "\n".join(out)


def level_html(src: Path, key: str, name: str, lead: str, lv: dict) -> str:
    notes = Notes(src, f"{key}-")
    body = [f'<section id="{key}"><h2>{esc(name)}<span class="status">{esc(lead)}</span></h2>',
            "<h3>要旨</h3>", notes.paragraph(lv["summary"])]
    if lv.get("terms"):
        body.append("<h3>用語</h3><dl>")
        for t in lv["terms"]:
            body.append(f"<dt>{esc(t['term'])}</dt><dd>{notes.paragraph(t['sentences'])}</dd>")
        body.append("</dl>")
    if lv.get("answers"):
        body.append("<h3>主張ごとの答え</h3>")
        for a in lv["answers"]:
            body.append(f'<h4>{esc(a["claim"])}</h4>{notes.paragraph(a["sentences"])}')
    body.append(notes.render())
    body.append("</section>")
    return "\n".join(body)


def records_html(records: list[dict], fetched: dict[str, dict]) -> str:
    """検索の全記録。周ごとに折りたたみ、検索語ごとにヒットを全件並べる。"""
    linked = set()
    total_hits = sum(len(r["hits"]) for r in records)
    empty = sum(1 for r in records if not r["hits"])
    body = ['<section id="record"><h2>記録<span class="status">機械で生成</span></h2>',
            f'<p class="note">検索 {len(records)}本・ヒット {total_hits}件・空振り {empty}本。'
            "ヒットは検索で挙がったものの全件で、〔保存〕は fetch.py が原文を保存できたもの。</p>"]
    for rnd in sorted({r["round"] for r in records}):
        rs = [r for r in records if r["round"] == rnd]
        n_hits = sum(len(r["hits"]) for r in rs)
        body.append(f"<details><summary>{rnd}周目（検索 {len(rs)}本・ヒット {n_hits}件）</summary>")
        for r in rs:
            body.append(f'<h4>「{esc(r["query"])}」<span class="meta">{esc(r["by"])}・{esc(r["method"])}</span></h4>')
            if not r["hits"]:
                body.append('<p class="note">ヒット0</p>')
                continue
            body.append("<ul class=\"hits\">")
            for h in r["hits"]:
                f = None
                for u in [h["url"], *h.get("also", [])]:
                    k = url_key(u)
                    if k in fetched:
                        f = fetched[k]
                        linked.add(k)
                        break
                if f is None:
                    tag = '<span class="tag">未取得</span>'
                elif f.get("status") == "OK":
                    tag = f'<span class="tag ok">保存</span><code>{esc(f.get("file", ""))}</code>'
                else:
                    tag = f'<span class="tag ng">取得失敗</span><span class="cn">{esc(f.get("reason", ""))}</span>'
                body.append(f"<li>{tag} {link(h['url'], h['title'])}</li>")
            body.append("</ul>")
        body.append("</details>")
    loose = [f for k, f in fetched.items() if k not in linked]
    if loose:
        body.append("<h3>どの検索にも結び付かない取得</h3>"
                    '<p class="note">記事の中のリンクをたどったものや、検索の URL と取得の URL が違うもの。</p><ul class="hits">')
        for f in loose:
            st = "保存" if f.get("status") == "OK" else "取得失敗"
            detail = f.get("file", "") if f.get("status") == "OK" else f.get("reason", "")
            body.append(f'<li><span class="tag">{st}</span> {link(f["url"], f["url"])} <code>{esc(detail)}</code></li>')
        body.append("</ul>")
    body.append("</section>")
    return "\n".join(body)


CSS = report.CSS + """
nav{display:flex;flex-wrap:wrap;gap:.4rem 1rem;margin:1rem 0;font-size:.9rem}
a{color:var(--accent)}
h3{font-size:1.05rem;margin:2rem 0 .3rem}
h4{font-size:.95rem;margin:1.2rem 0 .2rem}
dt{font-weight:600;margin-top:1rem}
dd{margin:0}
dd p{margin:.2rem 0}
p.lines span{display:block;margin:.15rem 0}
a.fn{font-size:.7rem;vertical-align:super;text-decoration:none;margin-left:.1rem}
.meta{font-weight:400;font-size:.8rem;color:var(--sub);margin-left:.5rem}
ul.hits{padding-left:1.1rem;font-size:.88rem}
ul.hits li{margin:.25rem 0;overflow-wrap:anywhere}
.tag.ok{border-color:var(--accent);color:var(--accent)}
.tag.ng{border-style:dashed}
.notes ol{padding-left:1.4rem}
.cn{font-size:.8rem;color:var(--sub);margin-left:.3rem}
"""


def build(topic: Path, check_only: bool) -> int:
    data = json.loads((topic / "digest.json").read_text(encoding="utf-8"))
    records = load_jsonl(topic / QUERIES)
    bad = check(topic, data)
    if bad:
        print("根拠の照合に通らない文がある（根拠が無いか、引用が原文と合わない）:")
        print("\n".join("  " + b for b in bad))
        return 1
    print(f"照合 OK: 解説の文がすべて原文の引用を持つ・検索の記録 {len(records)}本")
    if check_only:
        return 0

    src = topic / "sources"
    fetched = fetch_status(load_jsonl(topic / FETCH_LOG))
    nav = ['<a href="#intro">入門編</a>', '<a href="#adjacent">隣接分野編</a>']
    body = [f"<h1>{esc(data['title'])}</h1>",
            f'<p class="lead">問い: {esc(data["question"])}</p>',
            f'<p class="lead">止めた理由: {esc(data["stopped"])}</p>',
            '<p class="note">解説の文の後ろの番号は、その文の根拠として原文からそのまま切り出した引用（機械で照合済み）。'
            "解説はエージェントが書いた。記録の層は機械で組んだ。</p>"]
    sections = [level_html(src, k, n, lead, data["levels"][k]) for k, n, lead in LEVELS]
    if data.get("people_places"):
        notes = Notes(src, "pp-")
        sections.append('<section id="people"><h2>人と場</h2>' + notes.paragraph(data["people_places"])
                        + notes.render() + "</section>")
        nav.append('<a href="#people">人と場</a>')
    if data.get("unresolved"):
        sections.append('<section id="open"><h2>未解決</h2><ul>'
                        + "".join(f"<li>{esc(u)}</li>" for u in data["unresolved"]) + "</ul></section>")
        nav.append('<a href="#open">未解決</a>')
    sections.append(records_html(records, fetched))
    nav.append('<a href="#record">記録</a>')
    body.append("<nav>" + "".join(nav) + "</nav>")
    body += sections
    body.append(f'<footer>imozuru の詳細レポート・原文はこのページと同じ場所の {esc(src.name)}/</footer>')

    page = f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>{esc(data['title'])} 詳細レポート</title><style>{CSS}</style></head>
<body><main>
{chr(10).join(body)}
</main></body></html>
"""
    out = topic / "digest.html"
    # 一時ファイルに書いてから置き換える。途中で止まっても前のページが壊れずに残る
    tmp = out.with_name(".digest.html.tmp")
    tmp.write_text(page, encoding="utf-8")
    os.replace(tmp, out)
    print(f"→ {out}")
    return 0


def add(argv: list[str]) -> int:
    topic = Path(argv[0]).expanduser()
    round_, by = None, None
    rest = argv[1:]
    while rest:
        flag = rest.pop(0)
        if flag == "--round" and rest and re.fullmatch(r"[0-9]+", rest[0]):
            round_ = int(rest.pop(0))
        elif flag == "--by" and rest:
            by = rest.pop(0)
        else:
            print(f"分からない指定: {flag}", file=sys.stderr)
            return 2
    try:
        raw = json.loads(sys.stdin.read() or "[]")
        records = [normalize_record(r, round_, by) for r in (raw if isinstance(raw, list) else [raw])]
    except ValueError as e:  # 壊れた JSON も ValueError
        print(f"足せなかった: {e}", file=sys.stderr)
        return 1
    topic.mkdir(parents=True, exist_ok=True)
    n = append_records(topic, records)
    print(f"{n}本を {topic / QUERIES} に足した（ヒット {sum(len(r['hits']) for r in records)}件）")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__, file=sys.stderr)
        return 2
    if args[0] == "add" and len(args) >= 2:
        return add(args[1:])
    return build(Path(args[0]).expanduser(), "--check" in args[1:])


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")
    sys.exit(main())
