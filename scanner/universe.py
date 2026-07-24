"""Build the subsector universe.

yfinance supplies the classification (sector / industry / market cap); SEC's
company_tickers.json supplies the CIK that everything downstream joins on.

This is slow (one yfinance call per ticker), so it's cached to disk and run on
its own weekly schedule, not in the daily job.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from . import edgar

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
UNIVERSE_PATH = DATA_DIR / "universe.json"


def norm(s: str) -> str:
    """Fold punctuation so 'Software - Application' == 'Software—Application'."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def load_universe() -> dict[str, dict]:
    if not UNIVERSE_PATH.exists():
        raise SystemExit(
            f"No universe at {UNIVERSE_PATH}. Run:  python -m scanner.universe --build"
        )
    with open(UNIVERSE_PATH) as fh:
        return json.load(fh)


def by_cik(universe: dict[str, dict]) -> dict[str, dict]:
    return {row["cik"]: {**row, "ticker": t} for t, row in universe.items() if row.get("cik")}


def build(cfg: dict, candidates: list[str] | None = None) -> dict[str, dict]:
    import yfinance as yf

    ucfg = cfg["universe"]
    want_ind = [norm(x) for x in ucfg.get("industries", [])]
    want_sec = [norm(x) for x in ucfg.get("sectors", [])]
    lo = ucfg.get("market_cap_min", 0)
    hi = ucfg.get("market_cap_max", 1e15)
    exclude = {t.upper() for t in ucfg.get("exclude_tickers", [])}

    cik_map = edgar.ticker_cik_map()
    if candidates is None:
        candidates = sorted(cik_map)          # every SEC registrant with a ticker
    candidates = [t for t in candidates if t.upper() not in exclude]

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = DATA_DIR / "_yf_cache.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}

    out: dict[str, dict] = {}
    for i, t in enumerate(candidates, 1):
        t = t.upper()
        if t not in cache:
            try:
                info = yf.Ticker(t).get_info() or {}
                cache[t] = {
                    "name": info.get("shortName") or info.get("longName") or "",
                    "sector": info.get("sector") or "",
                    "industry": info.get("industry") or "",
                    "market_cap": info.get("marketCap") or 0,
                    "exchange": info.get("exchange") or "",
                }
            except Exception:
                cache[t] = {"name": "", "sector": "", "industry": "", "market_cap": 0,
                            "exchange": ""}
            time.sleep(0.25)
        if i % 200 == 0:
            cache_path.write_text(json.dumps(cache))
            print(f"  ...{i}/{len(candidates)} screened, {len(out)} kept")

        row = cache[t]
        mc = row.get("market_cap") or 0
        if not (lo <= mc <= hi):
            continue
        ind, sec = norm(row.get("industry")), norm(row.get("sector"))
        if want_ind and not any(w in ind or ind in w for w in want_ind if w):
            if not (want_sec and any(w == sec for w in want_sec)):
                continue
        cik = cik_map.get(t)
        if not cik:
            continue
        out[t] = {**row, "cik": cik}

    for t in ucfg.get("extra_tickers", []):
        t = t.upper()
        if t in out or t not in cik_map:
            continue
        out[t] = {"name": t, "sector": "", "industry": "(manual add)",
                  "market_cap": 0, "exchange": "", "cik": cik_map[t]}

    cache_path.write_text(json.dumps(cache))
    UNIVERSE_PATH.write_text(json.dumps(out, indent=1, sort_keys=True))
    return out


def known_industries() -> dict[str, set[str]]:
    """Canonical Yahoo taxonomy, straight from the installed yfinance."""
    from yfinance.const import SECTOR_INDUSTY_MAPPING  # note: library typo

    return SECTOR_INDUSTY_MAPPING


def validate(cfg: dict) -> list[str]:
    """Return config industry/sector strings that don't exist in the taxonomy.

    Worth running before a build — an unrecognised string silently matches
    nothing, and you don't find out until an hour of screening returns 0 names.
    """
    import itertools

    mapping = known_industries()
    all_ind = {norm(i): i for i in itertools.chain(*mapping.values())}
    all_sec = {norm(s): s for s in mapping}
    problems = []
    for i in cfg["universe"].get("industries", []):
        if norm(i) not in all_ind:
            problems.append(f"industry: {i!r}")
    for s in cfg["universe"].get("sectors", []):
        if norm(s) not in all_sec:
            problems.append(f"sector: {s!r}")
    return problems


if __name__ == "__main__":
    import argparse

    import yaml

    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--validate", action="store_true",
                    help="Check config industry/sector names against the taxonomy")
    ap.add_argument("--list-industries", metavar="SECTOR", nargs="?", const="*",
                    help="Print available industries (optionally for one sector)")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--tickers-file", help="Optional newline-delimited candidate tickers "
                                           "(much faster than screening all ~10k registrants)")
    a = ap.parse_args()

    if a.list_industries:
        mapping = known_industries()
        for sec in sorted(mapping):
            if a.list_industries != "*" and norm(a.list_industries) != norm(sec):
                continue
            print(f"\n{sec}")
            for ind in sorted(mapping[sec]):
                print(f"  {ind}")
        raise SystemExit(0)

    cfg = yaml.safe_load(open(a.config))

    if a.validate or a.build:
        bad = validate(cfg)
        if bad:
            print("Unrecognised names in config.yaml:")
            for b in bad:
                print("   " + b)
            print("\nSee SECTORS.md or run --list-industries")
            raise SystemExit(1)
        print(f"Config OK: {len(cfg['universe'].get('industries', []))} industries, "
              f"{len(cfg['universe'].get('sectors', []))} sectors")
        if not a.build:
            raise SystemExit(0)

    edgar.set_user_agent(os.environ.get("SEC_UA", cfg.get("sec_user_agent", "")))
    cands = None
    if a.tickers_file:
        cands = [l.strip() for l in open(a.tickers_file) if l.strip()]
    u = build(cfg, cands)
    print(f"Universe: {len(u)} names -> {UNIVERSE_PATH}")
