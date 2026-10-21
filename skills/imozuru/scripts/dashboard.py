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

import base64
import hashlib
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
# 区切った理由の分類ごとの標本の色（.rc1〜.rc4）。記録なしは色を付けず破線
REASON_COLOR = {"論点に根拠がそろった": 1, "一部の論点を残して区切った": 2, "約束で打ち切り": 3, "取得の壁": 4}
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

LOGO_LIGHT = LOGO.with_name("logo-light.svg")

# 外観は「昆虫の標本箱」（2026-10-21 かしぇが3つの棚の見本から A を選んだ）。胡桃材の濃い地にガラス越しの面、
# 標本の色を点で入れる。色はすべて OKLCH。標本の4色は dataviz の validate_palette.js を、
# 暗い面 oklch(0.33 0.035 58) の上で全組み合わせ通した（色覚の差は注意の範囲＝文字ラベルを必ず添える）。
# 案1・案2 はキーカラーとキーフォントだけを変えた見比べ用。決まったら1つにする
THEMES = {
    "1": {"bar": "oklch(0.62 0.13 75)", "src": "oklch(0.66 0.15 150)",
          "r1": "oklch(0.62 0.14 245)", "r2": "oklch(0.66 0.15 150)", "r3": "oklch(0.62 0.13 75)", "r4": "oklch(0.56 0.17 345)",
          "head": '"Shippori Mincho"', "font": "Shippori+Mincho:wght@500;700"},
    "2": {"bar": "oklch(0.66 0.13 85)", "src": "oklch(0.6 0.15 350)",
          "r1": "oklch(0.6 0.13 250)", "r2": "oklch(0.64 0.13 160)", "r3": "oklch(0.66 0.13 85)", "r4": "oklch(0.6 0.15 350)",
          "head": '"Zen Old Mincho"', "font": "Zen+Old+Mincho:wght@500;700"},
}
THEME = THEMES[os.environ.get("IMOZURU_DASH_THEME", "1")]

CSS = """
:root{--paper:oklch(0.27 0.035 55);--paper2:oklch(0.21 0.03 50);--ink:oklch(0.93 0.02 85);--sub:oklch(0.76 0.03 80);
--line:oklch(0.42 0.04 60);--card:oklch(0.32 0.035 58);--card2:oklch(0.36 0.035 60);
--serif:"Noto Serif JP","Hiragino Mincho ProN","Yu Mincho",serif;--latin:"EB Garamond",Georgia,serif;color-scheme:dark}
*{box-sizing:border-box}
html,body{overflow-x:clip}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--serif);font-size:16px;line-height:1.7}
a{color:var(--ink)}
header{display:flex;align-items:center;gap:16px;flex-wrap:wrap;padding:16px max(16px,env(safe-area-inset-right,0px)) 12px max(16px,env(safe-area-inset-left,0px));border-bottom:1px solid var(--line);background:var(--paper2)}
header svg{height:40px;width:auto;flex:none}
header .ttl{font-family:var(--head);font-weight:500;letter-spacing:.04em;font-size:1.15rem}
header .sum{color:var(--sub);font-size:.85rem;margin-left:auto}
header .menu{display:none;position:relative;width:40px;height:40px;flex:none;border:1px solid var(--line);background:var(--card);border-radius:8px;padding:0;cursor:pointer}
/* 2本線が開くと X に変わる（design-library の menu-cross-morph・makery: translateY＋rotate、0.2s ease-out） */
header .menu span,header .menu span::before{position:absolute;left:11px;width:18px;height:2px;border-radius:1px;background:var(--ink)}
header .menu span{top:15px;transition:transform .2s ease-out}
header .menu span::before{content:"";left:0;top:8px;transition:transform .2s ease-out}
body.open header .menu span{transform:translateY(4px) rotate(45deg)}
body.open header .menu span::before{transform:translateY(-8px) rotate(-90deg)}
header .menu:focus-visible{outline:2px solid var(--src);outline-offset:2px}
.layout{display:grid;grid-template-columns:var(--side,18rem) minmax(0,1fr);min-height:calc(100vh - 72px)}
.layout>*{min-width:0}
.side{position:sticky;top:0;align-self:start;height:100vh;background:var(--paper2);border-right:1px solid var(--line)}
body.collapsed .layout{grid-template-columns:0 minmax(0,1fr)}
body.collapsed aside{visibility:hidden}
aside{height:100%;overflow-y:auto;overflow-x:hidden;padding:12px 10px}
aside h2{font-family:var(--head);font-size:.8rem;color:var(--sub);font-weight:500;letter-spacing:.08em;margin:4px 6px 8px}
aside ol{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:minmax(0,1fr);gap:6px}
aside li{min-width:0}
aside a{display:block;padding:8px 10px;text-decoration:none;background:var(--paper);border:1px solid var(--line);border-left:3px solid var(--line);border-radius:6px}
aside a:hover,aside a:focus-visible{background:var(--card);border-color:var(--sub)}
aside .q{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:.9rem;line-height:1.5}
aside .m{display:flex;gap:6px;align-items:center;font-size:.72rem;color:var(--sub);margin-top:2px;min-width:0}
.grip{position:absolute;top:50%;right:-15px;transform:translateY(-50%);width:14px;height:72px;padding:0;border:1px solid var(--line);border-left:none;border-radius:0 8px 8px 0;background:var(--paper2);cursor:ew-resize;touch-action:none;z-index:5}
.grip::before{content:"";position:absolute;left:5px;top:22px;bottom:22px;width:2px;border-left:1px solid var(--sub);border-right:1px solid var(--sub)}
.grip:focus-visible{outline:2px solid var(--src);outline-offset:2px}
body.dragging,body.dragging *{cursor:ew-resize!important;user-select:none}
.shade{display:none}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:4px 16px 14px;margin:16px 0}
.card>h3{margin-top:10px}
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
  /* Discord 型（takubase の OverlappingPanes に合わせた）。一覧は下に敷き、頭と本文が右へ退いて見せる */
 /* 上下は画面の安全域（ノッチ・下のバー）の内側に合わせる。Artifact などの枠は :root に安全域ぶんの余白を足すので、
    一覧だけ端まで伸びると本文より上下に大きく見えた（2026-10-21 かしぇ） */
 .side{position:fixed;left:0;top:env(safe-area-inset-top,0px);bottom:env(safe-area-inset-bottom,0px);height:calc(100dvh - env(safe-area-inset-top,0px) - env(safe-area-inset-bottom,0px));align-self:stretch;width:84vw;z-index:0;border-right:none}
 /* グリッドの子なので、パソコン用の align-self:start が固定配置にも効き、中身の高さ（24px）まで縮んだ。伸ばす指定に戻す */
 header,main{position:relative;z-index:1;transform:translateX(var(--x,0px));transition:transform .25s cubic-bezier(.2,.8,.2,1)}
 main{background:var(--paper);min-height:100dvh}
 /* 縦にスクロールする要素には、その要素自身に pan-y を宣言する。ブラウザは触れた所から最寄りの
    スクロールする祖先までしか touch-action を見ず、無いと横の操作をブラウザが取って中断する（takubase の規約） */
 html,body,header,main,aside,.tablewrap,pre{touch-action:pan-y}
 /* 表の箱は、はみ出していなくても横にスクロールする箱として扱われ、横の操作をブラウザが取った（20px で中断）。
    本当にはみ出す表だけスクリプトが横スクロールを戻し、そこから始めた操作は表に譲る */
 body.dragging header,body.dragging main,body.dragging .shade{transition:none}
 body.open{--x:84vw;overflow:hidden}
 body.collapsed .layout,.layout{grid-template-columns:minmax(0,1fr)}
 body.collapsed aside{visibility:visible}
 .grip{display:none}
 .shade{display:block;position:fixed;inset:0;z-index:2;background:rgba(72,36,4,.25);transform:translateX(var(--x,0px));transition:transform .25s cubic-bezier(.2,.8,.2,1),opacity .25s;opacity:0;pointer-events:none;border:0;padding:0}
 body.open .shade,body.dragging .shade{opacity:1;pointer-events:auto}
 body.open header{z-index:3}
 header .menu{display:inline-block}
 @media (prefers-reduced-motion:reduce){header .menu span,header .menu span::before{transition:none}}
 header{gap:10px 12px;padding-top:10px;padding-bottom:10px}
 header svg{height:30px}
 header .ttl{display:none}
 td.bar{width:35%}
 main{padding:16px max(16px,env(safe-area-inset-right,0px)) 48px max(16px,env(safe-area-inset-left,0px))}
 header .sum{margin-left:0;width:100%}
 dl.facts{grid-template-columns:minmax(0,6.5rem) minmax(0,1fr);gap:6px 10px}
 main h2{font-size:1.15rem}
}
@media (prefers-reduced-motion:reduce){header,main,.shade{transition:none!important}}
/* --- 標本箱（2026-10-21 協議: カードの一式をやめ、大胆さは詳細の紙面1か所に集める） --- */
/* 一番外側も塗る。塗らないと、枠が足した上下の余白に後ろの画面が透ける */
html{background:var(--paper2)}
:root{--ivory:oklch(0.94 0.025 85);--ivory-ink:oklch(0.3 0.03 55);--ivory-sub:oklch(0.44 0.03 60);--ivory-line:oklch(0.82 0.03 80)}
/* 地は無地の胡桃材に、上から電灯を当てたような減光（色の転び・周辺減光）。縦縞の木目は作り物に見えたのでやめた。
   スマホでは本文が下の一覧を隠す不透明な板なので、本文にも同じ地を敷く */
body,main{background-color:var(--paper);background-image:radial-gradient(120% 90% at 35% -10%,oklch(0.34 0.045 62),transparent 60%),radial-gradient(140% 120% at 50% 50%,transparent 55%,oklch(0.17 0.025 50 / .7))}
header{background:var(--paper2);border-bottom:1px solid var(--line)}
header .ttl{display:none}
main h2,main h3,aside h2{font-family:var(--head);font-weight:700}
summary,.note{color:var(--sub)}
a{color:var(--ink)}

/* 一覧: カードをやめて、罫線で区切った密な目録。左端の太い帯が区切った理由の色＝色で探せる */
aside ol{gap:0}
aside a{background:none;border:0;border-bottom:1px solid var(--line);border-radius:0;box-shadow:none;padding:9px 10px 9px 16px;position:relative}
aside a::before{content:"";position:absolute;left:2px;top:9px;bottom:9px;width:5px;border-radius:3px;background:var(--c,transparent);box-shadow:0 0 8px -1px var(--c,transparent)}
aside a.none::before{background:none;border:1px dashed var(--line);width:3px}
aside a:hover,aside a:focus-visible{background:oklch(1 0 0 / .05)}
aside .m .chip{border:0;background:none;padding:0;color:var(--sub)}
aside .m .chip::before{display:none}

/* ホーム: 集計は箱に入れず、木の地に直接置く。見出しの書体と罫線で区切り、棒は太くして色の面積を増やす */
.home .card{background:none;border:0;box-shadow:none;padding:0;margin:36px 0 44px}
.home .card>h3{font-size:1.25rem;margin:0 0 6px;border-bottom:1px solid var(--line);padding-bottom:6px}
td.bar span{height:14px;border-radius:0 3px 3px 0;background:var(--bar)}
td.bar.src span{background:var(--src)}
/* 「class に rc を含む」で書くと src も当たって原文の棒が消えた。番号で名指しする */
td.bar.rc1 span,td.bar.rc2 span,td.bar.rc3 span,td.bar.rc4 span{background:var(--c)}
thead th{border-bottom-color:var(--line)}

/* チップ: 文字は墨のまま、標本の色は点で（文字を系列の色にしない） */
.chip,.st{border:1px solid var(--line);color:var(--ink);background:oklch(0.2 0.03 50 / .45)}
.chip::before,.st::before{content:"";display:inline-block;width:.6em;height:.6em;border-radius:50%;margin-right:.35em;vertical-align:.02em;background:var(--c,var(--sub))}
.chip.none,.st.no{border-style:dashed;color:var(--sub)}
.rc1{--c:var(--r1)}.rc2{--c:var(--r2)}.rc3{--c:var(--r3)}.rc4{--c:var(--r4)}
.st{--c:var(--r1)}.st.part{--c:var(--r3);border-style:solid}.st.no{--c:transparent}

/* 詳細: 胡桃材にピンで留めた象牙色の紙面。ページの中で一番大きく、一番明るい面＝大胆さはここだけ。
   紙の繊維は SVG のノイズを一度だけ絵にして敷く（スクロールのたびに計算させない） */
.topic{position:relative;background-color:var(--ivory);color:var(--ivory-ink);border-radius:3px;padding:30px 30px 24px;margin-top:8px;
background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='180' height='180'%3E%3Cfilter id='f'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.012 .55' numOctaves='3' seed='9'/%3E%3CfeColorMatrix values='0 0 0 0 .35 0 0 0 0 .27 0 0 0 0 .18 0 0 0 .08 0'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23f)'/%3E%3C/svg%3E");
box-shadow:0 1px 0 oklch(1 0 0 / .5) inset,0 2px 0 oklch(0.2 0.03 50 / .35),0 26px 44px -20px oklch(0.08 0.02 50 / .95)}
.topic::before{content:"";position:absolute;top:-7px;left:50%;width:16px;height:16px;margin-left:-8px;border-radius:50%;
background:radial-gradient(circle at 35% 30%,oklch(1 0 0 / .8),var(--c,var(--ivory-sub)) 45%,oklch(0.2 0.03 50) 100%);box-shadow:0 3px 4px oklch(0.1 0.02 50 / .6)}
.topic h2,.topic a{color:var(--ivory-ink)}
.topic .note,.topic dt,.topic summary{color:var(--ivory-sub)}
.topic .card{background:none;border:0;box-shadow:none;padding:0;margin:22px 0 0}
.topic .card>h3{border-bottom-color:var(--ivory-line);font-size:1.02rem}
.topic .chip,.topic .st{border-color:var(--ivory-line);color:var(--ivory-ink);background:oklch(1 0 0 / .45)}
.topic .docs a{background:oklch(1 0 0 / .5);border:1px solid var(--ivory-line);color:var(--ivory-ink)}
.topic .docs a:hover,.topic .docs a:focus-visible{border-color:var(--ivory-ink)}
.topic .hosts span{border-color:var(--ivory-line);background:oklch(1 0 0 / .4)}
.topic .hosts b{color:var(--ivory-sub)}
/* 切り替えは View Transitions でつなぐ。一覧の題名が紙面の見出しまで移って大きくなり、ほかは重ねて入れ替わる。
   対応しないブラウザと、動きを減らす設定では即座に切り替わる（下の留まる動きだけ残る） */
::view-transition-old(root),::view-transition-new(root){animation-duration:.32s;animation-timing-function:cubic-bezier(.2,.8,.2,1)}
::view-transition-group(specimen){animation-duration:.38s;animation-timing-function:cubic-bezier(.2,.8,.2,1)}
::view-transition-old(specimen),::view-transition-new(specimen){height:100%;object-fit:none;object-position:left top}
html.vt .topic:target{animation:none!important}
/* 選んだときの動きは1つだけ: 紙面が上から留まる */
@media (prefers-reduced-motion:no-preference){.topic:target{animation:pinned .32s cubic-bezier(.2,.8,.2,1)}
@keyframes pinned{from{opacity:0;transform:translateY(-12px) rotate(-.5deg)}to{opacity:1;transform:none}}}

.grip{background:var(--paper2);border-color:var(--line)}
.grip::before{border-color:var(--sub)}
header .menu{background:var(--card2);border-color:var(--line)}
.shade{background:oklch(0.1 0.02 50 / .55)!important}
/* 一覧を開くと、退いた本体の四隅をスマホの画面と同じ角丸に見せる（2026-10-21 かしぇ）。
   頭と本文は別の要素なので、本体の四隅に一覧の地の色の扇形を重ねて丸く見せる。本体と一緒に動く */
.frame{display:none}
@media (max-width:760px){
 .frame{display:block;position:fixed;inset:0;z-index:4;pointer-events:none;opacity:0;transform:translateX(var(--x,0px));
 transition:transform .25s cubic-bezier(.2,.8,.2,1),opacity .2s;border-radius:34px;
 /* 角丸の外側を一覧の地の色の輪で覆い、内側の左の縁に影を入れて本体を一段手前に見せる。
    同じ色の扇形を四隅に置くだけだと、同じ色の頭の帯の上で角丸が見えなかった */
 box-shadow:0 0 0 30px var(--paper2),inset 14px 0 22px -14px oklch(0.06 0.02 50 / .9),inset 0 0 0 1px oklch(1 0 0 / .08)}
 body.open .frame,body.dragging .frame{opacity:1}
 body.dragging .frame{transition:none}
}
@media (max-width:760px){.topic{padding:24px 16px 18px}.topic h2{font-size:1.05rem;font-weight:600;line-height:1.55}}
"""


# 一覧の開閉。広い画面は一覧の右端のつまみをドラッグして幅を変える・畳む（押すだけでも開閉）。
# 狭い画面は takubase の OverlappingPanes（Discord 型）に合わせ、本文のどこでも横にスワイプして開閉する:
#  - 指が 15px 動くまでは何もせず、縦の動きが横の 1.5 倍以上なら縦スクロールに任せる（軸のロック）
#  - 画面の左右 24px から始めた操作は受けない（iOS の「戻る」スワイプに譲る）
#  - 0.3px/ms 以上で払えば向きで決め、ゆっくりなら動き始めから 72px で開閉を切り替える
#    （「半分以上」で決めると 84% 幅の一覧は大きく動かさないと開かない。takubase で実際に戻りが起きた）
#  - 開くと履歴を1つ積み、端末の戻るで閉じる。開いている間は本文に暗い面をかけ、押すと閉じる
# 幅と開閉は localStorage に覚える。スクリプトが動かなくても一覧は CSS だけで出ている
SIDE_JS = """(function(){var b=document.body,s=document.getElementById('side'),g=s.querySelector('.grip'),
m=matchMedia('(max-width:760px)'),K='imozuru-side',LOCK=15,RATIO=1.5,EDGE=24,FLICK=0.3,COMMIT=72,last=288,pushed=false;
function st(v){try{localStorage.setItem(K,v)}catch(e){}}
try{var v=localStorage.getItem(K);if(v==='0')b.classList.add('collapsed');else if(v){last=+v;b.style.setProperty('--side',v+'px')}}catch(e){}
function W(){return s.getBoundingClientRect().width}
var mb=document.querySelector('header .menu');
function setOpen(o,fromPop){if(o===b.classList.contains('open'))return;b.classList.toggle('open',o);mb.setAttribute('aria-expanded',String(o));
if(o){history.pushState({imozuruPane:1},'');pushed=true}else if(pushed&&!fromPop){pushed=false;history.back()}else pushed=false}
addEventListener('popstate',function(){if(b.classList.contains('open'))setOpen(false,true)});
document.querySelector('header .menu').addEventListener('click',function(){setOpen(!b.classList.contains('open'))});
document.querySelector('.shade').addEventListener('click',function(e){if(e.detail>0&&e.timeStamp-endAt<250)return;setOpen(false)});
s.querySelectorAll('aside a').forEach(function(a){a.addEventListener('click',function(){
if(m.matches&&b.classList.contains('open')){pushed=false;b.classList.remove('open');mb.setAttribute('aria-expanded','false')}})});
// 一覧の調査を選ぶ・ホームへ戻るを View Transitions でつなぐ。選んだ題名と紙面の見出しに同じ名前を付け替えて、
// 古い画面の題名から新しい画面の見出しへ移す（同じ名前が2つあると動かないので、更新の中で付け替える）
var VT=!!document.startViewTransition&&!matchMedia('(prefers-reduced-motion:reduce)').matches;
if(VT)document.documentElement.classList.add('vt');
function vt(from,toSel,hash){var go=function(){location.hash=hash};if(!VT){go();return}
if(from)from.style.viewTransitionName='specimen';
document.startViewTransition(function(){if(from)from.style.viewTransitionName='';go();
var to=toSel&&document.querySelector(toSel);if(to&&from)to.style.viewTransitionName='specimen'}).finished.then(function(){
var to=toSel&&document.querySelector(toSel);if(to)to.style.viewTransitionName=''})}
s.querySelectorAll('aside a').forEach(function(a){a.addEventListener('click',function(e){if(e.defaultPrevented||!VT)return;e.preventDefault();
var id=a.getAttribute('href');var q=m.matches?null:a.querySelector('.q');vt(q,id+' h2',id.slice(1))})});
document.querySelectorAll('.topic .back').forEach(function(a){a.addEventListener('click',function(e){if(!VT)return;e.preventDefault();vt(null,null,'')})});
var t=null,endAt=-1e9;
function wide(el){var w=el&&el.closest&&el.closest('.tablewrap,pre');return w&&w.scrollWidth>w.clientWidth}
function marks(){document.querySelectorAll('.tablewrap,pre').forEach(function(w){w.style.touchAction=w.scrollWidth>w.clientWidth?'auto':''})}
marks();addEventListener('resize',marks);addEventListener('hashchange',marks);document.addEventListener('toggle',marks,true);
document.addEventListener('pointerdown',function(e){if(!m.matches||e.button>0)return;
// リンクの上から始めた操作も受ける（開いた一覧の項目の上から閉じられるように）。ドラッグの直後のクリックは下で止める
if(e.target.closest&&e.target.closest('header .menu,summary,input'))return;
// 横にスクロールできる表の上で始めた操作は、表のスクロールに譲る
if(wide(e.target))return;
var open=b.classList.contains('open');if(!open&&(e.clientX<EDGE||e.clientX>innerWidth-EDGE))return;
var f=open?W():0;t={id:e.pointerId,x:e.clientX,y:e.clientY,axis:'p',v:null,lx:e.clientX,lt:e.timeStamp,from:f,cur:f};
if(e.pointerType==='mouse')e.preventDefault()});
addEventListener('pointermove',function(e){if(!t||e.pointerId!==t.id)return;var dx=e.clientX-t.x,dy=e.clientY-t.y;
if(t.axis==='p'){if(Math.hypot(dx,dy)<LOCK)return;var ax=Math.abs(dx),ay=Math.abs(dy);
t.axis=ay>=RATIO*ax?'y':(ax>ay?'x':'p');if(t.axis==='y'){t=null;return}if(t.axis!=='x')return;
// 横に決まったら指の追跡をページが握る（指が要素の外へ出ても動きを拾い続ける）
try{b.setPointerCapture(t.id)}catch(x){}b.classList.add('dragging');t.kx=e.clientX;t.kt=e.timeStamp}
var dt=e.timeStamp-t.lt;if(dt>0){var inst=(e.clientX-t.lx)/dt;t.v=t.v===null?inst:0.5*inst+0.5*t.v}t.lx=e.clientX;t.lt=e.timeStamp;
t.cur=Math.min(W(),Math.max(0,t.from+dx));b.style.setProperty('--x',t.cur+'px')});
// iOS では指のドラッグが「放した」ではなく「中断」や「握りを失った」で終わることが多い。
// 軸が横に決まった後なら、終わり方によらず最後の位置と速さで開閉を決める（中断を「元に戻す」と読むと、放した瞬間に戻る）
function end(e){if(!t||e.pointerId!==t.id)return;var g0=t;t=null;if(g0.axis!=='x')return;
b.classList.remove('dragging');b.style.removeProperty('--x');endAt=e.timeStamp;
var moved=g0.cur-g0.from,v=g0.v,open;
if(v===null)v=(g0.lx-g0.kx)/Math.max(1,g0.lt-g0.kt);
if(Math.abs(v)>=FLICK)open=v>0;else open=g0.from>0?moved>-COMMIT:moved>=COMMIT;setOpen(open)}
addEventListener('pointerup',end);addEventListener('pointercancel',end);
// 横にドラッグした直後のクリック（指を放した所のリンクや暗い面）は、押したことにしない
document.addEventListener('click',function(e){if(e.detail>0&&e.timeStamp-endAt<250){e.preventDefault();e.stopPropagation()}},true);
// 握りを失った合図は、ページが握っていたときだけ終了と読む。子の要素から出た合図で終えると、握った直後に自分で止まる
b.addEventListener('lostpointercapture',function(e){if(e.target===b)end(e)});
var d=null;
g.addEventListener('pointerdown',function(e){if(m.matches)return;d={x:e.clientX,mv:false,w:b.classList.contains('collapsed')?0:W()};e.preventDefault()});
addEventListener('pointermove',function(e){if(!d)return;var dx=e.clientX-d.x;if(Math.abs(dx)>4&&!d.mv){d.mv=true;b.classList.add('dragging')}if(!d.mv)return;
b.classList.remove('collapsed');b.style.setProperty('--side',Math.min(520,Math.max(0,d.w+dx))+'px')});
addEventListener('pointerup',function(e){if(!d)return;var g1=d,dx=e.clientX-g1.x,w=Math.min(520,g1.w+dx);d=null;b.classList.remove('dragging');
if(!g1.mv){if(b.classList.toggle('collapsed'))st('0');else{b.style.setProperty('--side',last+'px');st(String(last))}return}
var close=g1.w===0?dx<COMMIT:(w<120||(dx<=-COMMIT&&w<200));
if(close){b.classList.add('collapsed');b.style.removeProperty('--side');st('0')}
else{w=Math.max(200,w);last=w;b.style.setProperty('--side',w+'px');st(String(w))}});
})();"""


def bars(rows: list[tuple[str, int]], cls: str = "", head: str = "", unit: str = "", colors: dict | None = None) -> str:
    """横棒。共通の目盛り（最大値を全幅）で、値は棒の横に数字でも出す。
    colors を渡すと行ごとに標本の色（.rc1〜.rc4）で塗る＝一覧のチップと同じ色で対応させる。ラベルは必ず文字で出す"""
    if not rows:
        return '<p class="note">記録なし</p>'
    peak = max(v for _, v in rows) or 1
    body = "".join(
        f'<tr><td class="lbl">{esc(k)}</td><td class="bar {cls} {("rc%d" % colors[k]) if colors and k in colors else ""}"><span style="width:{v / peak * 100:.1f}%" '
        f'title="{esc(k)}: {v}{unit}"></span></td><td class="n">{v}</td></tr>' for k, v in rows)
    return (f'<div class="tablewrap"><table><thead><tr><th>{esc(head)}</th><th></th><th class="n">{esc(unit)}</th></tr></thead>'
            f"<tbody>{body}</tbody></table></div>")


def reason_cls(t: dict) -> str:
    n = REASON_COLOR.get(t["reason_class"])
    return f"rc{n}" if n else "none"


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
             ("調査を区切った理由", (f'<span class="chip {reason_cls(t)}">{esc(t["reason_class"])}</span> '
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
    out = [f'<section class="topic {reason_cls(t)}" id="t-{esc(t["slug"])}">',
           '<a class="back" href="#">← ホームへ</a>',
           f"<h2>{esc(title_of(t))}</h2>",
           f'<p class="note">{esc(t["slug"])}</p>',
           '<div class="card"><dl class="facts">' + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in facts) + "</dl></div>"]
    if t["docs"]:
        out.append('<div class="card"><h3>文書</h3><div class="docs">'
                   + "".join(f'<a href="{esc(t["slug"])}/{esc(name)}">{esc(label)}</a>' for name, label in t["docs"]) + "</div></div>")
    if t["claims"]:
        out.append('<div class="card"><h3>調査の論点</h3><ul class="claims">'
                   + "".join(f"<li>{status_chip(s)}<span>{esc(c)}</span></li>" for c, s in t["claims"]) + "</ul></div>")
    if t["hosts"]:
        out.append('<div class="card"><h3>原文を取得した媒体</h3><div class="hosts">'
                   + "".join(f"<span>{esc(h)}<b>{n}</b></span>" for h, n in Counter(t["hosts"]).most_common()) + "</div></div>")
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
    out = ['<section class="home">',
           f'<p class="note">一覧から調査を選ぶと詳細が開く。数字はすべて各調査のフォルダの記録から機械で数えた。</p>',
           '<div class="card"><h3>調査を区切った理由</h3>',
           f'<p class="note">自動分類。{n}件中 {len(with_reason)}件に記録がある。元の文は各調査の詳細にある。</p>',
           bars(reasons, head="分類", unit="件", colors=REASON_COLOR),
           f"<details><summary>分類の規則（記録の文にこの語があれば、上から順に当てはめる）</summary><ul>{rules}</ul></details></div>",
           '<div class="card"><h3>原文を取得した媒体</h3>',
           f'<p class="note">map.md の原文の表の URL から数えた。{n}件中 {with_hosts}件に記録がある。上位{SHOWN_HOSTS}件。</p>',
           bars(hosts.most_common(SHOWN_HOSTS), cls="src", head="媒体", unit="本")]
    if len(hosts) > SHOWN_HOSTS:
        out.append(f"<details><summary>ほか {len(hosts) - SHOWN_HOSTS} 媒体</summary>"
                   + bars(hosts.most_common()[SHOWN_HOSTS:], cls="src", head="媒体", unit="本") + "</details>")
    if fetch_topics:
        ok = sum(t["fetch_ok"] for t in fetch_topics)
        ng = sum(t["fetch_ng"] for t in fetch_topics)
        out.append(f'<p class="note">取得の成否の記録は {len(fetch_topics)}件の調査にだけある（保存 {ok}・取得失敗 {ng}）。</p>')
    out.append("</div>")
    out += ['<div class="card"><h3>たどった周の数</h3>',
            f'<p class="note">1周目以降にたどった周の数。0周目（地図づくり）は数えない。{n}件中 {len(by_date)}件に記録がある。</p>',
            bars(sorted(Counter(f"{t['rounds']} 周" for t in by_date).items(), key=lambda kv: int(kv[0].split()[0])),
                 head="周", unit="件"),
            "</div></section>"]
    return "\n".join(out)


def render(topics: list[dict], root: Path) -> str:
    try:
        logo = (LOGO_LIGHT if LOGO_LIGHT.is_file() else LOGO).read_text(encoding="utf-8")
        logo = logo.replace("<svg ", '<svg role="img" aria-label="imozuru" ', 1)
    except OSError:
        logo = '<span class="ttl">imozuru</span>'  # 公開版の外で動かしたときなど、ロゴが無ければ文字で出す
    total_sources = sum(t["sources"] for t in topics)
    items = "".join(
        f'<li><a class="{reason_cls(t)}" href="#t-{esc(t["slug"])}"><span class="q">{esc(title_of(t))}</span>'
        f'<span class="m">{esc(t["date"] or "日付なし")}<span class="chip {reason_cls(t)}">'
        f'{esc(t["reason_class"])}</span></span></a></li>' for t in topics)
    # 選んでいる調査を一覧で示す。スクリプトを使わず、:target を :has() で一覧へ伝える
    current = "".join(f'.layout:has(#t-{t["slug"]}:target) aside a[href="#t-{t["slug"]}"]'
                      "{background:oklch(1 0 0 / .09)}" for t in topics)
    body = [f'<header><button class="menu" type="button" aria-controls="side" aria-expanded="false" aria-label="調査の一覧"><span></span></button>{logo}'
            f'<span class="sum">調査 {len(topics)} 件・原文 {total_sources} 本</span></header>',
            '<div class="layout">',
            f'<div class="side" id="side"><aside><h2>調査（新しい順）</h2><ol>{items}</ol></aside>'
            '<button class="grip" type="button" aria-label="調査の一覧を開く・閉じる（ドラッグでも動く）"></button></div>',
            '<button class="shade" type="button" aria-label="一覧を閉じる" tabindex="-1"></button><div class="frame" aria-hidden="true"></div>',
            "<main>", home_html(topics), *[topic_html(t) for t in topics],
            '<footer>imozuru の dashboard.py が生成。文書へのリンクは、このページと同じ場所にある調査のフォルダを指す。</footer>',
            "</main></div>"]
    # スクリプトは中身のハッシュで許可する。'unsafe-inline' にすると、調査の記録から紛れ込んだ文字列も動きうる
    script_hash = base64.b64encode(hashlib.sha256(SIDE_JS.encode()).digest()).decode()
    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; img-src data:; script-src 'sha256-{script_hash}'">
<title>imozuru 調べものの記録</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=EB+Garamond:wght@400;500&family=Noto+Serif+JP:wght@400;600&family={THEME["font"]}&display=swap">
<style>{CSS}:root{{--bar:{THEME["bar"]};--src:{THEME["src"]};--r1:{THEME["r1"]};--r2:{THEME["r2"]};--r3:{THEME["r3"]};--r4:{THEME["r4"]};--head:{THEME["head"]},"Hiragino Mincho ProN","Yu Mincho",serif}}{current}</style></head>
<body>
{chr(10).join(body)}
<script>{SIDE_JS}</script>
</body></html>
"""


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")
    sys.exit(main())
