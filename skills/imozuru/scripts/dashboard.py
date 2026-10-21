#!/usr/bin/env python3
"""調べものの履歴を1枚のページ（index.html）にする。調査の置き場（既定 ~/research）を走査する。

人が後から見返すためのページ。ホームには調査をまたいだ集計を3つだけ置き、左の一覧から調査を選ぶと
その調査の詳細（調査の論点・周回・原文・文書へのリンク）が開く。

数字は手元の記録から機械で数え、どの記録から数えたかと、何件の調査に記録があったかを必ず添える。
古い調査には検索や取得の記録（queries.jsonl・fetch-log.jsonl）が無く、0 と区別して「記録なし」と出す。

使い方:
    dashboard.py [research_dir] [--runs runs.md] [--out index.html]
        research_dir … 調査の置き場（既定 ~/research）。直下のフォルダを1件の調査として読む
        --runs       … 回した記録の台帳（任意）。「調査を区切った理由」「感覚と合った？」の列を読む
        --out        … 書き出す先（既定 <research_dir>/index.html）
"""

import html
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

esc = lambda s: html.escape(str(s), quote=True)  # noqa: E731

# 区切った理由の自動分類。記録の文は書き手ごとに違うので、語で振り分ける。上から順に最初に当たったもの。
# 分類はページで「自動分類」と明示し、元の文を必ず並べて見せる（分類の誤りを読み手が確かめられるように）
REASON_RULES = (
    ("約束で打ち切り", ("約束", "打ち切", "試験運転", "上限")),
    ("取得の壁", ("ログイン", "有料", "壁", "ボット", "CAPTCHA", "取れない", "取得できな", "購読")),
    ("飽和", ("飽和",)),
    ("4周の上限", ("4周",)),
    ("論点に根拠がそろった", ("被覆", "根拠あり", "全部に原文", "全部付いた", "原文が付いた", "原文の場所が付いた")),
    ("一部の論点を残して区切った", ("一部", "区切", "停止", "まで", "打ち止め", "判断")),
)
# 「論点6/6に原文」のように分子と分母が同じなら、全部そろったとみなす
ALL_ATTACHED = re.compile(r"(\d+)\s*/\s*\1\s*に原文")
ROUND_HEAD = re.compile(r"^#{2,4}\s*(\d+)\s*周目", re.M)
CHECK_ITEM = re.compile(r"^\s*-\s*\[([ xX])\]\s*(.+)$", re.M)
SOURCE_ROW = re.compile(r"^\|(.+)\|\s*$", re.M)
URL = re.compile(r"https?://[^\s|)>\]]+")


def classify(reason: str) -> str:
    if ALL_ATTACHED.search(reason) and not any(w in reason for w in REASON_RULES[0][1] + REASON_RULES[1][1]):
        return "論点に根拠がそろった"
    for label, words in REASON_RULES:
        if any(w in reason for w in words):
            return label
    return "その他"


def section(text: str, heading: str) -> str:
    """map.md の「## 見出し」から次の「## 」までを返す。無ければ空。"""
    m = re.search(rf"^##\s*{re.escape(heading)}\s*$", text, re.M)
    if not m:
        return ""
    rest = text[m.end():]
    n = re.search(r"^##\s", rest, re.M)
    return rest[:n.start()] if n else rest


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in read(path).splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def load_runs(path: Path | None) -> list[dict]:
    """回した記録の台帳の表を読む。列の名前で引くので、列が増えても読める。"""
    if not path:
        return []
    rows, header = [], None
    for line in read(path).splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if header is None:
            header = cells
            continue
        if set(line.replace("|", "").strip()) <= set("-: "):
            continue
        # 途中から「回した人」の列を省いて書いた行がある。列が1つ足りなければ、その列を空として読む
        if len(cells) == len(header) - 1 and "回した人" in header:
            cells.insert(header.index("回した人"), "")
        rows.append(dict(zip(header, cells)))
    return rows


def match_run(slug: str, runs: list[dict]) -> dict | None:
    """台帳の行を話題のフォルダ名で探す。名前が書かれていない行とは結び付けない（取り違えるより記録なしのほうがよい）。
    同じ話題を何度か回したときは最後の行を使う。見張り（新着の確認）の行より、掘った行を優先する。"""
    hits = [r for r in runs if re.search(rf"(?<![\w-]){re.escape(slug)}(?![\w-])", r.get("話題", ""))]
    dug = [r for r in hits if "掘り" in r.get("型", "")]
    return (dug or hits or [None])[-1]


def count_sources(src: Path) -> int:
    """原文の本数。同じ名前の .pdf と .txt は1本と数える。"""
    if not src.is_dir():
        return 0
    stems = {p.stem for p in src.iterdir() if p.is_file() or p.is_symlink()}
    return len(stems)


def source_hosts(map_text: str) -> list[str]:
    """map.md の原文の表から、元の URL のホストを1行1つ拾う。"""
    hosts = []
    for row in SOURCE_ROW.findall(section(map_text, "原文（sources/）") or section(map_text, "原文")):
        if set(row.replace("|", "").strip()) <= set("-: "):
            continue
        m = URL.search(row)
        if m:
            hosts.append((urlsplit(m.group(0)).hostname or "?").removeprefix("www."))
    return hosts


def topic_info(d: Path, runs: list[dict]) -> dict:
    map_text, log_text = read(d / "map.md"), read(d / "log.md")
    info: dict = {"slug": d.name, "has_map": bool(map_text)}
    q = re.search(r"^問い[:：]\s*(.+)$", map_text, re.M)
    info["question"] = q.group(1).strip() if q else ""
    date = re.search(r"(20\d\d-\d\d-\d\d)", log_text) or re.search(r"(20\d\d-\d\d-\d\d)", map_text)
    info["date"] = date.group(1) if date else ""
    rounds = [int(n) for n in ROUND_HEAD.findall(log_text)]
    records = load_jsonl(d / "queries.jsonl")
    if records:
        rounds += [r.get("round", 0) for r in records if isinstance(r.get("round"), int)]
    # 周は SKILL.md の定義どおり、1周目以降にたどった周の数（0周目は地図づくりなので数えない）。
    # 台帳（runs.md）は行によって 0周目を数えたり数えなかったりするので、台帳の値は別に原文のまま見せる
    info["rounds"] = len({r for r in rounds if r >= 1}) if rounds else None
    info["sources"] = count_sources(d / "sources")
    info["hosts"] = source_hosts(map_text)

    claims = []
    for path in (d / "digest.json", d / "extracts.json"):
        try:
            data = json.loads(read(path) or "null")
        except ValueError:
            data = None
        if isinstance(data, dict):
            info.setdefault("stopped", data.get("stopped", ""))
            if not claims and isinstance(data.get("claims"), list):
                claims = [(c.get("claim", ""), c.get("status", "")) for c in data["claims"] if isinstance(c, dict)]
    if not claims:
        claims = [(text.strip(), "根拠あり" if mark.lower() == "x" else "未確認")
                  for mark, text in CHECK_ITEM.findall(section(map_text, "調査の論点"))]
    old = {"付いた": "根拠あり", "一部だけ": "一部のみ", "付かなかった": "根拠なし"}
    info["claims"] = [(c, old.get(s, s)) for c, s in claims]

    run = match_run(d.name, runs)
    info["run"] = run
    reason = (run or {}).get("調査を区切った理由") or info.get("stopped") or ""
    info["reason"] = reason
    info["reason_class"] = classify(reason) if reason else "記録なし"

    info["searches"] = len(records) if records else None
    info["hits"] = sum(len(r.get("hits", [])) for r in records) if records else None
    fetches = load_jsonl(d / "fetch-log.jsonl")
    info["fetch_ok"] = sum(1 for f in fetches if f.get("status") == "OK") if fetches else None
    info["fetch_ng"] = sum(1 for f in fetches if f.get("status") == "FAIL") if fetches else None
    info["docs"] = [(name, label) for name, label in (("digest.html", "詳細レポート"), ("report.html", "抜書き"),
                                                       ("map.md", "map.md"), ("log.md", "log.md"))
                    if (d / name).is_file()]
    return info


def collect(root: Path, runs: list[dict]) -> list[dict]:
    topics = [topic_info(d, runs) for d in sorted(root.iterdir()) if d.is_dir() and not d.name.startswith(".")]
    return sorted(topics, key=lambda t: (t["date"] or "0000", t["slug"]), reverse=True)


def main() -> int:
    args = sys.argv[1:]
    runs_path = out = None
    if "--runs" in args:
        i = args.index("--runs")
        runs_path = Path(args[i + 1]).expanduser()
        del args[i:i + 2]
    if "--out" in args:
        i = args.index("--out")
        out = Path(args[i + 1]).expanduser()
        del args[i:i + 2]
    root = Path(args[0] if args else "~/research").expanduser()
    if not root.is_dir():
        print(f"調査の置き場が無い: {root}", file=sys.stderr)
        return 2
    topics = collect(root, load_runs(runs_path))
    if "--dump" in args:
        for t in topics:
            print("\t".join(str(x) for x in (t["slug"], t["date"], t["rounds"], t["sources"], len(t["hosts"]),
                                             t["reason_class"], (t["run"] or {}).get("周", "-"),
                                             (t["run"] or {}).get("原文", "-"))))
        return 0
    page = render(topics, root)
    out = out or root / "index.html"
    tmp = out.with_name("." + out.name + ".tmp")
    tmp.write_text(page, encoding="utf-8")
    os.replace(tmp, out)
    print(f"→ {out}（調査 {len(topics)}件）")
    return 0


LOGO = Path(__file__).resolve().parent.parent / "assets" / "logo.svg"
SHOWN_HOSTS = 12

# 色は DESIGN-imozuru.md の紙とセピアの墨から。棒の2色は dataviz の検証（validate_palette.js）を通した値。
# 紅紫は「芋＝原文」だけに使う（DESIGN の決まり）。それ以外の数量はセピアの棒にする
CSS = """
:root{--paper:#f6efd3;--paper2:#ebe2c0;--ink:#482404;--sub:#6b4a26;--line:#d9c99c;--bar:#aa691b;--src:#8a3462;--card:#fbf6e2;
--serif:"Noto Serif JP","Hiragino Mincho ProN","Yu Mincho",serif;--head:"Shippori Mincho","Hiragino Mincho ProN","Yu Mincho",serif;--latin:"EB Garamond",Georgia,serif;color-scheme:light}
*{box-sizing:border-box}
html,body{overflow-x:clip}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--serif);font-size:16px;line-height:1.7}
a{color:var(--ink)}
header{display:flex;align-items:center;gap:16px;flex-wrap:wrap;padding:16px max(16px,env(safe-area-inset-right,0px)) 12px max(16px,env(safe-area-inset-left,0px));border-bottom:1px solid var(--line);background:var(--paper2)}
header svg{height:40px;width:auto;flex:none}
header .ttl{font-family:var(--head);font-weight:500;letter-spacing:.04em;font-size:1.15rem}
header .sum{color:var(--sub);font-size:.85rem;margin-left:auto}
.layout{display:grid;grid-template-columns:minmax(0,17rem) minmax(0,1fr);min-height:calc(100vh - 70px)}
.layout>*{min-width:0}
aside{border-right:1px solid var(--line);background:var(--paper2);padding:12px 0;max-height:calc(100vh - 70px);overflow-y:auto;position:sticky;top:0}
aside h2{font-family:var(--head);font-size:.8rem;color:var(--sub);font-weight:500;letter-spacing:.08em;margin:4px 16px 8px}
aside ol{list-style:none;margin:0;padding:0}
aside a{display:block;padding:8px 16px;text-decoration:none;border-left:3px solid transparent}
aside a:hover,aside a:focus-visible{background:var(--card)}
aside .q{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;font-size:.9rem;line-height:1.5}
aside .m{display:flex;gap:6px;align-items:center;font-size:.72rem;color:var(--sub);margin-top:2px}
.chip{display:inline-block;font-size:.7rem;border:1px solid var(--sub);border-radius:99px;padding:0 .5em;white-space:nowrap;color:var(--sub)}
.chip.none{border-style:dashed}
main{padding:24px max(16px,env(safe-area-inset-right,0px)) 64px 24px;max-width:60rem}
main h2{font-family:var(--head);font-weight:500;letter-spacing:.04em;font-size:1.4rem;margin:0 0 8px;text-wrap:balance}
main h3{font-family:var(--head);font-weight:500;font-size:1.05rem;letter-spacing:.04em;margin:32px 0 4px;border-bottom:1px solid var(--line);padding-bottom:4px}
.note{color:var(--sub);font-size:.85rem;margin:4px 0}
.tablewrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:.88rem;font-variant-numeric:tabular-nums}
th,td{padding:5px 8px;text-align:left;vertical-align:middle}
thead th{font-weight:400;color:var(--sub);border-bottom:1px solid var(--line);font-size:.78rem}
td.n{text-align:right;width:3.5em;white-space:nowrap}
td.bar{width:45%;min-width:5rem}
td.bar span{display:block;height:10px;background:var(--bar);border-radius:0 4px 4px 0}
td.bar.src span{background:var(--src)}
td.lbl{overflow-wrap:anywhere}
.topic{display:none}
.topic:target{display:block}
main:has(.topic:target) .home{display:none}
.back{display:inline-block;margin-bottom:12px;font-size:.85rem}
dl.facts{display:grid;grid-template-columns:minmax(0,9rem) minmax(0,1fr);gap:6px 16px;margin:16px 0}
dl.facts dt{color:var(--sub);font-size:.85rem}
dl.facts dd{margin:0;overflow-wrap:anywhere}
ul.claims{list-style:none;padding:0;margin:8px 0}
ul.claims li{display:flex;gap:8px;align-items:baseline;margin:6px 0}
.st{flex:none;font-size:.7rem;border:1px solid var(--ink);border-radius:99px;padding:0 .5em}
.st.part{border-style:dotted}
.st.no{border-style:dashed;color:var(--sub);border-color:var(--sub)}
.docs{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0}
.docs a{border:1px solid var(--ink);border-radius:6px;padding:4px 12px;text-decoration:none;font-size:.9rem;background:var(--card)}
.docs a:hover,.docs a:focus-visible{background:var(--paper2)}
.hosts{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}
.hosts span{font-size:.78rem;border:1px solid var(--line);border-radius:4px;padding:0 6px;overflow-wrap:anywhere}
.hosts b{color:var(--src);font-weight:600;margin-left:4px}
details{margin:8px 0;font-size:.85rem}
summary{cursor:pointer;color:var(--sub)}
footer{color:var(--sub);font-size:.78rem;margin-top:48px;border-top:1px solid var(--line);padding-top:12px}
@media (max-width:760px){
 .layout{grid-template-columns:minmax(0,1fr)}
 aside{position:static;max-height:none;border-right:none;border-bottom:1px solid var(--line)}
 .layout:has(.topic:target) aside{display:none}
 main{padding:16px max(16px,env(safe-area-inset-right,0px)) 48px max(16px,env(safe-area-inset-left,0px))}
 header .sum{margin-left:0;width:100%}
 dl.facts{grid-template-columns:minmax(0,1fr)}
 dl.facts dd{margin-bottom:6px}
}
@media (prefers-reduced-motion:no-preference){aside a{transition:background .15s}}
"""


def bars(rows: list[tuple[str, int]], cls: str = "", head: str = "", unit: str = "") -> str:
    """1系列の横棒。共通の目盛り（最大値を全幅）で、値は棒の横に数字でも出す。"""
    if not rows:
        return '<p class="note">記録なし</p>'
    peak = max(v for _, v in rows) or 1
    body = "".join(
        f'<tr><td class="lbl">{esc(k)}</td><td class="bar {cls}"><span style="width:{v / peak * 100:.1f}%" '
        f'title="{esc(k)}: {v}{unit}"></span></td><td class="n">{v}</td></tr>' for k, v in rows)
    return (f'<div class="tablewrap"><table><thead><tr><th>{esc(head)}</th><th></th><th class="n">{esc(unit)}</th></tr></thead>'
            f"<tbody>{body}</tbody></table></div>")


def status_chip(status: str) -> str:
    cls = {"根拠あり": "", "一部のみ": " part", "根拠なし": " no"}.get(status, " no")
    return f'<span class="st{cls}">{esc(status)}</span>'


def title_of(t: dict) -> str:
    return t["question"] or t["slug"]


def topic_html(t: dict) -> str:
    run = t["run"] or {}
    rounds = "記録なし" if t["rounds"] is None else f"{t['rounds']} 周"
    if run.get("周"):
        rounds += f'<span class="note">（台帳の記載: {esc(run["周"])}）</span>'
    facts = [("日付", esc(t["date"] or "記録なし")), ("たどった周", rounds),
             ("原文", f"{t['sources']} 本" + (f'<span class="note">（台帳の記載: {esc(run["原文"])}）</span>' if run.get("原文") else "")),
             ("調査を区切った理由", (f'<span class="chip{" none" if t["reason_class"] == "記録なし" else ""}">{esc(t["reason_class"])}</span> '
                                    + esc(t["reason"])) if t["reason"] else "記録なし")]
    if run.get("感覚と合った？"):
        facts.append(("感覚と合った？", esc(run["感覚と合った？"])))
    if t["searches"] is not None:
        line = f"検索 {t['searches']} 本・ヒット {t['hits']} 件"
        if t["fetch_ok"] is not None:
            line += f"・保存 {t['fetch_ok']}・取得失敗 {t['fetch_ng']}"
        facts.append(("検索と取得", line))
    else:
        facts.append(("検索と取得", '記録なし<span class="note">（検索の記録を残す前の調査）</span>'))
    out = [f'<section class="topic" id="t-{esc(t["slug"])}">',
           '<a class="back" href="#">← 一覧とホームへ</a>',
           f"<h2>{esc(title_of(t))}</h2>",
           f'<p class="note">{esc(t["slug"])}</p>',
           '<dl class="facts">' + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in facts) + "</dl>"]
    if t["docs"]:
        out.append("<h3>文書</h3><div class=\"docs\">"
                   + "".join(f'<a href="{esc(t["slug"])}/{esc(name)}">{esc(label)}</a>' for name, label in t["docs"]) + "</div>")
    if t["claims"]:
        out.append("<h3>調査の論点</h3><ul class=\"claims\">"
                   + "".join(f"<li>{status_chip(s)}<span>{esc(c)}</span></li>" for c, s in t["claims"]) + "</ul>")
    if t["hosts"]:
        out.append("<h3>原文を取得した媒体</h3><div class=\"hosts\">"
                   + "".join(f"<span>{esc(h)}<b>{n}</b></span>" for h, n in Counter(t["hosts"]).most_common()) + "</div>")
    out.append("</section>")
    return "\n".join(out)


def home_html(topics: list[dict]) -> str:
    n = len(topics)
    with_reason = [t for t in topics if t["reason_class"] != "記録なし"]
    reasons = Counter(t["reason_class"] for t in with_reason).most_common()
    hosts = Counter(h for t in topics for h in t["hosts"])
    with_hosts = sum(1 for t in topics if t["hosts"])
    fetch_topics = [t for t in topics if t["fetch_ok"] is not None]
    by_date = [t for t in topics if t["rounds"] is not None]
    rules = "".join(f"<li>{esc(label)}: {esc('・'.join(words))}</li>" for label, words in REASON_RULES)
    out = ['<section class="home">', "<h2>調べものの記録</h2>",
           f'<p class="note">左の一覧から調査を選ぶと詳細が開く。数字はすべて各調査のフォルダの記録から機械で数えた。</p>',
           "<h3>調査を区切った理由</h3>",
           f'<p class="note">自動分類。{n}件中 {len(with_reason)}件に記録がある。元の文は各調査の詳細にある。</p>',
           bars(reasons, head="分類", unit="件"),
           f"<details><summary>分類の規則（記録の文にこの語があれば、上から順に当てはめる）</summary><ul>{rules}</ul></details>",
           "<h3>原文を取得した媒体</h3>",
           f'<p class="note">map.md の原文の表の URL から数えた。{n}件中 {with_hosts}件に記録がある。上位{SHOWN_HOSTS}件。</p>',
           bars(hosts.most_common(SHOWN_HOSTS), cls="src", head="媒体", unit="本")]
    if len(hosts) > SHOWN_HOSTS:
        out.append(f"<details><summary>ほか {len(hosts) - SHOWN_HOSTS} 媒体</summary>"
                   + bars(hosts.most_common()[SHOWN_HOSTS:], cls="src", head="媒体", unit="本") + "</details>")
    if fetch_topics:
        ok = sum(t["fetch_ok"] for t in fetch_topics)
        ng = sum(t["fetch_ng"] for t in fetch_topics)
        out.append(f'<p class="note">取得の成否の記録は {len(fetch_topics)}件の調査にだけある（保存 {ok}・取得失敗 {ng}）。</p>')
    out += ["<h3>たどった周の数</h3>",
            f'<p class="note">1周目以降にたどった周の数。0周目（地図づくり）は数えない。{n}件中 {len(by_date)}件に記録がある。</p>',
            bars(sorted(Counter(f"{t['rounds']} 周" for t in by_date).items(), key=lambda kv: int(kv[0].split()[0])),
                 head="周", unit="件"),
            "</section>"]
    return "\n".join(out)


def render(topics: list[dict], root: Path) -> str:
    try:
        logo = LOGO.read_text(encoding="utf-8")
        logo = logo.replace("<svg ", '<svg role="img" aria-label="imozuru" ', 1)
    except OSError:
        logo = '<span class="ttl">imozuru</span>'  # 公開版の外で動かしたときなど、ロゴが無ければ文字で出す
    total_sources = sum(t["sources"] for t in topics)
    items = "".join(
        f'<li><a href="#t-{esc(t["slug"])}"><span class="q">{esc(title_of(t))}</span>'
        f'<span class="m">{esc(t["date"] or "日付なし")}<span class="chip{" none" if t["reason_class"] == "記録なし" else ""}">'
        f'{esc(t["reason_class"])}</span></span></a></li>' for t in topics)
    # 選んでいる調査を一覧で示す。スクリプトを使わず、:target を :has() で一覧へ伝える
    current = "".join(f'.layout:has(#t-{t["slug"]}:target) aside a[href="#t-{t["slug"]}"]'
                      "{background:var(--card);border-left-color:var(--src)}" for t in topics)
    body = [f'<header>{logo}<span class="ttl">調べものの記録</span>'
            f'<span class="sum">調査 {len(topics)} 件・原文 {total_sources} 本</span></header>',
            '<div class="layout">',
            f'<aside><h2>調査（新しい順）</h2><ol>{items}</ol></aside>',
            "<main>", home_html(topics), *[topic_html(t) for t in topics],
            '<footer>imozuru の dashboard.py が生成。文書へのリンクは、このページと同じ場所にある調査のフォルダを指す。</footer>',
            "</main></div>"]
    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; img-src data:">
<title>imozuru 調べものの記録</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=EB+Garamond:wght@400;500&family=Noto+Serif+JP:wght@400;600&family=Shippori+Mincho:wght@500&display=swap">
<style>{CSS}{current}</style></head>
<body>
{chr(10).join(body)}
</body></html>
"""


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")
    sys.exit(main())
