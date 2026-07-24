# Catalyst Scanner

Daily EDGAR sweep for four hard catalysts across a yfinance-defined subsector
universe, emailed pre-open via Gmail SMTP on GitHub Actions.

| Signal | Detected from | Notes |
|---|---|---|
| CEO/CFO change | 8-K **Item 5.02**, read from the SGML header | Body text confirms which seat; flags "effective immediately" / interim |
| Buyback | 8-K keyword + size extraction | Highest-value match wins; scored as % of market cap |
| Insider buy | Form 4, `transactionCode = P`, A/D = `A` | Clusters (2+ distinct buyers in the window) get a large score bump |
| Non-plan sale | Form 4, `transactionCode = S`, **no 10b5-1 flag** | See below |
| Activist | `SC 13D`, `SC 13D/A`, `DFAN14A`, `PREC14A`, `DEFC14A`, `SC 14N` | Filer name matched against a known-activist list |

## Why EDGAR and not Yahoo

Yahoo carries none of these as structured events. `info['companyOfficers']` has
no change timestamp, buyback authorizations aren't exposed at all, and there is
no Form 4 detail. yfinance is used here only for what it's genuinely good at —
sector/industry classification and market cap — to define the universe. Every
signal itself comes from the filing.

## The 10b5-1 filter

This is the part worth understanding, since it's what separates a real sell
signal from noise. Following the SEC's December 2022 amendments, Form 4 carries
an explicit cover-page checkbox indicating a transaction was made under a
Rule 10b5-1(c) plan, and it is machine-readable in the XML. `signals.insider()`
matches **any** element whose tag contains `10b5` rather than hard-coding one
name — schema element naming has shifted across revisions, and this survives
that. It then falls back to scanning footnote text for a plan reference, which
is how filings disclosed it before the checkbox existed.

A sale is reported only when neither check fires. Two caveats:

- The checkbox is the filer's assertion. Some agents still disclose only in a
  footnote, and some disclose nowhere. Treat a "discretionary" tag as *worth a
  look*, not as established fact.
- 10b5-1 **adoption or termination** is itself a signal, and it isn't reported
  here. Plan terminations before bad news are a well-known pattern. Extending
  `insider()` to surface footnotes mentioning plan adoption/termination dates
  is the highest-value next addition.

## Where to edit the universe

Everything lives in `config.yaml` under `universe:`.

| What you want | Where |
|---|---|
| Add/remove a subsector | `universe.industries` — strings from `SECTORS.md` |
| Widen to a whole sector | `universe.sectors` — only consulted if no industry matches |
| Force-include a name off-screen | `universe.extra_tickers` (bypasses cap and industry filters) |
| Blacklist a name | `universe.exclude_tickers` |
| Change the SMID band | `universe.market_cap_min` / `market_cap_max` |
| Restrict the candidate pool | `--tickers-file` at build time |

`data/universe.json` is generated output — edit the config and rebuild, don't
hand-edit it.

```bash
python -m scanner.universe --validate                    # check names, no network
python -m scanner.universe --list-industries             # all 145
python -m scanner.universe --list-industries "Technology"
python -m scanner.universe --build --tickers-file my_coverage.txt
```

`--build` runs `--validate` first and aborts on an unknown string, so a typo
costs you a second rather than an hour of screening that returns zero names.

### The taxonomy

Yahoo uses a modified GICS-style scheme: **11 sectors, 145 industries**, listed
in full in `SECTORS.md` (generated from `yfinance.const.SECTOR_INDUSTY_MAPPING`
— the typo is the library's, not a mistake here). It is a *single-label* scheme:
each company gets exactly one industry, so a name that straddles categories
lands wherever Yahoo put it and nowhere else. Practical consequences for TMT
coverage:

- **Payments are split.** Networks and processors sit in `Credit Services`
  (Financial Services), but several payment-software names classify as
  `Software—Infrastructure` instead. Take both.
- **`Software—Application` vs `Software—Infrastructure`** is Yahoo's judgment
  call and often not yours. Take both and filter on market cap.
- **Exchanges and market-structure names** are `Financial Data & Stock
  Exchanges`, separate from `Capital Markets` (brokers, IBs, asset managers).
- **Marketplaces scatter** across `Internet Retail`, `Internet Content &
  Information`, and `Specialty Business Services`.
- Note the **em dashes**: `Software—Application`, `Banks—Regional`. Matching
  here is punctuation-insensitive, so a plain hyphen works in the config, but
  exact string comparisons elsewhere will fail.

Given the misclassification risk, `extra_tickers` is the pragmatic escape hatch
— seed it with the names you know you cover and let the screen catch the rest.



```bash
pip install -r requirements.txt

# 1. Put a real contact string in config.yaml (SEC blocks generic UAs)
# 2. Build the universe (slow — see note below)
python -m scanner.universe --build --tickers-file my_coverage.txt

# 3. Preview against a past date, no email sent
python -m scanner.run --date 2026-07-22 --days 1 --dry-run --no-state
open out/preview.html
```

GitHub secrets required: `SEC_UA`, `GMAIL_USER`, `GMAIL_APP_PASSWORD`
(a Gmail *app password*, not the account password), `MAIL_TO`.

### Universe build time

Screening all ~10k SEC registrants through `yfinance` takes roughly an hour and
will hit rate limits. Two better options: pass `--tickers-file` with your own
coverage list, or seed it from an index constituent file. Results cache to
`data/_yf_cache.json`, so re-runs are fast and the weekly workflow only picks up
drift.

## Rate limits and etiquette

SEC fair access is 10 req/sec with a descriptive User-Agent containing contact
info. `edgar.py` throttles globally to 7 rps and retries on 403/429 with
backoff. One daily-index request covers the entire market; per-filing fetches
then run only for in-universe CIKs, so a 400-name universe is typically a few
hundred requests per run — a couple of minutes, comfortably inside Actions'
free tier.

## Scheduling

Two crons: an 06:45 ET pre-open sweep and a 21:15 ET catch-up for filings
arriving after EDGAR's 17:30 dissemination cut (Form 4s cluster there). The
default 4-day lookback tolerates a missed run or a long weekend; the dedupe
file in `state/` (carried between runs by `actions/cache`) prevents repeats.

Note the crons are UTC and do **not** shift with DST — they drift an hour in
winter. Either accept the drift or adjust in November.

## Tuning

Expect to iterate on precision for two weeks. The likely offenders:

- **Item 5.02 false positives.** The item also covers director elections and
  comp arrangements. `_NOISE_RE` filters the common boilerplate; add to it.
- **Buyback misses.** Announcements often live in the EX-99.1 press release, and
  the fetch caps at 4MB of the complete submission. If you see misses, either
  raise the cap or fetch the exhibit specifically via the filing's `index.json`.
- **13D/A noise.** Amendments fire on every position change, not just campaign
  escalations. Consider suppressing amendments unless the stake moved >1pt or
  Item 4 language changed.

## What's deliberately not covered

13G→13D conversions (a genuine activist tell), 13F-based position building,
Item 5(c) buyback *execution* from 10-Q filings as distinct from authorization,
and S-8/S-3 shelf activity. All are reachable with the same index-and-parse
plumbing in `edgar.py`.
