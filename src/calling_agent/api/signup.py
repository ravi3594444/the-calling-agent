"""The ten-minute form (PRD §15): a venue onboards itself, no terminal.

Everything behind the dashboard link was already self-serve. The one thing
that was not is getting the link: `cli onboard`, `cli hours` and `cli link`
all needed a shell on the server, so every new customer was an operator
ticket. This is those three commands as one form.

WHY IT IS OFF BY DEFAULT
`/start` creates a tenant and hands back a live dashboard link with nothing in
front of it. That is the correct behaviour for a signup page and a terrible
default for a deployment that did not ask for one, so SIGNUP_ENABLED must be
set before the route exists at all -- the same reasoning that leaves
SMS_PROVIDER on `log`. SIGNUP_CODE adds an invite word on top, which is how
you run a private beta without building accounts first.

WHAT IT DELIBERATELY DOES NOT DO
No email, no password, no account. The dashboard's security model is the
secret link (PRD §14) and this mints one; adding a login here would mean
inventing a second one. /start/ready shows that link once and says to keep
it, because only its hash is stored and nobody can send it again.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from importlib import resources
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import business_config, businesses
from ..config import settings
from .deps import business_for_token, issue_dashboard_token

router = APIRouter(tags=["signup"])

#: Monday = 0, matching capacity_rules.weekday.
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

_TIME_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")


# --- the guard ---------------------------------------------------------------


def _require_open() -> None:
    """404 rather than 403 when signup is off.

    A 403 confirms the route is there and tells a scanner to come back after
    the next deploy. An installation that never turned this on should look
    exactly like one that does not have the code.
    """
    if not settings.signup_enabled:
        raise HTTPException(404, "Not found")


#: {address: [completed signup timestamps]}. Per process and lost on restart,
#: which is the honest description of what it is: a floor under a real limiter,
#: not one. Only successful signups count -- a typo in the invite code must not
#: lock somebody out of their own second attempt.
_recent: dict[str, list[float]] = {}


def _rate_limit(request: Request) -> None:
    caller = request.client.host if request.client else "unknown"
    cutoff = time.monotonic() - 3600
    seen = [stamp for stamp in _recent.get(caller, []) if stamp > cutoff]
    _recent[caller] = seen
    if len(seen) >= settings.signup_max_per_hour:
        raise HTTPException(429, "Too many venues created from here. Try again later.")


def _record_signup(request: Request) -> None:
    caller = request.client.host if request.client else "unknown"
    _recent.setdefault(caller, []).append(time.monotonic())


# --- reference data ----------------------------------------------------------


def _countries() -> list[dict[str, Any]]:
    raw = resources.files("calling_agent").joinpath("data/locales.json").read_text("utf-8")
    return json.loads(raw)["countries"]


def _country(code: str) -> dict[str, Any]:
    for row in _countries():
        if row["code"] == code:
            return row
    raise HTTPException(422, f"We do not have locale settings for {code!r} yet.")


# --- slugs -------------------------------------------------------------------


def slugify(name: str) -> str:
    """"Café Ramona" -> "cafe-ramona". Ascii only: it goes in a URL.

    A name that reduces to nothing -- one written entirely in a script this
    strips -- still has to produce a usable slug, so it falls back to "venue"
    and takes a number from the collision loop like any other clash.
    """
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")
    return slug[:40] or "venue"


def _free_slug(name: str) -> str:
    """The first slug nobody has. Bounded, because this runs behind a form.

    The loop races: two venues submitting the same name in the same instant can
    both read "free". The unique index is what actually decides it, and the
    caller turns that into a message -- this only keeps the common case tidy.
    """
    base = slugify(name)
    for suffix in range(1, 50):
        candidate = base if suffix == 1 else f"{base}-{suffix}"
        try:
            businesses.by_slug(candidate)
        except businesses.UnknownBusiness:
            return candidate
    raise HTTPException(422, "Too many venues already have that name. Try a different one.")


# --- the form ----------------------------------------------------------------


def _page(*, error: str = "", values: dict[str, str] | None = None) -> str:
    from html import escape

    kept = values or {}

    def keep(field: str, fallback: str = "") -> str:
        return escape(kept.get(field, fallback))

    countries = "".join(
        f'<option value="{escape(row["code"])}"'
        f'{" selected" if kept.get("country") == row["code"] else ""}>'
        f'{escape(row["name"])}</option>'
        for row in _countries()
    )
    # known_verticals() is sorted, so without this the first thing a restaurant
    # owner sees is "Clinic" -- and a form whose first answer is already wrong
    # is one people correct by not finishing it.
    chosen_vertical = kept.get("vertical") or "restaurant"
    verticals = "".join(
        f'<option value="{escape(name)}"'
        f'{" selected" if chosen_vertical == name else ""}>'
        f"{escape(name.capitalize())}</option>"
        for name in business_config.known_verticals()
    )
    # Monday to Sunday ticked by default; a venue that closes on Monday unticks
    # it. Nothing is ticked for them on a redisplay they did not ask for.
    ticked = kept.get("days", "0,1,2,3,4,5,6").split(",") if kept else list("0123456")
    days = "".join(
        f'<label class="day"><input type="checkbox" name="days" value="{index}"'
        f'{" checked" if str(index) in ticked else ""}> {label}</label>'
        for index, label in enumerate(_DAYS)
    )
    code_field = (
        """
      <div class="f"><label for="code">Invite code</label>
        <input id="code" name="code" required autocomplete="off">
        <span class="help">The code whoever invited you gave you.</span></div>"""
        if settings.signup_code
        else ""
    )
    problem = f'<p class="error" role="alert">{escape(error)}</p>' if error else ""

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Set up your venue</title>
<style>{_STYLE}</style>
</head><body><div class="wrap">
  <h1>Set up your venue</h1>
  <p class="lead">About ten minutes. You can change every one of these later.</p>
  {problem}
  <form method="post" action="/start">
    <div class="card">
      <h2>The place</h2>
      <div class="f"><label for="name">What it's called</label>
        <input id="name" name="name" required maxlength="120" value="{keep("name")}"
               placeholder="Café Ramona" autofocus>
        <span class="help">Exactly as you'd say it answering the phone.</span></div>
      <div class="f"><label for="description">What you are</label>
        <input id="description" name="description" maxlength="200"
               value="{keep("description")}" placeholder="small plates and natural wine">
        <span class="help">One line. The agent uses it when someone asks.</span></div>
      <div class="f"><label for="agent_name">Who answers</label>
        <input id="agent_name" name="agent_name" maxlength="60"
               value="{keep("agent_name")}" placeholder="Inês">
        <span class="help">The name your agent gives itself. Leave it blank
          and it just says where it's calling from.</span></div>
      <div class="f"><label for="vertical">What kind of place</label>
        <select id="vertical" name="vertical">{verticals}</select></div>
    </div>

    <div class="card">
      <h2>Where you are</h2>
      <div class="f"><label for="country">Country</label>
        <select id="country" name="country">{countries}</select>
        <span class="help">Sets your currency, timezone and how times are
          spoken. All three are editable afterwards.</span></div>
      <div class="f"><label for="phone">Your phone number</label>
        <input id="phone" name="phone" type="tel" maxlength="32" value="{keep("phone")}"
               placeholder="+351 21 000 0000">
        <span class="help">The number people dial to reach you, if you have it
          yet. You can add it later.</span></div>
    </div>

    <div class="card">
      <h2>When you're open</h2>
      <div class="days">{days}</div>
      <div class="row">
        <div class="f"><label for="opens">Opens</label>
          <input id="opens" name="opens" type="time" required
                 value="{keep("opens", "18:00")}"></div>
        <div class="f"><label for="closes">Closes</label>
          <input id="closes" name="closes" type="time" required
                 value="{keep("closes", "23:00")}"></div>
      </div>
      <div class="f"><label for="seats">How many people you can seat at once</label>
        <input id="seats" name="seats" type="number" min="0" max="10000"
               value="{keep("seats")}" placeholder="Leave blank for no limit">
        <span class="help">Blank means the agent takes every booking, however
          busy you get. You can turn capacity on later.</span></div>
      {code_field}
    </div>

    <button type="submit">Create it</button>
    <p class="foot">The next page gives you a private dashboard link. Save it
      there and then — it's the only way back in, and it can't be re-sent.</p>
  </form>
</div></body></html>"""


_STYLE = """
:root{--ink:#1a1a18;--ink-2:#5c5b57;--line:#dcdad3;--bg:#f7f6f2;--card:#fff;
  --key:#1a1a18;--no:#a8352c}
*{box-sizing:border-box}
body{margin:0;padding:40px 20px;background:var(--bg);color:var(--ink);
  font:16px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:620px;margin:0 auto}
h1{font-size:1.7rem;margin:0 0 6px}
.lead{color:var(--ink-2);margin:0 0 26px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
  padding:22px;margin-bottom:18px}
h2{font-size:1rem;margin:0 0 18px;letter-spacing:.01em}
.f{margin-bottom:18px}
.f:last-child{margin-bottom:0}
label{display:block;font-weight:500;font-size:.94rem;margin-bottom:6px}
input,select{width:100%;padding:11px 12px;border:1px solid var(--line);
  border-radius:8px;font:inherit;background:var(--card);color:var(--ink)}
input:focus,select:focus{outline:2px solid var(--ink);outline-offset:1px}
.help{display:block;color:var(--ink-2);font-size:.83rem;margin-top:6px}
.row{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.days{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:20px}
.day{display:flex;align-items:center;gap:6px;font-weight:400;margin:0;
  border:1px solid var(--line);border-radius:999px;padding:7px 13px}
.day input{width:auto;padding:0}
button{width:100%;padding:14px;border:0;border-radius:9px;background:var(--key);
  color:#fff;font:inherit;font-weight:600;cursor:pointer}
button:hover{opacity:.9}
.error{background:#fdf0ef;border:1px solid var(--no);color:var(--no);
  padding:12px 14px;border-radius:9px;margin:0 0 20px}
.foot{color:var(--ink-2);font-size:.85rem;text-align:center;margin:14px 0 0}
@media (max-width:520px){.row{grid-template-columns:1fr}}
"""


@router.get("/start", response_class=HTMLResponse)
def start_form() -> HTMLResponse:
    _require_open()
    return HTMLResponse(_page())


@router.post("/start")
async def create_venue(request: Request) -> Any:
    """Onboard, set the week, mint a link, and land them on their dashboard.

    The form is read by hand rather than through Form(...) parameters so that
    _require_open() is genuinely the first thing that runs. Declared fields are
    validated by FastAPI BEFORE the body of the handler, so a disabled install
    answered a malformed POST with 422 while answering GET with 404 -- which
    told anyone probing that the route was there and merely switched off.

    Failures redraw the form with what they typed still in it. A signup form
    that empties itself on a bad invite code is one people abandon.
    """
    _require_open()
    _rate_limit(request)

    form = await request.form()

    def field(key: str, fallback: str = "") -> str:
        value = form.get(key)
        return value.strip() if isinstance(value, str) else fallback

    name = field("name")
    opens = field("opens")
    closes = field("closes")
    description = field("description")
    agent_name = field("agent_name")
    vertical = field("vertical", "restaurant") or "restaurant"
    country = field("country", "IN") or "IN"
    phone = field("phone")
    seats = field("seats")
    code = field("code")
    days = [value for value in form.getlist("days") if isinstance(value, str)]

    typed = {
        "name": name, "description": description, "agent_name": agent_name,
        "vertical": vertical, "country": country, "phone": phone, "seats": seats,
        "opens": opens, "closes": closes, "days": ",".join(days),
    }

    def refuse(message: str) -> HTMLResponse:
        # 200, not 4xx: this is the form again, for a person, not an API error.
        return HTMLResponse(_page(error=message, values=typed))

    if settings.signup_code and code != settings.signup_code:
        return refuse("That invite code is not right.")
    if not name:
        return refuse("Your venue needs a name.")
    if not (_TIME_RE.match(opens) and _TIME_RE.match(closes)):
        return refuse("Opening and closing times need to look like 18:00.")
    if opens >= closes:
        return refuse(
            "Closing time has to be after opening time. If you serve past "
            "midnight, set it to 23:59 for now and adjust it in Hours."
        )
    weekdays = sorted({int(day) for day in days if day.isdigit() and 0 <= int(day) <= 6})
    if not weekdays:
        return refuse("Pick at least one day you're open.")

    seats_value: int | None = None
    if seats:
        if not seats.isdigit():
            return refuse("How many people you can seat has to be a number.")
        seats_value = int(seats)

    locale = _country(country)
    config: dict[str, Any] = {
        "identity": {
            "display_name": name,
            "description": description,
            "agent_name": agent_name,
        },
        "locale": {
            "country": locale["code"],
            "currency": locale["currency"],
            "currency_symbol": locale["symbol"],
            "currency_name": locale["currency_name"],
            "timezone": locale["timezone"],
            "clock": locale["clock"],
        },
        # No number given means no limit, per PRD §15d: an agent that takes
        # everything is a working venue; one that blocks on a capacity nobody
        # set is a venue whose phone refuses people.
        "capacity": {"enabled": seats_value is not None},
    }
    if vertical not in business_config.known_verticals():
        vertical = "restaurant"

    try:
        business = businesses.create(
            slug=_free_slug(name),
            name=name,
            timezone=locale["timezone"],
            vertical=vertical,
            phone_number=phone or None,
            config=config,
        )
    except business_config.ConfigError as exc:
        return refuse(str(exc))
    except Exception as exc:  # noqa: BLE001 -- the unique indexes speak here
        text = str(exc).lower()
        if "businesses_phone_key" in text:
            return refuse("That phone number already answers for another venue.")
        if "businesses_slug_key" in text:
            return refuse("Somebody just took that name. Try a slightly different one.")
        raise

    businesses.set_capacity_rules(
        business.id,
        [
            {
                "weekday": weekday,
                "start_time": opens,
                "end_time": closes,
                "total_units": seats_value or 0,
                "slot_minutes": 30,
                "turn_minutes": 90,
                "label": "",
            }
            for weekday in weekdays
        ],
    )

    _record_signup(request)
    secret = issue_dashboard_token(business.id, label="self-serve signup")
    # 303 to a page that SHOWS them the link, rather than straight into the
    # dashboard. Straight in looked tidier and quietly stranded people: the
    # dashboard lifts the token out of the URL into localStorage, so the owner
    # never saw the one thing they need to get back in from another device --
    # and nobody can hand it to them later, because only its hash is stored.
    # Still a redirect, not HTML from the POST: a refresh must not re-post the
    # form and onboard them twice.
    return RedirectResponse(f"/start/ready?token={secret}", status_code=303)


# --- the link, shown once -----------------------------------------------------


def _base_url(request: Request) -> str:
    """Where this venue's dashboard lives, as the owner should save it.

    DASHBOARD_BASE_URL wins because that is the public name of the service;
    the request's own host is the fallback for a deployment that has not set
    it, and is right often enough to be better than nothing.
    """
    return (settings.dashboard_base_url or str(request.base_url)).rstrip("/")


def _ready_page(*, venue: str, link: str) -> str:
    from html import escape

    safe_link = escape(link)
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>{escape(venue)} is ready</title>
<style>{_STYLE}{_READY_STYLE}</style>
</head><body><div class="wrap">
  <h1>{escape(venue)} is ready</h1>
  <p class="lead">One thing to do before you go any further.</p>

  <div class="card">
    <h2>Save this link</h2>
    <p class="say">It is the only way into your dashboard. There is no password
      and no account -- whoever has this link is you. We store it scrambled, so
      if you lose it we cannot look it up or send it again.</p>
    <label for="link" class="sr">Your dashboard link</label>
    <input id="link" value="{safe_link}" readonly onfocus="this.select()">
    <div class="acts">
      <button type="button" id="copy" data-link="{safe_link}">Copy link</button>
      <a class="ghost" href="{safe_link}">Open my dashboard</a>
    </div>
    <p class="say">Email it to yourself, or bookmark it on the phone or tablet
      you will actually use behind the counter.</p>
  </div>
</div>
<script>
// Progressive: the input is selectable and the link is a plain anchor, so a
// browser with no clipboard API and no JS at all still hands over the link.
document.getElementById("copy").addEventListener("click", async function(){{
  var field = document.getElementById("link");
  field.select();
  try {{
    await navigator.clipboard.writeText(this.dataset.link);
  }} catch (e) {{
    try {{ document.execCommand("copy"); }} catch (e2) {{ /* selected; copy by hand */ }}
  }}
  this.textContent = "Copied";
  setTimeout(function(){{ document.getElementById("copy").textContent = "Copy link"; }}, 2000);
}});
</script>
</body></html>"""


_READY_STYLE = """
.say{color:var(--ink-2);font-size:.9rem;margin:0 0 16px}
.card .say:last-child{margin:16px 0 0}
#link{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.86rem;
  background:var(--bg);word-break:break-all}
.acts{display:flex;flex-wrap:wrap;gap:10px;margin-top:14px}
.acts button{width:auto;padding:12px 20px}
.acts .ghost{display:inline-flex;align-items:center;padding:12px 20px;border-radius:9px;
  border:1px solid var(--line);color:var(--ink);text-decoration:none;font-weight:500}
.acts .ghost:hover{border-color:var(--ink)}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
"""


@router.get("/start/ready", response_class=HTMLResponse)
def ready(request: Request, token: str = "") -> HTMLResponse:
    """Show the link once, before anything can swallow it.

    The token is resolved rather than trusted: a page that printed whatever
    string arrived would happily tell somebody a typo was their way back in.
    """
    _require_open()
    business = business_for_token(token)
    if business is None:
        raise HTTPException(404, "Not found")
    identity = business.config["identity"]
    return HTMLResponse(
        _ready_page(
            venue=identity["display_name"] or business.name,
            link=f"{_base_url(request)}/dashboard?token={token}",
        )
    )
