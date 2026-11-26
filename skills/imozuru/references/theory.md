# imozuru の拠りどころ

SKILL.md の各決まりがどの原文のどこから来たかをここにまとめる。原文は 2026-10-09 の試行（題材＝探し方の研究）でこのスキル自身を使って取得した。行番号は下の URL を `scripts/fetch.py` で取得した .txt / .md のもの（PDF は `pdftotext -layout`、HTML は ax で変換。変換の仕方が違うと行番号はずれるので、短い引用で探す）。
決まりを直すときは、ここで元の文を読み直してから直す。

## 原文の取り先

| ファイル名 | URL |
|---|---|
| `bates-1989-berrypicking` | https://www.pages.gseis.ucla.edu/faculty/bates/articles/berrypicking.pdf |
| `ellis-1987-phd-behavioural-model` | https://etheses.whiterose.ac.uk/id/eprint/2975/1/DX093006.pdf |
| `kuhlthau-1991-inside-search-process` | https://tefkos.rutgers-sci.domains/Courses/612/Articles/Kuhlthau_Inside_91.pdf |
| `arxiv-2609.01294-explore-before-committing` | https://arxiv.org/pdf/2609.01294 |
| `arxiv-2608.02009-halt` | https://arxiv.org/pdf/2608.02009 |
| `arxiv-2602.03304-to-search-or-not` | https://arxiv.org/pdf/2602.03304 |
| `kastner-2007-horizon-estimate` | https://pmc.ncbi.nlm.nih.gov/articles/PMC2655834/ |
| `eumesy-unknown-terms` | https://speakerdeck.com/eumesy/examine-the-meaning-of-unknown-terms |
| `arxiv-2505.09316-scent-of-knowledge` | https://arxiv.org/pdf/2505.09316 |

## 決まりと出典

| 決まり | 出典 | 原文の場所 |
|---|---|---|
| 問いは探すうちに変わる。拾った断片を集めていく | Bates 1989 "The design of browsing and berrypicking techniques for the online search interface" | `bates-1989-berrypicking.txt` |
| たどり方の6つの動き（後ろへ・前へ・雑誌を通しで・近くを眺める・主題の索引・著者） | Bates 1989 | 同 L295-355 |
| 引用をたどれば、主題を言葉で言い表す問題を避けられる | Bates 1989 | 同 L297-302 |
| starting / chaining / browsing / differentiating / monitoring / extracting | Ellis 1987 博士論文（1989 の JoD 論文の元・本体は有料） | `ellis-1987-phd-behavioural-model.txt` L64、目次 L165-276 |
| 調べ始めは混乱するのがふつう。詳しいノートは早すぎる閉じを招く。当てはまりそうな事実を並べる | Kuhlthau 1991 "Inside the Search Process" | `kuhlthau-1991-inside-search-process.txt` L345-368 |
| 1つの方向に決め打ちすると、後の検索が同じ道を強める。仮説を枝分けして比べてから進む | Zhou ら 2026 "Explore Before Committing"（HypoSearch）arXiv 2609.01294 | `arxiv-2609.01294-explore-before-committing.txt` 冒頭の要旨 |
| 止めどきは確信度ではなく、調査の論点の全部を証拠が支えたか | Roh & Han 2026 "HALT" arXiv 2608.02009 | `arxiv-2608.02009-halt.txt` 要旨 |
| 誤りは2種類＝足りているのに探し続ける over-search と、早くやめて外す under-search | Zhang ら 2026 "To Search or Not to Search" arXiv 2602.03304 | `arxiv-2602.03304-to-search-or-not.txt` 要旨 |
| 独立した2つの探し方の重なりから、まだ見つかっていない総数を見積もる（capture-mark-recapture） | Kastner ら 2007 "Estimating the Horizon of Articles to Decide When to Stop Searching" | `kastner-2007-horizon-estimate.md` L51 |
| 知らない用語は粒度の粗い順に＝百科事典 → 専門事典 → 教科書・講義資料 → サーベイ → 原著 | 横井祥 2017 「Examine the meaning of unknown terms」（Speaker Deck） | `eumesy-unknown-terms.md` L343-373 |
| 情報の匂い（information scent）で次にたどる先を選ぶ | Pirolli & Card 1999 "Information foraging"（本体は有料・DOI 10.1037/0033-295x.106.4.643） | 未取得。LLM に当てた例は `arxiv-2505.09316-scent-of-knowledge.txt` |
| 詳細レポートの各編末尾に確かめる問い（`quiz`）を置く（読み返すより自分をテストするほうが定着する） | Dunlosky ら 2013（テスト効果 / practice testing） | 詳細レポートの `quiz` 仕様の根拠 |

## 試行で分かった、流れの側の事実

- 検索結果の URL は古い。11 本中 5 本が 404・ログインの壁・PDF のふりをした HTML → `fetch.py` で中身を見て判定する
- LLM の検索エージェントの論文3本の参考文献に、人の情報探索の研究（Bates・Kuhlthau・Ellis・Marchionini）は0件、情報採餌理論が1件だけ。AI 側はこの蓄積をほとんど使っていない
- OpenAlex で題名から探すとき、全文検索（`search=`）だと解説記事が上に来る。題名に絞って被引用順にすると本体が1位に来る。ただし登録された題名が副題抜きのことがある → DOI を最優先
- 検索役のサブエージェントは「探していない」の節をきちんと書く。そのまま次の周の手がかりの一覧になる

## まだ読んでいない定番（届いたら足す）

- Pirolli & Card 1999（有料）／Marchionini 2006 "Exploratory search"（有料）
- Case & Given 2016 "Looking for Information"（情報探索研究の教科書。OpenAlex で Bates・Kuhlthau を引く論文の上位）
- Wilson 1999 "Models in information behaviour research"／Booth 2010 "How much searching is enough?"／TARCiS 2024（引用をたどる探索の指針）

## 3つの題材で試して分かったこと（2026-10-09）

- 題材＝探し方の研究（親が手で）／AI 生成画像の編集可能化（論文の多い分野・2回）／大陸塗り（論文が無く個人の発信だけの分野）
- 論点を「〜がある」だけで並べると1周で埋まり、依頼の芯が抜けたまま止まる → 論点の1つは依頼の条件そのまま、半分以上は原文を読まないと答えられない形
- 論文の多い分野では chain.py で古い論文の引用はたどれるが、新しい論文（出て1年ほど）の引用はたどれない
- 個人の発信の分野では chain.py も DOI も使えず、道具は fetch.py と WebSearch だけ。standard の検索はほぼ空振りで extended で一斉に出た。大事な中身は X・pixiv・小紅書のログインの壁の向こうに集まる
- 日本語の記事は短くても本物（2,904字）。英語の論文向けの下限 3,000 字では FAIL になる
- PDF でも同じ。和文の字が3割を超える PDF は HTML と同じ 1,000 字で判定する
- 検索役の報告は検索結果の抜粋がもと。Type-R の説明が原文と違っていた

## 各決まりの元になった試行の記録

- スキルが生まれたきっかけ: 「自分の感覚と AI の調査結果がかみあわない」という経験から、調べている間は要約せず原文の場所を指し、判断をユーザーへ委ねる形にした
- 論点の作り方（依頼の条件を入れる）: 試行では5つ全部の論点が「〜がある」になり、1周で原文が付いて止まった結果、依頼の中心の条件（「AI で生成した画像を」）が確かめられないまま残った
- 原典・研究を論点に入れる（2026-10-12 の比較）: チュートリアルだけで答えると「規則は無い」と結論しやすい。2026-10-12 の比較では、素の依頼では3回とも「規則は無い／理論ではない」と結論し、原典を問いに入れずに調べた imozuru も Rood 1879 に届かなかった
- 検索役にファイルを書かせない: 試行で検索役が `curl` で一時ファイルを書いたため、ダウンロードは親の仕事に限定した
- JSON を手で写し直さない: 手で写すと誤記が記録に入る。試行では依頼文の中の書式の見本を取り違えて1行入れ、消して入れ直した
- 英語で始める: 日本語で検索語が見つからない分野は英語で始める（試行で「berrypicking ベイツ」を検索したところ果物狩りの記事ばかりだった）
- 検索の深さの切り替え: 大陸塗りの試行では普通の検索の8本がほぼ空振りで、深い検索の1本目で note・YouTube・ブログが一斉に出た。MIX の試行では記事は出たが、普通の検索だと YouTube が0件だった。深い検索は費用が数倍のため、欲しい種類が0件のときだけ使う
- 検索役の報告に（未確認）を付ける: 検索役は検索結果の抜粋がもとで原文を開いていない（試行では2本とも原文を開いた回数が 0）。人の所属・「公開済み」・数字は外れていることがある
- 別の置き場を探す（2026-10-14 の DTIC）: 2026-10-14 には DTIC で 403 だった Mann & Thompson と Hovy の原典が、SFU の RST サイトに置いてあった
- 飽和の7割と別仮説の扱い: 飽和の7割は試行で決めた目安で、まだ根拠は弱い。別の仮説を追わせた2本は話題が違うため重なりが低く出るのが当たり前で（試行では 2/14）、飽和を数える材料にはしない
- 「既知」に親の拾った分も入れる: 親が自分で拾った分を入れ忘れ、飽和の分子が小さく出たことがあるため、検索役へ渡す既知の一覧にも同じ一覧を渡す
- 返す前に残った処理を止める（2026-10-12 の試行）: 調べものを任せたエージェントが報告を返したあとも、中で起動したものが動き続けていた（何だったかは `log.md` に無く分からなかった）。そのため、返す前に確認して止め、`log.md` に残す決まりにした
