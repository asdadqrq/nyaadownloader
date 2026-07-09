"""bt4g (bt4gprx.com) fallback search — DISABLED, kept for reference.

This module is not used in normal runs. ``util.sukebei_search`` sets
``_BT4G_FALLBACK_ENABLED = False`` so only sukebei.nyaa.si is queried.

To experiment again: set that flag to True, or call ``search_bt4g()`` directly.

RSS endpoint (no Cloudflare on RSS, unlike the HTML search page):

    https://bt4gprx.com/search?q=MOGI-013&page=rss

Each <item> has a direct ``magnet:?...`` link. When enabled, sukebei_search
used this when nyaa had no match or only a censored ("standard") release.
"""

from __future__ import annotations

import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional

import requests

_RSS_URL = "https://bt4gprx.com/search"
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_TIMEOUT = 30
_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 4.0

_SIZE_RE = re.compile(r"([\d.]+)\s*(KB|MB|GB|TB)", re.IGNORECASE)
_HASH_RE = re.compile(r"\b([a-f0-9]{40})\b", re.IGNORECASE)
_SIZE_MULT = {"KB": 1 << 10, "MB": 1 << 20, "GB": 1 << 30, "TB": 1 << 40}


def _parse_size_bytes(description: str) -> int:
    if not description:
        return 0
    m = _SIZE_RE.search(description)
    if not m:
        return 0
    try:
        value = float(m.group(1))
    except ValueError:
        return 0
    return int(value * _SIZE_MULT.get(m.group(2).upper(), 0))


def _parse_info_hash(description: str, magnet: str) -> str:
    if magnet:
        m = re.search(r"urn:btih:([a-f0-9]{40})", magnet, re.IGNORECASE)
        if m:
            return m.group(1).lower()
    m = _HASH_RE.search(description or "")
    return m.group(1).lower() if m else ""


def _fetch_rss(product_code: str) -> str:
    params = {"q": product_code, "page": "rss"}
    url = f"{_RSS_URL}?{urllib.parse.urlencode(params)}"
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    last_error: Optional[Exception] = None
    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.get(url, headers=headers, timeout=_TIMEOUT)
            if resp.status_code == 200 and resp.text:
                return resp.text
            last_error = RuntimeError(f"HTTP {resp.status_code} from bt4g")
        except (requests.RequestException, ConnectionError, OSError) as exc:
            last_error = exc
        time.sleep(_RETRY_BASE_DELAY * (attempt + 1))
    if last_error:
        raise last_error
    return ""


def search_bt4g(product_code: str, *, delay_seconds: float = 0) -> List[Dict]:
    """Search bt4gprx.com for a product code, returning torrent-shaped dicts.

    Each dict contains: ``name``, ``magnet``, ``url`` (bt4g detail page),
    ``size_bytes``, ``info_hash``, ``pub_date``, ``seeders``, ``leechers``,
    ``downloads``, ``source`` (= ``"bt4g"``).
    """
    code = (product_code or "").strip()
    if not code:
        return []
    if delay_seconds > 0:
        time.sleep(delay_seconds)

    xml_text = _fetch_rss(code)
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    results: List[Dict] = []
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        magnet = (item.findtext("link") or "").strip()
        detail = (item.findtext("guid") or "").strip()
        description = (item.findtext("description") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        if not magnet.startswith("magnet:"):
            continue
        results.append(
            {
                "name": title,
                "magnet": magnet,
                "url": detail,
                "size_bytes": _parse_size_bytes(description),
                "info_hash": _parse_info_hash(description, magnet),
                "pub_date": pub_date,
                "seeders": 0,
                "leechers": 0,
                "downloads": 0,
                "source": "bt4g",
            }
        )
    return results
