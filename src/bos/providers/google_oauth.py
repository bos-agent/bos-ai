"""
OAuth utilities shared by the Google Gemini CLI and Antigravity providers.

Includes:
- PKCE challenge/verifier generation
- Local HTTP callback server for receiving OAuth redirects
- Token exchange and refresh helpers
- Project discovery via Cloud Code Assist API
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

# ---------------------------------------------------------------------------
# PKCE helpers  (mirrors utils/oauth/pkce.ts)
# ---------------------------------------------------------------------------


def generate_pkce() -> tuple[str, str]:
    """
    Generate a PKCE verifier and challenge pair.

    Returns:
        ``(verifier, challenge)`` where *challenge* is the S256-hashed verifier.
    """
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


# ---------------------------------------------------------------------------
# OAuth credentials
# ---------------------------------------------------------------------------


@dataclass
class OAuthCredentials:
    refresh: str = ""
    access: str = ""
    expires: float = 0.0  # ms-epoch
    project_id: str = ""
    email: str | None = None


# ---------------------------------------------------------------------------
# Local callback server
# ---------------------------------------------------------------------------


class _CallbackHandler(BaseHTTPRequestHandler):
    """Tiny handler that captures the ``code`` and ``state`` query params."""

    # Shared across requests via the server instance
    result: dict[str, str] | None = None

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        error = params.get("error", [None])[0]
        if error:
            self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(f"<h2>Authentication Failed</h2><p>Error: {error}</p>".encode())
            return

        code = params.get("code", [None])[0]
        state = params.get("state", [None])[0]

        if code and state:
            self.server._oauth_result = {"code": code, "state": state}  # type: ignore[attr-defined]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<h2>Authentication Successful</h2><p>You can close this window and return to the terminal.</p>"
            )
        else:
            self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>Missing code or state</h2>")

    # Silence request logs
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass


def start_callback_server(port: int, path: str = "/oauth2callback") -> HTTPServer:
    """
    Start a local HTTP server on *port* that waits for an OAuth redirect.

    The captured result dict (``{"code": ..., "state": ...}``) is stored as
    ``server._oauth_result``.
    """
    server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server._oauth_result = None  # type: ignore[attr-defined]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def wait_for_callback(server: HTTPServer, timeout: float = 300) -> dict[str, str] | None:
    """Block until the callback server captures a result, or *timeout* elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = getattr(server, "_oauth_result", None)
        if result is not None:
            return result
        time.sleep(0.1)
    return None


# ---------------------------------------------------------------------------
# Token exchange / refresh
# ---------------------------------------------------------------------------

TOKEN_URL = "https://oauth2.googleapis.com/token"


async def exchange_code_for_tokens(
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    verifier: str,
) -> dict[str, Any]:
    """Exchange an authorization *code* for access + refresh tokens."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            },
        )
        resp.raise_for_status()
        return resp.json()


async def refresh_token(
    refresh: str,
    client_id: str,
    client_secret: str,
) -> dict[str, Any]:
    """Refresh an expired access token."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh,
                "grant_type": "refresh_token",
            },
        )
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# User info
# ---------------------------------------------------------------------------


async def get_user_email(access_token: str) -> str | None:
    """Fetch the authenticated user's email, or ``None`` on failure."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://www.googleapis.com/oauth2/v1/userinfo",
                params={"alt": "json"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.is_success:
                return resp.json().get("email")
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Parse a pasted redirect URL
# ---------------------------------------------------------------------------


def parse_redirect_url(raw: str) -> dict[str, str | None]:
    """Extract ``code`` and ``state`` from a pasted redirect URL."""
    raw = raw.strip()
    if not raw:
        return {"code": None, "state": None}
    try:
        parsed = urlparse(raw)
        qs = parse_qs(parsed.query)
        return {
            "code": qs.get("code", [None])[0],
            "state": qs.get("state", [None])[0],
        }
    except Exception:
        return {"code": None, "state": None}
