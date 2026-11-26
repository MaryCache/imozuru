# 道具の詳細仕様とフォールバック手順

スクリプトは Python 3.10 以上の標準ライブラリだけで動く。外部コマンドはあれば使う。

## 目次

- [fetch.py の詳細仕様](#fetchpy-の詳細仕様)
- [ingest.py（ローカル保存資料の取り込み）](#ingestpyローカル保存資料の取り込み)
- [fetch.py で取れないときのブラウザ取得手順](#fetchpy-で取れないときのブラウザ取得手順)
- [chain.py（OpenAlex）の詳細と注意点](#chainpyopenalexの詳細と注意点)
- [report.py / digest.py / dashboard.py / watch.py](#reportpy--digestpy--dashboardpy--watchpy)

---

## fetch.py の詳細仕様

`scripts/fetch.py <sources_dir> <name> <url> [--no-fallback]`: ダウンロードして本物か確かめ、作業用フォルダで検査して通ったものだけを `sources_dir` へ移す。

- PDF: `.pdf` と `.txt` を保存する（`pdftotext -layout`・poppler か Xpdf。無ければ `pdfminer.six`）。
  - 文字の層が無い PDF は `pdftoppm` と `tesseract` で OCR する。言語は tesseract の言語データに `jpn`・`jpn_vert` があれば英語に足し、環境変数 `IMOZURU_OCR_LANG` があればそれを使う（海外言語の例: `chi_sim+eng`、`kor+eng`、`deu+eng`、`fra+eng`、`rus+eng`、`ara+eng`）。
  - 読み取りの確信度が低いとき（言語データが合わず意味のない字の列になったとき）と、読めなかったページがあるときは OCR の結果を使わず、理由を添えて `FAIL` する。必要なコマンドが無いときは、足りないコマンドの名前を書いて `FAIL` する。
- HTML: `.md` を保存する。[ax](https://github.com/yusukebe/ax) があれば ax で、無ければ標準ライブラリで本文を文字にする（見出しや表の形は ax のほうが残る）。取得はいつも `fetch.py` 自身が宛先を確かめながら行い、ax には取ってきた HTML を渡して変換だけさせる。
  - 文字コードは BOM → UTF-8 → 宣言 charset → HTML の `lang` ヒントに基づく言語別コードページ（GB18030・Big5・CP949・CP1251・CP1252・CP1256 等）→ 汎用フォールバックの順で解決する。国際化ドメイン名（IDN）も Punycode 変換したうえで SSRF 検査と取得を行う。
  - HTML のページは User-Agent を変えて再取得を試み、PDF 本体が返ればそちらを残す（HAL はブラウザの User-Agent に閲覧用の HTML を返す）。
- YouTube: 字幕を `[分:秒] 文` の `.md` で保存する（`yt-dlp`・動画は保存しない）。既定では日本語・英語の手動字幕を先に試し、無ければ動画の元言語（`zh-Hans` のようにサブタグつきでも）の手動字幕、それも無ければ元言語の自動字幕を取る。特定言語を優先したいときは環境変数 `IMOZURU_YT_LANGS`（`zh-Hans,en` のような並び）で指定でき、字幕が複数あればその並び順で選ぶ（`all`・`live_chat` は指定不可）。動画の場所は行番号の代わりに時刻で指す（`sources/x.md [12:34]`）。
- PMC（`pmc.ncbi.nlm.nih.gov`）: 記事ページは captcha で取れないため、同じ本文を Europe PMC の全文 XML から取り、見出し・段落・図表の説明・文献を1つずつ1行にした `.md` にする（題名と書誌は冒頭の数行。Europe PMC に全文が無ければ `FAIL`）。
- 一時障害の再試行と公式 OA 補完: 一時障害（HTTP 429・503・504・タイムアウト）は `Retry-After` を尊重して最大2回までジッター付きで再試行する（400・401・403・404 等の恒久エラーは再試行しない）。`--no-fallback` が無ければ、明示的な DOI または PMCID を持つ学術 URL が失敗したときに Europe PMC 検索 API・OpenAlex Works API から DOI・PMCID が完全一致する公開版（JATS 全文 XML・OA PDF・HTML）を取り直す。補完で取得した経路は `fetch-log.jsonl` の `via`・`resolved_url` に残す。
- FAIL 条件: 404・ログインの要求・ボット確認のページ（日英および中・韓・独・仏・西・葡・露・アラビア語の主要文言を含む）・短すぎる本文は `FAIL`（同じ名前の原文がすでにあれば、そのまま残る）。
  - 短さの下限は PDF が 3,000字、HTML が 1,000字。日本語・中国語・韓国語（ハングル）の字が3割を超える PDF は HTML と同じ 1,000字で判定する。
  - X は本文が取れないため取りに行かず `FAIL` とし、URL と検索役が拾った抜粋を（未確認）で `map.md` に残す。
- ファイル名規則: `<著者>-<年>-<短い題>`、arXiv は `arxiv-<番号>-<短い題>`。

---

## ingest.py（ローカル保存資料の取り込み）

`scripts/ingest.py <sources_dir> (--file <path> [--name <name>] [--url <url>] | --inbox <dir>) [--short-ok] [--move-processed]`

ユーザーが自分のブラウザで保存したローカルの HTML・PDF・テキストを、サイズ上限・壁と確認画面の検査・LF 正規化・原子的保存を通して `sources/` へ取り込み、`fetch-log.jsonl` に `"via": "user-browser"` で記録する。HTML 先頭の `saved from url=` コメントや `.meta.json` サイドカーから元 URL を引き継ぐ。頁数や描画本文で本当に短いと確認した資料は `--short-ok` で取り込む（壁検査は維持される）。

---

## fetch.py で取れないときのブラウザ取得手順

有料や `FAIL` で取れなかった定番は「届かなかった」と書く前に、著者や研究室のページ・その分野のまとめサイト・元になった技術報告・大学のリポジトリ等の別置き場（海賊版サイトは使わない）を探す（2026-10-14 には、DTIC で 403 だった Mann & Thompson と Hovy の原典が SFU の RST サイトに置いてあった）。有料・ログイン・404 でない `FAIL` は次の3つのどれかに当てはまることが多く、届かなかったと書く前に使える道具で取り直すことを勧める。

1. 403 や JavaScript 描画の画面（本文が1字や数十字の `FAIL`）: **Playwright など本物のブラウザ**で開き、返った PDF か描画した本文を保存する（試行では学会の PDF の 403 と、大学のリポジトリの JavaScript の画面が取れた）。
   - サンドボックス明示: Playwright で開くときは、サンドボックスを明示して起動する（Python は `p.chromium.launch(chromium_sandbox=True)`、JavaScript は `chromium.launch({ chromiumSandbox: true })`）。Playwright の既定は `--no-sandbox` で、信頼できない外のページをサンドボックスなしで開くことになる。手元の Chrome があれば `channel="chrome"`（JavaScript は `channel: "chrome"`）を足してもよい。
   - サンドボックスつきで起動できない環境（Docker の root、user namespace を使えない Linux など）でも、`--no-sandbox` を足して開かない（CLI や MCP から起動するときも同じ）。起動できなかったことと理由をユーザーに伝え、下の「ユーザーのブラウザを操作できる道具」か、ユーザーに開いて保存してもらう手に切り替える。
2. 資料が本当に短い（1頁の要約など）: 「短すぎる」の `FAIL` は先に頁数や描画した本文で本当に短いかを確かめる。短いなら、壁検査を保ったまま `scripts/ingest.py --short-ok` で取り込み、原文の表に（短い資料）と書く。
3. Cloudflare などの確認画面: headless のブラウザでも通らない。**ユーザーのブラウザを操作できる道具**（DevTools をつなぐ MCP など）があれば、ユーザーに確かめてからそのブラウザで同じ URL を開き、確認画面が自然に通るのを待って転送先の URL を `fetch.py` に渡す。無ければユーザーに開いてローカルへ保存してもらい、`scripts/ingest.py` で検証付きのまま `sources/` へ取り込む。

どの道具で取ったものも `fetch.py` と同じ検査（PDF なら文字起こしの長さ、確認画面や壁のページでないか）を通してから `sources/` に置き、`fetch-log.jsonl` に取り方を書き添える。手元や内側の宛先へはブラウザでも取りに行かない。確認画面をすり抜ける細工はしない。

---

## chain.py（OpenAlex）の詳細と注意点

`scripts/chain.py find|refs|cites|author [--log <topic_dir> --round N]`: OpenAlex で引用と著者をたどる。`--log` を付けると出した一覧を `queries.jsonl` に足す。

- 無料枠と制限: 1日 1,000回まで無料の枠で、プリペイド残高が 0 なら超えても止まるだけ。**API キーや支払いの登録はしない**。
- 新しい論文: 出て1年ほどの論文は OpenAlex に引用の登録が追いついておらず、0件を返す（試行では出て1年以内の論文で `refs` も `cites` も 0件）。0件は「無い」ではない。後ろ（参考文献）へは保存した本文の参考文献の節を grep し（試行ではこれで目当ての論文が見つかった）、前（被引用）へは検索役に「`<題名>` を引いている論文」を探させる。
- 題名検索の注意: 題名で引くと別の記録や解説記事が出ることがあるため、`chain.py find` の候補を年と題で確かめてから、DOI で引く。
- 同名の著者（author）: `author` は名前の検索の1位を使うため、同名の別人の論文を返すことがある。出力の「`# 別の候補`」に別人の ID が並ぶため、所属と論文数で確かめ、違えば `chain.py author A…` で引き直す。
  - 古い論文ほど OpenAlex の著者の記録は当てにならない。著者の欄に ID が無い論文や、同名の何人もの論文を1人にまとめた記録がある（試行では、情報探索の Ellis の 1989 年の論文には ID が無く、1993 年の論文は所属が国際ポテトセンターで論文289本の記録に入っていた）。著者で追うときは所属先や本人のページにある論文の一覧を検索役に探させ、`chain.py author` の結果はその一覧と突き合わせる。

---

## report.py / digest.py / dashboard.py / watch.py

- `scripts/report.py <topic_dir> [--check]`: `extracts.json` の抜き書きを原文と照合し、合えば `report.html` にする。`--check` は照合だけ。
- `scripts/digest.py add <topic_dir> [--round N] [--by 誰]`: 標準入力の JSON（検索語ごとのヒット）を `queries.jsonl` に足す。
- `scripts/digest.py <topic_dir> [--check]`: `digest.json` の解説の引用を原文と照合し、合えば検索の記録と合わせて `digest.html` にする。
- `scripts/dashboard.py [research_dir] [--runs runs.md]`: 調査の保存先（既定 `~/research`）を走査して、調べものの履歴のページ `index.html` を作る。ホームには区切った理由・原文を取得した媒体・たどった周の数の集計を表示し、左の一覧から各調査の詳細（論点・周・原文・文書へのリンク）を開ける。数字はフォルダの記録から数え、記録の無い所は「記録なし」と出す。
- `scripts/dashboard.py [research_dir] --bundle <dir>`: 履歴のページと各調査の文書（詳細レポート・抜き書き・`map.md`・`log.md`）だけを `<dir>` に写す。原文（`sources/`）は写さない。入れたファイルの一覧を出す。出力した束（バンドル）には原文からの引用が入るため、公開の場所へは置かない。スマホで見る載せ方は検討中（下書き）。
- `scripts/watch.py add|list|check`: RSS / Atom フィードを監視し、前回からの新着を出す。検索を使わないため費用はかからず結果も揺れない。`DOCTYPE`・`ENTITY` を含むフィードは安全のため読まない。フィードの指定先（URL 等）にタブや改行が含まれる場合は追加しない。
