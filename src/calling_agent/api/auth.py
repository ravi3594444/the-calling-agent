"""Signing in: how an owner gets a working link when theirs is gone.

The dashboard link is unchanged and still the thing that actually authorises a
request. All this does is mint one and put it in a cookie, so every handler
downstream of `current_business` is untouched -- there is one kind of
credential in this product, not two.

WHY THERE IS STILL NO LOGIN DURING SERVICE
PRD §14 is right that a host mid-rush should not type a password on a shared
tablet, and they do not have to: the saved link keeps working, and a session
cookie lasts SESSION_DAYS. /login is for the evening somebody clears their
browser, or sets up the tablet by the door for the first time, or picks up a
phone that has never seen the venue.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import businesses, owners
from ..config import settings
from .deps import business_for_token, issue_dashboard_token, revoke_dashboard_token
from .signup import _STYLE

router = APIRouter(tags=["auth"])

#: The cookie `current_business` already reads. Reusing it rather than
#: inventing a session cookie is the whole point: one credential, one code path.
COOKIE = "tableline_token"


def _cookie_kwargs(request: Request) -> dict[str, object]:
    """Secure only over https, so this still works on a local http deployment.

    Lax rather than Strict: a venue clicking their own dashboard link out of an
    email is a top-level navigation, and Strict would drop the cookie on it.
    """
    return {
        "httponly": True,
        "samesite": "lax",
        "secure": request.url.scheme == "https",
        "max_age": settings.session_days * 24 * 3600,
        "path": "/",
    }


def _page(*, error: str = "", email: str = "") -> str:
    from html import escape

    offer = (
        '<p class="foot">No venue yet? <a href="/start">Set one up</a>.</p>'
        if settings.signup_enabled
        else ""
    )
    problem = f'<p class="error" role="alert">{escape(error)}</p>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Sign in</title>
<style>{_STYLE}{_LOGIN_STYLE}</style>
</head><body><div class="wrap narrow">
  <h1>Sign in</h1>
  <p class="lead">To your venue's dashboard.</p>
  {problem}
  <form method="post" action="/login">
    <div class="card">
      <div class="f"><label for="email">Email</label>
        <input id="email" name="email" type="email" required autocomplete="username"
               value="{escape(email)}" autofocus></div>
      <div class="f"><label for="password">Password</label>
        <input id="password" name="password" type="password" required
               autocomplete="current-password"></div>
    </div>
    <button type="submit">Sign in</button>
  </form>
  <p class="foot">Forgotten it? We can't email you a reset yet — ask whoever
    set this up for you.</p>
  {offer}
</div></body></html>"""


_LOGIN_STYLE = """
.wrap.narrow{max-width:420px}
.foot a{color:inherit}
"""


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request) -> HTMLResponse:
    """Already signed in? Straight through, rather than asking again."""
    if business_for_token(request.cookies.get(COOKIE, "")) is not None:
        return RedirectResponse("/dashboard", status_code=303)
    return HTMLResponse(_page())


@router.post("/login")
async def login(request: Request) -> Response:
    """Mint a dashboard token for whoever proved they own the venue.

    Read by hand rather than through Form(...) parameters so a submission
    missing a field is the form again with a sentence on it, not a 422 of JSON
    in front of somebody trying to log in.
    """
    form = await request.form()
    email = str(form.get("email") or "").strip()
    password = str(form.get("password") or "")

    owner = owners.authenticate(email, password) if email and password else None
    if owner is None:
        # One message for both, deliberately: saying which was wrong turns this
        # form into a way to ask whether a venue exists. 200, not 401 -- it is
        # the page again, for a person.
        return HTMLResponse(_page(error="That email or password is wrong.", email=email))

    try:
        business = businesses.by_id(owner.business_id)
    except businesses.UnknownBusiness:
        raise HTTPException(500, "That account's venue no longer exists.") from None

    secret = issue_dashboard_token(
        business.id,
        label="signed in",
        # Unlike the link handed out at signup, a session ends. The link is
        # something they chose to keep; this is a browser they happened to use.
        expires_at=datetime.now(UTC) + timedelta(days=settings.session_days),
    )
    response = RedirectResponse("/dashboard", status_code=303)
    response.set_cookie(COOKIE, secret, **_cookie_kwargs(request))
    return response


@router.post("/logout")
def logout(request: Request) -> Response:
    """Revoke the token, not just the cookie.

    Clearing the cookie alone leaves a live credential in whatever copied it.
    """
    raw = request.cookies.get(COOKIE, "")
    business = business_for_token(raw)
    if business is not None:
        revoke_dashboard_token(business.id, raw)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE, path="/")
    return response
