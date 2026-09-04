"""Sign in, sign out, the SSO callback, and password management.

Every password screen here begins by asking the auth chain which provider owns
the signed-in identity's password. Under OIDC or behind an authenticating
gateway nobody here does, and the page says where the password actually lives
instead of offering a form that would change nothing.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from starlette.responses import RedirectResponse, Response

from app.auth.base import AuthError, PasswordsNotManaged
from app.auth.oidc import OIDCAuth
from app.core.errors import AuthenticationRequired, NotFound
from app.core.results import Record
from app.web.deps import View, build_view

router = APIRouter(tags=["auth"])


def _safe_next(raw: str) -> str:
    """Only allow redirects back into this site.

    Without this check, ``?next=https://elsewhere.example`` turns the login page
    into an open redirect that lends this site's credibility to a phishing page.
    """
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return "/"
    return raw


def _login_page(view: View, *, next_url: str, error: str = "", username: str = "",
                status_code: int = 200) -> Response:
    """Render the sign-in form.

    One helper rather than five near-identical render calls, so the page cannot
    drift between the paths that reach it -- a bad password, a failed SSO
    callback and a first visit should all look the same.
    """
    return view.render(
        "auth/login.html",
        next_url=next_url,
        providers=view.state.auth.interactive,
        error=error,
        username=username,
        # Only offer the link when something can actually honour it.
        reset_available=view.state.auth.self_service_reset() is not None,
        status_code=status_code,
    )


@router.get("/login", name="login")
async def login_page(view: View = Depends(build_view)) -> Response:
    next_url = _safe_next(view.param("next"))
    if view.identity.is_authenticated:
        return view.redirect(next_url)
    return _login_page(view, next_url=next_url)


@router.post("/login")
async def do_login(request: Request, view: View = Depends(build_view)) -> Response:
    """Verify credentials against the configured interactive provider."""
    if not view.state.login_limiter.check(view.client_ip):
        # 429, not 401: the credentials were never examined. Saying so lets a
        # legitimate client back off instead of retrying immediately, and does
        # not tell an attacker anything about the password they sent.
        wait = view.state.login_limiter.retry_after(view.client_ip)
        return _login_page(
            view,
            next_url=_safe_next(str((await request.form()).get("next", ""))),
            error=f"Too many sign-in attempts from here. Try again in {wait} seconds.",
            status_code=429,
        )

    form = await request.form()
    next_url = _safe_next(str(form.get("next", "")))
    username = str(form.get("username", ""))

    provider = view.state.auth.get(view.settings.login_provider)
    if provider is None:
        return _login_page(
            view,
            next_url=next_url,
            error="No password sign-in is configured on this deployment.",
            username=username,
            status_code=500,
        )

    try:
        identity = await provider.login(request, dict(form))
    except AuthError as exc:
        # 401 with the form re-rendered, so both a browser and a scripted client
        # see the refusal.
        return _login_page(
            view, next_url=next_url, error=exc.message, username=username, status_code=401
        )

    if identity is None:
        return _login_page(
            view,
            next_url=next_url,
            error="That email address and password do not match.",
            username=username,
            status_code=401,
        )

    # A proved identity clears the budget: someone who mistyped twice before
    # getting it right should not be rationed for the rest of the hour.
    view.state.login_limiter.reset(view.client_ip)

    response: Response = view.redirect(next_url)
    view.state.sessions.save_identity(response, identity)
    return view.toast(response, f"Welcome back, {identity.label}.", "success")


@router.get("/auth/{provider_name}/start", name="auth_start")
async def start_sso(provider_name: str, request: Request, view: View = Depends(build_view)) -> Response:
    """Begin a redirect-based sign-in."""
    provider = view.state.auth.get(provider_name)
    if provider is None:
        return view.redirect("/login")

    next_url = _safe_next(view.param("next"))
    url = await provider.login_url(request, next_url)
    if not url:
        return view.redirect("/login")

    response = RedirectResponse(url, status_code=303)
    if isinstance(provider, OIDCAuth):
        # The flow's state, nonce and PKCE verifier ride in a short-lived cookie
        # so the callback can verify what it started.
        provider.attach_flow(request, response)
    return response


@router.get("/auth/callback", name="auth_callback")
async def auth_callback(request: Request, view: View = Depends(build_view)) -> Response:
    """Complete a redirect-based sign-in."""
    provider = view.state.auth.get(view.settings.login_provider)
    if provider is None or not provider.interactive:
        provider = next(iter(view.state.auth.interactive), None)
    if provider is None:
        return view.redirect("/login")

    try:
        identity = await provider.callback(request)
    except AuthError as exc:
        return _login_page(view, next_url="/", error=exc.message, status_code=401)

    if identity is None:
        return view.redirect("/login")

    next_url = provider.next_url(request) if isinstance(provider, OIDCAuth) else "/"
    response: Response = RedirectResponse(_safe_next(next_url), status_code=303)
    view.state.sessions.save_identity(response, identity)
    if isinstance(provider, OIDCAuth):
        provider.clear_flow(response)
    return response


@router.post("/logout", name="logout")
@router.get("/logout")
async def logout(request: Request, view: View = Depends(build_view)) -> Response:
    """Sign out and clear the session cookie."""
    response: Response = view.redirect("/login")
    provider = view.state.auth.get(view.identity.provider)
    if provider is not None:
        # An SSO provider may want to end its own session too.
        elsewhere = await provider.logout(request, response)
        if elsewhere:
            response = RedirectResponse(elsewhere, status_code=303)
    view.state.sessions.clear(response)
    return response


@router.get("/whoami")
async def whoami(view: View = Depends(build_view)) -> Response:
    """The current identity, for debugging a deployment's auth chain."""
    identity = view.identity
    return view.json(
        {
            "authenticated": identity.is_authenticated,
            "subject": identity.subject,
            "email": identity.email,
            "display_name": identity.display_name,
            "roles": sorted(identity.roles),
            "provider": identity.provider,
        }
    )


# -- account passwords -------------------------------------------------------
#
# Every screen below asks the auth chain who owns the password before offering
# to do anything with it. Under SSO or behind a gateway the answer is "not this
# application", and the page says so rather than showing a form that would
# change nothing.


@router.get("/account/password", name="change_password")
async def change_password_page(view: View = Depends(build_view)) -> Response:
    # Not require_login(): that refuses anyone who must change their password,
    # which is precisely the person this page exists for.
    if not view.identity.is_authenticated:
        raise AuthenticationRequired()
    provider = view.state.auth.password_manager(view.identity)
    return view.render(
        "auth/password.html",
        provider=provider,
        note=_password_note(view),
        error="",
        required=view.identity.must_change_password,
        title="Password",
    )


@router.post("/account/password")
async def do_change_password(request: Request, view: View = Depends(build_view)) -> Response:
    if not view.identity.is_authenticated:
        raise AuthenticationRequired()
    provider = view.state.auth.password_manager(view.identity)

    form = await request.form()
    current = str(form.get("current_password", ""))
    new = str(form.get("new_password", ""))
    confirm = str(form.get("confirm_password", ""))

    error = ""
    if provider is None or not provider.capabilities.change:
        error = _password_note(view)
    elif new != confirm:
        error = "The two new passwords do not match."
    else:
        try:
            await provider.change_password(view.identity, current, new)
        except (AuthError, PasswordsNotManaged) as exc:
            error = exc.message

    if error:
        return view.render(
            "auth/password.html",
            provider=provider,
            note=_password_note(view),
            error=error,
            required=view.identity.must_change_password,
            title="Password",
            status_code=400,
        )

    # The session survives: the person changing their password is the person
    # holding it, and signing them out here would be a punishment for good
    # practice. Sessions elsewhere are a separate problem -- see docs/scaling.md
    # on why a cookie session cannot be revoked early.
    #
    # It is reissued, though, to drop the must-change flag it carries. Without
    # that the cookie would keep redirecting them back here after they had
    # done what was asked.
    response = view.redirect("/" if view.identity.must_change_password else "/account/password")
    view.state.sessions.save_identity(
        response, view.identity.replace(must_change_password=False)
    )
    return view.toast(response, "Your password has been changed.")


@router.get("/forgot", name="forgot_password")
async def forgot_page(view: View = Depends(build_view)) -> Response:
    provider = view.state.auth.self_service_reset()
    if provider is None:
        raise NotFound("Password reset is not available on this deployment.")
    return view.render("auth/forgot.html", error="", sent=False, title="Reset your password")


@router.post("/forgot")
async def do_forgot(request: Request, view: View = Depends(build_view)) -> Response:
    """Send a reset link, and say the same thing either way.

    The response never reveals whether the address has an account. Telling
    someone "no account with that address" turns this form into a way to
    enumerate a customer list.
    """
    provider = view.state.auth.self_service_reset()
    if provider is None:
        raise NotFound("Password reset is not available on this deployment.")

    # Rate limited on the same budget as sign-in: without it this form is a
    # way to have the application send mail to any address, repeatedly.
    if not view.state.login_limiter.check(view.client_ip):
        return view.render(
            "auth/forgot.html",
            error="Too many requests from here. Try again shortly.",
            sent=False, title="Reset your password", status_code=429,
        )

    form = await request.form()
    issued = await provider.issue_reset_token(str(form.get("username", "")))
    if issued is not None:
        token, user = issued
        await _send_reset_link(view, user, token)

    return view.render("auth/forgot.html", error="", sent=True, title="Reset your password")


@router.get("/reset", name="reset_password")
async def reset_page(view: View = Depends(build_view)) -> Response:
    provider = view.state.auth.self_service_reset()
    if provider is None:
        raise NotFound("Password reset is not available on this deployment.")
    return view.render(
        "auth/reset.html", token=view.param("token"), error="", title="Choose a new password"
    )


@router.post("/reset")
async def do_reset(request: Request, view: View = Depends(build_view)) -> Response:
    provider = view.state.auth.self_service_reset()
    if provider is None:
        raise NotFound("Password reset is not available on this deployment.")

    form = await request.form()
    token = str(form.get("token", ""))
    new = str(form.get("new_password", ""))

    error = ""
    if new != str(form.get("confirm_password", "")):
        error = "The two passwords do not match."
    else:
        try:
            user = await provider.redeem_reset_token(token, new)
        except AuthError as exc:
            error = exc.message
        else:
            if user is None:
                # One message for expired, already-used and forged: the caller
                # has the same next step in every case, and distinguishing them
                # tells someone probing which guess was closest.
                error = (
                    "That reset link is no longer valid. It may have expired or "
                    "already been used — request a new one."
                )

    if error:
        return view.render(
            "auth/reset.html", token=token, error=error,
            title="Choose a new password", status_code=400,
        )

    return view.toast(
        view.redirect("/login"), "Your password has been set. Sign in with it."
    )


def _password_note(view: View) -> str:
    """What to say when this application does not hold the password."""
    provider = view.state.auth.get(view.identity.provider)
    if provider is None:
        return "This application does not manage your password."
    return provider.password_note


async def _send_reset_link(view: View, user: Record, token: str) -> None:
    """Deliver the link through the notification channels.

    A reset link goes through the same machinery as everything else the
    application sends, which means a deployment that has configured email once
    has configured it for this too.
    """
    from app.notify import Notification, notifier
    from app.notify.base import Kind, Priority

    base = view.settings.notify_base_url.rstrip("/")
    await notifier.send(
        Notification(
            recipient=str(user.get("email", "")),
            title="Reset your password",
            body=(
                f"Someone asked to reset the password for this account. "
                f"Open {base}/reset?token={token} to choose a new one. "
                f"The link stops working once it is used, and expires within the hour. "
                f"If this was not you, no action is needed."
            ),
            kind=Kind.INFO,
            priority=Priority.URGENT,
            url=f"/reset?token={token}",
        )
    )
