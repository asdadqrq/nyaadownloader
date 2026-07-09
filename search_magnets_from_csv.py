#!/usr/bin/env python3
"""Batch-search Sukebei magnet links from a product-code CSV (品番一覧)."""

import argparse
import csv
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

from util.sukebei_search import (
    censorship_label,
    find_magnet_for_code,
    normalize_product_code,
)

CODE_COLUMN = "品番"
TITLE_COLUMN = "作品名・備考"
DEFAULT_DELAY = 2.5
DIRECT_CODES_OUTPUT = "product_codes_magnets.csv"
PRODUCT_CODE_RE = re.compile(r"^[A-Z0-9]+(?:-[A-Z0-9]+)+$", re.IGNORECASE)


def _md_cell(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ")


def parse_exclude_codes(values: List[str]) -> set:
    """Parse comma/newline-separated product codes to exclude from search."""
    codes: set = set()
    for value in values:
        codes.update(parse_product_codes(value))
    return codes


def parse_product_codes(value: str) -> List[str]:
    """Parse comma/semicolon/whitespace-separated product codes."""
    codes: List[str] = []
    seen = set()
    for part in re.split(r"[\s,;]+", value or ""):
        code = normalize_product_code(part.strip())
        if code and code not in seen:
            codes.append(code)
            seen.add(code)
    return codes


def looks_like_product_codes(value: str) -> bool:
    codes = parse_product_codes(value)
    return bool(codes) and all(PRODUCT_CODE_RE.match(code) for code in codes)


def rows_from_product_codes(codes: List[str]) -> tuple[List[str], List[Dict[str, str]]]:
    return [CODE_COLUMN], [{CODE_COLUMN: code} for code in codes]


def _is_preferred_release(release_type: str) -> bool:
    rt = (release_type or "").lower()
    return (
        "uncensored" in rt
        or "reducing_mosaic" in rt
        or rt == "-u"
        or "+-u" in rt
        or rt.endswith("-u")
    )


def _stats(rows: List[Dict[str, str]]) -> Dict[str, int]:
    found = not_found = uncensored = errors = skipped = pending = 0
    from_sukebei = from_bt4g = 0
    for row in rows:
        status = (row.get("search_status") or "").strip()
        if status == "found":
            found += 1
            if _is_preferred_release(row.get("release_type", "")):
                uncensored += 1
            source = (row.get("source") or "").strip().lower()
            if source == "bt4g":
                from_bt4g += 1
            else:
                from_sukebei += 1
        elif status == "not_found":
            not_found += 1
        elif status in ("skipped_empty_code", "skipped_excluded"):
            skipped += 1
        elif status.startswith("error"):
            errors += 1
        elif not status:
            pending += 1
    return {
        "total": len(rows),
        "found": found,
        "not_found": not_found,
        "uncensored": uncensored,
        "errors": errors,
        "skipped": skipped,
        "pending": pending,
        "from_sukebei": from_sukebei,
        "from_bt4g": from_bt4g,
    }


def write_markdown_report(
    md_path: str,
    rows: List[Dict[str, str]],
    *,
    title: Optional[str] = None,
) -> None:
    """Write magnet search results to a markdown file."""
    stats = _stats(rows)
    heading = title or "# 品番一覧 — Sukebei 磁链"
    lines = [
        heading,
        "",
        "",
        "## 统计",
        "",
        f"| 项目 | 数量 |",
        f"|------|------|",
        f"| 合计 | {stats['total']} |",
        f"| 已找到磁链 | {stats['found']} |",
        f"| 未找到 | {stats['not_found']} |",
        f"| 优选版 (无修/RM/-u) | {stats['uncensored']} |",
        f"| 来源: sukebei | {stats['from_sukebei']} |",
        f"| 来源: bt4g（已停用，历史数据） | {stats['from_bt4g']} |",
        f"| 错误（待重试） | {stats['errors']} |",
        f"| 跳过（空品番/排除） | {stats['skipped']} |",
        f"| 待搜索 | {stats['pending']} |",
        "",
        "说明: 搜索 [sukebei.nyaa.si](https://sukebei.nyaa.si/)，匹配标题含品番的种子；"
        "在匹配项中优先 **无修正 / uncensored / Reducing Mosaic / 品番-u** 等版本，其次标准有码版。"
        "（bt4g 回退已关闭，见 ``util/sukebei_search._BT4G_FALLBACK_ENABLED``。）",
        "",
        "---",
        "",
        "## 磁链一览",
        "",
        "| # | 区分 | 発売日 | 品番 | 作品名 | release_type | 来源 | 磁链 | 详情 | 状态 |",
        "|---|------|--------|------|--------|--------------|------|------|------|------|",
    ]

    for row in rows:
        num = _md_cell(row.get("番号", ""))
        kind = _md_cell(row.get("区分", ""))
        date = _md_cell(row.get("発売日", ""))
        code = _md_cell(row.get(CODE_COLUMN, ""))
        title = _md_cell(row.get(TITLE_COLUMN, ""))[:120]
        release = _md_cell(row.get("release_type", ""))
        source = _md_cell(row.get("source", ""))
        status = _md_cell(row.get("search_status", "") or "pending")

        magnet = (row.get("magnet") or "").strip()
        if magnet and not magnet.startswith("magnet:"):
            magnet = f"magnet:{magnet}"
        if magnet:
            magnet_link = f"[magnet]({magnet})"
        else:
            magnet_link = ""

        url = (row.get("sukebei_url") or "").strip()
        if url:
            detail_link = f"[详情]({url})"
        else:
            detail_link = ""

        lines.append(
            f"| {num} | {kind} | {date} | **{code}** | {title} | {release} | "
            f"{source} | {magnet_link} | {detail_link} | {status} |"
        )

    lines.append("")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def load_rows(path: str) -> tuple[List[str], List[Dict[str, str]]]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError("CSV has no header row.")
        if CODE_COLUMN not in reader.fieldnames:
            raise ValueError(
                f'CSV must include a "{CODE_COLUMN}" column. Found: {reader.fieldnames}'
            )
        rows = list(reader)
    return list(reader.fieldnames), rows


def write_rows(path: str, fieldnames: List[str], rows: List[Dict[str, str]]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def process_csv(
    input_path: str,
    output_path: str,
    *,
    delay: float,
    start: int,
    limit: Optional[int],
    resume: bool,
    reacquire: bool = False,
    exclude_codes: Optional[set] = None,
    markdown_path: Optional[str] = None,
    product_codes: Optional[List[str]] = None,
) -> None:
    if product_codes is None:
        fieldnames, rows = load_rows(input_path)
    else:
        fieldnames, rows = rows_from_product_codes(product_codes)
    exclude_codes = exclude_codes or set()

    extra_cols = [
        "magnet",
        "torrent_name",
        "release_type",
        "seeders",
        "leechers",
        "downloads",
        "sukebei_url",
        "source",
        "search_status",
    ]
    for col in extra_cols:
        if col not in fieldnames:
            fieldnames.append(col)

    if resume and os.path.isfile(output_path):
        _, existing = load_rows(output_path)
        by_code = {
            normalize_product_code(r.get(CODE_COLUMN, "")): r
            for r in existing
            if r.get(CODE_COLUMN)
        }
        for i, row in enumerate(rows):
            code = normalize_product_code(row.get(CODE_COLUMN, ""))
            if code in by_code and (by_code[code].get("search_status") or "").strip():
                rows[i] = {**row, **by_code[code]}

    if exclude_codes:
        write_rows(output_path, fieldnames, rows)

    subset = rows[start:]
    if limit is not None:
        subset = subset[:limit]

    total = len(subset)
    found = 0
    not_found = 0
    skipped = 0

    for offset, row in enumerate(subset):
        index = start + offset
        code = (row.get(CODE_COLUMN) or "").strip()
        if not code:
            row["search_status"] = "skipped_empty_code"
            skipped += 1
            continue

        if normalize_product_code(code) in exclude_codes:
            row["search_status"] = "skipped_excluded"
            row["magnet"] = ""
            row["torrent_name"] = ""
            row["release_type"] = ""
            row["seeders"] = ""
            row["leechers"] = ""
            row["downloads"] = ""
            row["sukebei_url"] = ""
            rows[index] = row
            skipped += 1
            print(f"[{index + 1}/{len(rows)}] {code} — excluded (skip)")
            write_rows(output_path, fieldnames, rows)
            continue

        global_n = index + 1
        global_total = len(rows)

        if resume and not reacquire and row.get("search_status") == "found" and row.get("magnet"):
            found += 1
            print(f"[{global_n}/{global_total}] {code} — already found (resume)")
            continue

        if resume and not reacquire and row.get("search_status") == "not_found":
            not_found += 1
            print(f"[{global_n}/{global_total}] {code} — already not_found (resume)")
            continue

        print(f"[{global_n}/{global_total}] Searching {code} ...", flush=True)
        try:
            torrent = find_magnet_for_code(code, delay_seconds=delay if offset > 0 else 0)
        except Exception as exc:
            row["search_status"] = f"error: {exc}"
            rows[index] = row
            write_rows(output_path, fieldnames, rows)
            print(f"  Error (saved, will retry on resume): {exc}")
            time.sleep(delay * 2)
            continue

        if torrent:
            row["magnet"] = torrent.get("magnet", "")
            row["torrent_name"] = torrent.get("name", "")
            row["release_type"] = censorship_label(torrent.get("name", ""), code)
            row["seeders"] = str(torrent.get("seeders", ""))
            row["leechers"] = str(torrent.get("leechers", ""))
            row["downloads"] = str(
                torrent.get("completed_downloads") or torrent.get("downloads", "")
            )
            row["sukebei_url"] = torrent.get("url", "")
            row["source"] = torrent.get("source", "sukebei")
            row["search_status"] = "found"
            found += 1
            print(f"  OK [{row['source']}]: {row['torrent_name'][:80]}")
        else:
            row["magnet"] = ""
            row["torrent_name"] = ""
            row["release_type"] = ""
            row["seeders"] = ""
            row["leechers"] = ""
            row["downloads"] = ""
            row["sukebei_url"] = ""
            row["source"] = ""
            row["search_status"] = "not_found"
            not_found += 1
            print("  Not found on Sukebei")

        rows[index] = row
        write_rows(output_path, fieldnames, rows)
        if markdown_path and (offset + 1) % 25 == 0:
            write_markdown_report(markdown_path, rows)

    if markdown_path:
        write_markdown_report(markdown_path, rows)
        print(f"Markdown updated: {markdown_path}")

    print()
    print(f"Done. Output: {output_path}")
    print(f"  Found: {found}  Not found: {not_found}  Skipped: {skipped}")


def _apply_torrent_to_row(row: Dict[str, str], code: str, torrent: Optional[Dict]) -> None:
    if torrent:
        row["magnet"] = torrent.get("magnet", "")
        row["torrent_name"] = torrent.get("name", "")
        row["release_type"] = censorship_label(torrent.get("name", ""), code)
        row["seeders"] = str(torrent.get("seeders", ""))
        row["leechers"] = str(torrent.get("leechers", ""))
        row["downloads"] = str(
            torrent.get("completed_downloads") or torrent.get("downloads", "")
        )
        row["sukebei_url"] = torrent.get("url", "")
        row["source"] = torrent.get("source", "sukebei")
        row["search_status"] = "found"
    else:
        row["magnet"] = ""
        row["torrent_name"] = ""
        row["release_type"] = ""
        row["seeders"] = ""
        row["leechers"] = ""
        row["downloads"] = ""
        row["sukebei_url"] = ""
        row["source"] = ""
        row["search_status"] = "not_found"


def process_csv_parallel(
    input_path: str,
    output_path: str,
    *,
    delay: float,
    resume: bool,
    reacquire: bool,
    workers: int,
    exclude_codes: Optional[set] = None,
    markdown_path: Optional[str] = None,
    md_title: Optional[str] = None,
    product_codes: Optional[List[str]] = None,
) -> None:
    """Search with multiple workers; global rate limit between HTTP requests."""
    if product_codes is None:
        fieldnames, rows = load_rows(input_path)
    else:
        fieldnames, rows = rows_from_product_codes(product_codes)
    exclude_codes = exclude_codes or set()
    extra_cols = [
        "magnet",
        "torrent_name",
        "release_type",
        "seeders",
        "leechers",
        "downloads",
        "sukebei_url",
        "source",
        "search_status",
    ]
    for col in extra_cols:
        if col not in fieldnames:
            fieldnames.append(col)

    if resume and os.path.isfile(output_path):
        _, existing = load_rows(output_path)
        by_code = {
            normalize_product_code(r.get(CODE_COLUMN, "")): r
            for r in existing
            if r.get(CODE_COLUMN)
        }
        for i, row in enumerate(rows):
            code = normalize_product_code(row.get(CODE_COLUMN, ""))
            if code in by_code and (by_code[code].get("search_status") or "").strip():
                rows[i] = {**row, **by_code[code]}

    for index, row in enumerate(rows):
        code = (row.get(CODE_COLUMN) or "").strip()
        if code and normalize_product_code(code) in exclude_codes:
            row["search_status"] = "skipped_excluded"
            row["magnet"] = ""
            row["torrent_name"] = ""
            row["release_type"] = ""
            row["seeders"] = ""
            row["leechers"] = ""
            row["downloads"] = ""
            row["sukebei_url"] = ""
            row["source"] = ""
            rows[index] = row
    if exclude_codes:
        write_rows(output_path, fieldnames, rows)

    pending: List[Tuple[int, str]] = []
    for index, row in enumerate(rows):
        code = (row.get(CODE_COLUMN) or "").strip()
        if not code:
            row["search_status"] = "skipped_empty_code"
            continue
        if normalize_product_code(code) in exclude_codes:
            continue
        if resume and not reacquire and row.get("search_status") in ("found", "not_found"):
            if row.get("search_status") == "found" and not row.get("magnet"):
                pass
            else:
                continue
        pending.append((index, code))

    write_lock = threading.Lock()
    rate_lock = threading.Lock()
    last_request = [0.0]

    def search_one(item: Tuple[int, str]) -> Tuple[int, str, Optional[Dict], Optional[str]]:
        index, code = item
        try:
            with rate_lock:
                wait = delay - (time.time() - last_request[0])
                if wait > 0:
                    time.sleep(wait)
                last_request[0] = time.time()
            torrent = find_magnet_for_code(code, delay_seconds=0)
            return index, code, torrent, None
        except Exception as exc:
            return index, code, None, str(exc)

    found = not_found = errors = 0
    total = len(rows)
    done = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(search_one, item): item for item in pending}
        for fut in as_completed(futures):
            index, code, torrent, err = fut.result()
            done += 1
            row = dict(rows[index])
            if err:
                row["search_status"] = f"error: {err}"
                errors += 1
                print(f"[{done}/{len(pending)}] {code} error: {err}")
            elif torrent:
                _apply_torrent_to_row(row, code, torrent)
                found += 1
                print(
                    f"[{done}/{len(pending)}] {code} OK [{row.get('source', '')}]: "
                    f"{row['torrent_name'][:70]}"
                )
            else:
                _apply_torrent_to_row(row, code, None)
                not_found += 1
                print(f"[{done}/{len(pending)}] {code} not found")

            with write_lock:
                rows[index] = row
                write_rows(output_path, fieldnames, rows)
                if markdown_path and done % 10 == 0:
                    write_markdown_report(markdown_path, rows, title=md_title)

    if markdown_path:
        write_markdown_report(markdown_path, rows, title=md_title)
        print(f"Markdown updated: {markdown_path}")

    print()
    print(f"Done (parallel x{workers}). Output: {output_path}")
    print(f"  Found: {found}  Not found: {not_found}  Errors: {errors}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Search Sukebei magnet links for product codes from a CSV "
            "(品番 column) or a direct code list."
        )
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        help=(
            "Input CSV path (must have a 品番 column), or product codes like "
            "'DEVR-011-2D,CRZU-033,VRKM-1399'"
        ),
    )
    parser.add_argument(
        "--codes",
        help="Product codes to search directly (comma/newline/space-separated)",
    )
    parser.add_argument(
        "-o",
        "--output",
        help=(
            "Output CSV path (default: <input>_magnets.csv for CSV input, "
            f"or {DIRECT_CODES_OUTPUT} for direct codes)"
        ),
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY,
        help=f"Seconds between searches (default: {DEFAULT_DELAY})",
    )
    parser.add_argument(
        "--start",
        type=int,
        default=0,
        help="Skip first N data rows (0-based, after header)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most N rows (for testing)",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Do not load prior results from output CSV",
    )
    parser.add_argument(
        "--reacquire",
        action="store_true",
        help="Re-search all rows (refresh magnets with preferred uncen/RM/-u priority)",
    )
    parser.add_argument(
        "--markdown",
        metavar="PATH",
        help="Write/update markdown report after processing (full list from output CSV)",
    )
    parser.add_argument(
        "--markdown-only",
        metavar="PATH",
        help="Regenerate markdown from output CSV without searching",
    )
    parser.add_argument(
        "--md-title",
        help="Markdown report title (first heading line)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Parallel search workers (default: 1 = sequential)",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Product codes to skip (comma-separated or repeat flag)",
    )
    parser.add_argument(
        "--exclude-file",
        help="File with product codes to skip (one per line or comma-separated)",
    )
    args = parser.parse_args()

    product_codes: Optional[List[str]] = None
    input_path: Optional[str] = None

    if args.codes:
        product_codes = parse_product_codes(args.codes)
        if not product_codes:
            parser.error("--codes did not contain any product codes")
    else:
        if not args.inputs:
            parser.error("provide an input CSV path or product codes")
        raw_input = " ".join(args.inputs).strip()
        candidate_path = os.path.abspath(raw_input)
        if os.path.isfile(candidate_path):
            input_path = candidate_path
        elif looks_like_product_codes(raw_input):
            product_codes = parse_product_codes(raw_input)
        else:
            print(f"File not found: {candidate_path}", file=sys.stderr)
            print(
                "Or pass product codes like: "
                "DEVR-011-2D,CRZU-033,VRKM-1399",
                file=sys.stderr,
            )
            sys.exit(1)

    exclude_values = list(args.exclude)
    if args.exclude_file:
        with open(args.exclude_file, encoding="utf-8") as f:
            exclude_values.append(f.read())
    exclude_codes = parse_exclude_codes(exclude_values)
    if exclude_codes:
        print(f"Excluding {len(exclude_codes)} product codes from search.")

    if args.output:
        output_path = os.path.abspath(args.output)
    elif input_path:
        base, ext = os.path.splitext(input_path)
        output_path = f"{base}_magnets{ext or '.csv'}"
    else:
        output_path = os.path.abspath(DIRECT_CODES_OUTPUT)

    md_path = os.path.abspath(args.markdown) if args.markdown else None

    if args.markdown_only:
        if not os.path.isfile(output_path):
            print(f"Output CSV not found: {output_path}", file=sys.stderr)
            sys.exit(1)
        _, rows = load_rows(output_path)
        write_markdown_report(
            os.path.abspath(args.markdown_only), rows, title=args.md_title
        )
        stats = _stats(rows)
        print(f"Markdown: {args.markdown_only}")
        print(f"  {stats}")
        return

    if args.workers > 1 and (args.start or args.limit):
        print("Warning: --start/--limit ignored when using --workers > 1", file=sys.stderr)

    if args.workers > 1:
        process_csv_parallel(
            input_path or "",
            output_path,
            delay=args.delay,
            resume=not args.no_resume,
            reacquire=args.reacquire,
            workers=args.workers,
            exclude_codes=exclude_codes,
            markdown_path=md_path,
            md_title=args.md_title,
            product_codes=product_codes,
        )
    else:
        process_csv(
            input_path or "",
            output_path,
            delay=args.delay,
            start=args.start,
            limit=args.limit,
            resume=not args.no_resume,
            reacquire=args.reacquire,
            exclude_codes=exclude_codes,
            markdown_path=md_path,
            product_codes=product_codes,
        )
        if md_path and args.md_title:
            _, rows = load_rows(output_path)
            write_markdown_report(md_path, rows, title=args.md_title)


if __name__ == "__main__":
    main()
