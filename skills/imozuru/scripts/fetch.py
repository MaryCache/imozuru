#!/usr/bin/env python3
"""原文を1本落とし、本物かどうか確かめてから sources/ に残す。

検索結果の URL は古いことが多く、2026-10-09 の試行では 11 本中 5 本が
404・ログインの壁・PDF のふりをした HTML だった。拡張子や HTTP の成否では
見分けられないので、中身の先頭と文字数で判定する。

論文だけでなく個人の発信も落とす。YouTube は字幕を `[分:秒] 文` の形で
.md にする（場所を時刻で指せるように）。X はログインの壁で本文が取れないので、
取りに行かずに FAIL を返す。PMC の記事ページは captcha で取れないので、
同じ本文を Europe PMC の全文 XML から取り、見出し・段落ごとの行の .md にする。

使い方:
    fetch.py <sources_dir> <name> <url> [--no-fallback]
出力（標準出力に1行・タブ区切り）:
    OK   <name> <保存したファイル> <文字数> <先頭の一行>
    FAIL <name> <理由>
"""

import codecs
import contextlib
import email.utils
import functools
import gzip
import http.client
import io
import ipaddress
import json
import os
import random
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from xml.parsers import expat

# 本文として読める最低の文字数。404 ページや認証の壁は数百〜数千文字に収まる。
# PDF は英語の論文が多いので高め。HTML は日本語の短い記事を通すため低め
# （大陸塗りの試行で 2,904 字の本物の記事が 3,000 の下限で落ちた）。
MIN_TEXT_CHARS = 3000
MIN_HTML_CHARS = 1000
# 和文（日本語・中国語）の PDF は、英語より少ない字数で中身がある。
# 本文の空白以外の字のうち和文の字がこの割合を超える PDF は、HTML と同じ下限で判定する。
CJK_RATIO_FOR_PDF = 0.3
# 和文の字＝かな・漢字・全角の英数字と記号。韓国語のハングル（音節・字母・互換字母）も数える。
# 韓国語は日本語・中国語と同じく、英語より少ない字数で同じ中身を表せるため
_CJK = re.compile(r"[\u3000-\u30ff\u3400-\u9fff\uf900-\ufaff\uff00-\uffef\uac00-\ud7af\u1100-\u11ff\u3130-\u318f]")
MAX_BYTES = 40_000_000
# 保存する原文の改行。Windows の write_text は \n を \r\n に変えるので、同じ原文の写しが
# OS によってバイトで食い違っていた（2026-10-26 に Windows で試験を回して分かった）。OS によらず LF にそろえる
SOURCE_NEWLINE = "\n"
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
    # 中国語（簡体・繁体）・韓国語・独・仏・西・葡・露・アラビア語の、ログイン壁・404・アクセス拒否。
    # 通常の本文に紛れにくい、ページ全体の状態を告げる言い回しだけを選ぶ（小文字で照合するため、小文字で記入する）
    "您访问的页面不存在", "您訪問的頁面不存在", "请登录后查看", "請登入後查看", "请先登录", "請先登入",
    "访问被拒绝", "存取被拒絕",
    "페이지를 찾을 수 없습니다", "로그인이 필요합니다", "접근이 거부되었습니다",
    "seite nicht gefunden", "zugriff verweigert", "bitte melden sie sich an, um fortzufahren",
    "page introuvable", "accès refusé", "veuillez vous connecter pour continuer",
    "página no encontrada", "acceso denegado", "inicia sesión para continuar",
    "página não encontrada", "acesso negado",
    "страница не найдена", "доступ запрещён", "доступ запрещен", "войдите, чтобы продолжить",
    "الصفحة غير موجودة", "تم رفض الوصول",
)
# Cloudflare などの確認画面に特有の表示。通常の文章に紛れる「just a moment」単体は含めない。
CHALLENGE_PATTERNS = (
    "just a moment...",
    "checking your browser",
    "verify you are human",
    "cf-chl",
    "enable javascript and cookies to continue",
    "attention required! | cloudflare",
    "しばらくお待ちください",
    # Anubis のボット確認ページ（OpenEdition・Heidelberg など）。本文の語としては現れにくい、
    # 画面の見出しとフッターの定型文だけを選ぶ。単体の「anubis」は神の名や記事の題材として本文に出るため使わない。
    # 見出しは「'」と「’」の両方で出るため、どちらでも当たるようにする
    "making sure you're not a bot", "making sure you’re not a bot",
    "protected by anubis",
    "has set up anubis to protect",
    # Cloudflare などの確認画面の各国語版
    "确认您是真人", "確認您是真人", "请完成安全验证", "請完成安全驗證",
    "bestätigen sie, dass sie ein mensch sind",
    "vérifiez que vous êtes humain", "vérification de sécurité en cours",
    "verifica que eres humano", "verificando que eres humano",
    "подтвердите, что вы человек", "подтвердите, что вы не робот",
)
# 一時障害やレート制限だけ再試行する。400/401/403/404 のような恒久エラーは繰り返さない。
RETRYABLE_HTTP_STATUS = frozenset({429, 503, 504})
MAX_TRANSIENT_RETRIES = 2
RETRY_BASE_SECONDS = 1.0
RETRY_MAX_WAIT_SECONDS = 15.0
DOWNLOAD_DEADLINE_SECONDS = 120.0
OA_API_TIMEOUT_SECONDS = 25.0
MAX_OA_CANDIDATES = 4
EUROPE_PMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
OPENALEX_WORK = "https://api.openalex.org/works/https://doi.org/{}"
# OpenAlex の推奨に合わせて連絡先を添えた User-Agent を名乗る。
OA_API_UA = "imozuru/1.0 (mailto:imozuru@users.noreply.github.com)"
_sleep = time.sleep


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


# 宛先が残っているときに1つの宛先を待つ上限（秒）。IPv6 の経路が無い PC では、IPv6 の宛先ごとに
# 呼び出し側の timeout（fetch は90秒、watch は60秒）まで待ってから IPv4 へ進んでいた（4つの IPv6 で最大6分）
CONNECT_TRY_SECONDS = 10


def _connect_order(addrs: list[str]) -> list[str]:
    """OS が先に返した種類から、IPv6 と IPv4 を交互に並べる（RFC 8305 の4節）。

    片方の種類に経路が無くても、もう片方を2番目に試せる。
    """
    v6 = [a for a in addrs if ":" in a]
    v4 = [a for a in addrs if ":" not in a]
    first, second = (v6, v4) if addrs and ":" in addrs[0] else (v4, v6)
    order = []
    for i in range(max(len(first), len(second))):
        order += first[i:i + 1] + second[i:i + 1]
    return order


def _real_timeout(timeout):
    """既定の timeout の印を、いま使われる秒数（None は待ち続ける）に置き換える。"""
    return socket.getdefaulttimeout() if timeout is socket._GLOBAL_DEFAULT_TIMEOUT else timeout


def _dial(ip: str, port: int, timeout, source_address):
    """確かめた IP へつなぐ。テストはここを差し替える。"""
    return socket.create_connection((ip, port), timeout, source_address)


def _pinned_connection(host: str, port: int, timeout, source_address):
    """つなぐ直前に名前を引き直し、確かめた IP へそのままつなぐ。

    url_problem で名前を引いてから urllib がつなぐまでの間に、引き先を 127.0.0.1 などへ
    変えられると検査をすり抜ける（DNS rebinding）。つなぐ時に引いた結果をもう一度確かめ、
    その IP にじかにつなぐので、検査した宛先と実際の宛先がずれない。HTTPS の証明書の確認と
    SNI はホスト名のまま行われる。
    プロキシを通すときは名前をプロキシが引くので、この固定では DNS rebinding を防げない。
    """
    try:
        addrs = list(resolve(host))
    except (OSError, UnicodeError, ValueError) as e:
        raise UnsafeURL(f"ホストを引けない（{e}）")
    if os.environ.get("IMOZURU_ALLOW_PRIVATE") != "1":
        for addr in addrs:
            for ip in _addresses_inside(ipaddress.ip_address(addr.split("%", 1)[0])):
                if _is_blocked_ip(ip):
                    raise UnsafeURL(f"つなぐ直前に引いた先が手元や内側の宛先（{ip}）だった")
    err = None
    full = _real_timeout(timeout)
    order = _connect_order(addrs)
    cut = []
    for i, addr in enumerate(order):
        short = i < len(order) - 1 and (full is None or full > CONNECT_TRY_SECONDS)
        try:
            sock = _dial(addr, port, CONNECT_TRY_SECONDS if short else timeout, source_address)
        except TimeoutError as e:
            err = e
            if short:
                cut.append(addr)
            continue
        except OSError as e:
            err = e
            continue
        if short:
            # create_connection は待ち時間をそのままソケットに残す。つないだあとの読み込みは元の timeout で待つ
            sock.settimeout(full)
        return sock
    # 短く待って切った宛先は、ほかが全部だめだったときだけ元の timeout で待ち直す。10秒より遅くつながる宛先を失わない
    for addr in cut:
        try:
            return _dial(addr, port, timeout, source_address)
        except OSError as e:
            err = e
    raise err or OSError(f"{host} の宛先が無い")


class _PinnedMixin:
    """接続を _pinned_connection で作る。プロキシを通すとき（CONNECT）はプロキシへ素直につなぐ。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = self._pinned_create

    def _pinned_create(self, address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
        if self._tunnel_host:
            return socket.create_connection(address, timeout, source_address)
        return _pinned_connection(address[0], address[1], timeout, source_address)


class _PinnedHTTPConnection(_PinnedMixin, http.client.HTTPConnection):
    pass


class _PinnedHTTPSConnection(_PinnedMixin, http.client.HTTPSConnection):
    pass


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        if req.has_proxy():
            return self.do_open(http.client.HTTPConnection, req)
        return self.do_open(_PinnedHTTPConnection, req)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        kwargs = {"context": self._context}
        if hasattr(self, "_check_hostname"):  # 3.11 まで
            kwargs["check_hostname"] = self._check_hostname
        return self.do_open(_PinnedHTTPSConnection, req, **kwargs)


_OPENER = urllib.request.build_opener(_Redirect308, _PinnedHTTPHandler, _PinnedHTTPSHandler)


class UnsafeURL(Exception):
    """取りに行ってはいけない URL。呼び手はほかの取得の失敗と同じく FAIL にする。"""


def resolve(host: str) -> list[str]:
    """ホスト名を引いて IP アドレスを返す。テストはここを差し替える。

    OS が返した順（IPv6 と IPv4 のどちらを先に試すか）を保つ。_pinned_connection はこの順の先頭の種類から、
    IPv6 と IPv4 を交互につなぐ。
    """
    return list(dict.fromkeys(info[4][0] for info in socket.getaddrinfo(host, None)))


# fake-IP の DNS（Clash・Surge などの fake-ip プールや一部の egress プロキシ）が全ホスト名を
# 198.18.0.0/15（RFC 2544 のベンチマーク用範囲）に解決する環境では、is_global が全部 False に
# なり全 URL が止まってしまう。IMOZURU_ALLOW_FAKE_IP=1 のときだけこの範囲を外の宛先として扱い、
# 127.0.0.1 や LAN などそれ以外の内側チェックは残す（全部外す IMOZURU_ALLOW_PRIVATE とは別）。
FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")


def _is_blocked_ip(ip) -> bool:
    """取りに行ってはいけない宛先なら True。fake-IP の opt-in があれば 198.18.0.0/15 だけ通す。"""
    if ip.is_global:
        return False
    if (os.environ.get("IMOZURU_ALLOW_FAKE_IP") == "1"
            and ip.version == 4 and ip in FAKE_IP_NETWORK):
        return False
    return True


def _blocked_hint(ip) -> str:
    if ip.version == 4 and ip in FAKE_IP_NETWORK:
        return "（fake-IP の DNS 環境なら IMOZURU_ALLOW_FAKE_IP=1 で 198.18.0.0/15 だけ外せる）"
    return "（VPN などで外の名前も内側に引ける環境なら IMOZURU_ALLOW_PRIVATE=1 で外せる）"

def url_problem(url: str) -> str | None:
    """取りに行ってはいけない URL なら理由を、よければ None を返す。

    URL も転送の先も、検索結果のページを書いた他人が決める。127.0.0.1 や社内の宛先を書かれると、
    手元で動いているサービス（開発用のサーバー・ローカルの AI・ブラウザのデバッグ口）の中身を
    sources/ に写してしまう（2026-10-20 のレビュー）。名前を引いた先が全部インターネット上の
    宛先のときだけ通す。10進や16進で書いた IP も、名前を引けば 127.0.0.1 などになるので同じく止まる。
    名前を引いてから接続するまでの間に引き先を変える手（DNS rebinding）は、_pinned_connection が
    つなぐ直前に引き直して確かめた IP へじかにつなぐことで防ぐ。
    プロキシを通すときは名前をプロキシが引くので、この固定では DNS rebinding を防げない。

    ホストの部分に「@」か「%」があれば断る。urlsplit は「x@a.example」の a.example を見るが、
    urllib は x@a.example をそのまま引き、「%61」は urllib だけが a に戻す。検査とつなぐ先がずれる。
    fake-IP の DNS が全ホスト名を 198.18.0.0/15 に引く環境では、環境変数 IMOZURU_ALLOW_FAKE_IP=1 で
    その範囲だけを外せる。VPN などで外の名前まで内側の宛先に引ける環境では、環境変数 IMOZURU_ALLOW_PRIVATE=1 で
    宛先の検査だけを外せる（http(s) の検査は残る）。
    """
    try:
        p = urllib.parse.urlsplit(url)
        host = p.hostname
        # 範囲外（:99999）や数字でないポート（:abc・全角の :８０）は、ここで ValueError にして断る。
        # 全角の数字は古い Python の int() が通すため、ASCII の数字だけかも自分で見る
        if p.port is not None and p.port > 65535:
            raise ValueError("ポートが範囲外")
        if not _PORT_TEXT.fullmatch(p.netloc.rpartition("@")[2]):
            raise ValueError("ポートが ASCII の数字でない")
    except ValueError:
        return "URL の形が壊れている"
    if p.scheme.lower() not in {"http", "https"}:
        return "http(s) の URL だけを取りに行く"
    if not host:
        return "URL にホストが無い"
    if "@" in p.netloc or "%" in p.netloc:
        return "URL のホストに「@」か「%」が入っている"
    # 国際化ドメイン名は、検査より前に Punycode（ASCII）へ1回だけ直す。検査も接続（ascii_url）も
    # この直した名前を使うため、検査した宛先とつなぐ宛先がずれない。全角の「。」や全角の数字で
    # 書いた 127.0.0.1 も、直せば普通の 127.0.0.1 になって下の検査で止まる。
    # ASCII のホストも同じ関数で LDH を確認する（空白や制御文字を含む名前を resolve に渡さない）
    try:
        host = _ascii_host(host)
    except ValueError:
        return "URL の形が壊れている"
    if os.environ.get("IMOZURU_ALLOW_PRIVATE") == "1":
        return None
    try:
        addrs = resolve(host)
    except (OSError, UnicodeError, ValueError) as e:
        return f"ホストを引けない（{e}）"
    for addr in addrs:
        for ip in _addresses_inside(ipaddress.ip_address(addr.split("%", 1)[0])):  # IPv6 の %eth0 を外す
            if _is_blocked_ip(ip):
                return (f"手元や内側の宛先（{ip}）には取りに行かない"
                        + _blocked_hint(ip))
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


# ホスト名のラベル（RFC 1123）。英数字で始まり終わり、間にハイフンを許す
_LDH_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
# 「host:port」の port 部分が ASCII の数字だけ（空も可）のとき一致する。IPv6 の「[::1]:80」にも使う
_PORT_TEXT = re.compile(r"(?:\[[^\]]*\]|[^:\[\]]*)(?::[0-9]*)?")


def _ascii_host(host: str) -> str:
    """ホスト名を ASCII にする。ASCII ならそのまま、非 ASCII なら IDNA（Punycode）に直して返す。

    空・制御文字を含む・IDNA に直せない（空のラベルや長すぎるラベル）ときは ValueError。
    直した結果が IP リテラルでなければ、各ラベルが LDH（英数字とハイフン・両端はハイフンでない・63 字まで）かも確認する。
    全角の「％」（NFKC で「%」になる）や NBSP のように、IDNA の変換を通っても ASCII の記号や空白で残る字を、
    ホストとして通さないため。FQDN の末尾の「.」は1個だけ許す。
    標準の idna 符号化（IDNA 2003）を使うのは、外のパッケージを入れず、http.client が
    つなぐときに使う変換と同じ結果にそろえるため。
    """
    if not host or any(ord(c) < 0x20 or ord(c) == 0x7F for c in host):
        raise ValueError("ホスト名が空か、制御文字を含む")
    if host.isascii():
        ascii_host = host
    else:
        try:
            ascii_host = host.encode("idna").decode("ascii")
        except UnicodeError as e:
            raise ValueError(f"ホスト名を IDNA に直せない（{e}）") from e
    try:
        ipaddress.ip_address(ascii_host)
    except ValueError:
        labels = ascii_host.split(".")
        if len(labels) > 1 and labels[-1] == "":
            labels.pop()  # FQDN の末尾の「.」
        if not all(_LDH_LABEL.fullmatch(label) for label in labels):
            raise ValueError("ホスト名のラベルが LDH（英数字とハイフン）でない") from None
    return ascii_host


def ascii_url(url: str) -> str:
    """パスとクエリにそのまま入った日本語の字などを % で符号化し、国際化ドメイン名を Punycode にする。

    urllib は要求の行を ASCII で組むので、辞書の項目名や自治体のファイル名を符号化せずに
    書いたリンクは UnicodeEncodeError で止まる（和竿の漆塗りの試行で 36 本中 2 本）。
    safe に % を入れるため、符号化済みの URL は変わらない。
    ホストが非 ASCII なら、url_problem が検査したのと同じ _ascii_host の結果に差し替える（ポートは残す）。
    「@」か「%」を含む netloc は url_problem が断るため、ここでも触らない。
    """
    p = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(p.path, safe="/%:@!$&'()*+,;=~")
    query = urllib.parse.quote(p.query, safe="=&%/:+?@!$'()*,;~")
    netloc = p.netloc
    host = p.hostname
    if host and not host.isascii() and "@" not in netloc and "%" not in netloc:
        port = p.port  # 範囲外のポートは ValueError。検査済みの URL では起きない
        netloc = _ascii_host(host) + (f":{port}" if port is not None else "")
    return urllib.parse.urlunsplit((p.scheme, netloc, path, query, p.fragment))


# ふだんの名乗り。ブラウザでないと本文を返さないサイトがあるので、ブラウザの形で始める。
BROWSER_UA = "Mozilla/5.0 imozuru"
# HAL（hal.science）は、ブラウザの名乗りには閲覧用の HTML を、それ以外の名乗りには PDF を返す。
# PDF を頼み直すときはこちらで名乗る（2026-10-20 に確かめた）。
PLAIN_UA = "imozuru"


def _parse_retry_after(value: str | None) -> float | None:
    """Retry-After ヘッダを待ち秒数へ直す。解釈できなければ None を返す。"""
    if not value:
        return None
    raw = value.strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())


def _retry_wait(attempt: int, retry_after: float | None) -> float | None:
    """次の再試行までの待ち秒数。上限を超える Retry-After は待たずに打ち切る（None）。"""
    if retry_after is not None:
        return None if retry_after > RETRY_MAX_WAIT_SECONDS else retry_after
    # 指数バックオフにジッターを掛け、複数件の再試行が同じ時刻へ集中しないようにする。
    delay = RETRY_BASE_SECONDS * (2 ** attempt) * random.uniform(0.5, 1.5)
    return min(RETRY_MAX_WAIT_SECONDS, delay)


def _is_retryable_url_error(err: Exception) -> bool:
    """タイムアウトに起因する URLError かどうか。DNS 失敗や接続拒否は再試行しない。"""
    return isinstance(err, TimeoutError) or (
        isinstance(err, urllib.error.URLError) and isinstance(err.reason, (TimeoutError, socket.timeout))
    )


_LAST_RETRIED = 0

# 中身が gzip なのに Content-Encoding を付け忘れる誤配信を検出する対象。PDF などのバイナリには触らない
_GZIP_MISDELIVERY_TYPES = ("text/", "html", "xml", "json")


def _decompress_gzip_bounded(raw: bytes, max_bytes: int = MAX_BYTES) -> bytes:
    """gzip を展開する。展開後が max_bytes を超えるものと、壊れたものは ValueError にする。

    圧縮したままの大きさは MAX_BYTES で見ているが、gzip は数十 KB が数 GB に膨らむ（gzip bomb）。
    max_bytes + 1 バイトまでしか読まないため、膨らむ本体でもメモリを使い切らない。
    壊れた gzip をそのまま返すと、化けた生バイトが文字化けの本文として OK になる。
    """
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as gz:
            out = gz.read(max_bytes + 1)
    except (gzip.BadGzipFile, EOFError, OSError, zlib.error) as e:
        raise ValueError("gzip の展開に失敗した") from e
    if len(out) > max_bytes:
        raise ValueError(f"展開後の本体が {max_bytes:,} バイトを超える")
    return out


def _needs_gzip_decode(headers, ctype: str, body: bytes) -> bool:
    """本体を gzip として展開すべきか。Content-Encoding の宣言か、宣言の無い誤配信の兆候で決める。"""
    tokens = {t.strip() for t in (headers.get("Content-Encoding", "") or "").lower().split(",")}
    if tokens & {"gzip", "x-gzip"}:
        return True
    # 宣言が無くても先頭が gzip のマジックバイトで、型がテキスト系なら、サーバーの付け忘れとみて試す
    return body[:2] == b"\x1f\x8b" and any(t in ctype.lower() for t in _GZIP_MISDELIVERY_TYPES)


def _download_impl(
    url: str,
    user_agent: str = BROWSER_UA,
    *,
    retries: int = MAX_TRANSIENT_RETRIES,
    deadline_seconds: float = DOWNLOAD_DEADLINE_SECONDS,
) -> tuple[bytes, str]:
    global _LAST_RETRIED
    _LAST_RETRIED = 0
    problem = url_problem(url)
    if problem:
        raise UnsafeURL(problem)
    deadline = time.monotonic() + max(1.0, deadline_seconds)
    req = urllib.request.Request(ascii_url(url), headers={"User-Agent": user_agent})
    for attempt in range(max(0, retries) + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("取得の期限を超えた")
        timeout = min(90.0, max(1.0, remaining))
        try:
            with _OPENER.open(req, timeout=timeout) as res:
                body = res.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES:
                    raise ValueError(f"本体が {MAX_BYTES:,} バイトを超える")
                if isinstance(res.length, int) and res.length > 0:
                    raise ValueError("本体が途中で切れた")
                ctype = res.headers.get("Content-Type", "")
                if _needs_gzip_decode(res.headers, ctype, body):
                    # 引数で MAX_BYTES を渡す（既定値は定義時に固定されるため）
                    body = _decompress_gzip_bounded(body, MAX_BYTES)
                _LAST_RETRIED = attempt
                return body, ctype
        except urllib.error.HTTPError as e:
            retry_after = _parse_retry_after(e.headers.get("Retry-After") if e.headers else None)
            if e.code not in RETRYABLE_HTTP_STATUS or attempt >= retries:
                raise
            wait = _retry_wait(attempt, retry_after)
            if wait is None or time.monotonic() + wait >= deadline:
                raise
            e.close()
            _sleep(wait)
        except (TimeoutError, urllib.error.URLError) as e:
            if not _is_retryable_url_error(e) or attempt >= retries:
                raise
            wait = _retry_wait(attempt, None)
            if wait is None or time.monotonic() + wait >= deadline:
                raise
            _sleep(wait)
    raise TimeoutError("取得の再試行上限に達した")


def download(url: str, user_agent: str = BROWSER_UA) -> tuple[bytes, str]:
    """URL を取りに行き、本体と Content-Type を返す。

    MAX_BYTES を超える本体や、Content-Length に届かずに切れた本体は、取得ごと失敗にする。
    Content-Encoding が gzip の本体は展開してから返す（展開後も MAX_BYTES で見る）。
    切れた原文を OK として残すと、後ろの部分を引用できないのに「原文に無い」と読み違える。
    read(n) は接続が途中で切れても例外を出さないので、残りの長さ（res.length）で確かめる。
    HTTP 429/503/504 とタイムアウトだけ、期限と回数の上限内でジッター付き再試行する。
    """
    return _download_impl(url, user_agent)


def pdf_save(url: str, out: Path) -> bool:
    """名乗りを変えて頼み直し、PDF が返ってきたら out に保存して True を返す。

    前は ax に取り直させていた（ax の名乗りは Bun/…）。ax は自分で通信し、転送の先を
    こちらで確かめられないので、検査つきの download で頼み直す。
    """
    try:
        # 既存のテストが download を2引数で差し替えるため、そのままの呼び出し形を保つ。
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


# 言語タグの先頭（BCP 47）から、その言語の旧来の文字コードの候補。文字コードを名乗らない古いページでも、
# 言語の名乗りは html の lang や Content-Language に残っていることが多い。前の候補ほど有力。
# 繁体字（地域・文字種つき）と簡体字は同じ zh でも文字コードの系統が違うため、先頭の2つのタグで引く
_LANG_CODECS: dict[str, tuple[str, ...]] = {
    "zh-tw": ("big5hkscs", "big5", "gb18030"), "zh-hk": ("big5hkscs", "big5", "gb18030"),
    "zh-mo": ("big5hkscs", "big5", "gb18030"), "zh-hant": ("big5hkscs", "big5", "gb18030"),
    "zh": ("gb18030", "gbk"), "zh-cn": ("gb18030", "gbk"), "zh-sg": ("gb18030", "gbk"),
    "zh-hans": ("gb18030", "gbk"),
    "ko": ("cp949", "euc_kr"),
    "ru": ("cp1251", "koi8_r", "koi8_u"), "uk": ("cp1251", "koi8_r", "koi8_u"),
    "bg": ("cp1251", "koi8_r", "koi8_u"),
    "ar": ("cp1256",), "he": ("cp1255",), "th": ("cp874",), "tr": ("cp1254",), "vi": ("cp1258",),
    "pl": ("cp1250",), "cs": ("cp1250",), "hu": ("cp1250",), "ro": ("cp1250",),
    "el": ("cp1253",),
    "en": ("cp1252", "latin-1"),
    "de": ("cp1252", "latin-1"), "fr": ("cp1252", "latin-1"), "es": ("cp1252", "latin-1"),
    "pt": ("cp1252", "latin-1"), "it": ("cp1252", "latin-1"), "nl": ("cp1252", "latin-1"),
    "ja": ("cp932", "euc_jp"),
}
# data-lang のような別の属性を検出しないよう、lang の前は英数字・ハイフンでないものに限る（xml:lang は検出する）
_HTML_LANG = re.compile(rb"<html\b[^>]*?(?<![\w-])lang\s*=\s*[\"']?([A-Za-z0-9_-]+)", re.I)
_META_TAG = re.compile(rb"<meta\b[^>]*>", re.I)
_META_CONTENT_LANGUAGE = re.compile(rb"http-equiv\s*=\s*[\"']?content-language", re.I)
_META_CONTENT = re.compile(rb"[\s\"']content\s*=\s*[\"']?([A-Za-z0-9_-]+)", re.I)


def _declared_lang_codecs(body: bytes) -> list[str]:
    """HTML の lang と Content-Language の名乗りから、その言語の文字コードの候補を返す。

    文字コードの名乗りが無い古い中国語・韓国語・ロシア語などのページを、UTF-8 や日本語の文字コードで
    無理に読んで化けるのを避ける。名乗りはページを書いた他人が決めるため、ここでは候補を並べるだけにして、
    decode_text が _DOCUMENT_CODECS で絞る。ファイルの頭だけを見るのは、_declared_charsets と同じ理由。
    """
    head = body[:4096]
    tags = [m.group(1) for m in _HTML_LANG.finditer(head)]
    # meta は http-equiv と content のどちらが先に記述されてもよいため、タグごとに両方を探す
    for meta in _META_TAG.findall(head):
        if _META_CONTENT_LANGUAGE.search(meta):
            tags += [m.group(1) for m in _META_CONTENT.finditer(meta)]
    found: list[str] = []
    for tag in tags:
        name = tag.decode("ascii", "ignore").lower().replace("_", "-")
        # 完全なタグ（zh-hant-tw）→ 先頭2つ（zh-hant）→ 言語だけ（zh）の順に引く
        parts = name.split("-")
        keys = [name] + (["-".join(parts[:2])] if len(parts) > 2 else []) + [parts[0]]
        for key in keys:
            if key in _LANG_CODECS:
                found += [c for c in _LANG_CODECS[key] if c not in found]
                break
    return found


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
    # 言語の名乗りからの候補は、文字コードの名乗りの後ろ・汎用の候補の前。汎用の候補は、名乗りが
    # 何も無いページの最後の手段（gb18030・cp949・cp1252 は前の候補が全部外れたときだけ試す）
    for name in ["utf-8", *_declared_charsets(body, ctype), *_declared_lang_codecs(body),
                 "cp932", "euc_jp", "gb18030", "cp949", "cp1252"]:
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


# pdfminer.six で PDF を文字にする小さなプログラム。pdftotext と同じく別の処理で動かし、時間の上限で止められるようにする
PDFMINER = ("import sys\nfrom pdfminer.high_level import extract_text\n"
            "with open(sys.argv[2], 'w', encoding='utf-8', newline='\\n') as f:\n"
            "    f.write(extract_text(sys.argv[1]))\n")


@functools.lru_cache(maxsize=None)
def has_pdfminer() -> bool:
    """文字にするときと同じ子の処理で、pdfminer.six を読み込めるか。
    この処理の中で調べると、-I で動かしたときに pip install --user で入れたものが見えない"""
    done = run_tool([sys.executable, "-c", "import pdfminer.high_level"], stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, cwd=Path(__file__).resolve().parent)
    return done is not None and done.returncode == 0


def pdf_text_command(pdf: Path, out: Path) -> list[str] | None:
    """PDF を文字にするコマンド。pdftotext が無ければ pdfminer.six を使う。どちらも無ければ None。

    poppler を入れられない環境（sudo が使えない WSL など）でも、pip で入る pdfminer.six なら
    文字の層がある PDF まで読める（#15）。行の切れ方は pdftotext と違うが、抜き書きの照合は
    同じ .txt に対して行うので困らない。文字の層が無い PDF の OCR は pdftoppm が要るまま
    """
    if not missing("pdftotext"):
        # Xpdf の pdftotext（Git for Windows に入っている）は既定で Latin-1 を出すので、
        # -enc UTF-8 で出力の文字コードを決める。poppler の pdftotext も同じ引数を受け付ける（#3）
        return ["pdftotext", "-layout", "-enc", "UTF-8", str(pdf), str(out)]
    if has_pdfminer():
        # 子の処理の作業フォルダは、取ってきたファイルの無いこのスクリプトの置き場にする。
        # python -c は作業フォルダから先に読み込むので、ダウンロードの隣で動かさない
        return [sys.executable, "-c", PDFMINER, str(pdf), str(out)]
    if has_pdf2txt():
        return ["pdf2txt.py", "-o", str(out), str(pdf)]
    return None


def has_pdf2txt() -> bool:
    """pdfminer.six のコマンド pdf2txt.py を使えるか。pipx などで、コマンドだけ PATH にあることがある。
    Windows は .py を直接は動かせないので使わない"""
    return os.name != "nt" and not missing("pdf2txt.py")


def pdf_to_text(pdf: Path) -> str:
    """PDF を、行番号で原文を指せる文字にする。pdftotext なら段組みを保つ -layout を使う。"""
    out = pdf.with_suffix(".txt")
    # 子の処理は別の作業フォルダで動かすので、ファイルは絶対パスで渡す
    cmd = pdf_text_command(pdf.resolve(), out.resolve())
    if cmd is None:
        return ""
    done = run_tool(cmd, stderr=subprocess.DEVNULL, cwd=Path(__file__).resolve().parent)
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
        # 途中で落ちると、書き出せたページの画像だけが残る。残りを OCR すると欠けた本文が成功扱いになる（#14）
        if done.returncode != 0:
            return "", f"PDF を画像にする途中で失敗（pdftoppm の終了コード {done.returncode}）"
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
                    "言語データが合っていないかも（日本語なら tesseract の jpn と jpn_vert。"
                    "ほかの言語は IMOZURU_OCR_LANG で指定できる。例: chi_sim+eng・kor+eng・deu+eng）")
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
        "誤りが残るので、引用は原本の PDF で確かめる）\n\n" + ocr, encoding="utf-8", newline=SOURCE_NEWLINE)
    return ocr, ""


HAS_AX = shutil.which("ax") is not None


def md_link(text: str, href: str) -> str:
    """原文のリンクを、開いても危険な処理が動かず markdown の形が崩れないように残す。"""
    href = href.strip()
    # JavaScript: や data: のリンクは、.md を開いた先で押すとページの中で動く。http(s) だけ残す。
    # 全角の「＃」などを含む URL は urlsplit が ValueError を出す。リンク1本でページ全体を FAIL にしない
    try:
        scheme = urllib.parse.urlsplit(href).scheme.lower()
    except ValueError:
        return text
    if scheme not in {"http", "https"}:
        return text
    # 括弧と空白が URL の中にあると、[文字](URL) の形がそこで切れる
    href = re.sub(r"[()\s]", lambda m: urllib.parse.quote(m.group(0), safe=""), href)
    # 「\」を先に符号化する。後にすると、元からある「\]」が「\\]」になって「]」が効き直す
    text = text.replace("\\", "\\\\").replace("[", r"\[").replace("]", r"\]")
    return f"[{text}]({href})"


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
                try:
                    self.href = urllib.parse.urljoin(self.base, href)
                except ValueError:  # 閉じていない「[」などの壊れた href。リンクにせず文字だけ残す
                    return
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
            self._emit(md_link(text, self.href) if text else "")
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
    out.write_text(text, encoding="utf-8", newline=SOURCE_NEWLINE)
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


# yt-dlp の --sub-langs に渡せる形（言語コードのカンマ区切り）。「all」や正規表現は通さない。
# 各コードの先頭は英数字に限る。yt-dlp は先頭の「-」を「その言語を除く」の指定に読むため。
# 動画の持ち主や環境変数の値で、字幕を全部落とさせたり、yt-dlp の引数を変えさせたりしないため
_YT_LANGS_FORM = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*(,[A-Za-z0-9][A-Za-z0-9_-]*)*")


# --sub-langs に渡すと字幕を全部落とす、または動画ではなくチャットの記録を落とす指定。環境変数でも動画の名乗りでも通さない
_YT_BLOCKED_LANGS = frozenset({"all", "live_chat"})
# 動画の元の言語として受ける形。zh-Hans・zh-TW・pt-BR・es-419 のようにサブタグつきの完全なタグも通す
_YT_SPOKEN_FORM = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,4})*")


# 動画の字幕の言語キー（subtitles・automatic_captions のキー）として --sub-langs に渡せる形。
# キーは動画の持ち主が決めるため、「,」や正規表現の記号を含むもの・先頭が「-」のものは通さない
# （yt-dlp は先頭の「-」を「その言語を除く」の指定に読む）。ko・zh-Hans・pt-BR・es-419 の形に絞る
_YT_LANG_TOKEN = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*")


def _safe_yt_lang(key: str) -> bool:
    """字幕の言語キーが、そのまま --sub-langs に渡して安全な言語タグか。「all」「live_chat」は除く。"""
    return (_YT_LANG_TOKEN.fullmatch(key) is not None
            and key.split("-")[0].lower() not in _YT_BLOCKED_LANGS)


def _probe_youtube_sub_langs(url: str) -> tuple[str | None, str | None]:
    """動画が実際に持つ字幕の言語を yt-dlp で調べ、(手動字幕の言語, 自動字幕の元の言語) を返す。

    動画の言語（%(language)s）が空のとき、ja・en の指定だけでは韓国語や中国語の字幕を取り逃がし、
    本文が 0 文字になった（実地のバグ）。字幕の一覧そのものを見れば、言語が空でも手動字幕（ko・zh-Hans）や
    元の言語の自動字幕（ko-orig）を指名できる。
    手動字幕は ja・en を除いた最初の安全なキー（ja・en は1回目で試行済み）。自動字幕は「-orig」で終わる
    最初の安全なキー（翻訳された自動字幕を取ると 429 になる・youtube_to_md の docstring）。
    調べられなかったとき（時間切れ・出力の崩れ）は (None, None)。
    """
    res = run_tool(
        ["yt-dlp", "--no-update", "--no-playlist", "--skip-download", "--encoding", "utf-8",
         "--print", "%(subtitles)j\t%(automatic_captions)j", "--", url],
        capture_output=True, encoding="utf-8", errors="replace")
    lines = res.stdout.splitlines() if res is not None and res.stdout else []
    if not lines:
        return None, None
    parts = lines[0].split("\t", 1)

    def keys(raw: str) -> list[str]:
        try:
            data = json.loads(raw)
        except ValueError:
            return []  # "NA"（字幕の項目が無い動画）や崩れた出力
        return [k for k in data if isinstance(k, str)] if isinstance(data, dict) else []

    manual = keys(parts[0])
    auto = keys(parts[1]) if len(parts) > 1 else []
    manual_lang = next((k for k in manual if k.lower() not in ("ja", "en") and _safe_yt_lang(k)), None)
    auto_orig = next((k for k in auto if k.lower().endswith("-orig") and _safe_yt_lang(k[:-5])), None)
    return manual_lang, auto_orig


def _youtube_manual_langs() -> str:
    """IMOZURU_YT_LANGS が有効な言語の並びならそれを、そうでなければ空文字を返す。"""
    val = os.environ.get("IMOZURU_YT_LANGS", "").strip()
    if val and _YT_LANGS_FORM.fullmatch(val) and not _YT_BLOCKED_LANGS & set(val.lower().split(",")):
        return val
    return ""


def _spoken_language(printed: str) -> str:
    """yt-dlp の出力（題名・投稿者・日付・言語のタブ区切り）から、動画の元の言語の完全なタグを返す。

    言語の名乗りは動画の持ち主が決める。「all」などで字幕を全部落とさせないよう、言語の形だけ通す。
    形が合わなければ空文字を返す。「all」は3字の形に合うが、--sub-langs に渡すと全部の字幕を落とすため除く。
    zh-Hans と zh-Hant のように、サブタグで字幕の言語が変わるため、基底の言語（先頭のタグ）だけにせず完全なタグを返す。
    基底の言語は呼び手が split("-")[0] で取る。
    """
    raw = printed.rstrip("\n").split("\t")[-1].strip()
    if _YT_SPOKEN_FORM.fullmatch(raw) and raw.split("-")[0].lower() not in _YT_BLOCKED_LANGS:
        return raw
    return ""


def _vtt_language(path: Path) -> str:
    """v.<lang>.vtt の <lang> を小文字で返す。"""
    name = path.name.lower()
    return name[2:-4] if name.startswith("v.") and name.endswith(".vtt") else name


def _vtt_order(vtts: list[Path], preferred: list[str]) -> list[Path]:
    """字幕ファイルを、言語の優先順（完全一致 → 前方一致 → ファイル名順）に並べる。

    前方一致は zh-Hans-orig のような自動字幕を zh-Hans の仲間として検出するため、「タグ + "-"」で始まるものに限る
    （en が en-orig には当たるが、enx には当たらない）。
    """
    prefs = list(dict.fromkeys(t.lower() for t in preferred if t))

    def rank(path: Path) -> tuple[int, int, str]:
        tag = _vtt_language(path)
        for i, want in enumerate(prefs):
            if tag == want:
                return (0, i, path.name)
        for i, want in enumerate(prefs):
            if tag.startswith(want + "-"):
                return (1, i, path.name)
        return (2, 0, path.name)

    return sorted(vtts, key=rank)


def youtube_to_md(url: str, out: Path) -> str:
    """yt-dlp で字幕だけ落とし（動画は落とさない）、時刻つきの本文にする。

    手動の字幕を優先し、無ければ動画の元の言語の自動字幕。自動字幕は同じ文が
    次の区間にも繰り返し出るので、直前と同じ文は捨てる。
    手動の字幕の言語は、環境変数 IMOZURU_YT_LANGS（zh-Hans,en のような並び・all は不可）があればそれ、
    無ければ ja,en。ja,en に無く、動画の元の言語が中国語・韓国語などのときは、自動字幕へ落ちる前に
    その言語の手動字幕を1回だけ試す（zh-Hans のようにサブタグがあれば zh-Hans,zh）。
    複数の字幕が取れたときの優先は、環境変数の並び → 元の言語（完全なタグ・基底の言語）→ 日本語 → 英語の順。
    動画の言語が空のときは、ja,en で取れなかった場合に限り、字幕の一覧を1回だけ調べる（_probe_youtube_sub_langs）。
    手動字幕（ko・zh-Hans など）があれば自動字幕へ落ちる前にそれを取り、無ければ自動字幕は一覧の元の言語（ko-orig など）を指名する。

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
    manual_langs = _youtube_manual_langs()
    kind = "手動"
    printed = ""
    spoken = ""
    base = ""
    manual_lang = ""  # 字幕の一覧から見つけた手動字幕の言語（動画の言語が空のとき）

    def fetch_subs(flag: str, langs: str) -> subprocess.CompletedProcess | None:
        return run_tool(
            # --no-playlist: watch?v=…&list=… の URL で、再生リストの全部の字幕を取りに行かない
            ["yt-dlp", "--no-update", "--no-playlist", "--skip-download", flag, "--encoding", "utf-8",
             "--sub-langs", langs, "--sub-format", "vtt", "--no-simulate",
             "--print", "%(title)s\t%(uploader)s\t%(upload_date)s\t%(language|)s",
             "-o", str(tmp / "v.%(ext)s"), "--", url],
            capture_output=True, encoding="utf-8", errors="replace")

    # 1回目: 手動の字幕。時間切れ（None）は、字幕なしとして呼び手が FAIL にする
    meta = fetch_subs("--write-subs", manual_langs or "ja,en")
    if meta is not None:
        printed = meta.stdout
        # 言語は en-US のように地域つきで来るが、字幕は en-orig。判別できなければ空（日本語か英語の元の字幕を依頼する）
        spoken = _spoken_language(printed)
        base = spoken.split("-")[0].lower()
        if not any(tmp.glob("*.vtt")):
            # 動画の元の言語が ja・en 以外（中国語・韓国語など）で、環境変数の指定も無いときは、
            # 自動字幕へ落ちる前にその言語の手動字幕を1回だけ試す。手動字幕は自動より誤変換が少ない。
            # zh-Hans のようにサブタグがあれば、完全なタグを先に、基底の言語（zh）を続けて依頼する
            if spoken and base not in ("ja", "en") and not manual_langs:
                meta = fetch_subs("--write-subs", spoken if spoken.lower() == base else f"{spoken},{base}")
                if meta is not None:
                    printed = meta.stdout or printed
            # 動画の言語が空（実地では ko・zh-Hans の動画で起きた）だと、上の1回目は ja,en の指定だけで 0 文字になる。
            # 字幕の一覧を1回だけ調べ、手動字幕があれば自動字幕へ落ちる前に試行し、自動字幕は元の言語（-orig）を指名する
            probed_auto: str | None = None
            if meta is not None and not spoken and not manual_langs and not any(tmp.glob("*.vtt")):
                probed_manual, probed_auto = _probe_youtube_sub_langs(url)
                if probed_manual:
                    manual_lang = probed_manual
                    meta = fetch_subs("--write-subs", probed_manual)
                    if meta is not None:
                        printed = meta.stdout or printed
            if meta is not None and not any(tmp.glob("*.vtt")):
                kind = "自動（誤変換あり・引用は動画で確かめる）"
                # 完全なタグの -orig（zh-Hans-orig）も依頼する。ja・en は地域つき（en-US）でも字幕は en-orig のため、基底だけにする
                if not spoken:
                    auto_langs = probed_auto or "ja-orig,en-orig"
                elif spoken.lower() == base or base in ("ja", "en"):
                    auto_langs = f"{base}-orig"
                else:
                    auto_langs = f"{spoken}-orig,{base}-orig"
                meta = fetch_subs("--write-auto-subs", auto_langs)
                if meta is not None:
                    printed = meta.stdout or printed
    # 環境変数で言語を指定したときはその並びを最優先にする。指定が無ければ、動画の元の言語の字幕を最優先にし、
    # 次に日本語・英語、残りはファイル名順
    ordered = ([*manual_langs.split(","), spoken, base, "ja", "en"] if manual_langs
               else [spoken, base, manual_lang, "ja", "en"])
    vtts = _vtt_order([p for p in tmp.glob("*.vtt") if p.stat().st_size <= MAX_BYTES], ordered)
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
                   encoding="utf-8", newline=SOURCE_NEWLINE)
    return body


PMC_PATHS = {"pmc.ncbi.nlm.nih.gov": r"/articles/(PMC\d+)", "www.ncbi.nlm.nih.gov": r"/pmc/articles/(PMC\d+)",
             "europepmc.org": r"/article/PMC/(PMC\d+)"}


def pmc_id(url: str) -> str | None:
    """PMC の記事の URL なら PMCID を返す。arxiv_id と同じく、URL のどこかではなくホスト名とパスで見る。"""
    p = urllib.parse.urlsplit(url)
    pattern = PMC_PATHS.get((p.hostname or "").lower().rstrip("."))
    m = re.fullmatch(pattern + r"/?", p.path, re.I) if pattern else None
    return m.group(1).upper() if m else None


EUROPE_PMC_XML = "https://www.ebi.ac.uk/europepmc/webservices/rest/{}/fullTextXML"
# 長い論文にも余裕を持たせつつ、小さな XML でメモリや再帰の上限を使い切らせない。
JATS_MAX_ELEMENTS = 200_000
JATS_MAX_DEPTH = 200
_XLINK_HREF = "{http://www.w3.org/1999/xlink}href"
# 段落の中の飾りや参照。前後に空白を足すと「Smith et al. ( 2020 )」のように崩れるので、そのままつなぐ
_JATS_INLINE = {"italic", "bold", "sup", "sub", "sc", "underline", "monospace", "roman", "strike",
                "overline", "xref", "ext-link", "uri", "named-content", "styled-content",
                "inline-formula", "abbrev"}
# 本文の文字ではないもの（図の差し替え用の説明や ID）
_JATS_SKIP = {"alt-text", "object-id", "graphic", "inline-graphic", "media"}
# 見出しを持つ入れ物。title が無ければ下の既定の見出しを使う（無いものは見出しなしで中へ）
_JATS_SECTIONS = {"sec": "", "abstract": "Abstract", "trans-abstract": "Abstract", "ack": "Acknowledgements",
                  "app": "Appendix", "ref-list": "References", "glossary": "Glossary", "boxed-text": ""}


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _jats_raw(el) -> str:
    parts = [el.text or ""]
    for child in el:
        tag = _local(child.tag)
        if tag in _JATS_SKIP:
            inner = ""
        elif tag == "alternatives":
            # 同じ式が TeX・MathML・画像で並ぶ。読める TeX を1つだけ残す
            kids = list(child)
            pick = next((k for k in kids if _local(k.tag) == "tex-math"), kids[0] if kids else None)
            inner = _jats_raw(pick) if pick is not None else ""
        else:
            inner = _jats_raw(child).strip()
            href = child.get(_XLINK_HREF)
            if tag in ("ext-link", "uri") and href and href != inner:
                inner = md_link(inner, href)
        parts.append(inner if tag in _JATS_INLINE else f" {inner} ")
        parts.append(child.tail or "")
    return "".join(parts)


def _jats_flat(el) -> str:
    """要素の文字を1行にする。文献の <surname>Smith</surname><given-names>J</given-names> も語の間を空ける。"""
    if el is None:
        return ""
    text = " ".join(_jats_raw(el).split())
    return re.sub(r"([(\[])\s+", r"\1", re.sub(r"\s+([,.;:)\]])", r"\1", text))


def _jats_blocks(el, level: int, out: list[str]) -> None:
    """本文の入れ物をたどり、見出しは1行、段落・図表の説明・文献は1つずつ1行にする。"""
    for child in el:
        tag = _local(child.tag)
        if tag in ("title", "label") or tag in _JATS_SKIP:
            continue  # 見出しは親が書く
        if tag in _JATS_SECTIONS:
            title = _jats_flat(child.find("title")) or _JATS_SECTIONS[tag]
            label = _jats_flat(child.find("label"))
            if title:
                out += ["", "#" * min(level, 6) + " " + " ".join(filter(None, [label, title])), ""]
            _jats_blocks(child, level + 1 if title else level, out)
        elif tag in ("p", "disp-formula", "disp-quote", "def-item", "fn", "statement"):
            line = _jats_flat(child)
            if line:
                out.append(line)
        elif tag in ("list",):
            out += ["- " + _jats_flat(item) for item in child if _local(item.tag) == "list-item"]
        elif tag in ("fig", "table-wrap", "supplementary-material"):
            label = _jats_flat(child.find("label")) or {"fig": "図", "table-wrap": "表"}.get(tag, "補足")
            caption = _jats_flat(child.find("caption"))
            out.append(f"[{label}] {caption}".rstrip())
            for row in child.iter():
                if _local(row.tag) == "tr":
                    out.append(" | ".join(_jats_flat(cell) for cell in row))
            for foot in child.findall("table-wrap-foot"):
                _jats_blocks(foot, level, out)
        elif tag == "ref":
            label = _jats_flat(child.find("label"))
            cites = [c for c in child if _local(c.tag) != "label"]
            text = " ".join(_jats_flat(c) for c in cites)
            doi = next((p.text for p in child.iter() if _local(p.tag) == "pub-id"
                        and p.get("pub-id-type") == "doi" and p.text), None)
            if doi and f"doi.org/{doi.strip()}" not in text:
                text += f" https://doi.org/{doi.strip()}"
            out.append(" ".join(filter(None, [f"[{label}]" if label else "", text])))
        else:
            _jats_blocks(child, level, out)  # body・back・fn-group・notes などはそのまま中へ


def jats_to_md(xml: bytes, url: str, xml_url: str, *, expected_doi: str | None = None) -> str:
    """Europe PMC の全文 XML（JATS）を、見出し・段落ごとの行にした markdown にする。要約はしない。

    XML をそのまま文字にすると、雑誌名・ID・著者が区切りなく1行目につながって読めなかった
    （2026-10-19 の試しで Dully 2021 など）。題名と書誌は頭の数行にまとめ、本文は行番号で指せる形にする。
    """
    def refuse_entity(*args):
        raise ValueError("XML が ENTITY を宣言している")

    count = depth = 0

    def start(tag, attrs):
        nonlocal count, depth
        count += 1
        depth += 1
        if count > JATS_MAX_ELEMENTS or depth > JATS_MAX_DEPTH:
            raise ValueError("XML の要素数か深さが上限を超えた")

    def end(tag):
        nonlocal depth
        depth -= 1

    # JATS の DOCTYPE は許すが、実体の宣言・外部参照は文字コードに関係なく断る。
    # 木を作る前に上限も検査し、細工した XML でメモリを使い切らせない。外部 DTD は読まない。
    parser = expat.ParserCreate()
    parser.EntityDeclHandler = parser.ExternalEntityRefHandler = refuse_entity
    parser.StartElementHandler, parser.EndElementHandler = start, end
    parser.Parse(xml, True)
    root = ET.fromstring(xml)
    meta = root.find("front/article-meta")
    if meta is None:
        meta = ET.Element("article-meta")
    journal = _jats_flat(root.find("front/journal-meta/journal-title-group/journal-title"))
    year = next((y.text for y in meta.iter("year") if y.text), "")
    ids = {a.get("pub-id-type"): (a.text or "").strip() for a in meta.findall("article-id")}
    requested = pmc_id(url)
    if requested and any(not re.fullmatch(r"(?:PMC)?" + requested[3:], (a.text or "").strip(), re.I)
                         for a in meta.findall("article-id") if a.get("pub-id-type") == "pmcid"):
        raise ValueError("XML の PMCID が要求した記事と違う")
    if expected_doi:
        norm_expected = normalize_doi(expected_doi)
        xml_dois = [normalize_doi((a.text or "").strip())
                    for a in meta.findall("article-id") if a.get("pub-id-type") == "doi"]
        if not norm_expected or norm_expected not in xml_dois:
            raise ValueError("XML の DOI が要求した論文と違う")
    authors = []
    for contrib in meta.iter("contrib"):
        if contrib.get("contrib-type") != "author":
            continue
        name = contrib.find("name")
        authors.append(" ".join(filter(None, [_jats_flat(name.find("given-names")),
                                              _jats_flat(name.find("surname"))]))
                       if name is not None else _jats_flat(contrib.find("collab")))
    out = [f"# {_jats_flat(meta.find('title-group/article-title')) or url}", "", url,
           f"本文: Europe PMC の全文 XML（{xml_url}）",
           " / ".join(filter(None, [journal, year, ids.get("doi") and f"https://doi.org/{ids['doi']}",
                                    ids.get("pmcid")])),
           "著者: " + ", ".join(a for a in authors if a)]
    keywords = [_jats_flat(k) for k in meta.iter("kwd")]
    if keywords:
        out.append("キーワード: " + ", ".join(keywords))
    # 要旨は article-meta の中にあるので、見出しつきの節として本文・巻末と同じ流れに乗せる
    parts = ET.Element("article")
    parts.extend([*meta.findall("abstract"), *meta.findall("trans-abstract"),
                  *(n for n in (root.find("body"), root.find("back")) if n is not None)])
    _jats_blocks(parts, 2, out)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip() + "\n"


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
    """検査済みの原文を移し、途中で失敗したら保存済みの原文を戻す。"""
    files = [saved, kept] if saved != kept else [kept]
    files = [p for p in files if p.parent != destination]
    # 保存先に同じ名前のフォルダがあれば、何も動かす前に断る。Linux はフォルダを退避の一時ファイルへ
    # 移す操作を断るが、Windows の os.replace は移してしまい、後片付けで消せずに失敗して、
    # フォルダが隠れた名前のまま残った（2026-10-26 に Windows で試験を回して分かった）。
    # 検査のあと移すまでのあいだに、別のプロセスが同じ名前のフォルダを作る競合までは防いでいない
    for source in files:
        target = destination / source.name
        if target.is_dir() and not target.is_symlink():
            raise IsADirectoryError(f"保存先に同じ名前のフォルダがある: {target}")
    backups, moved = {}, []
    # 強制終了や電源断のときは退避した写しが残るだけで、自動では戻らない（その備えが要るなら復旧の記録を残す形にする）
    try:
        # PDF と本文の片方だけ新しくなると引用元が食い違うので、両方を退避してから移す。
        for source in files:
            target = destination / source.name
            if target.exists() or target.is_symlink():
                with tempfile.NamedTemporaryFile(prefix=f".{target.name}-", dir=destination, delete=False) as tmp:
                    backup = Path(tmp.name)
                try:
                    target.replace(backup)
                except BaseException:
                    backup.unlink()
                    raise
                backups[target] = backup
        for source in files:
            target = destination / source.name
            source.replace(target)
            moved.append(target)
    except BaseException:
        for target in moved:
            target.unlink()
        for target, backup in backups.items():
            backup.replace(target)
        raise
    for backup in backups.values():
        backup.unlink()
    return destination / kept.name


def valid_source_name(name: str) -> bool:
    """保存する原文の名前は、パスではなく単一のファイル名に限る。

    Windows で使えない字（<>"|?*）は、そこで保存が OSError で止まるので先に断る。
    字の向きを変える制御文字（U+202E など）は、名前の見た目を偽れるので断る。
    """
    device = name.split(".", 1)[0].rstrip(" ").upper()
    return (bool(name.strip(" .")) and name == name.rstrip(" .")
            and not any(c in name for c in "/\\:<>\"|?*")
            and not any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in name)
            and not re.search(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]", name)
            and device not in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
            and not re.fullmatch(r"(?:COM|LPT)[1-9¹²³]", device))


_DOI_CORE = re.compile(r"10\.\d{4,9}/[^\s?#\"'<>]+")
_DOI_HOSTS = frozenset({"doi.org", "dx.doi.org"})
_DOI_PATH = re.compile(r"/doi/(?:(?:abs|full|pdf|epdf|pdfdirect|book|figure)/)?(10\.\d{4,9}/[^\s?#]+)", re.I)
_SPRINGER_DOI_PATH = re.compile(r"/(?:article|chapter|content/pdf)/(10\.\d{4,9}/[^\s?#]+)", re.I)


def normalize_doi(raw: str) -> str:
    """DOI を小文字の標準形（10.xxxx/...）にそろえる。形に合わなければ空文字を返す。"""
    if not raw:
        return ""
    s = urllib.parse.unquote(raw.strip())
    s = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", s, flags=re.I)
    s = re.sub(r"^doi:\s*", "", s, flags=re.I)
    s = s.strip().rstrip(".,;)/")
    if not _DOI_CORE.fullmatch(s):
        return ""
    return s.lower()


def extract_doi(url: str) -> str | None:
    """学術論文の URL から明示的な DOI だけを抽出する。題名などからの推測はしない。"""
    try:
        p = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    host = (p.hostname or "").lower().rstrip(".")
    path = urllib.parse.unquote(p.path)
    candidate = ""
    if host in _DOI_HOSTS:
        candidate = path.lstrip("/")
    else:
        m = _DOI_PATH.search(path)
        if m:
            candidate = m.group(1)
        elif host == "link.springer.com":
            m = _SPRINGER_DOI_PATH.match(path)
            if m:
                candidate = m.group(1)
                if path.lower().startswith("/content/pdf/") and candidate.lower().endswith(".pdf"):
                    candidate = candidate[:-4]
    norm = normalize_doi(candidate)
    return norm or None


def _oa_candidates_from_europepmc(
    doi: str | None, pmcid: str | None, *, skip_jats_pmcid: str | None = None
) -> tuple[list[tuple[str, str, str]], str | None]:
    """Europe PMC 検索 API から、DOI か PMCID が完全一致する OA 候補だけを返す。"""
    query = f'DOI:"{doi}"' if doi else f"PMCID:{pmcid}" if pmcid else ""
    if not query:
        return [], doi
    qs = urllib.parse.urlencode({"query": query, "resultType": "core", "format": "json", "pageSize": "3"})
    api_url = f"{EUROPE_PMC_SEARCH}?{qs}"
    try:
        body, _ = download(api_url, user_agent=OA_API_UA)
        data = json.loads(body.decode("utf-8", errors="replace"))
    except Exception:
        return [], doi
    results = (data.get("resultList") or {}).get("result") or []
    discovered_doi = doi
    candidates: list[tuple[str, str, str]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        item_doi = normalize_doi(str(item.get("doi") or ""))
        item_pmcid = str(item.get("pmcid") or "").strip().upper()
        # 別の論文を抽出すると引用の前提が崩れるため、要求した DOI か PMCID との完全一致だけを受け入れる。
        if doi and item_doi != doi:
            continue
        if pmcid and item_pmcid != pmcid.upper():
            continue
        if not discovered_doi and item_doi:
            discovered_doi = item_doi
        if item.get("isOpenAccess") == "Y" and item_pmcid:
            if not skip_jats_pmcid or item_pmcid != skip_jats_pmcid.upper():
                candidates.append(("jats", EUROPE_PMC_XML.format(item_pmcid), "oa-europepmc-xml"))
        ft_list = ((item.get("fullTextUrlList") or {}).get("fullTextUrl")) or []
        pdfs, htmls = [], []
        for entry in ft_list:
            if not isinstance(entry, dict) or entry.get("availabilityCode") != "OA":
                continue
            cand_url = str(entry.get("url") or "").strip()
            if not cand_url:
                continue
            style = str(entry.get("documentStyle") or "").lower()
            if style == "pdf":
                pdfs.append(("pdf", cand_url, "oa-europepmc-pdf"))
            elif style == "html":
                htmls.append(("html", cand_url, "oa-europepmc-html"))
        candidates.extend(pdfs)
        candidates.extend(htmls)
        break
    return candidates, discovered_doi


def _oa_candidates_from_openalex(doi: str, original_url: str) -> list[tuple[str, str, str]]:
    """OpenAlex Works API から、DOI が完全一致する OA 候補だけを返す。"""
    api_url = OPENALEX_WORK.format(urllib.parse.quote(doi, safe="/"))
    try:
        body, _ = download(api_url, user_agent=OA_API_UA)
        work = json.loads(body.decode("utf-8", errors="replace"))
    except Exception:
        return []
    if not isinstance(work, dict):
        return []
    if normalize_doi(str(work.get("doi") or "")) != doi:
        return []
    orig_host = (urllib.parse.urlsplit(original_url).hostname or "").lower().rstrip(".")
    pdfs: list[tuple[str, str, str]] = []
    landings: list[tuple[str, str, str]] = []
    locations = []
    best = work.get("best_oa_location")
    if isinstance(best, dict):
        locations.append(best)
    for loc in work.get("locations") or []:
        if isinstance(loc, dict):
            locations.append(loc)
    for loc in locations:
        if not loc.get("is_oa"):
            continue
        pdf_url = str(loc.get("pdf_url") or "").strip()
        if pdf_url:
            pdfs.append(("pdf", pdf_url, "oa-openalex-pdf"))
        landing = str(loc.get("landing_page_url") or "").strip()
        if landing:
            land_host = (urllib.parse.urlsplit(landing).hostname or "").lower().rstrip(".")
            # 元と同じ出版社の着地ページへ戻しても 403/壁を繰り返すだけのため別ホストのリポジトリだけ試す。
            if land_host and land_host != orig_host:
                landings.append(("html", landing, "oa-openalex-oa"))
    return pdfs + landings


def content_problem(text: str, *, is_pdf: bool, short_ok: bool = False) -> tuple[str | None, str | None]:
    """本文が壁・確認画面か短すぎるかを判定し、(種別, 理由) を返す。問題なければ (None, None)。"""
    floor = 1 if short_ok else (MIN_TEXT_CHARS if is_pdf and not mostly_cjk(text) else MIN_HTML_CHARS)
    head = text[:4000].lower()
    wall = next((p for p in (*WALL_PATTERNS, *CHALLENGE_PATTERNS) if p in head), None)
    if wall and len(text) < MIN_TEXT_CHARS * 4:
        return "wall", f"壁に当たった（{wall}）"
    if not text.strip() or len(text) < floor:
        return "short", f"本文が短すぎる（{len(text)}文字）"
    return None, None


# HAL の記録ページのパス。ID は「ポータル名-数字」（hal-01234567・inria-00123456・tel-01234567）で、
# バージョンは v2 のように付く。/file/ や /document を含むパスは記録ページではないため当てない
_HAL_RECORD_PATH = re.compile(r"/[a-z][a-z0-9]*-\d{6,9}(?:v\d+)?/?", re.I)


def _hal_document_url(url: str) -> str | None:
    """HAL の記録ページの URL から、本文 PDF を返す /document の URL を作る。当てはまらなければ None。

    記録ページがボット確認で落ちても、/document は PDF へ転送することが多い。
    記録ページ以外（トップ・/document・.pdf・検索ページなど）に /document を追加すると別物を取るため、None にする。
    """
    try:
        p = urllib.parse.urlsplit(url)
        host = (p.hostname or "").lower()
    except ValueError:
        return None
    if p.scheme.lower() not in {"http", "https"}:
        return None
    # ポートや認証情報つきの URL は作り直しで落ちる情報が出るため扱わない
    if p.netloc.lower() != host:
        return None
    if host != "hal.science" and not host.endswith(".hal.science"):
        return None
    if not _HAL_RECORD_PATH.fullmatch(p.path):
        return None
    return f"https://{host}{p.path.rstrip('/')}/document"


def _try_hal_document(
    sources: Path,
    name: str,
    original_url: str,
    destination: Path,
    *,
    meta: dict | None = None,
) -> int | None:
    """HAL の記録ページが取れなかったとき、/document の PDF を取り直し、検証に通れば保存する。"""
    doc_url = _hal_document_url(original_url)
    if doc_url is None or url_problem(doc_url):
        return None
    try:
        body, _ = download(doc_url)
    except Exception:
        return None
    if body[:5] != b"%PDF-":
        return None
    saved = sources / f"{name}.pdf"
    kept = saved.with_suffix(".txt")
    try:
        saved.write_bytes(body)
        text = pdf_to_text(saved)
        if len(text.strip()) < 200:
            text, _ = ocr_if_longer(saved, text)
    except Exception:
        for p in (saved, kept):
            p.unlink(missing_ok=True)
        return None
    prob_kind, _ = content_problem(text, is_pdf=True)
    if prob_kind is not None:
        for p in (saved, kept):
            p.unlink(missing_ok=True)
        return None
    final = save_verified(saved, kept, destination)
    if meta is not None:
        meta["via"] = "hal-document"
        meta["resolved_url"] = doc_url
    print(f"OK\t{name}\t{final}\t{len(text)}\t{first_line(text)}")
    return 0


def _try_oa_fallback(
    sources: Path,
    name: str,
    original_url: str,
    destination: Path,
    *,
    doi: str | None = None,
    pmcid: str | None = None,
    skip_jats_pmcid: str | None = None,
    meta: dict | None = None,
) -> int | None:
    """公式 OA API（Europe PMC・OpenAlex）から同一論文の公開版を取り直し、検証に通れば保存する。"""
    epmc_cands, discovered_doi = _oa_candidates_from_europepmc(doi, pmcid, skip_jats_pmcid=skip_jats_pmcid)
    effective_doi = doi or discovered_doi
    oa_cands = _oa_candidates_from_openalex(effective_doi, original_url) if effective_doi else []
    seen = {original_url}
    if skip_jats_pmcid:
        seen.add(EUROPE_PMC_XML.format(skip_jats_pmcid.upper()))
    ordered: list[tuple[str, str, str]] = []
    for item in (*epmc_cands, *oa_cands):
        _, cand_url, _ = item
        if cand_url in seen or url_problem(cand_url):
            continue
        seen.add(cand_url)
        ordered.append(item)
        if len(ordered) >= MAX_OA_CANDIDATES:
            break

    for kind, cand_url, via_label in ordered:
        try:
            body, ctype = download(cand_url)
        except Exception:
            continue
        if not body.strip():
            continue
        ctype_low = ctype.lower()
        saved: Path | None = None
        kept: Path | None = None
        text = ""
        is_pdf = False
        try:
            if kind == "jats" or ("xml" in ctype_low and body.lstrip()[:20].startswith((b"<?xml", b"<article"))):
                text = jats_to_md(body, original_url, cand_url, expected_doi=effective_doi)
                kept = saved = sources / f"{name}.md"
                kept.write_text(text, encoding="utf-8", newline=SOURCE_NEWLINE)
            elif body[:5] == b"%PDF-":
                is_pdf = True
                saved = sources / f"{name}.pdf"
                saved.write_bytes(body)
                text = pdf_to_text(saved)
                kept = saved.with_suffix(".txt")
                if len(text.strip()) < 200:
                    text, _ = ocr_if_longer(saved, text)
            elif "html" in ctype_low or body.lstrip()[:1] == b"<":
                kept = saved = sources / f"{name}.md"
                text = html_to_md(cand_url, kept, body, ctype)
            else:
                continue
        except Exception:
            for p in (saved, kept):
                if p is not None:
                    p.unlink(missing_ok=True)
            continue

        prob_kind, _ = content_problem(text, is_pdf=is_pdf)
        if prob_kind is None and saved is not None and kept is not None:
            final = save_verified(saved, kept, destination)
            if meta is not None:
                meta["via"] = via_label
                meta["resolved_url"] = cand_url
            print(f"OK\t{name}\t{final}\t{len(text)}\t{first_line(text)}")
            return 0
        for p in (saved, kept):
            if p is not None:
                p.unlink(missing_ok=True)
    return None


def fetch_source(
    sources: Path,
    name: str,
    url: str,
    destination: Path,
    *,
    allow_fallback: bool = True,
    meta: dict | None = None,
) -> int:
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

    pmc = pmc_id(url)
    if pmc:
        # PMC の記事ページは urllib には captcha を返す（2026-10-19）。同じ本文を Europe PMC が
        # 公開の API で全文 XML として配っているので、そちらを正規の別ルートとして取る
        xml_url = EUROPE_PMC_XML.format(pmc)
        try:
            text = jats_to_md(download(xml_url)[0], url, xml_url)
        except Exception as e:  # 全文が無い（OA でない）と 404/500 などが返る。XML が壊れていても同じく取れない
            if allow_fallback:
                oa_res = _try_oa_fallback(sources, name, url, destination, pmcid=pmc,
                                          skip_jats_pmcid=pmc, meta=meta)
                if oa_res == 0:
                    return 0
            print(f"FAIL\t{name}\tEurope PMC に全文 XML が無い（{type(e).__name__}: {clean(str(e))}）。"
                  "PMC は captcha なので、ブラウザで開いてもらう候補")
            return 1
        if _LAST_RETRIED and meta is not None:
            meta["via"] = "retry"
        kept = saved = sources / f"{name}.md"
        kept.write_text(text, encoding="utf-8", newline=SOURCE_NEWLINE)
        return verify_and_save(name, text, saved, kept, destination)

    doi = extract_doi(url)
    try:
        body, ctype = download(url)
    except Exception as e:  # HTTP エラーも接続失敗も、呼び手にとっては同じ「取れない」
        # 404 はページが無いため /document も無い。壁や確認画面の 403 などのときだけ PDF を取り直す
        not_found = isinstance(e, urllib.error.HTTPError) and e.code == 404
        if allow_fallback and not not_found and _try_hal_document(sources, name, url, destination, meta=meta) == 0:
            return 0
        if allow_fallback and doi:
            oa_res = _try_oa_fallback(sources, name, url, destination, doi=doi, meta=meta)
            if oa_res == 0:
                return 0
        print(f"FAIL\t{name}\t{type(e).__name__}: {clean(str(e))}")
        return 1
    if _LAST_RETRIED and meta is not None:
        meta["via"] = "retry"
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
        kept.write_text(text, encoding="utf-8", newline=SOURCE_NEWLINE)
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

    prob_kind, _ = content_problem(text, is_pdf=saved.suffix == ".pdf")
    if prob_kind is not None and allow_fallback and _try_hal_document(sources, name, url, destination, meta=meta) == 0:
        for p in (saved, kept):
            p.unlink(missing_ok=True)
        return 0
    if prob_kind is not None and allow_fallback and doi:
        oa_res = _try_oa_fallback(sources, name, url, destination, doi=doi, meta=meta)
        if oa_res == 0:
            for p in (saved, kept):
                p.unlink(missing_ok=True)
            return 0

    return verify_and_save(name, text, saved, kept, destination, ocr_note)


def verify_and_save(
    name: str,
    text: str,
    saved: Path,
    kept: Path,
    destination: Path,
    ocr_note: str = "",
    *,
    short_ok: bool = False,
) -> int:
    """短すぎる本文と壁を FAIL にし、通ったものだけを保存先に移して OK を出す。"""
    prob_kind, reason = content_problem(text, is_pdf=saved.suffix == ".pdf", short_ok=short_ok)
    if prob_kind is not None:
        for p in (saved, kept):
            p.unlink(missing_ok=True)
        # PDF の文字起こしに要るコマンドが欠けていたなら、短い理由はたいていそれ
        lacking = missing("pdftotext", "pdftoppm", "tesseract") if saved.suffix == ".pdf" else []
        if "pdftotext" in lacking and (has_pdfminer() or has_pdf2txt()):
            lacking.remove("pdftotext")  # 代わりの pdfminer.six で文字の層は読んだ。足りないのは OCR の道具
        if lacking:
            reason += f"。{'・'.join(lacking)} が無い"
        elif ocr_note and saved.suffix == ".pdf":
            reason += f"。{ocr_note}"
        print(f"FAIL\t{name}\t{reason}")
        return 1

    kept = save_verified(saved, kept, destination)
    print(f"OK\t{name}\t{kept}\t{len(text)}\t{first_line(text)}")
    return 0


def _parse_args(argv: list[str]) -> tuple[Path, str, str, bool] | None:
    """位置引数 3 つ（任意で --no-fallback 1 つ）だけを解釈する。

    -o/tmp/x のようなハイフン始まりの壊れた URL をオプションと誤認させず、url_problem へ渡して FAIL(1) にする。
    """
    if len(argv) == 3:
        return Path(argv[0]), argv[1], argv[2], True
    if len(argv) == 4 and "--no-fallback" in argv:
        rest = [a for i, a in enumerate(argv) if i != argv.index("--no-fallback")]
        return Path(rest[0]), rest[1], rest[2], False
    return None


def main() -> int:
    parsed = _parse_args(sys.argv[1:])
    if parsed is None:
        print(__doc__, file=sys.stderr)
        return 2
    sources, name, url, allow_fallback = parsed
    if not valid_source_name(name):
        print(f"FAIL\t{name!r}\t原文の名前は単一のファイル名にする")
        return 1
    sources.mkdir(parents=True, exist_ok=True)
    # 変換や FAIL の後始末は一時置き場だけで行う。同名の原文を検査前に上書きしない。
    # 保存先の中に作るので、成功時の replace はファイルシステムをまたがない。
    # 結果の行は画面に出すのと同じものを取得の記録にも残す。詳細レポート（digest.py）が
    # 検索で挙がった URL と突き合わせて〔保存〕〔取得失敗〕を付ける。URL は OK の行に出ないため、ここで記録する
    buf = io.StringIO()
    meta: dict = {}
    try:
        with tempfile.TemporaryDirectory(prefix=".imozuru-fetch-", dir=sources) as tmp, \
                contextlib.redirect_stdout(buf):
            code = fetch_source(Path(tmp), name, url, sources,
                                allow_fallback=allow_fallback, meta=meta)
    finally:
        sys.stdout.write(buf.getvalue())
    record_fetch(sources, name, url, buf.getvalue(), via=meta.get("via"), resolved_url=meta.get("resolved_url"))
    return code


def record_fetch(
    sources: Path,
    name: str,
    url: str,
    printed: str,
    *,
    via: str | None = None,
    resolved_url: str | None = None,
) -> None:
    """最後の OK/FAIL の行を、調べものの置き場の fetch-log.jsonl に1行足す。書けなくても取得の結果は変えない。

    置き場は sources/ の親。sources/ には原文だけを置く決まりなので、記録は外に出す。
    保存先が sources という名前でないとき（手で別の場所を指したとき）は、親が何か分からないので書かない。
    """
    if sources.name != "sources":
        return
    lines = [l for l in printed.splitlines() if l.startswith(("OK\t", "FAIL\t"))]
    if not lines:
        return
    parts = lines[-1].split("\t")
    # 時刻はダッシュボードが同じ日の調査を始めた順に並べるのに使う。ファイルの時刻は clone やコピーで変わる
    entry = {"url": url, "name": name, "status": parts[0], "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if parts[0] == "OK" and len(parts) > 2:
        entry["file"] = Path(parts[2]).name
    elif len(parts) > 2:
        entry["reason"] = parts[2]
    if via:
        entry["via"] = via
    if resolved_url and resolved_url != url:
        entry["resolved_url"] = resolved_url
    try:
        with (sources.parent / "fetch-log.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


if __name__ == "__main__":
    # 本文の先頭の一行を出すので、端末の文字コードに無い字（cp932 の à など）で落ちないようにする
    sys.stdout.reconfigure(errors="replace")
    sys.exit(main())
