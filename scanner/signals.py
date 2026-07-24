"""Catalyst detectors. Each takes a filing + its raw text and returns Signals."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from . import edgar
from .edgar import Filing, visible_text


@dataclass
class Signal:
    kind: str                 # officer | buyback | insider_buy | insider_sale | activist
    ticker: str
    company: str
    headline: str
    detail: str
    url: str
    filed: str
    form: str
    score: int = 0
    value: float | None = None       # $ where applicable, for sorting
    tags: list[str] = field(default_factory=list)

    @property
    def dedupe_key(self) -> str:
        return f"{self.kind}|{self.url}|{self.headline[:120]}"


# ==========================================================================
# 1. CEO / CFO change  --  8-K Item 5.02
# ==========================================================================

_ROLE_RE = re.compile(
    r"(chief executive officer|chief financial officer|principal financial officer|"
    r"principal accounting officer|\bC\.?E\.?O\.?\b|\bC\.?F\.?O\.?\b)",
    re.I,
)
_EVENT_RE = re.compile(
    r"(resign|retir|step(?:ping|ped|s)?\s+down|depart|separat|terminat|appoint|"
    r"elect|promot|nam(?:ed|ing)|succeed|successor|transition|interim|"
    r"effective immediately|mutual agreement)",
    re.I,
)
# Item 5.02 also fires on routine comp-plan and director-slate housekeeping.
_NOISE_RE = re.compile(
    r"(annual meeting|equity incentive plan|compensatory arrangement|"
    r"annual retainer|restricted stock unit grant|director nominee)",
    re.I,
)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.;])\s+(?=[A-Z(])", text) if s.strip()]


def officer_change(f: Filing, raw: str, ticker: str) -> list[Signal]:
    if f.form not in ("8-K", "8-K/A"):
        return []
    if "5.02" not in edgar.header_items(raw):
        return []

    text = visible_text(raw)
    hits = [
        s for s in _sentences(text)
        if _ROLE_RE.search(s) and _EVENT_RE.search(s) and len(s) < 700
    ]
    if not hits:
        return []

    strong = [s for s in hits if not _NOISE_RE.search(s)]
    if not strong:
        return []

    is_ceo = any(re.search(r"chief executive|\bC\.?E\.?O\.?\b", s, re.I) for s in strong)
    is_cfo = any(re.search(r"chief financial|principal financial|\bC\.?F\.?O\.?\b", s, re.I)
                 for s in strong)
    roles = "/".join(r for r, ok in (("CEO", is_ceo), ("CFO", is_cfo)) if ok) or "Officer"
    abrupt = bool(re.search(r"(effective immediately|immediate|mutual agreement|for cause|"
                            r"without cause|interim)", " ".join(strong), re.I))

    tags = ["8-K 5.02"]
    if abrupt:
        tags.append("abrupt / interim")

    return [Signal(
        kind="officer",
        ticker=ticker,
        company=f.company,
        headline=f"{roles} change",
        detail=" ".join(strong[:3])[:900],
        url=f.index_url,
        filed=f.filed,
        form=f.form,
        score=90 + (10 if is_ceo else 0) + (15 if abrupt else 0),
        tags=tags,
    )]


# ==========================================================================
# 2. Buyback announcement  --  8-K, any item, keyword + size
# ==========================================================================

_BUYBACK_RE = re.compile(
    r"((?:share|stock|common stock)?\s?repurchase (?:program|authorization|plan)|"
    r"authoriz\w+ (?:the )?(?:re)?purchase of|buyback program|"
    r"accelerated share repurchase|increas\w+ .{0,60}repurchase (?:program|authorization)|"
    r"expand\w+ .{0,60}repurchase)",
    re.I,
)
_BUYBACK_NOISE_RE = re.compile(
    r"(satisfy tax withholding|net share settle|withheld to (?:cover|satisfy)|"
    r"forfeit|401\(k\)|employee stock purchase plan)",
    re.I,
)
_AMOUNT_RE = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)\s*(billion|million|bn\b|mm\b)", re.I)
_SHARE_CT_RE = re.compile(r"([\d,]+(?:\.\d+)?)\s*(million|thousand)?\s+shares", re.I)


def _to_dollars(num: str, unit: str) -> float:
    v = float(num.replace(",", ""))
    u = unit.lower()
    if u.startswith("b"):
        return v * 1e9
    return v * 1e6


def buyback(f: Filing, raw: str, ticker: str, market_cap: float | None = None) -> list[Signal]:
    if f.form not in ("8-K", "8-K/A"):
        return []

    text = visible_text(raw)
    best = None
    for m in _BUYBACK_RE.finditer(text):
        # Veto only if the disqualifying language sits in the SAME sentence.
        # A neighbouring tax-withholding sentence must not kill a real
        # authorization, which is exactly what a character-radius check does.
        lo = max(text.rfind(".", 0, m.start()), text.rfind("\n", 0, m.start())) + 1
        hi = m.end() + 400
        dot = text.find(".", m.end())
        if dot != -1:
            hi = min(hi, dot + 1)
        if _BUYBACK_NOISE_RE.search(text[lo:hi]):
            continue
        window = text[lo: m.end() + 500]
        amt = _AMOUNT_RE.search(window)
        shares = _SHARE_CT_RE.search(window)
        if not amt and not shares:
            continue
        dollars = _to_dollars(*amt.groups()) if amt else None
        cand = (dollars or 0, window.strip()[:900], dollars)
        if best is None or cand[0] > best[0]:
            best = cand
    if best is None:
        return []

    _, snippet, dollars = best
    size = f"${dollars/1e6:,.0f}mm" if dollars else "size n/d"
    pct = ""
    sc = 70
    if dollars and market_cap:
        p = dollars / market_cap
        pct = f" ({p*100:.1f}% of mkt cap)"
        sc += min(40, int(p * 300))          # 10% of cap -> +30

    return [Signal(
        kind="buyback",
        ticker=ticker,
        company=f.company,
        headline=f"Repurchase authorization {size}{pct}",
        detail=snippet,
        url=f.index_url,
        filed=f.filed,
        form=f.form,
        score=sc,
        value=dollars,
        tags=["buyback"],
    )]


# ==========================================================================
# 3. Insider buys / non-10b5-1 sales  --  Form 4 XML
# ==========================================================================

_OWNERSHIP_RE = re.compile(r"<ownershipDocument>.*?</ownershipDocument>", re.S | re.I)
_OPEN_MARKET = {"P", "S"}


def _txt(node, path, default="") -> str:
    if node is None:
        return default
    el = node.find(path)
    if el is None:
        return default
    v = el.find("value")
    src = v if v is not None else el
    return (src.text or default).strip()


def _num(node, path) -> float | None:
    s = _txt(node, path)
    try:
        return float(s.replace(",", ""))
    except (ValueError, AttributeError):
        return None


def insider(f: Filing, raw: str, ticker: str, cfg: dict) -> list[Signal]:
    if f.form not in ("4", "4/A"):
        return []
    m = _OWNERSHIP_RE.search(raw)
    if not m:
        return []
    try:
        root = ET.fromstring(m.group(0))
    except ET.ParseError:
        return []

    issuer = _txt(root, "issuer/issuerName") or f.company
    sym = _txt(root, "issuer/issuerTradingSymbol") or ticker

    owner = root.find("reportingOwner")
    owner_name = _txt(owner, "reportingOwnerId/rptOwnerName")
    rel = owner.find("reportingOwnerRelationship") if owner is not None else None
    title = _txt(rel, "officerTitle")
    is_dir = _txt(rel, "isDirector") in ("1", "true")
    is_off = _txt(rel, "isOfficer") in ("1", "true")
    is_10pct = _txt(rel, "isTenPercentOwner") in ("1", "true")
    role = title or ("Director" if is_dir else "10% owner" if is_10pct else "Officer" if is_off else "")

    # --- Rule 10b5-1 detection -------------------------------------------
    # Since the Dec-2022 amendments the cover page carries a machine-readable
    # checkbox. Element naming has shifted across schema revisions, so match
    # any tag containing "10b5" rather than hard-coding one name; fall back to
    # footnote text, which is how pre-2023 filings disclosed it.
    plan = False
    for el in root.iter():
        if "10b5" in el.tag.lower():
            if (el.text or "").strip().lower() in ("1", "true", "y", "yes"):
                plan = True
    foot = " ".join("".join(fn.itertext()) for fn in root.iter("footnote"))
    if re.search(r"10b5\s?-?\s?1", foot, re.I):
        plan = True

    buys, sells = [], []
    for tx in root.iter("nonDerivativeTransaction"):
        code = _txt(tx, "transactionCoding/transactionCode")
        if code not in _OPEN_MARKET:
            continue
        shares = _num(tx, "transactionAmounts/transactionShares")
        price = _num(tx, "transactionAmounts/transactionPricePerShare")
        ad = _txt(tx, "transactionAmounts/transactionAcquiredDisposedCode")
        if not shares:
            continue
        rec = {"shares": shares, "price": price, "value": (shares * price) if price else None}
        (buys if (code == "P" and ad == "A") else sells if (code == "S" and ad == "D") else []).append(rec)

    out: list[Signal] = []
    who = f"{owner_name}" + (f" ({role})" if role else "")
    senior = bool(re.search(r"chief executive|chief financial|\bceo\b|\bcfo\b|president|chair",
                            role, re.I))

    if buys:
        tot_sh = sum(b["shares"] for b in buys)
        tot_val = sum(b["value"] for b in buys if b["value"]) or None
        if tot_val is None or tot_val >= cfg.get("insider_buy_min_value", 50_000):
            px = next((b["price"] for b in buys if b["price"]), None)
            out.append(Signal(
                kind="insider_buy",
                ticker=sym,
                company=issuer,
                headline=f"{who} bought {tot_sh:,.0f} sh"
                         + (f" (~${tot_val/1e3:,.0f}k)" if tot_val else ""),
                detail=f"Open-market purchase (code P)"
                       + (f" at ~${px:,.2f}" if px else "")
                       + (". Filed under a 10b5-1 plan." if plan else "."),
                url=f.index_url,
                filed=f.filed,
                form=f.form,
                score=80 + (25 if senior else 0) + (10 if (tot_val or 0) > 500_000 else 0),
                value=tot_val,
                tags=["cluster-candidate"] + (["10b5-1"] if plan else []),
            ))

    if sells and not plan:
        tot_sh = sum(s["shares"] for s in sells)
        tot_val = sum(s["value"] for s in sells if s["value"]) or None
        if tot_val is None or tot_val >= cfg.get("insider_sale_min_value", 250_000):
            out.append(Signal(
                kind="insider_sale",
                ticker=sym,
                company=issuer,
                headline=f"{who} sold {tot_sh:,.0f} sh"
                         + (f" (~${tot_val/1e3:,.0f}k)" if tot_val else "")
                         + " — no 10b5-1 flag",
                detail="Discretionary open-market sale (code S) with no Rule 10b5-1 "
                       "checkbox and no plan reference in the footnotes.",
                url=f.index_url,
                filed=f.filed,
                form=f.form,
                score=60 + (20 if senior else 0),
                value=tot_val,
                tags=["discretionary"],
            ))
    return out


# ==========================================================================
# 4. Activist involvement  --  SC 13D and proxy-fight forms
# ==========================================================================

ACTIVIST_FORMS = {
    "SC 13D", "SC 13D/A",
    "DFAN14A", "PREC14A", "DEFC14A", "PREN14A", "DFRN14A", "DEFN14A", "SC 14N",
}
_PURPOSE_RE = re.compile(
    r"(board (?:representation|seats?|refresh)|strategic alternatives|"
    r"engage in discussions with (?:the )?(?:management|board)|nominat\w+ .{0,40}director|"
    r"sale of the (?:company|issuer)|undervalued|maximiz\w+ (?:share|stockholder)holder value|"
    r"explore .{0,30}alternatives|urge the board|withhold|consent solicitation)",
    re.I,
)


def activist(f: Filing, raw: str, ticker: str, known: list[str],
             counterparty: str = "") -> list[Signal]:
    if f.form not in ACTIVIST_FORMS:
        return []

    text = visible_text(raw)
    name = counterparty or ""
    matched = next((k for k in known if k.upper() in name.upper()), None)
    if not matched:
        matched = next((k for k in known if re.search(re.escape(k), text[:60_000], re.I)), None)

    purpose = [m.group(0) for m in _PURPOSE_RE.finditer(text)][:5]
    pct = None
    pm = re.search(r"(?:aggregate\w*\s+)?([\d.]{1,5})\s?%\s+of\s+(?:the\s+)?(?:outstanding|class|"
                   r"issued and outstanding|common)", text, re.I)
    if pm:
        try:
            pct = float(pm.group(1))
        except ValueError:
            pass

    is_new_13d = f.form == "SC 13D"
    head = f"{f.form} filed" + (f" by {name}" if name else "")
    if pct:
        head += f" — {pct:.1f}% stake"

    sc = 85 + (15 if is_new_13d else 0) + (25 if matched else 0) + (10 if purpose else 0)
    tags = ["13D" if "13D" in f.form else "proxy contest"]
    if matched:
        tags.append(f"known activist: {matched}")

    return [Signal(
        kind="activist",
        ticker=ticker,
        company=f.company,
        headline=head,
        detail=("Stated purpose language: " + "; ".join(purpose)) if purpose
               else "No explicit Item 4 activism language detected — review manually.",
        url=f.index_url,
        filed=f.filed,
        form=f.form,
        score=sc,
        tags=tags,
    )]
