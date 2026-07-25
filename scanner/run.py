"""Daily catalyst scan.

  python -m scanner.run                 # scan lookback window, email results
  python -m scanner.run --dry-run       # write out/preview.html instead
  python -m scanner.run --date 2026-07-22 --days 1
"""
from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

from . import edgar, signals as sig, universe as uni
from .mailer import plain, render, send

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state" / "seen.json"

WATCHED_FORMS = {"8-K", "8-K/A", "4", "4/A"} | sig.ACTIVIST_FORMS
# Form 4 bodies are tiny; 8-K/13D bodies can carry fat exhibits.
FETCH_CAP = {"4": 400_000, "4/A": 400_000}


def load_seen() -> set[str]:
    if STATE.exists():
        try:
            return set(json.loads(STATE.read_text()).get("keys", []))
        except ValueError:
            pass
    return set()


def save_seen(keys: set[str], keep: int = 6000) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"keys": sorted(keys)[-keep:]}))


def fetch_moves(tickers: list[str]) -> dict[str, float]:
    """% change of the most recent completed session, per ticker.

    One batched yfinance download for only the surfaced names. Any failure
    returns partial/empty results rather than blocking the email.
    """
    if not tickers:
        return {}
    out: dict[str, float] = {}
    try:
        import yfinance as yf

        px = yf.download(tickers, period="7d", interval="1d",
                         progress=False, auto_adjust=True, group_by="ticker",
                         threads=True)
        for t in tickers:
            try:
                closes = (px[t]["Close"] if len(tickers) > 1 else px["Close"]).dropna()
                if len(closes) >= 2:
                    out[t] = float(closes.iloc[-1] / closes.iloc[-2] - 1.0)
            except (KeyError, IndexError, TypeError):
                continue
    except Exception as exc:                      # noqa: BLE001 - never block the email
        print(f"move fetch failed: {exc}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--date", help="End date YYYY-MM-DD (default: today)")
    ap.add_argument("--days", type=int, help="Calendar days to look back")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-state", action="store_true", help="Ignore the dedupe file")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(args.config))
    edgar.set_user_agent(os.environ.get("SEC_UA") or cfg.get("sec_user_agent") or "")

    end = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else date.today()
    days = args.days if args.days is not None else cfg.get("lookback_days", 4)
    # Monday's pre-open run must reach back to Friday (weekday 0 -> +2 days);
    # same for a run landing on Sunday/Saturday after a holiday weekend.
    if args.days is None and end.weekday() == 0:
        days += 2
    start = end - timedelta(days=days - 1)

    universe = uni.load_universe()
    cik_index = uni.by_cik(universe)
    print(f"Universe: {len(cik_index)} names with CIKs")

    filings = edgar.index_range(start, end)
    print(f"EDGAR daily index {start}..{end}: {len(filings):,} filings")

    # Group by accession so we can name the counterparty on a 13D/Form 4
    # (the index lists a row per filer AND per subject company).
    by_acc: dict[str, list] = defaultdict(list)
    for f in filings:
        by_acc[f.accession].append(f)

    targets = []
    for f in filings:
        if f.form not in WATCHED_FORMS:
            continue
        if f.cik not in cik_index:
            continue          # keep only rows keyed to a covered issuer
        targets.append(f)

    seen = set() if args.no_state else load_seen()
    scfg = cfg.get("signals", {})
    activists = cfg.get("activists", [])
    found: list[sig.Signal] = []

    print(f"Fetching {len(targets)} in-universe filings...")
    for f in targets:
        meta = cik_index[f.cik]
        ticker = meta["ticker"]
        raw = edgar.get(f.txt_url, max_bytes=FETCH_CAP.get(f.form, 4_000_000))
        if not raw:
            continue

        counterparty = ""
        for other in by_acc.get(f.accession, []):
            if other.cik != f.cik:
                counterparty = other.company
                break

        hits: list[sig.Signal] = []
        hits += sig.officer_change(f, raw, ticker)
        hits += sig.buyback(f, raw, ticker, meta.get("market_cap"))
        hits += sig.insider(f, raw, ticker, scfg)
        hits += sig.activist(f, raw, ticker, activists, counterparty)

        for h in hits:
            if h.dedupe_key in seen:
                continue
            seen.add(h.dedupe_key)
            found.append(h)

    # Cluster bonus: 2+ distinct insiders buying the same name in the window.
    buyers = defaultdict(set)
    for s in found:
        if s.kind == "insider_buy":
            buyers[s.ticker].add(s.headline.split(" bought")[0])
    for s in found:
        if s.kind == "insider_buy" and len(buyers[s.ticker]) >= 2:
            s.score += 35
            s.tags = [t for t in s.tags if t != "cluster-candidate"]
            s.tags.insert(0, f"CLUSTER: {len(buyers[s.ticker])} buyers")

    window = f"{start:%b %d}–{end:%b %d, %Y}" if start != end else f"{end:%b %d, %Y}"
    moves = fetch_moves(sorted({s.ticker for s in found})) if found else {}
    html_body = render(found, window, len(cik_index), moves)
    text_body = plain(found, moves)
    counts = defaultdict(int)
    for s in found:
        counts[s.kind] += 1
    subject = (
        f"Catalysts {end:%m/%d} — "
        f"{counts['officer']} exec · {counts['activist']} activist · "
        f"{counts['buyback']} buyback · {counts['insider_buy']} buys"
    )

    if args.dry_run:
        out = ROOT / "out"
        out.mkdir(exist_ok=True)
        (out / "preview.html").write_text(html_body)
        print(subject)
        print(text_body)
        print(f"\nPreview -> {out/'preview.html'}")
        return

    if found or cfg.get("send_when_empty", False):
        send(subject, html_body, text_body)
        print(f"Sent: {subject}")
    else:
        print("No signals; email suppressed.")
    if not args.no_state:
        save_seen(seen)


if __name__ == "__main__":
    main()
