import secrets
from urllib.parse import quote, urlsplit

from fastapi import Request
from fastapi.responses import RedirectResponse

from mileage_logger.config import Settings, get_settings

WEB_AUTH_SESSION_KEY = "web_authenticated"
WEB_AUTH_OPEN_PATHS = {
    "/login",
    "/logout",
    "/favicon.ico",
}


def web_login_enabled(settings: Settings | None = None) -> bool:
    """Return whether web UI login is enabled by configured username and password."""

    active_settings = settings or get_settings()
    return bool(active_settings.web_login_username and active_settings.web_login_password)


def valid_next_path(value: str | None) -> str:
    """Return a safe local redirect path after login or logout."""

    if not value:
        return "/"
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        return "/"
    if parsed.path.startswith("/api") or parsed.path in WEB_AUTH_OPEN_PATHS:
        return "/"
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{parsed.path}{query}"


def login_redirect_for_request(request: Request) -> RedirectResponse:
    """Build the login redirect for an unauthenticated web UI request."""

    next_path = request.url.path
    if request.url.query:
        next_path = f"{next_path}?{request.url.query}"
    return RedirectResponse(
        url=f"/login?next={quote(valid_next_path(next_path), safe='')}",
        status_code=303,
    )


def request_is_authenticated(request: Request) -> bool:
    """Check whether the current signed session is logged into the web UI."""

    return request.session.get(WEB_AUTH_SESSION_KEY) is True


def authenticate_web_credentials(
    username: str,
    password: str,
    settings: Settings | None = None,
) -> bool:
    """Validate submitted web UI credentials with constant-time comparisons."""

    active_settings = settings or get_settings()
    username_matches = secrets.compare_digest(username, active_settings.web_login_username)
    password_matches = secrets.compare_digest(password, active_settings.web_login_password)
    return username_matches and password_matches


def mark_request_authenticated(request: Request) -> None:
    """Mark the current signed session as authenticated for web UI pages."""

    request.session[WEB_AUTH_SESSION_KEY] = True


def clear_request_authentication(request: Request) -> None:
    """Remove web UI authentication state from the current signed session."""

    request.session.pop(WEB_AUTH_SESSION_KEY, None)


async def enforce_web_login(request: Request, call_next):
    """Require login for rendered web pages while leaving API and static paths open."""

    if not web_login_enabled():
        return await call_next(request)

    path = request.url.path
    if path == "/api" or path.startswith("/api/"):
        return await call_next(request)
    if path.startswith("/static/") or path in WEB_AUTH_OPEN_PATHS:
        return await call_next(request)
    if request_is_authenticated(request):
        return await call_next(request)
    return login_redirect_for_request(request)
