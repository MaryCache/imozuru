#!/usr/bin/env python3
"""ユーザーが自分のブラウザで正規に保存した HTML・PDF・テキストを、検証付きで sources/ に取り込む。

ログイン壁や Cloudflare の確認画面の先は自動取得せず、ユーザーが開いて保存したローカルファイルだけを
変換・検証し、fetch-log.jsonl に via="user-browser" で記録する。

使い方:
    ingest.py <sources_dir> --file <path> [--name <source_name>] [--url <orig_url>] [--short-ok] [--move-processed]
    ingest.py <sources_dir> --inbox <inbox_dir> [--short-ok] [--move-processed]

入力フォルダに <name>.meta.json（{"url": "...", "name": "...", "short_ok": true}）を添えると
元 URL や保存名を自動で引き継ぐ。Chrome などが保存した HTML の `saved from url=` コメントも読む。
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import stat
import sys
import tempfile
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch  # noqa: E402

SUPPORTED_EXTS = frozenset({".pdf", ".html", ".htm", ".md", ".txt"})
# 意図せず巨大なフォルダを指定したときに処理が暴走しない上限。
MAX_BATCH_FILES = 200
_SAVED_FROM_URL = re.compile(r"saved from url=\(\d+\)(https?://[^\s>\"']+)", re.I)
_CANONICAL_URL = re.compile(
    r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\'](https?://[^"\']+)["\']', re.I
)
_HEADER_URL = re.compile(r"^(?:url|source)\s*:\s*(https?://\S+)\s*$", re.I)


def _canonical(path: Path) -> Path:
    """判定前にパスを一度だけ正規化し、シンボリックリンクや .. を解決する。"""
    return Path(os.path.realpath(path))


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _dirs_overlap(a: Path, b: Path) -> bool:
    """入出力フォルダが同一または親子関係にあるかを判定する。"""
    return _is_within(a, b) or _is_within(b, a)


def _valid_http_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    try:
        p = urllib.parse.urlsplit(url)
    except ValueError:
        return ""
    if p.scheme.lower() not in {"http", "https"} or not p.hostname:
        return ""
    return url


def _safe_source_name(raw_stem: str) -> str:
    """ファイル名の語幹を sources/ で安全な単一ファイル名に整える。"""
    candidate = raw_stem.strip()
    if fetch.valid_source_name(candidate):
        return candidate
    cleaned = re.sub(r"[/\\:<>\"|?*\s\x00-\x1f\x7f-\x9f]+", "-", candidate).strip(" .")
    return cleaned if fetch.valid_source_name(cleaned) else ""


def _read_bounded(path: Path) -> bytes:
    """通常ファイルか確認し、MAX_BYTES の上限を守ってバイナリで読み込む。"""
    st = os.stat(path)
    if not stat.S_ISREG(st.st_mode):
        raise ValueError("通常ファイルではない")
    if st.st_size == 0:
        raise ValueError("空のファイル")
    if st.st_size > fetch.MAX_BYTES:
        raise ValueError(f"ファイルが {fetch.MAX_BYTES:,} バイトを超える")
    with io.FileIO(path, "rb") as f:
        # stat と read の間にファイルが肥大化しても上限を超えないよう 1 バイトだけ多く読む。
        data = f.read(fetch.MAX_BYTES + 1)
    if len(data) > fetch.MAX_BYTES:
        raise ValueError(f"ファイルが {fetch.MAX_BYTES:,} バイトを超える")
    return data


def _extract_url_from_html(text: str) -> str:
    head = text[:8192]
    for pattern in (_SAVED_FROM_URL, _CANONICAL_URL):
        m = pattern.search(head)
        if m:
            valid = _valid_http_url(m.group(1))
            if valid:
                return valid
    return ""


def _extract_url_from_text(text: str) -> str:
    for line in text.splitlines()[:5]:
        m = _HEADER_URL.match(line.strip())
        if m:
            valid = _valid_http_url(m.group(1))
            if valid:
                return valid
    return ""


def _load_sidecar(path: Path) -> tuple[dict, Path | None]:
    """隣に .meta.json があれば読み込む。壊れた JSON は無視せずエラーにする。"""
    for sidecar in (path.with_name(path.name + ".meta.json"), path.with_suffix(".meta.json")):
        if sidecar.is_file():
            raw = _read_bounded(sidecar)
            meta = json.loads(raw.decode("utf-8"))
            if not isinstance(meta, dict):
                raise ValueError(f"メタ情報の形が違う: {sidecar.name}")
            return meta, sidecar
    return {}, None


def ingest_file(
    src_path: Path,
    sources_dir: Path,
    *,
    name: str | None = None,
    url: str = "",
    short_ok: bool = False,
    allowed_root: Path | None = None,
    move_processed: bool = False,
) -> int:
    """ローカルの 1 ファイルを変換・検証し、sources_dir へ原子的に保存する。"""
    canon_src = _canonical(src_path)
    canon_dst = _canonical(sources_dir)
    if allowed_root is not None and not _is_within(canon_src, allowed_root):
        print(f"FAIL\t{src_path.name}\t入力フォルダの外を指すリンクは取り込まない")
        return 1
    if _is_within(canon_src, canon_dst):
        print(f"FAIL\t{src_path.name}\t保存先フォルダの中のファイルは入力にできない")
        return 1

    ext = canon_src.suffix.lower()
    if ext not in SUPPORTED_EXTS:
        print(f"FAIL\t{src_path.name}\t扱えない拡張子: {ext or 'なし'}")
        return 1

    sidecar_meta: dict = {}
    sidecar_path: Path | None = None
    try:
        if allowed_root is not None:
            sidecar_meta, sidecar_path = _load_sidecar(src_path)
            if sidecar_path is not None and not _is_within(_canonical(sidecar_path), allowed_root):
                raise ValueError("メタ情報のリンクが入力フォルダの外を指している")
    except Exception as e:
        print(f"FAIL\t{src_path.name}\tメタ情報を読めない（{fetch.clean(str(e))}）")
        return 1

    effective_name = name or str(sidecar_meta.get("name") or "").strip() or _safe_source_name(src_path.stem)
    if not effective_name or not fetch.valid_source_name(effective_name):
        print(f"FAIL\t{effective_name or src_path.name!r}\t原文の名前は単一のファイル名にする")
        return 1

    effective_short_ok = bool(short_ok or sidecar_meta.get("short_ok"))
    effective_url = _valid_http_url(url) or _valid_http_url(str(sidecar_meta.get("url") or ""))

    try:
        data = _read_bounded(canon_src)
    except Exception as e:
        print(f"FAIL\t{effective_name}\t{fetch.clean(str(e))}")
        return 1

    sources_dir.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    ocr_note = ""
    try:
        with tempfile.TemporaryDirectory(prefix=".imozuru-ingest-", dir=sources_dir) as tmp, \
                contextlib.redirect_stdout(buf):
            work = Path(tmp)
            if ext == ".pdf":
                if data[:5] != b"%PDF-":
                    print(f"FAIL\t{effective_name}\tPDF ではない（先頭バイトが %PDF- ではない）")
                    code = 1
                else:
                    saved = work / f"{effective_name}.pdf"
                    saved.write_bytes(data)
                    text = fetch.pdf_to_text(saved)
                    kept = saved.with_suffix(".txt")
                    if len(text.strip()) < 200:
                        text, ocr_note = fetch.ocr_if_longer(saved, text)
                    code = fetch.verify_and_save(
                        effective_name, text, saved, kept, sources_dir, ocr_note, short_ok=effective_short_ok
                    )
            elif ext in {".html", ".htm"}:
                decoded = fetch.decode_text(data, "text/html")
                if not effective_url:
                    effective_url = _extract_url_from_html(decoded)
                kept = saved = work / f"{effective_name}.md"
                text = fetch.html_to_md(effective_url or "https://local.invalid/", kept, body=data, ctype="text/html")
                code = fetch.verify_and_save(
                    effective_name, text, saved, kept, sources_dir, short_ok=effective_short_ok
                )
            else:
                text = fetch.decode_text(data).replace("\r\n", "\n").replace("\r", "\n")
                if not effective_url:
                    effective_url = _extract_url_from_text(text)
                kept = saved = work / f"{effective_name}.md"
                kept.write_text(text, encoding="utf-8", newline=fetch.SOURCE_NEWLINE)
                code = fetch.verify_and_save(
                    effective_name, text, saved, kept, sources_dir, short_ok=effective_short_ok
                )
    finally:
        sys.stdout.write(buf.getvalue())

    fetch.record_fetch(sources_dir, effective_name, effective_url, buf.getvalue(), via="user-browser")

    if code == 0 and move_processed:
        dest_dir = src_path.parent / "_processed"
        dest_dir.mkdir(exist_ok=True)
        src_path.replace(dest_dir / src_path.name)
        if sidecar_path is not None and sidecar_path.exists():
            sidecar_path.replace(dest_dir / sidecar_path.name)
    return code


def ingest_inbox(
    inbox_dir: Path,
    sources_dir: Path,
    *,
    short_ok: bool = False,
    move_processed: bool = False,
) -> int:
    """入力フォルダ内の全ファイルを決定的な順で検証・取り込みする。"""
    if not inbox_dir.exists():
        print(f"入力フォルダが無い: {inbox_dir}", file=sys.stderr)
        return 1
    if not inbox_dir.is_dir():
        print(f"入力先がフォルダではない: {inbox_dir}", file=sys.stderr)
        return 1
    sources_dir.mkdir(parents=True, exist_ok=True)
    canon_in = _canonical(inbox_dir)
    canon_out = _canonical(sources_dir)
    if _dirs_overlap(canon_in, canon_out):
        print(f"入力フォルダと保存先フォルダが重なっている: {inbox_dir} / {sources_dir}", file=sys.stderr)
        return 1

    entries = sorted(
        (
            p for p in inbox_dir.iterdir()
            if not p.name.startswith(".")
            and p.name != "_processed"
            and not p.name.endswith(".meta.json")
            and (p.is_file() or p.is_symlink())
        ),
        key=lambda p: p.name,
    )
    if len(entries) > MAX_BATCH_FILES:
        print(f"入力ファイルが上限（{MAX_BATCH_FILES} 件）を超えている: {len(entries)} 件", file=sys.stderr)
        return 1
    if not entries:
        print(f"取り込み対象なし: {inbox_dir}", file=sys.stderr)
        return 0

    ok = fail = 0
    for entry in entries:
        rc = ingest_file(
            entry,
            sources_dir,
            short_ok=short_ok,
            allowed_root=canon_in,
            move_processed=move_processed,
        )
        if rc == 0:
            ok += 1
        else:
            fail += 1
    print(f"取り込み完了: OK={ok} FAIL={fail}", file=sys.stderr)
    return 0 if fail == 0 else 1


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ユーザーがブラウザで保存した原文を検証付きで sources/ に取り込む。",
        allow_abbrev=False,
    )
    parser.add_argument("sources", type=Path, help="保存先の sources/ フォルダ")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", type=Path, help="単一ファイルを取り込む")
    group.add_argument("--inbox", type=Path, help="入力フォルダからまとめて取り込む")
    parser.add_argument("--name", default="", help="--file 指定時の保存名")
    parser.add_argument("--url", default="", help="--file 指定時の元 URL")
    parser.add_argument("--short-ok", action="store_true", help="短い本文の下限を緩める（壁検査は維持）")
    parser.add_argument("--move-processed", action="store_true", help="取り込み済みのファイルを _processed/ へ移す")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.file is not None:
        return ingest_file(
            args.file,
            args.sources,
            name=args.name or None,
            url=args.url,
            short_ok=args.short_ok,
            move_processed=args.move_processed,
        )
    return ingest_inbox(args.inbox, args.sources, short_ok=args.short_ok, move_processed=args.move_processed)


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")
    sys.exit(main())
