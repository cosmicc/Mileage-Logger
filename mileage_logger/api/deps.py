import base64
import secrets

from fastapi import HTTPException, Request, status

from mileage_logger.config import get_settings


def verify_owntracks_auth(request: Request) -> None:
    settings = get_settings()
    token_configured = bool(settings.owntracks_api_token)
    basic_configured = bool(settings.owntracks_username or settings.owntracks_password)

    if not token_configured and not basic_configured:
        return

    if token_configured:
        header_token = request.headers.get("x-api-key", "")
        authorization = request.headers.get("authorization", "")
        bearer_token = authorization.removeprefix("Bearer ").strip()
        header_matches = secrets.compare_digest(header_token, settings.owntracks_api_token)
        bearer_matches = secrets.compare_digest(
            bearer_token,
            settings.owntracks_api_token,
        )
        if header_matches or bearer_matches:
            return

    if basic_configured:
        authorization = request.headers.get("authorization", "")
        if authorization.startswith("Basic "):
            encoded = authorization.removeprefix("Basic ").strip()
            try:
                username, password = base64.b64decode(encoded).decode("utf-8").split(":", 1)
            except (ValueError, UnicodeDecodeError):
                username, password = "", ""
            username_matches = secrets.compare_digest(username, settings.owntracks_username)
            password_matches = secrets.compare_digest(
                password,
                settings.owntracks_password,
            )
            if username_matches and password_matches:
                return

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
