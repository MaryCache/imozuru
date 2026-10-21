#!/usr/bin/env python3
"""原文を1本落とし、本物かどうか確かめてから sources/ に残す。

検索結果の URL は古いことが多く、2026-10-09 の試行では 11 本中 5 本が
404・ログインの壁・PDF のふりをした HTML だった。拡張子や HTTP の成否では
見分けられないので、中身の先頭と文字数で判定する。

論文だけでなく個人の発信も落とす。YouTube は字幕を `[分:秒] 文` の形で
.md にする（場所を時刻で指せるように）。X はログインの壁で本文が取れないので、
取りに行かずに FAIL を返す。

使い方:
    fetch.py <sources_dir> <name> <url>
出力（標準出力に1行・タブ区切り）:
    OK   <name> <保存したファイル> <文字数> <先頭の一行>
    FAIL <name> <理由>
"""

import codecs
import ipaddress
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

# 本文として読める最低の文字数。404 ページや認証の壁は数百〜数千文字に収まる。
# PDF は英語の論文が多いので高め。HTML は日本語の短い記事を通すため低め
# （大陸塗りの試行で 2,904 字の本物の記事が 3,000 の下限で落ちた）。
MIN_TEXT_CHARS = 3000
MIN_HTML_CHARS = 1000
# 和文（日本語・中国語）の PDF は、英語より少ない字数で中身がある。
# 本文の空白以外の字のうち和文の字がこの割合を超える PDF は、HTML と同じ下限で判定する。
CJK_RATIO_FOR_PDF = 0.3
# 和文の字＝かな・漢字・全角の英数字と記号
_CJK = re.compile(r"[\u3000-\u30ff\u3400-\u9fff\uf900-\ufaff\uff00-\uffef]")
MAX_BYTES = 40_000_000
# 外のコマンド（pdftotext・pdftoppm・tesseract・ax・yt-dlp）1回の時間の上限（秒）。
# 細工した PDF やページで止まらなくなるのを防ぐ。ふつうの論文は数秒〜数十秒で終わる。
TOOL_TIMEOUT = 300
# OCR にかけるページ数の上限。文字の層が無い古い論文は数十ページに収まる。
# 何千ページもある PDF で、画像の書き出しが手元のディスクを埋めないようにする。
OCR_MAX_PAGES = 60
# OCR の読み取りの確信度（tesseract の 0〜100）の、字数で重みを付けた平均の下限。
# 言語データが合わないと、意味のない英字の列が数千字返って字数の下限を通ってしまう（#11）。
# 2026-10-21 の計測: 和書のスキャンを eng で読むと 18〜25、jpn で読むと 72〜82。
# 英語の古い論文（1987・1993）は 87〜96。合成した縦書き・横書きの和文も同じ分かれ方をした。
OCR_MIN_CONFIDENCE = 50
# 入っていれば自動で足す言語データ。jpn だけでは縦書きが、jpn_vert だけでは横書きが読めない（#11）
OCR_EXTRA_LANGS = ("jpn", "jpn_vert")
# IMOZURU_OCR_LANG で受け付ける形（eng・jpn+eng など）。tesseract の引数に入るので、ほかの字は通さない
OCR_LANG_FORM = re.compile(r"[A-Za-z_]+(\+[A-Za-z_]+)*")
# 本文ではなく壁に当たったと分かる言い回し。小文字で照合する。
WALL_PATTERNS = (
    "404 not found",
    "page not found",
    "access denied",
    "you do not currently have access",
    "sign in to continue",
    "log in to continue",
    "enable javascript",
    "are you a robot",
    "captcha",
    # Anubis（ボット除けの計算をさせるページ）。HAL などが使う。中身の無い 1,000 字ほどのページが
    # HTML の下限を越えて OK になった（2026-10-20）
    "you're not a bot",
)


class _Redirect308(urllib.request.HTTPRedirectHandler):
    """Python 3.10 でも 308 の転送をたどる。

    3.10 の HTTPRedirectHandler は http_error_308 を持たず、redirect_request も 308 をはねる
    （どちらも 3.11 で足された）。308 は 307 と同じく「同じメソッドのまま別の URL へ」なので、
    307 として扱わせる。3.11 以降では動きは変わらない（openhands.dev などが 308 を返す・#3）。
    """

    http_error_308 = urllib.request.HTTPRedirectHandler.http_error_302

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 転送の応答の本体は読まずに閉じる。素の http_error_302 は次へ進む前に本体を最後まで
        # 読むので、大きな本体を付けた転送で MAX_BYTES の上限を素通りする（3.12 の実装で確認）
        fp.close()
        problem = url_problem(newurl)
        if problem:
            raise UnsafeURL(f"転送先 {newurl}: {problem}")
        return super().redirect_request(req, fp, 307 if code == 308 else code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_Redirect308)


class UnsafeURL(Exception):
    """取りに行ってはいけない URL。呼び手はほかの取得の失敗と同じく FAIL にする。"""


def resolve(host: str) -> set[str]:
    """ホスト名を引いて IP アドレスの集まりを返す。テストはここを差し替える。"""
    return {info[4][0] for info in socket.getaddrinfo(host, None)}


def url_problem(url: str) -> str | None:
    """取りに行ってはいけない URL なら理由を、よければ None を返す。

    URL も転送の先も、検索結果のページを書いた他人が決める。127.0.0.1 や社内の宛先を書かれると、
    手元で動いているサービス（開発用のサーバー・ローカルの AI・ブラウザのデバッグ口）の中身を
    sources/ に写してしまう（2026-10-20 のレビュー）。名前を引いた先が全部インターネット上の
    宛先のときだけ通す。10進や16進で書いた IP も、名前を引けば 127.0.0.1 などになるので同じく止まる。
    名前を引いてから接続するまでの間に引き先を変える手（DNS rebinding）は防げない。

    ホストの部分に「@」か「%」があれば断る。urlsplit は「x@a.example」の a.example を見るが、
    urllib は x@a.example をそのまま引き、「%61」は urllib だけが a に戻す。検査とつなぐ先がずれる。
    VPN などで外の名前まで内側の宛先に引ける環境では、環境変数 IMOZURU_ALLOW_PRIVATE=1 で
    宛先の検査だけを外せる（http(s) の検査は残る）。
    """
    try:
        p = urllib.parse.urlsplit(url)
        host = p.hostname
    except ValueError:
        return "URL の形が壊れている"
    if p.scheme.lower() not in {"http", "https"}:
        return "http(s) の URL だけを取りに行く"
    if not host:
        return "URL にホストが無い"
    if "@" in p.netloc or "%" in p.netloc:
        return "URL のホストに「@」か「%」が入っている"
    if os.environ.get("IMOZURU_ALLOW_PRIVATE") == "1":
        return None
    try:
        addrs = resolve(host)
    except (OSError, UnicodeError, ValueError) as e:
        return f"ホストを引けない（{e}）"
    for addr in addrs:
        for ip in _addresses_inside(ipaddress.ip_address(addr.split("%", 1)[0])):  # IPv6 の %eth0 を外す
            if not ip.is_global:
                return (f"手元や内側の宛先（{ip}）には取りに行かない"
                        "（VPN などで外の名前も内側に引ける環境なら IMOZURU_ALLOW_PRIVATE=1 で外せる）")
    return None


def _addresses_inside(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> list:
    """IP と、その IPv6 の中に埋め込まれた IPv4 を返す。

    64:ff9b::7f00:1（NAT64）や ::127.0.0.1 は IPv6 としては「インターネット上」に数えられるが、
    中の IPv4 は 127.0.0.1。NAT64 のある回線では手元へ届く（2026-10-20 の2回目のレビュー）。
    """
    found = [ip]
    if ip.version == 6:
        low32 = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if ip.ipv4_mapped:
            found.append(ip.ipv4_mapped)
        if ip.sixtofour:
            found.append(ip.sixtofour)
        if ip.teredo:
            found.extend(ip.teredo)
        if (ip in ipaddress.ip_network("::/96") or ip in ipaddress.ip_network("64:ff9b::/96")
                or ip in ipaddress.ip_network("64:ff9b:1::/48")):
            found.append(low32)
    return found


def ascii_url(url: str) -> str:
    """パスとクエリにそのまま入った日本語の字などを % で符号化する。

    urllib は要求の行を ASCII で組むので、辞書の項目名や自治体のファイル名を符号化せずに
    書いたリンクは UnicodeEncodeError で止まる（和竿の漆塗りの試行で 36 本中 2 本）。
    safe に % を入れるので、符号化済みの URL は変わらない。国際化ドメイン名は扱わない。
    """
    p = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(p.path, safe="/%:@!$&'()*+,;=~")
    query = urllib.parse.quote(p.query, safe="=&%/:+?@!$'()*,;~")
    return urllib.parse.urlunsplit((p.scheme, p.netloc, path, query, p.fragment))


# ふだんの名乗り。ブラウザでないと本文を返さないサイトがあるので、ブラウザの形で始める。
BROWSER_UA = "Mozilla/5.0 imozuru"
# HAL（hal.science）は、ブラウザの名乗りには閲覧用の HTML を、それ以外の名乗りには PDF を返す。
# PDF を頼み直すときはこちらで名乗る（2026-10-20 に確かめた）。
PLAIN_UA = "imozuru"


def download(url: str, user_agent: str = BROWSER_UA) -> tuple[bytes, str]:
    """URL を取りに行き、本体と Content-Type を返す。

    MAX_BYTES を超える本体や、Content-Length に届かずに切れた本体は、取得ごと失敗にする。
    切れた原文を OK として残すと、後ろの部分を引用できないのに「原文に無い」と読み違える。
    read(n) は接続が途中で切れても例外を出さないので、残りの長さ（res.length）で確かめる。
    """
    problem = url_problem(url)
    if problem:
        raise UnsafeURL(problem)
    req = urllib.request.Request(ascii_url(url), headers={"User-Agent": user_agent})
    with _OPENER.open(req, timeout=90) as res:
        body = res.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError(f"本体が {MAX_BYTES:,} バイトを超える")
        if isinstance(res.length, int) and res.length > 0:
            raise ValueError("本体が途中で切れた")
        return body, res.headers.get("Content-Type", "")


def pdf_save(url: str, out: Path) -> bool:
    """名乗りを変えて頼み直し、PDF が返ってきたら out に保存して True を返す。

    前は ax に取り直させていた（ax の名乗りは Bun/…）。ax は自分で通信し、転送の先を
    こちらで確かめられないので、検査つきの download で頼み直す。
    """
    try:
        body, _ = download(url, user_agent=PLAIN_UA)
    except Exception:  # 取れなければ HTML のまま進める
        return False
    if body[:5] != b"%PDF-":
        return False
    out.write_bytes(body)
    return True


# Shift_JIS と名乗るページの多くは実際には Windows の拡張（①・髙 など）を含むので、上位互換の cp932 で読む
_CHARSET_ALIASES = {"shift_jis": "cp932", "shift-jis": "cp932", "sjis": "cp932", "x-sjis": "cp932",
                    "windows-31j": "cp932", "ms_kanji": "cp932"}
# 名乗りを信じてよい文字コード。文書に使われるものだけに絞る。名乗りはページを書いた他人が決めるので、
# punycode のように長さの2乗の時間がかかるもの（40MB で何時間も止まる）や、utf-7・unicode_escape の
# ように文書の文字コードではないものを試させない（2026-10-20 の2回目のレビュー）。
_DOCUMENT_CODECS = {codecs.lookup(n).name for n in (
    "utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be",
    "ascii", "cp932", "shift_jis", "euc_jp", "euc_jis_2004", "iso2022_jp", "iso2022_jp_2",
    "gb2312", "gbk", "gb18030", "big5", "big5hkscs", "euc_kr", "cp949", "iso2022_kr",
    "latin-1", "iso8859_2", "iso8859_3", "iso8859_4", "iso8859_5", "iso8859_6", "iso8859_7",
    "iso8859_8", "iso8859_9", "iso8859_10", "iso8859_13", "iso8859_14", "iso8859_15", "iso8859_16",
    "cp1250", "cp1251", "cp1252", "cp1253", "cp1254", "cp1255", "cp1256", "cp1257", "cp1258",
    "koi8_r", "koi8_u", "mac_roman", "tis_620", "cp874")}
# 先頭の BOM で分かる文字コード。UTF-32 の BOM は UTF-16 の BOM で始まるので先に見る
_BOMS = ((codecs.BOM_UTF8, "utf-8-sig"), (codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"),
         (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16"))


def _declared_charsets(body: bytes, ctype: str) -> list[str]:
    """Content-Type と HTML の meta が名乗る文字コードを、この順に返す。"""
    found = re.findall(r"charset\s*=\s*[\"']?([\w.:-]+)", ctype or "", re.I)
    # meta はファイルの頭に置かれる決まり（HTML 仕様で先頭 1024 バイト以内）。余裕を見て 4096 まで見る
    found += re.findall(rb"<meta[^>]*charset\s*=\s*[\"']?([\w.:-]+)", body[:4096], re.I)
    names = [(n.decode("ascii", "ignore") if isinstance(n, bytes) else n).lower() for n in found]
    return [_CHARSET_ALIASES.get(n, n) for n in names]


def decode_text(body: bytes, ctype: str = "") -> str:
    """本文を、書かれている文字コードで文字にする。

    いつも UTF-8 で読んでいたため、Shift_JIS の日本語記事が U+FFFD の羅列になって中身が消えた
    （pju-hoge さんのフォークで見つかった・2026-10-15）。名乗りは嘘のこともあるので、
    BOM → UTF-8 → 名乗った文字コード → 日本語の定番の順に「化けずに読めるか」で試し、最初に通ったものを使う。
    どれも通らなければ、置換文字のいちばん少ないものを採る。

    UTF-8 を名乗りより先に試すのは、latin-1 のようにどんな並びでも読めてしまう名乗りがあるため。
    UTF-8 の文法に合う並びは、ほかの文字コードの文章からはまず偶然には生まれない。
    名乗りはページを書いた他人が決めるので、_DOCUMENT_CODECS に無いものは飛ばす
    （zlib_codec と名乗るだけで fetch.py が LookupError で止まった・2026-10-20 のレビュー）。
    """
    for bom, codec in _BOMS:
        if body.startswith(bom):
            try:
                return body.decode(codec)
            except UnicodeError:
                break
    candidates = []
    for name in ["utf-8", *_declared_charsets(body, ctype), "cp932", "euc_jp"]:
        try:
            info = codecs.lookup(name)
        except LookupError:  # 知らない名乗り（x-user-defined など）は飛ばす
            continue
        if info.name not in _DOCUMENT_CODECS:
            continue
        if info.name not in candidates:
            candidates.append(info.name)
    for codec in candidates:
        try:
            return body.decode(codec)
        except UnicodeError:
            continue
    decoded = []
    for codec in candidates:
        try:
            decoded.append(body.decode(codec, errors="replace"))
        except UnicodeError:  # idna など、置き換えでも読めない文字コードがある
            continue
    return min(decoded, key=lambda t: t.count("�"))


def mostly_cjk(text: str) -> bool:
    """本文の空白以外の字のうち、和文の字の割合が CJK_RATIO_FOR_PDF を超えるか。"""
    chars = re.sub(r"\s", "", text)
    return bool(chars) and len(_CJK.findall(chars)) / len(chars) > CJK_RATIO_FOR_PDF


def missing(*commands: str) -> list[str]:
    """手元に無い外部コマンドの名前を返す。無いまま呼ぶと FileNotFoundError で止まるため。"""
    return [c for c in commands if shutil.which(c) is None]


def run_tool(cmd: list[str], capture_output: bool = False, **kwargs) -> subprocess.CompletedProcess | None:
    """外のコマンドを TOOL_TIMEOUT 秒の上限つきで動かす。上限を超えたら子の処理ごと止めて None を返す。

    subprocess.run の timeout は、直接動かしたコマンドしか止めない。Windows で ax や yt-dlp が
    .cmd を経由して動くと、孫の処理が出力の管をつかんだまま残り、run が戻らなくなる
    （2026-10-20 の2回目のレビュー）。新しい組で動かし、時間切れのときは組ごと止める。
    """
    if capture_output:
        kwargs["stdout"] = kwargs["stderr"] = subprocess.PIPE
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    with subprocess.Popen(cmd, **kwargs) as proc:
        try:
            out, err = proc.communicate(timeout=TOOL_TIMEOUT)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            return None
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def _kill_tree(proc: subprocess.Popen) -> None:
    """proc と、そこから動いた処理をまとめて止める。管は読まない（孫がつかんでいると戻らない）。"""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    proc.wait()


def pdf_to_text(pdf: Path) -> str:
    """pdftotext で段組みを保ったまま文字にする。行番号で原文を指せるように -layout を使う。

    Xpdf の pdftotext（Git for Windows に入っている）は既定で Latin-1 を出すので、
    -enc UTF-8 で出力の文字コードを決める。poppler の pdftotext も同じ引数を受け付ける（#3）。
    """
    if missing("pdftotext"):
        return ""
    out = pdf.with_suffix(".txt")
    done = run_tool(["pdftotext", "-layout", "-enc", "UTF-8", str(pdf), str(out)],
                    stderr=subprocess.DEVNULL)
    # 時間切れの書きかけ、途中で失敗した書きかけ、圧縮を解くと何 GB にもなる PDF の文字は使わない。
    # 書きかけを使うと、後半を引用できないのに「原文に無い」と読み違える
    if done is None or done.returncode != 0 or (out.exists() and out.stat().st_size > MAX_BYTES):
        out.unlink(missing_ok=True)
        return ""
    return out.read_text(encoding="utf-8", errors="replace") if out.exists() else ""


def ocr_langs() -> str:
    """tesseract に渡す言語。IMOZURU_OCR_LANG があればそれ、無ければ入っている言語データから選ぶ。

    jpn と jpn_vert が入っていれば eng に足す。英語の論文を3つで読んでも確信度は下がらなかった
    （2026-10-21 の計測）。一覧の読み取りに失敗したら eng だけにする＝前と同じ動き。
    一覧は行ごとに名前と完全に一致するかで見る（「jpn_vert」の行を「jpn」と取り違えない）。
    古い tesseract は一覧を標準エラーに出すので両方を読む。
    """
    chosen = os.environ.get("IMOZURU_OCR_LANG", "").strip()
    if chosen:
        if OCR_LANG_FORM.fullmatch(chosen):
            return chosen
        print(f"IMOZURU_OCR_LANG の形が違うので使わない: {clean(chosen)}", file=sys.stderr)
    res = run_tool(["tesseract", "--list-langs"], capture_output=True, encoding="utf-8", errors="replace")
    have = set() if res is None else {line.strip() for line in f"{res.stdout}\n{res.stderr}".splitlines()}
    return "+".join([lang for lang in OCR_EXTRA_LANGS if lang in have] + ["eng"])


def ocr_pdf(pdf: Path) -> tuple[str, str]:
    """文字の層が無い PDF（紙を読み取った画像だけの古い論文）を、手元の tesseract で文字にする。

    Spencer ら 1995 の PDF は取れたのに pdftotext が1文字しか返さなかった（2026-10-10）。
    1ページずつ 200dpi の画像にして、ocr_langs() の言語で読む。OCR_MAX_PAGES を超えるページは起こさない。
    返すのは (本文, 使わなかった理由)。次のときは本文を空にして理由を返す。
    - 時間切れか失敗で読めなかったページがある。欠けたまま残すと、後半が「原文に無い」と読み違える
    - 確信度の平均が OCR_MIN_CONFIDENCE より低い。言語データが合わず意味のない字の列になっている（#11）
    pdftoppm か tesseract が無ければ何もせず空を返す（呼び手が FAIL に無いコマンドを書く）。
    .txt には書かない。書くかどうかは呼び手（ocr_if_longer）が決める。
    """
    if missing("pdftoppm", "tesseract"):
        return "", ""
    tmp = pdf.parent / f".{pdf.stem}-ocr"
    tmp.mkdir(exist_ok=True)
    try:
        done = run_tool(["pdftoppm", "-r", "200", "-l", str(OCR_MAX_PAGES), "-png", str(pdf), str(tmp / "p")],
                        stderr=subprocess.DEVNULL)
        if done is None:
            return "", "PDF を画像にする途中で時間切れ"
        langs = ocr_langs()
        pages, failed, weighted, chars = [], [], 0.0, 0
        for img in sorted(tmp.glob("p-*.png")):
            number = img.stem.split("-")[-1]
            out = tmp / img.stem
            # 字の間の空白を残す設定が無いと、和文の字のあいだに空白が入る（#11）
            res = run_tool(["tesseract", str(img), str(out), "-l", langs,
                            "-c", "preserve_interword_spaces=1",
                            "-c", "tessedit_create_txt=1", "-c", "tessedit_create_tsv=1"],
                           capture_output=True, encoding="utf-8", errors="replace")
            txt, tsv = out.with_suffix(".txt"), out.with_suffix(".tsv")
            if res is None or res.returncode != 0 or not txt.exists():
                failed.append(number)
                continue
            pages.append(f"=== page {number} ===\n{txt.read_text(encoding='utf-8', errors='replace')}")
            # tsv の列: … conf（11列目）・text（12列目）。単語でない行の conf は -1
            for row in tsv.read_text(encoding="utf-8", errors="replace").splitlines()[1:] if tsv.exists() else []:
                cols = row.split("\t")
                if len(cols) >= 12 and cols[11].strip():
                    try:
                        conf = float(cols[10])
                    except ValueError:
                        continue
                    if conf >= 0:
                        weighted += conf * len(cols[11].strip())
                        chars += len(cols[11].strip())
    finally:
        for leftover in tmp.iterdir():  # 読み終えた画像と、時間切れで止めた pdftoppm の書きかけ
            leftover.unlink()
        tmp.rmdir()
    if failed:
        return "", f"OCR で読めなかったページがある（{','.join(failed)}）"
    if chars and weighted / chars < OCR_MIN_CONFIDENCE:
        return "", (f"OCR の確信度が低い（平均 {weighted / chars:.0f}・言語 {langs}）。"
                    "言語データが合っていないかも（日本語なら tesseract の jpn と jpn_vert）")
    return "\n".join(pages), ""


def ocr_if_longer(pdf: Path, text: str) -> tuple[str, str]:
    """文字の層が薄い PDF を OCR にかけ、OCR のほうが長いときだけ .txt を置き換えて返す。

    OCR の道具が無いときや起こせなかったときに、pdftotext が取れた分まで空で上書きしていた
    （2026-10-20 のレビュー）。OCR の誤りは残るので、.txt の先頭に OCR だと書いて、
    読む人が原本と照らせるようにする。返すのは (本文, OCR を使わなかった理由)。
    """
    ocr, note = ocr_pdf(pdf)
    if len(ocr.strip()) <= len(text.strip()):
        return text, note
    pdf.with_suffix(".txt").write_text(
        f"（OCR で起こした本文。{OCR_MAX_PAGES} ページを超える分は起こしていない。"
        "誤りが残るので、引用は原本の PDF で確かめる）\n\n" + ocr, encoding="utf-8")
    return ocr, ""


HAS_AX = shutil.which("ax") is not None


class _TextExtractor(HTMLParser):
    """ax が無い環境で、HTML から本文の文字を取り出す。

    ax ほど本文を見分けられない。<main> か <article> があって中身が十分なら、その中だけを使う。
    無いページではナビゲーションやフッターの文字も混ざるが、行番号で原文を指すには足りる。
    リンクは「後ろへ」たどる手がかりなので `[文字](URL)` で残す。
    """

    FOCUS = {"main", "article"}

    SKIP = {"script", "style", "noscript", "svg", "template", "iframe"}
    BLOCK = {"p", "div", "section", "article", "main", "header", "footer", "nav", "aside",
             "li", "tr", "br", "hr", "pre", "blockquote", "table", "ul", "ol", "dl", "dt", "dd",
             "figure", "figcaption"}
    HEADING = {"h1": "# ", "h2": "## ", "h3": "### ", "h4": "#### ", "h5": "##### ", "h6": "###### "}

    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base
        self.parts: list[str] = []
        self.skip = 0
        self.href: str | None = None
        self.link_text: list[str] = []
        self.focus = 0
        self.focus_parts: list[str] = []

    def _emit(self, piece: str) -> None:
        self.parts.append(piece)
        if self.focus:
            self.focus_parts.append(piece)

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif self.skip:
            return
        elif tag in self.FOCUS:
            self.focus += 1
        elif tag in self.HEADING:
            self._emit("\n\n" + self.HEADING[tag])
        elif tag in self.BLOCK:
            self._emit("\n")
        elif tag == "a":
            href = (dict(attrs).get("href") or "").strip()
            if href and not href.startswith("#"):
                full = urllib.parse.urljoin(self.base, href)
                # JavaScript: や data: のリンクは、.md を開いた先で押すとページの中で動く。http(s) だけ残す
                if urllib.parse.urlsplit(full).scheme.lower() in {"http", "https"}:
                    # 括弧と空白が URL の中にあると、[文字](URL) の形がそこで切れる
                    self.href = re.sub(r"[()\s]", lambda m: f"%{ord(m.group(0)):02X}", full)
                    self.link_text = []

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif self.skip:
            return
        elif tag in self.FOCUS:
            self.focus = max(0, self.focus - 1)
        elif tag in self.HEADING or tag in self.BLOCK:
            self._emit("\n")
        elif tag == "a" and self.href is not None:
            text = " ".join("".join(self.link_text).split())
            # 「\」を先に符号化する。後にすると、元からある「\]」が「\\]」になって「]」が効き直す
            text = text.replace("\\", "\\\\").replace("[", r"\[").replace("]", r"\]")
            self._emit(f"[{text}]({self.href})" if text else "")
            self.href = None

    def handle_data(self, data):
        if self.skip:
            return
        if self.href is not None:
            self.link_text.append(data)
        else:
            self._emit(data)

    def text(self) -> str:
        focused = "".join(self.focus_parts)
        # 中身の薄い <main>（ページの殻だけ）を掴んだときは全体に戻す
        raw = focused if len(focused.strip()) >= MIN_HTML_CHARS else "".join(self.parts)
        lines = (" ".join(line.split()) for line in raw.splitlines())
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() + "\n"


def html_to_md(url: str, out: Path, body: bytes | None = None, ctype: str = "") -> str:
    """本文を markdown にする。要約はしない。

    取得はいつも download（宛先の検査つき）で行う。ax には取ってきた HTML をファイルで渡し、
    markdown への変換（--all で全文）だけをさせる。ax に URL を渡すと ax が自分で通信し、
    たどった転送の先をこちらで確かめられない（2026-10-20 の2回目のレビュー）。
    ax はファイルを UTF-8 として読むので、こちらで文字にしてから UTF-8 で書いて渡す。
    ax が無ければ標準ライブラリで文字を取り出す。
    body を渡されたら取り直さずにそれを使う。ctype は body の Content-Type（文字コードの名乗りを読む）。
    """
    if body is None:
        body, ctype = download(url)
    if body[:5] == b"%PDF-":
        # 転送先が PDF だった。呼び手が「%PDF」で気づいて PDF として取り直す
        text = "%PDF"
    elif HAS_AX:
        page = out.with_name(f".{out.stem}-page.html")
        page.write_text('<meta charset="utf-8">' + decode_text(body, ctype), encoding="utf-8")
        # 置き場は作業用の一時フォルダで「-」では始まらないが、念のため「--」の後ろに置く
        res = run_tool(["ax", "--md", "--all", "--", str(page)], capture_output=True,
                       encoding="utf-8", errors="replace")
        page.unlink(missing_ok=True)
        text = res.stdout if res else ""
    else:
        parser = _TextExtractor(url)
        parser.feed(decode_text(body, ctype))
        text = parser.text()
    out.write_text(text, encoding="utf-8")
    return text


YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
                 "youtube-nocookie.com", "www.youtube-nocookie.com"}
ARXIV_HOSTS = {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}
X_POST = re.compile(r"^https?://(www\.)?(x|twitter)\.com/")
# 字幕は数分の動画でも千文字に届かないことがあるので、論文より低い下限を使う。
MIN_TRANSCRIPT_CHARS = 300


def is_youtube(url: str) -> bool:
    """YouTube の動画の URL か。ホスト名で見る。

    URL のどこかに「youtu.be/」があれば YouTube と見なしていたので、
    https://evil.example/?next=youtu.be/ のような他人のページまで yt-dlp に渡っていた
    （2026-10-20 のレビュー）。yt-dlp は何でも読む汎用の読み取りを持っているので、YouTube だけに絞る。
    """
    p = urllib.parse.urlsplit(url)
    host = (p.hostname or "").lower().rstrip(".")
    if host == "youtu.be":
        return len(p.path) > 1
    return host in YOUTUBE_HOSTS and re.match(r"/(watch|shorts/|live/|embed/)", p.path) is not None


def arxiv_id(url: str) -> str | None:
    """arXiv の論文の URL なら番号を返す。ホスト名で見る。

    URL のどこかに「arxiv.org/abs/…」があれば当てていたので、他人のページの URL に
    書き込まれた番号の論文を、そのページの原文として保存しえた（2026-10-20 の2回目のレビュー）。
    """
    p = urllib.parse.urlsplit(url)
    if (p.hostname or "").lower().rstrip(".") not in ARXIV_HOSTS:
        return None
    m = re.match(r"/(?:pdf|abs)/(\d{4}\.\d{4,5}(?:v\d+)?)", p.path)
    return m.group(1) if m else None


def youtube_to_md(url: str, out: Path) -> str:
    """yt-dlp で字幕だけ落とし（動画は落とさない）、時刻つきの本文にする。

    手動の字幕を優先し（日本語→英語の順）、無ければ動画の元の言語の自動字幕。自動字幕は同じ文が
    次の区間にも繰り返し出るので、直前と同じ文は捨てる。

    自動字幕は誤変換が多い（大陸塗りの試行で「線画」が「洗画」）。どちらを使ったかを
    見出しに残し、読み手が引用を動画で確かめるべきか分かるようにする。手動と自動は
    同じファイル名で出てくるので、まず手動だけで取り、無いときだけ自動で取り直す。
    題名の出力は --encoding で UTF-8 に決める。Windows でパイプに出すと、決めない限り cp932 になる。

    自動字幕は、動画の元の言語のもの（en-orig など）だけを取る。ja,en を指定すると、
    英語の動画では ja が機械翻訳の字幕になり、YouTube はそれに 429 Too Many Requests を返す。
    yt-dlp はそこで止まり、英語の字幕も残さない（テスラの自動運転の試行で YouTube が3本とも
    落ちた・2026-10-19）。ja-orig,en-orig の指定でも足りない。AI の吹き替えが付いた動画では
    吹き替えの音声にも ja-orig が付き、中身は翻訳になる（CVPR の講演 jPCV4GKX9Dw）。そこで手動の回で
    動画の言語（元の音声の言語）も出させ、自動の回ではその言語の -orig だけを指定する。
    """
    tmp = out.parent / f".{out.stem}-subs"
    tmp.mkdir(exist_ok=True)
    kind, langs = "手動", "ja,en"
    printed = ""
    for flag in ("--write-subs", "--write-auto-subs"):
        meta = run_tool(
            # --no-playlist: watch?v=…&list=… の URL で、再生リストの全部の字幕を取りに行かない
            ["yt-dlp", "--no-update", "--no-playlist", "--skip-download", flag, "--encoding", "utf-8",
             "--sub-langs", langs, "--sub-format", "vtt", "--no-simulate",
             "--print", "%(title)s\t%(uploader)s\t%(upload_date)s\t%(language|)s",
             "-o", str(tmp / "v.%(ext)s"), "--", url],
            capture_output=True, encoding="utf-8", errors="replace")
        if meta is None:  # 時間切れ。字幕なしとして呼び手が FAIL にする
            break
        printed = meta.stdout
        if any(tmp.glob("*.vtt")):
            break
        kind = "自動（誤変換あり・引用は動画で確かめる）"
        # 言語は en-US のように地域つきで来るが、字幕は en-orig。分からなければ日本語か英語の元の字幕
        spoken = printed.rstrip("\n").split("\t")[-1].split("-")[0]
        # 言語の名乗りは動画の持ち主が決める。「all」などで字幕を全部落とさせないよう、言語の形だけ通す
        langs = f"{spoken}-orig" if re.fullmatch(r"[a-z]{2,3}", spoken) else "ja-orig,en-orig"
    vtts = sorted((p for p in tmp.glob("*.vtt") if p.stat().st_size <= MAX_BYTES),
                  key=lambda p: (".ja" not in p.name, p.name))
    lines, last = [], ""
    if vtts:
        stamp = "00:00"
        for raw in vtts[0].read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"(\d\d):(\d\d):(\d\d)\.\d+ -->", raw)
            if m:
                h, mi, s = (int(x) for x in m.groups())
                stamp = f"{h * 60 + mi:02d}:{s:02d}"
                continue
            # [^<>] にするのは、「<」が並んで「>」の無い行で、1つずつ行末まで読み直して遅くなるのを防ぐため
            text = re.sub(r"<[^<>]*>", "", raw).strip()
            if not text or text in ("WEBVTT",) or text.startswith(("Kind:", "Language:")):
                continue
            if text != last:
                lines.append(f"[{stamp}] {text}")
                last = text
    for p in tmp.iterdir():
        p.unlink()
    tmp.rmdir()
    head = printed.strip().split("\t")
    title = head[0] if head and head[0] else url
    info = " / ".join(head[1:3]) if len(head) > 1 else ""
    body = "\n".join(lines)
    out.write_text(f"# {title}\n\n{url}\n{info}\n字幕: {kind + ' ' + vtts[0].name if vtts else 'なし'}\n\n{body}\n",
                   encoding="utf-8")
    return body


def clean(s: str) -> str:
    """端末に出す文字列から、制御文字（色やクリップボードを操る ESC の並び）と改行の仲間を空白にする。

    他人が決めた文字列（本文・転送先の URL・HTTP のエラーの文）は、1行の出力の列や行を増やしたり、
    端末を操ったりできる（2026-10-20 の2回目のレビュー）。
    文字の向きを変える制御文字（U+202E など）と幅の無い空白も消す。ZWJ・ZWNJ は絵文字や一部の文字で要るので残す。
    """
    return re.sub(r"[\s\x00-\x1f\x7f-\x9f  \u200b\u200e\u200f\u202a-\u202e\u2060-\u2069\ufeff]+", " ", s).strip()


def first_line(text: str) -> str:
    """本文の最初の意味のある1行。端末に出すので clean() を通す。"""
    for line in text.splitlines():
        line = clean(line)
        if len(line) > 8:
            return line[:100]
    return ""


def save_verified(saved: Path, kept: Path, destination: Path) -> Path:
    """検査を通ったファイルだけを、同じファイルシステム内で保存先に移す。"""
    if saved != kept:
        saved.replace(destination / saved.name)
    target = destination / kept.name
    kept.replace(target)
    return target


def valid_source_name(name: str) -> bool:
    """保存する原文の名前は、パスではなく単一のファイル名に限る。

    Windows で使えない字（<>"|?*）は、そこで保存が OSError で止まるので先に断る。
    字の向きを変える制御文字（U+202E など）は、名前の見た目を偽れるので断る。
    """
    device = name.split(".", 1)[0].rstrip(" ").upper()
    return (bool(name.strip(" .")) and name == name.rstrip(" .")
            and not any(c in name for c in "/\\:<>\"|?*")
            and not any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in name)
            and not re.search(r"[‎‏‪-‮⁦-⁩]", name)
            and device not in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
            and not re.fullmatch(r"(?:COM|LPT)[1-9¹²³]", device))


def fetch_source(sources: Path, name: str, url: str, destination: Path) -> int:
    """sources は今回の取得専用の一時置き場。取得・変換・検査中は保存済みの原文に触れない。"""
    if not valid_source_name(name):
        print(f"FAIL\t{name!r}\t原文の名前は単一のファイル名にする")
        return 1
    # file:// や手元の宛先を渡すと、urllib が手元のファイルやサービスの中身を sources/ に写してしまう。
    # ax と yt-dlp に渡す前にもここで止める（両者がたどる転送の先までは見られない）
    problem = url_problem(url)
    if problem:
        print(f"FAIL\t{name}\t{clean(problem)}")
        return 1

    if X_POST.match(url):
        print(f"FAIL\t{name}\tX はログインの壁で本文が取れない（URL を map.md に残す）")
        return 1
    if is_youtube(url):
        if missing("yt-dlp"):
            print(f"FAIL\t{name}\tyt-dlp が無いので字幕を落とせない")
            return 1
        kept = sources / f"{name}.md"
        body = youtube_to_md(url, kept)
        if len(body) < MIN_TRANSCRIPT_CHARS:
            kept.unlink(missing_ok=True)
            print(f"FAIL\t{name}\t字幕が無いか短すぎる（{len(body)}文字）")
            return 1
        kept = save_verified(kept, kept, destination)
        print(f"OK\t{name}\t{kept}\t{len(body)}\t{first_line(body)}")
        return 0

    try:
        body, ctype = download(url)
    except Exception as e:  # HTTP エラーも接続失敗も、呼び手にとっては同じ「取れない」
        print(f"FAIL\t{name}\t{type(e).__name__}: {clean(str(e))}")
        return 1
    # メディアタイプは大文字と小文字を区別しない。「Text/Plain」で返すサーバーもある
    ctype = ctype.lower()
    ocr_note = ""  # OCR の結果を使わなかった理由。FAIL の理由に添える

    if not body.strip():
        # 中身の無い 200/202 を返すサーバーがある（Cornell の論文置き場・2026-10-10）。
        # ブラウザでないと中身を渡さないボット対策のことが多い＝ユーザーのブラウザで開いてもらう候補
        print(f"FAIL\t{name}\t空の応答（ボット対策の可能性・ブラウザで開けば読めるかも）")
        return 1
    if body[:5] == b"%PDF-":
        saved = sources / f"{name}.pdf"
        saved.write_bytes(body)
        text = pdf_to_text(saved)
        kept = saved.with_suffix(".txt")
        if len(text.strip()) < 200 and not arxiv_id(url):
            text, ocr_note = ocr_if_longer(saved, text)  # 文字の層が無い PDF。arXiv は下で HTML 版に切り替える
    elif ctype.startswith(("text/plain", "text/markdown", "application/json", "text/csv")):
        # GitHub の README やデータ（raw.githubusercontent.com）はこの形で来る
        # （和田三造の配色のデータで FAIL した・2026-10-10）。中身をそのまま残す。
        # HTML の判定より先に見る。先頭が `<!-- AUTO GENERATED -->` の markdown を
        # HTML と見なして ax に回し、改行が全部消えた（chrome-devtools-mcp の tool-reference・2026-10-11）。
        ext = ".json" if "json" in ctype else ".csv" if "csv" in ctype else ".md"
        kept = saved = sources / f"{name}{ext}"
        text = decode_text(body, ctype)
        kept.write_text(text, encoding="utf-8")
    elif "html" in ctype or body.lstrip()[:1] == b"<":
        kept = sources / f"{name}.md"
        text = html_to_md(url, kept, body, ctype)
        saved = kept
        # HAL は、ブラウザの名乗りには閲覧用の HTML を、それ以外の名乗りには PDF 本体を返す。
        # 前は ax の取り直しで PDF を受け取っていた。いまは名乗りを変えて自分で頼み直す。
        # ax を使っていたころと同じく、HTML のページには2回頼むことになる
        pdf = sources / f"{name}.pdf"
        if pdf_save(url, pdf) and pdf.read_bytes()[:5] == b"%PDF-":
            pdf_text = pdf_to_text(pdf)
            if len(pdf_text.strip()) < 200:
                pdf_text, _ = ocr_if_longer(pdf, pdf_text)  # 使えなければ HTML の本文が残る
            pdf_floor = MIN_HTML_CHARS if mostly_cjk(pdf_text) else MIN_TEXT_CHARS
            # PDF の本文が下限に届いたときだけ PDF に切り替える。届かないなら HTML の本文を残す。
            # 先に HTML を消すと、PDF が文字の層の無い画像だったときに両方を失う（2026-10-20 のレビュー）
            if len(pdf_text) >= pdf_floor or text.lstrip().startswith("%PDF"):
                kept.unlink(missing_ok=True)
                saved, text, kept = pdf, pdf_text, pdf.with_suffix(".txt")
            else:
                pdf.unlink(missing_ok=True)
                pdf.with_suffix(".txt").unlink(missing_ok=True)
        else:
            pdf.unlink(missing_ok=True)
            if text.lstrip().startswith("%PDF"):
                kept.unlink(missing_ok=True)
                print(f"FAIL\t{name}\t転送先の PDF を保存できなかった")
                return 1
    else:
        print(f"FAIL\t{name}\t扱えない形式: {ctype or '不明'}")
        return 1

    # arXiv の PDF は pdftotext で本文が0文字になることがある（2026-10-09 の試行で
    # COLE・Accordion）。同じ論文の HTML 版なら取れたので、短いときだけ切り替える。
    paper = arxiv_id(url)
    if paper and len(text) < MIN_TEXT_CHARS:
        # HTML 版は別の名前に取ってから比べる。先に元を消すと、HTML 版が取れなかったときに何も残らない。
        # 元が abs ページの .md だと同じ名前になるので、一時の名前を使う
        trial = sources / f".{name}-arxiv.md"
        try:
            html_text = html_to_md(f"https://arxiv.org/html/{paper}", trial)
        except Exception:  # HTML 版が無い論文もある。そのときは元のまま下限で判定する
            html_text = ""
        if len(html_text) > len(text):
            for p in (saved, kept):
                p.unlink(missing_ok=True)
            kept = saved = sources / f"{name}.md"
            trial.replace(kept)
            text = html_text
        else:
            trial.unlink(missing_ok=True)

    # PDF は英語の論文を基準に高い下限。和文の PDF だけは HTML と同じ下限にする
    floor = MIN_TEXT_CHARS if saved.suffix == ".pdf" and not mostly_cjk(text) else MIN_HTML_CHARS
    head = text[:4000].lower()
    wall = next((p for p in WALL_PATTERNS if p in head), None)
    if len(text) < floor or (wall and len(text) < MIN_TEXT_CHARS * 4):
        for p in (saved, kept):
            p.unlink(missing_ok=True)
        reason = f"壁に当たった（{wall}）" if wall else f"本文が短すぎる（{len(text)}文字）"
        # PDF の文字起こしに要るコマンドが欠けていたなら、短い理由はたいていそれ
        lacking = missing("pdftotext", "pdftoppm", "tesseract") if saved.suffix == ".pdf" else []
        if lacking:
            reason += f"。{'・'.join(lacking)} が無い"
        elif ocr_note and saved.suffix == ".pdf":
            reason += f"。{ocr_note}"
        print(f"FAIL\t{name}\t{reason}")
        return 1

    kept = save_verified(saved, kept, destination)
    print(f"OK\t{name}\t{kept}\t{len(text)}\t{first_line(text)}")
    return 0


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__, file=sys.stderr)
        return 2
    sources, name, url = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    if not valid_source_name(name):
        print(f"FAIL\t{name!r}\t原文の名前は単一のファイル名にする")
        return 1
    sources.mkdir(parents=True, exist_ok=True)
    # 変換や FAIL の後始末は一時置き場だけで行う。同名の原文を検査前に上書きしない。
    # 保存先の中に作るので、成功時の replace はファイルシステムをまたがない。
    with tempfile.TemporaryDirectory(prefix=".imozuru-fetch-", dir=sources) as tmp:
        return fetch_source(Path(tmp), name, url, sources)


if __name__ == "__main__":
    # 本文の先頭の一行を出すので、端末の文字コードに無い字（cp932 の à など）で落ちないようにする
    sys.stdout.reconfigure(errors="replace")
    sys.exit(main())
