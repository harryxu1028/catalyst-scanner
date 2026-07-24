"""Render signals to HTML and send via Gmail SMTP."""
from __future__ import annotations

import html
import os
import smtplib
from email.message import EmailMessage

from .signals import Signal

SECTIONS = [
    ("officer", "Leadership Change", "#b3261e"),
    ("activist", "Activist Involvement", "#7b1fa2"),
    ("buyback", "Buyback Authorization", "#1b5e20"),
    ("insider_buy", "Insider Buys", "#0b4f9c"),
    ("insider_sale", "Discretionary Sales (no 10b5-1)", "#8a6d00"),
]

_CSS = """
body{font:14px/1.45 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#1a1a1a;margin:0;padding:18px;background:#f6f7f9}
.wrap{max-width:820px;margin:0 auto;background:#fff;border:1px solid #e3e6ea;border-radius:8px;padding:22px}
h1{font-size:17px;margin:0 0 2px}
.sub{color:#6b7280;font-size:12px;margin-bottom:18px}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.6px;margin:22px 0 8px;padding-bottom:5px;border-bottom:2px solid}
.card{border:1px solid #e6e8eb;border-left:3px solid #cbd2d9;border-radius:5px;padding:10px 12px;margin-bottom:8px}
.tk{font-weight:700}
.hd{font-weight:600}
.dt{color:#4b5563;font-size:12.5px;margin-top:5px}
.meta{color:#8b9199;font-size:11.5px;margin-top:6px}
.tag{display:inline-block;background:#eef1f4;color:#41474d;border-radius:3px;padding:1px 6px;font-size:10.5px;margin-right:4px}
a{color:#0b4f9c;text-decoration:none}
.empty{color:#6b7280;font-style:italic}
"""


def render(signals: list[Signal], window: str, universe_size: int) -> str:
    parts = [f"<html><head><meta charset='utf-8'><style>{_CSS}</style></head><body><div class='wrap'>"]
    parts.append(f"<h1>Catalyst Scan — {window}</h1>")
    parts.append(f"<div class='sub'>{len(signals)} signal(s) across {universe_size} covered names · "
                 f"source: SEC EDGAR</div>")

    if not signals:
        parts.append("<div class='empty'>No qualifying filings in the window.</div>")

    for kind, label, colour in SECTIONS:
        rows = sorted([s for s in signals if s.kind == kind],
                      key=lambda s: (-s.score, -(s.value or 0)))
        if not rows:
            continue
        parts.append(f"<h2 style='border-color:{colour};color:{colour}'>{label} ({len(rows)})</h2>")
        for s in rows:
            tags = "".join(f"<span class='tag'>{html.escape(t)}</span>" for t in s.tags)
            parts.append(
                f"<div class='card' style='border-left-color:{colour}'>"
                f"<div><span class='tk'>{html.escape(s.ticker)}</span> "
                f"<span style='color:#6b7280'>· {html.escape(s.company[:60])}</span></div>"
                f"<div class='hd'>{html.escape(s.headline)}</div>"
                f"<div class='dt'>{html.escape(s.detail[:600])}</div>"
                f"<div class='meta'>{tags}{html.escape(s.form)} · {html.escape(s.filed)} · "
                f"<a href='{html.escape(s.url)}'>filing</a></div>"
                f"</div>"
            )

    parts.append("</div></body></html>")
    return "".join(parts)


def plain(signals: list[Signal]) -> str:
    lines = []
    for kind, label, _ in SECTIONS:
        rows = sorted([s for s in signals if s.kind == kind], key=lambda s: -s.score)
        if not rows:
            continue
        lines.append(f"\n== {label.upper()} ==")
        for s in rows:
            lines.append(f"[{s.ticker}] {s.headline}\n    {s.url}")
    return "\n".join(lines) or "No qualifying filings."


def send(subject: str, html_body: str, text_body: str) -> None:
    user = os.environ["GMAIL_USER"]
    pwd = os.environ["GMAIL_APP_PASSWORD"]
    to = [x.strip() for x in os.environ.get("MAIL_TO", user).split(",") if x.strip()]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(to)
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
        srv.login(user, pwd)
        srv.send_message(msg)
