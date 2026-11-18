# 道具の詳細仕様とフォールバック手順

スクリプトは Python 3.10 以上の標準ライブラリだけで動く。外部コマンドはあれば使う。

## 目次

- [fetch.py の詳細仕様](#fetchpy-の詳細仕様)
- [fetch.py で取れないときのブラウザ取得手順](#fetchpy-で取れないときのブラウザ取得手順)
- [chain.py（OpenAlex）の詳細と注意点](#chainpyopenalexの詳細と注意点)
- [report.py / digest.py / dashboard.py / watch.py](#reportpy--digestpy--dashboardpy--watchpy)

---

## fetch.py の詳細仕様

`scripts/fetch.py <sources_dir> <name> <url>`: ダウンロードして本物か確かめ、作業用フォルダで検査して通ったものだけを `sources_dir` へ移す。

- **PDF**: `.pdf` と `.txt` を保存する（`pdftotext -layout`・poppler か Xpdf。無ければ `pdfminer.six`）。
  - 文字の層が無い PDF は `pdftoppm` と `tesseract` で OCR する。言語は tesseract の言語データに `jpn`・`jpn_vert` があれば英語に足し、環境変数 `IMOZURU_OCR_LANG` があればそれを使う。
  - 読み取りの確信度が低いとき（言語データが合わず意味のない字の列になったとき）と、読めなかったページがあるときは OCR の結果を使わず、理由を添えて `FAIL` する。要るコマンドが無いときは、無いコマンドの名前を書いて `FAIL` する。
- **HTML**: `.md` を保存する。[ax](https://github.com/yusukebe/ax) があれば ax で、無ければ標準ライブラリで本文を文字にする（見出しや表の形は ax のほうが残る）。取得はいつも `fetch.py` 自身が宛先を確かめながら行い、ax には取ってきた HTML を渡して変換だけさせる。
  - HTML のページは、名乗りを変えてもう一度頼み、PDF 本体が返ればそちらを残す（HAL はブラウザの名乗りに閲覧用の HTML を返す）。
- **YouTube**: 字幕を `[分:秒] 文` の `.md` で保存する（`yt-dlp`・動画は保存しない）。動画の場所は行番号の代わりに時刻で指す（`sources/x.md [12:34]`）。
- **PMC（`pmc.ncbi.nlm.nih.gov`）**: 記事ページは captcha で取れないため、同じ本文を Europe PMC の全文 XML から取り、見出し・段落・図表の説明・文献を1つずつ1行にした `.md` にする（題名と書誌は頭の数行。Europe PMC に全文が無ければ `FAIL`）。
- **FAIL 条件**: 404・ログインの要求・ボット確認のページ・短すぎる本文は `FAIL`（同じ名前の原文がすでにあれば、そのまま残る）。
  - 短さの下限は PDF が 3,000字、HTML が 1,000字。和文の字が3割を超える PDF は HTML と同じ 1,000字で判定する。
  - X は本文が取れないので取りに行かず `FAIL` とし、URL と検索役が拾った抜粋を（未確認）で `map.md` に残す。
- **ファイル名規則**: `<著者>-<年>-<短い題>`、arXiv は `arxiv-<番号>-<短い題>`。

---

## fetch.py で取れないときのブラウザ取得手順

`fetch.py` は標準ライブラリで1回頼むだけなので、次の3つでは取れない。有料で取れなかった定番は著者ページや大学リポジトリ等の別置き場（海賊版サイトは使わない）を探し、有料・ログイン・404 でない `FAIL` は届かなかったと書く前に使える道具で取り直すことを勧める。

1. **403 や JavaScript 描画の画面（本文が1字や数十字の FAIL）**:
   **Playwright など本物のブラウザ**で開き、返った PDF か描画した本文を保存する。
   - **サンドボックス明示**: Playwright で開くときは、サンドボックスを明示して起動する（Python は `p.chromium.launch(chromium_sandbox=True)`、JavaScript は `chromium.launch({ chromiumSandbox: true })`）。Playwright の既定は `--no-sandbox` で、信頼できない外のページをサンドボックスなしで開くことになる。手元の Chrome があれば `channel="chrome"`（JavaScript は `channel: "chrome"`）を足してもよい。
   - サンドボックスつきで起動できない環境（Docker の root、user namespace を使えない Linux など）でも、`--no-sandbox` を足して開かない（CLI や MCP から起動するときも同じ）。起動できなかったことと理由をユーザーに伝え、下の「ユーザーのブラウザを操作できる道具」か、ユーザーに開いてもらう手に切り替える。
2. **資料が本当に短い（1頁の要約など）**:
   「短すぎる」の `FAIL` は、先に頁数や描画した本文で本当に短いかを確かめる。短いなら、そのまま保存して原文の表に（短い資料）と書く。
3. **Cloudflare などの確認画面**:
   headless のブラウザでも通らない。**ユーザーのブラウザを操作できる道具**（DevTools をつなぐ MCP など）があれば、ユーザーに確かめてからそのブラウザで同じ URL を開き、確認画面が自然に通るのを待って転送先の URL を `fetch.py` に渡す。無ければユーザーに開いてもらう。

どの道具で取ったものも、`fetch.py` と同じ検査（PDF なら文字起こしの長さ、壁のページでないか）を通してから `sources/` に置き、`fetch-log.jsonl` に取り方を書き添える。手元や内側の宛先へは、ブラウザでも取りに行かない。確認画面をすり抜ける細工はしない。

---

## chain.py（OpenAlex）の詳細と注意点

`scripts/chain.py find|refs|cites|author [--log <topic_dir> --round N]`: OpenAlex で引用と著者をたどる。`--log` を付けると出した一覧を `queries.jsonl` に足す。

- **無料枠と制限**: 1日 1,000回まで無料の枠で、プリペイド残高が 0 なら超えても止まるだけ。**API キーや支払いの登録はしない**。
- **新しい論文**: 出て1年ほどの論文は OpenAlex に引用の登録が追いついておらず、0件を返す。0件は「無い」ではない。後ろへは保存した本文の参考文献の節を grep し、前へは検索役に「`<題名>` を引いている論文」を探させる。
- **題名検索の注意**: 題名で引くと別の記録や解説記事が出ることがあるので、`chain.py find` の候補を年と題で確かめてから、DOI で引く。
- **同名の著者（author）**: `author` は名前の検索の1位を使うため、同名の別人の論文を返すことがある。出力の「`# 別の候補`」に別人の ID が並ぶので、所属と論文数で確かめ、違えば `chain.py author A…` で引き直す。
  - 古い論文ほど OpenAlex の著者の記録は当てにならない（ID が無い論文や、同名の何人もの論文を1人にまとめた記録がある）。著者で追うときは、所属先や本人のページにある論文の一覧を検索役に探させ、`chain.py author` の結果はその一覧と突き合わせる。

---

## report.py / digest.py / dashboard.py / watch.py

- `scripts/report.py <topic_dir> [--check]`: `extracts.json` の抜き書きを原文と照合し、合えば `report.html` にする。`--check` は照合だけ。
- `scripts/digest.py add <topic_dir> [--round N] [--by 誰]`: 標準入力の JSON（検索語ごとのヒット）を `queries.jsonl` に足す。
- `scripts/digest.py <topic_dir> [--check]`: `digest.json` の解説の引用を原文と照合し、合えば検索の記録と合わせて `digest.html` にする。
- `scripts/dashboard.py [research_dir] [--runs runs.md]`: 調査の置き場（既定 `~/research`）を走査して、調べものの履歴のページ `index.html` を作る。ホームに区切った理由・原文を取得した媒体・たどった周の数の集計、左の一覧から各調査の詳細（論点・周・原文・文書へのリンク）。数字はフォルダの記録から数え、記録の無い所は「記録なし」と出す。
- `scripts/dashboard.py [research_dir] --bundle <dir>`: 履歴のページと各調査の文書（詳細レポート・抜き書き・`map.md`・`log.md`）だけを `<dir>` に写す。原文（`sources/`）は写さない。入れたファイルの一覧を出す。束には原文からの引用が入るので、公開の場所へは置かない。
- `scripts/watch.py add|list|check`: 見張り。RSS / Atom を引いて前回からの新着を出す。検索を使わないので費用はかからず結果も揺れない。`DOCTYPE`・`ENTITY` を含むフィードは安全のため読まない。フィードの場所にタブや改行が入っていたら足さない。
