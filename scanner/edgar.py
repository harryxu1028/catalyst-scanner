"""Minimal, polite SEC EDGAR client.

SEC fair-access rules: <=10 req/sec and a descriptive User-Agent that includes
contact info. Violating either gets your IP blocked, so both are enforced here.
"""
from __future__ import annotations

import html
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, timedelta

import requests

ARCHIVES = "https://www.sec.gov/Archives"
DAILY_INDEX = "https://www.sec.gov/Archives/edgar/daily-index"
COMPANY_TICKERS = "https://www.sec.gov/files/company_tickers.json"


class _Throttle:
    """Global token-bucket-ish limiter. Default 7 rps leaves headroom under 10."""

    def __init__(self, rate: float = 7.0):
        self._interval = 1.0 / rate
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            gap = self._interval - (time.monotonic() - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.monotonic()


_throttle = _Throttle()
_session = requests.Session()


def set_user_agent(ua: str) -> None:
    _session.headers.update(
        {"User-Agent": ua, "Accept-Encoding": "gzip, deflate", "Connection": "keep-alive"}
    )


set_user_agent(os.environ.get("SEC_UA", "catalyst-scanner contact@example.com"))


def get(url: str, max_bytes: int = 4_000_000, retries: int = 3) -> str | None:
    """Fetch a URL, streaming and truncating at max_bytes.

    Complete-submission .txt files can carry huge uuencoded exhibits (charts,
    logos). Everything we parse lives in the first couple of MB, so we cap.
    """
    for attempt in range(retries):
        _throttle.wait()
        try:
            r = _session.get(url, stream=True, timeout=30)
            if r.status_code == 404:
                return None
            if r.status_code in (403, 429, 500, 502, 503, 504):
                time.sleep(2 ** attempt + 0.5)
                continue
            r.raise_for_status()
            buf, total = [], 0
            for chunk in r.iter_content(65536):
                buf.append(chunk)
                total += len(chunk)
                if total >= max_bytes:
                    break
            r.close()
            return b"".join(buf).decode("utf-8", errors="replace")
        except requests.RequestException:
            time.sleep(2 ** attempt + 0.5)
    return None


def get_json(url: str):
    txt = get(url)
    if not txt:
        return None
    import json

    try:
        return json.loads(txt)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Daily index
# --------------------------------------------------------------------------

@dataclass
class Filing:
    cik: str            # zero-padded 10-digit, as listed in the index row
    company: str        # name attached to THAT cik (issuer or filer)
    form: str
    filed: str          # YYYY-MM-DD
    path: str           # edgar/data/.../0001234567-26-000001.txt
    accession: str

    @property
    def txt_url(self) -> str:
        return f"https://www.sec.gov/Archives/{self.path}"

    @property
    def index_url(self) -> str:
        acc_nodash = self.accession.replace("-", "")
        cik_int = str(int(self.cik))
        return (
            f"https://www.sec.gov/Archives/edgar/data/{cik_int}/"
            f"{acc_nodash}/{self.accession}-index.htm"
        )


_ACC_RE = re.compile(r"(\d{10}-\d{2}-\d{6})")


def daily_index(day: date) -> list[Filing]:
    """Every filing disseminated on `day`. One request covers the whole market.

    Returns [] on weekends/holidays (the index file simply won't exist).
    """
    qtr = (day.month - 1) // 3 + 1
    url = f"{DAILY_INDEX}/{day.year}/QTR{qtr}/master.{day:%Y%m%d}.idx"
    raw = get(url, max_bytes=40_000_000)
    if not raw:
        return []

    out: list[Filing] = []
    for line in raw.splitlines():
        if line.count("|") != 4:
            continue
        cik, name, form, filed, path = (p.strip() for p in line.split("|"))
        if not cik.isdigit():
            continue  # header row
        m = _ACC_RE.search(path)
        if not m:
            continue
        filed_iso = f"{filed[:4]}-{filed[4:6]}-{filed[6:8]}" if len(filed) == 8 else filed
        out.append(
            Filing(
                cik=cik.zfill(10),
                company=name,
                form=form.upper(),
                filed=filed_iso,
                path=path,
                accession=m.group(1),
            )
        )
    return out


def index_range(start: date, end: date) -> list[Filing]:
    """Daily index across an inclusive calendar-date range."""
    filings: list[Filing] = []
    d = start
    while d <= end:
        filings.extend(daily_index(d))
        d += timedelta(days=1)
    return filings


def ticker_cik_map() -> dict[str, str]:
    """ticker -> zero-padded CIK, straight from SEC."""
    data = get_json(COMPANY_TICKERS) or {}
    out = {}
    for row in data.values():
        out[str(row["ticker"]).upper()] = str(row["cik_str"]).zfill(10)
    return out


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]{1,2000}>", re.S)
_WS_RE = re.compile(r"[ \t\r\f\v\xa0]+")


def visible_text(raw: str, strip_header: bool = True) -> str:
    """Crude but sturdy HTML/SGML -> text. Good enough for keyword work.

    The SGML header repeats item descriptions verbatim, which pollutes any
    snippet we quote back, so it is dropped by default.
    """
    if strip_header:
        end = raw.rfind("</SEC-HEADER>")
        if end == -1:
            end = raw.rfind("</IMS-HEADER>")
        if end != -1:
            raw = raw[end + 13:]
    txt = re.sub(r"<(script|style)\b.*?</\1>", " ", raw, flags=re.S | re.I)
    txt = re.sub(r"<(?:br|p|div|tr|/tr|li)[^>]*>", "\n", txt, flags=re.I)
    txt = _TAG_RE.sub(" ", txt)
    txt = html.unescape(txt)
    txt = _WS_RE.sub(" ", txt)
    return re.sub(r"\n\s*\n+", "\n", txt)


_ITEM_NUM_RE = re.compile(r"<ITEMS>\s*([0-9]\.[0-9]{2})", re.I)
_ITEM_TXT_RE = re.compile(r"^ITEM INFORMATION:\s*(.+)$", re.I | re.M)

# 8-K item descriptions as they appear in the SGML header, mapped to numbers.
_ITEM_DESC = {
    "entry into a material definitive agreement": "1.01",
    "termination of a material definitive agreement": "1.02",
    "completion of acquisition or disposition of assets": "2.01",
    "results of operations and financial condition": "2.02",
    "creation of a direct financial obligation": "2.03",
    "costs associated with exit or disposal activities": "2.05",
    "material impairments": "2.06",
    "notice of delisting or failure to satisfy": "3.01",
    "unregistered sales of equity securities": "3.02",
    "changes in registrant's certifying accountant": "4.01",
    "non-reliance on previously issued financial statements": "4.02",
    "changes in control of registrant": "5.01",
    "departure of directors or certain officers": "5.02",
    "amendments to articles of incorporation": "5.03",
    "submission of matters to a vote of security holders": "5.07",
    "regulation fd disclosure": "7.01",
    "other events": "8.01",
    "financial statements and exhibits": "9.01",
}


def header_items(raw: str) -> set[str]:
    """8-K item numbers, read from the filing's SGML header.

    More reliable than regexing the body, which is full of stray 'Item' strings.
    """
    items = set(_ITEM_NUM_RE.findall(raw[:20000]))
    for desc in _ITEM_TXT_RE.findall(raw[:20000]):
        d = desc.strip().lower()
        for key, num in _ITEM_DESC.items():
            if d.startswith(key[:40]):
                items.add(num)
                break
    return items
