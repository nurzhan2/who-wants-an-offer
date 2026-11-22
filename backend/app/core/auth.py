"""One password in front of a dashboard that runs on a server.

The project was a laptop program: the API listened on localhost and the only
person who could reach it was the owner. Phase 22b puts it on a server with a
public URL, and the dashboard shows the owner's CVs, contact block, letters and
applications — so every request needs the password, except two:

* ``/health``, which the platform's health check calls and which says nothing;
* the agent's queue under ``/api/v1/applications``, which has its own bearer
  token (``AGENT_API_TOKEN``) and is called by a program, not a browser.

HTTP Basic, any user name: the browser asks once and sends it with every
request after, so the dashboard needed no login screen. Over HTTPS only — the
platform terminates TLS in front of the service. Unset ``DASHBOARD_PASSWORD``
means no password at all, which is the laptop case and the behaviour before.
"""

import base64
import binascii
import secrets
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import settings

#: Paths that never ask for the password: the platform's probe, and the agent's
#: own token-guarded seam.
OPEN_PREFIXES: tuple[str, ...] = ("/health",)


def _token_guarded(path: str) -> bool:
    """Paths with their own guard: the agent's bearer token, and the Mini App,
    whose every API call carries Telegram's signed ``initData`` (see
    ``app.core.telegram_auth``) and whose page and bundles carry no data."""
    return (
        path.startswith(f"{settings.api_v1_prefix}/applications")
        or path.startswith(f"{settings.api_v1_prefix}/miniapp")
        or path in {"/tg", "/tg.html"}
        or path.startswith("/assets/")
    )


def password_from(header: str | None) -> str | None:
    """The password in a Basic ``Authorization`` header, or None."""
    if not header or not header.startswith("Basic "):
        return None
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    _, separator, password = decoded.partition(":")
    return password if separator else None


class DashboardPasswordMiddleware(BaseHTTPMiddleware):
    """Refuse every request without the dashboard password, when one is set."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        configured = settings.dashboard_password
        path = request.url.path
        if (
            configured is None
            or request.method == "OPTIONS"
            or path.startswith(OPEN_PREFIXES)
            or _token_guarded(path)
        ):
            return await call_next(request)
        presented = password_from(request.headers.get("authorization")) or ""
        # compare_digest on both branches: a missing header and a wrong password
        # take the same path and the same time.
        if secrets.compare_digest(presented, configured.get_secret_value()):
            return await call_next(request)
        return Response(
            status_code=401,
            content="Нужен пароль дашборда.",
            headers={"WWW-Authenticate": 'Basic realm="who-wants-an-offer", charset="UTF-8"'},
            media_type="text/plain; charset=utf-8",
        )
