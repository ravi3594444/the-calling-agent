"""The guest's own page for their booking (PRD §12).

Every confirmation text carries a link here. Before this existed the link was
in the template and the route was not, so each text sent a guest to a 404 --
the kind of thing you hear about from the guest.

THE LINK IS THE CREDENTIAL: 32+ random characters, stored hashed, expiring
after the booking date. The six-character reference is NOT one. It is read
aloud on the phone and printed in the same text, so anyone in earshot has it,
and it authorises nothing here.

The page is server-rendered and self-contained. A guest opens it once, on a
phone, probably on mobile data, possibly a year after we shipped it: one
request, no build step, no framework.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from html import escape

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import HTMLResponse

from .. import bookings, businesses, formatting, tokens
from ..businesses import Business
from ..db import fetch_one, readonly

log = logging.getLogger(__name__)

router = APIRouter(tags=["manage"])


def _find(token: str) -> tuple[Business, bookings.BookingRow]:
    """The booking this link opens, or 404.

    One message for every failure -- unknown, expired, mistyped -- because a
    page that distinguished them would let someone probe for live links.
    """
    if not token:
        raise HTTPException(404, "This link is not valid.")

    with readonly() as conn:
        row = fetch_one(
            conn,
            "SELECT id, business_id, manage_token_expires_at FROM bookings"
            " WHERE manage_token_hash = :h",
            h=tokens.hash_secret(token),
        )
    if row is None:
        raise HTTPException(404, "This link is not valid.")
    if row.manage_token_expires_at and row.manage_token_expires_at <= datetime.now(UTC):
        raise HTTPException(404, "This link has expired.")

    business = businesses.by_id(row.business_id)
    return business, bookings.by_id(business, row.id)


@router.get("/manage/{token}", response_class=HTMLResponse)
def show_booking(token: str) -> HTMLResponse:
    business, booking = _find(token)
    return HTMLResponse(_page(business, booking, token))


@router.post("/manage/{token}/cancel", response_class=HTMLResponse)
def cancel_booking(token: str, confirm: str = Form(default="")) -> HTMLResponse:
    """Cancel, putting the capacity straight back on sale.

    A POST, not a GET: messaging apps and mail scanners follow every URL they
    are sent, and a cancellation behind a GET would be triggered by the very
    text that announced the booking.
    """
    business, booking = _find(token)

    if booking.status in (bookings.CANCELLED, bookings.DECLINED):
        return HTMLResponse(_page(business, booking, token, note="This was already cancelled."))
    if confirm != "yes":
        return HTMLResponse(_page(business, booking, token), status_code=400)

    try:
        cancelled = bookings.cancel(business, booking.id, actor="guest")
    except bookings.IllegalTransition:
        return HTMLResponse(
            _page(
                business, booking, token,
                note="This booking can no longer be cancelled online. Please call us.",
            ),
            status_code=409,
        )
    return HTMLResponse(_page(business, cancelled, token))


# --- the page ----------------------------------------------------------------

_STYLE = """
:root{--bg:#f6f5f1;--card:#fff;--ink:#16181c;--ink-2:#565b63;--line:#e4e2dc;
      --no:#a03232;--ok:#2f6b46}
@media (prefers-color-scheme:dark){:root{--bg:#121316;--card:#1b1d21;--ink:#f2f2f0;
      --ink-2:#a4a8b0;--line:#2c2f35;--no:#e08a8a;--ok:#7fd1a5}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);padding:24px 16px;
     font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:32rem;margin:0 auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;
      padding:22px;margin-bottom:16px}
h1{font-size:1.35rem;margin:0 0 6px}
p{margin:0 0 12px;color:var(--ink-2)}
dl{display:grid;grid-template-columns:auto 1fr;gap:9px 18px;margin:18px 0 0}
dt{color:var(--ink-2);font-size:.9rem}
dd{margin:0;font-weight:600}
.ref{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:.06em}
.pill{display:inline-block;padding:3px 10px;border-radius:999px;font-size:.85rem;
      font-weight:600;background:color-mix(in srgb,var(--ok) 16%,transparent);color:var(--ok)}
.pill.no{background:color-mix(in srgb,var(--no) 16%,transparent);color:var(--no)}
button{font:inherit;font-weight:600;padding:14px 18px;min-height:48px;width:100%;
       border-radius:10px;border:1px solid var(--no);background:transparent;
       color:var(--no);cursor:pointer}
button:hover{background:color-mix(in srgb,var(--no) 10%,transparent)}
.note{margin-top:14px;color:var(--ink-2);font-size:.92rem}
.foot{text-align:center;color:var(--ink-2);font-size:.85rem}
a{color:inherit}
"""


def _page(
    business: Business, booking: bookings.BookingRow, token: str, note: str = ""
) -> str:
    identity = business.config["identity"]
    venue = escape(identity["display_name"] or business.name)
    gone = booking.status in (bookings.CANCELLED, bookings.DECLINED)

    if gone:
        headline = "This booking is cancelled"
        blurb = f"Nothing is held for you at {venue}. You are welcome to book again any time."
    elif booking.status == bookings.PENDING:
        headline = "We have your request"
        blurb = f"{venue} will confirm shortly. You will get a text either way."
    else:
        headline = "You're booked"
        blurb = f"We will see you at {venue}."

    action = "" if gone else f"""
      <form method="post" action="/manage/{escape(token)}/cancel"
            onsubmit="return confirm('Cancel this booking?')">
        <input type="hidden" name="confirm" value="yes">
        <button type="submit">Cancel this booking</button>
      </form>"""

    phone = business.phone_number
    contact = (
        f'<p class="foot">Need to change it instead? Call '
        f'<a href="tel:{escape(phone)}">{escape(phone)}</a>.</p>'
        if phone
        else ""
    )
    units = escape(business.unit_plural.capitalize())

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>{venue} — your booking</title>
<style>{_STYLE}</style>
</head><body><div class="wrap">
  <div class="card">
    <h1>{headline}</h1>
    <p>{blurb}</p>
    <dl>
      <dt>When</dt><dd>{escape(formatting.when(business, booking.start_time))}</dd>
      <dt>{units}</dt><dd>{booking.party_size}</dd>
      <dt>Name</dt><dd>{escape(booking.name)}</dd>
      <dt>Reference</dt><dd class="ref">{escape(booking.reference)}</dd>
      <dt>Status</dt>
      <dd><span class="pill {"no" if gone else ""}">{escape(_label(booking.status))}</span></dd>
    </dl>
    {f'<p class="note">{escape(note)}</p>' if note else ""}
  </div>
  {action}
  {contact}
</div></body></html>"""


def _label(status: str) -> str:
    return {
        bookings.CONFIRMED: "Confirmed",
        bookings.PENDING: "Waiting on the venue",
        bookings.ARRIVED: "Seated",
        bookings.CANCELLED: "Cancelled",
        bookings.DECLINED: "Not available",
        bookings.NO_SHOW: "Missed",
    }.get(status, status)
