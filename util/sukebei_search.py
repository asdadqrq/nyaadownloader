import re
import time
from typing import Dict, List, Optional, Tuple

import requests
from NyaaPy.sukebei import SukebeiNyaa

# ---------------------------------------------------------------------------
# bt4g fallback (DISABLED)
# ---------------------------------------------------------------------------
# We tried bt4gprx.com RSS when sukebei had no match or only "standard"
# releases. It was turned off: slow for large CSVs and often no better hit.
# Implementation kept in util/bt4g_search.py and _pick_best_bt4g() below.
# Set _BT4G_FALLBACK_ENABLED = True to turn the fallback back on.
# ---------------------------------------------------------------------------
_BT4G_FALLBACK_ENABLED = False

_MAX_RETRIES = 5
_RETRY_BASE_DELAY = 5.0

# Real Life → Videos on sukebei.nyaa.si
_CATEGORY = 2
_SUBCATEGORY = 2

_client: Optional[SukebeiNyaa] = None

_UNCENSORED = (
    r"無修正",
    r"无修正",
    r"uncensored",
    r"uncens",
    r"\buncen\b",
    r"ノーモザイク",
    r"nomosaic",
)
_REDUCING_MOSAIC = (
    r"reducing\s*mosaic",
    r"\[rm\]",
    r"\brm版\b",
)
_LEAK = (r"破壊", r"破坏", r"流出", r"\bleak\b")


def _client_instance() -> SukebeiNyaa:
    global _client
    if _client is None:
        _client = SukebeiNyaa()
    return _client


def normalize_product_code(code: str) -> str:
    return code.strip().upper()


def _code_in_title(code: str, title: str) -> bool:
    """Match product code in torrent title (case-insensitive)."""
    code = normalize_product_code(code)
    if not code:
        return False
    pattern = re.escape(code).replace(r"\-", r"[-\s]?")
    return re.search(pattern, title, re.IGNORECASE) is not None


def _has_u_variant(product_code: str, title: str) -> bool:
    """Match uncensored-style -u suffix on the product code (e.g. REAL-981-u)."""
    code = normalize_product_code(product_code)
    if not code:
        return False
    base = re.escape(code).replace(r"\-", r"[-\s]?")
    patterns = (
        rf"{base}[\s_-]*u\b",
        rf"{base}[\s_-]*u[\s_\[\(]",
        rf"\[u\][\s_-]*{base}",
        rf"\(u\)[\s_-]*{base}",
    )
    return any(re.search(p, title, re.IGNORECASE) for p in patterns)


def preferred_flags(product_code: str, title: str) -> Tuple[bool, bool, bool]:
    """Return (uncensored, reducing_mosaic, u_variant) for a torrent title."""
    uncensored = any(re.search(p, title, re.IGNORECASE) for p in _UNCENSORED)
    reducing = any(re.search(p, title, re.IGNORECASE) for p in _REDUCING_MOSAIC)
    u_variant = _has_u_variant(product_code, title)
    return uncensored, reducing, u_variant


def censorship_label(title: str, product_code: str = "") -> str:
    """Classify release type; preferred types are listed first."""
    uncensored, reducing, u_variant = preferred_flags(product_code, title)
    tags = []
    if uncensored:
        tags.append("uncensored")
    if reducing:
        tags.append("reducing_mosaic")
    if u_variant:
        tags.append("-u")
    if tags:
        return "+".join(tags)
    for pat in _LEAK:
        if re.search(pat, title, re.IGNORECASE):
            return "leak_or_broken_mosaic"
    return "standard"


def is_preferred_title(product_code: str, title: str) -> bool:
    """True when the title looks like an uncensored / RM / -u release."""
    uncensored, reducing, u_variant = preferred_flags(product_code, title)
    return uncensored or reducing or u_variant


def _match_score(title: str, seeders: int, product_code: str) -> int:
    """Higher is better: preferred (uncen / RM / -u) >> standard >> plain leak-only."""
    uncensored, reducing, u_variant = preferred_flags(product_code, title)
    preferred_count = sum((uncensored, reducing, u_variant))
    if preferred_count:
        # More preferred markers beat fewer; then seeders within same tier.
        return 3_000_000 + preferred_count * 100_000 + seeders
    if any(re.search(p, title, re.IGNORECASE) for p in _LEAK):
        return 1_000_000 + seeders
    return seeders


def search_by_product_code(
    product_code: str,
    *,
    filters: int = 0,
    delay_seconds: float = 0,
) -> List[Dict]:
    """Search sukebei.nyaa.si for torrents matching a JAV product code."""
    code = normalize_product_code(product_code)
    if not code:
        return []

    if delay_seconds > 0:
        time.sleep(delay_seconds)

    last_error: Optional[Exception] = None
    for attempt in range(_MAX_RETRIES):
        try:
            return _client_instance().search(
                code,
                category=_CATEGORY,
                subcategory=_SUBCATEGORY,
                filters=filters,
            )
        except (requests.RequestException, ConnectionError, OSError) as exc:
            last_error = exc
            wait = _RETRY_BASE_DELAY * (attempt + 1)
            time.sleep(wait)
    if last_error:
        raise last_error
    return []


def pick_best_match(product_code: str, torrents: List[Dict]) -> Optional[Dict]:
    """Prefer uncensored / Reducing Mosaic / -u, then seeders."""
    matches = [t for t in torrents if _code_in_title(product_code, t.get("name", ""))]
    if not matches:
        return None
    return max(
        matches,
        key=lambda t: _match_score(
            t.get("name", ""),
            int(t.get("seeders") or 0),
            product_code,
        ),
    )


def _pick_best_bt4g(product_code: str, torrents: List[Dict]) -> Optional[Dict]:
    """Pick best bt4g result (unused while _BT4G_FALLBACK_ENABLED is False).

    Title must contain the product code; prefer uncen/-u, then file size.
    """
    matches = [t for t in torrents if _code_in_title(product_code, t.get("name", ""))]
    if not matches:
        return None

    def score(t: Dict) -> Tuple[int, int]:
        preferred = 1 if is_preferred_title(product_code, t.get("name", "")) else 0
        return preferred, int(t.get("size_bytes") or 0)

    return max(matches, key=score)


def find_magnet_for_code(
    product_code: str,
    *,
    delay_seconds: float = 0,
    bt4g_fallback: Optional[bool] = None,
) -> Optional[Dict]:
    """Return the best sukebei torrent dict (includes magnet) or None.

    Only searches sukebei.nyaa.si unless bt4g fallback is enabled (see
    _BT4G_FALLBACK_ENABLED). Results include ``source`` (``"sukebei"``).
    """
    if bt4g_fallback is None:
        bt4g_fallback = _BT4G_FALLBACK_ENABLED

    torrents = search_by_product_code(product_code, delay_seconds=delay_seconds)
    sukebei_best = pick_best_match(product_code, torrents)
    if sukebei_best is not None:
        sukebei_best.setdefault("source", "sukebei")

    if not bt4g_fallback:
        return sukebei_best

    # --- bt4g fallback (inactive unless _BT4G_FALLBACK_ENABLED / bt4g_fallback) ---
    sukebei_is_preferred = sukebei_best is not None and is_preferred_title(
        product_code, sukebei_best.get("name", "")
    )
    if sukebei_is_preferred:
        return sukebei_best

    from util.bt4g_search import search_bt4g  # lazy import; optional path

    try:
        bt4g_results = search_bt4g(product_code)
    except Exception:
        return sukebei_best

    bt4g_best = _pick_best_bt4g(product_code, bt4g_results)
    if bt4g_best is None:
        return sukebei_best

    bt4g_is_preferred = is_preferred_title(product_code, bt4g_best.get("name", ""))

    if sukebei_best is None:
        return bt4g_best
    if bt4g_is_preferred and not sukebei_is_preferred:
        return bt4g_best
    return sukebei_best
