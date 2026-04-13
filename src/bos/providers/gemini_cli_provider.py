"""Gemini CLI Provider Extension for TradingDesk.

Uses the same OAuth credentials as Antigravity but routes through the
production Cloud Code Assist endpoint with Gemini CLI-style headers.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from bos.core import LLMResponse, ToolCallRequest, ep_provider
from bos.providers.antigravity_provider import (
    _AUTH_URL,
    _CLIENT_KEY,
    _CLIENT_VAL,
    _REDIRECT_URI,
    _SCOPES,
    _convert_messages,
    _convert_tools,
    _discover_antigravity_project,
    _iter_sse,
    _progress,
)
from bos.providers.google_oauth import (
    OAuthCredentials,
    exchange_code_for_tokens,
    generate_pkce,
    get_user_email,
    refresh_token,
    start_callback_server,
    wait_for_callback,
)

logger = logging.getLogger("bos")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GEMINI_CLI_ENDPOINT = "https://cloudcode-pa.googleapis.com"
SUPPORTED_MODELS = [
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-3-flash-preview",
    "gemini-3-pro-preview",
    "gemini-3.1-pro-preview",
]

_DEFAULT_AUTH_PATH = Path.home() / ".config" / "bos" / "gemini_cli_auth.default.json"


def _get_gemini_cli_headers() -> dict[str, str]:
    return {
        "User-Agent": "google-api-nodejs-client/9.15.1",
        "X-Goog-Api-Client": "gl-node/22.17.0",
        "Client-Metadata": "ideType=IDE_UNSPECIFIED,platform=PLATFORM_UNSPECIFIED,pluginType=GEMINI",
    }


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


async def login_gemini_cli(
    on_auth: Any | None = None,
    on_progress: Any | None = None,
) -> OAuthCredentials:
    """Interactive OAuth login for the Gemini CLI provider."""
    verifier, challenge = generate_pkce()

    _progress(on_progress, "Starting local server for OAuth callback...")
    server = start_callback_server(port=51121, path="/oauth-callback")

    try:
        params = urlencode(
            {
                "client_id": _CLIENT_KEY,
                "response_type": "code",
                "redirect_uri": _REDIRECT_URI,
                "scope": " ".join(_SCOPES),
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": verifier,
                "access_type": "offline",
                "prompt": "consent",
            }
        )
        auth_url = f"{_AUTH_URL}?{params}"

        if on_auth:
            on_auth(auth_url, "Complete the sign-in in your browser.")

        _progress(on_progress, "Waiting for OAuth callback...")
        result = wait_for_callback(server)
        if not result or not result.get("code"):
            raise RuntimeError("No authorization code received")

        if result.get("state") != verifier:
            raise RuntimeError("OAuth state mismatch – possible CSRF attack")

        code = result["code"]

        _progress(on_progress, "Exchanging authorization code for tokens...")
        token_data = await exchange_code_for_tokens(code, _CLIENT_KEY, _CLIENT_VAL, _REDIRECT_URI, verifier)

        if "refresh_token" not in token_data:
            raise RuntimeError("No refresh token received. Please try again.")

        access = token_data["access_token"]
        refresh = token_data["refresh_token"]
        expires = time.time() * 1000 + token_data["expires_in"] * 1000 - 5 * 60 * 1000

        _progress(on_progress, "Getting user info...")
        email = await get_user_email(access)

        _progress(on_progress, "Discovering project...")
        project_id = await _discover_antigravity_project(access, on_progress)

        # _discover_antigravity_project falls back to the Antigravity default project.
        # We don't want to use that for Gemini CLI as it often fails with 403 on non-Googler accounts.
        from bos.providers.antigravity_provider import _DEFAULT_PROJECT_ID

        if project_id == _DEFAULT_PROJECT_ID:
            project_id = ""

        return OAuthCredentials(
            refresh=refresh,
            access=access,
            expires=expires,
            project_id=project_id,
            email=email,
        )
    finally:
        server.shutdown()


# ---------------------------------------------------------------------------
# Token loading / refresh
# ---------------------------------------------------------------------------


async def get_gemini_cli_token(auth_file: str | None = None) -> OAuthCredentials:
    """Load and refresh Gemini CLI credentials."""
    path_str = auth_file or os.environ.get("BOS_GEMINICLI_AUTH_FILE") or str(_DEFAULT_AUTH_PATH)
    path = Path(path_str)

    if not path.exists():
        raise RuntimeError(
            f"Gemini CLI credentials not found at {path}. Run `bos auth gemini-cli` to authenticate first."
        )

    data = json.loads(path.read_text())
    creds = OAuthCredentials(**data)

    # Refresh if expired
    if creds.expires and creds.expires < time.time() * 1000:
        logger.debug("Refreshing expired Gemini CLI token")
        token_data = await refresh_token(
            refresh=creds.refresh,
            client_id=_CLIENT_KEY,
            client_secret=_CLIENT_VAL,
        )
        creds = OAuthCredentials(
            refresh=creds.refresh,
            access=token_data["access_token"],
            expires=time.time() * 1000 + token_data["expires_in"] * 1000 - 5 * 60 * 1000,
            project_id=data.get("project_id", ""),
            email=data.get("email"),
        )
        path.write_text(json.dumps(creds.__dict__))

    return creds


# ---------------------------------------------------------------------------
# SSE consumer (reuses _iter_sse from antigravity_provider)
# ---------------------------------------------------------------------------


async def _consume_sse(response: httpx.Response) -> tuple[str, list[ToolCallRequest], str]:
    content = ""
    tool_calls: list[ToolCallRequest] = []
    finish_reason = "stop"

    async for event in _iter_sse(response):
        # Unwrap the response envelope if present
        if "response" in event and isinstance(event["response"], dict):
            event = event["response"]

        # serverContent wrapping
        server_content = event.get("serverContent")
        if server_content:
            model_turn = server_content.get("modelTurn")
            if model_turn:
                parts = model_turn.get("parts", [])
                for part in parts:
                    if "text" in part:
                        content += part["text"]
                    if "functionCall" in part:
                        fc = part["functionCall"]
                        tool_calls.append(
                            ToolCallRequest(
                                id=f"call_{len(tool_calls)}_{int(time.time())}",
                                name=fc.get("name"),
                                arguments=fc.get("args", {}),
                            )
                        )
            continue

        # candidates array
        candidates = event.get("candidates")
        if candidates:
            for candidate in candidates:
                candidate_content = candidate.get("content", {})
                parts = candidate_content.get("parts", [])
                for part in parts:
                    if "text" in part:
                        content += part["text"]
                    elif "thought" in part and part.get("thought") is True:
                        pass
                    elif "functionCall" in part:
                        fc = part["functionCall"]
                        tool_calls.append(
                            ToolCallRequest(
                                id=f"call_{len(tool_calls)}_{int(time.time())}",
                                name=fc.get("name"),
                                arguments=fc.get("args", {}),
                            )
                        )
                finish = candidate.get("finishReason")
                if finish:
                    finish_reason = finish.lower()
            continue

        logger.debug("Unknown Gemini CLI SSE event: %s", json.dumps(event)[:500])

    return content, tool_calls, finish_reason


# ---------------------------------------------------------------------------
# Provider entry point
# ---------------------------------------------------------------------------


@ep_provider(name="gemini-cli")
async def gemini_cli_complete(
    messages: list[dict[str, Any]],
    model: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.7,
    reasoning_effort: str | None = None,
    tool_choice: str | dict[str, Any] | None = None,
    auth_file: str | None = None,
    **kwargs: Any,
) -> LLMResponse:
    """Use Gemini CLI to call Cloud Code Assist streamGenerateContent."""
    model = model or "gemini-2.5-flash"

    token_creds = await get_gemini_cli_token(auth_file)
    access_token = token_creds.access
    project_id = token_creds.project_id

    system_prompt, contents = _convert_messages(messages)

    gen_config: dict[str, Any] = {
        "temperature": temperature,
        "maxOutputTokens": max_tokens,
    }

    request: dict[str, Any] = {"contents": contents, "generationConfig": gen_config}

    if system_prompt:
        request["systemInstruction"] = {
            "role": "user",
            "parts": [{"text": system_prompt}],
        }

    if tools:
        request["tools"] = _convert_tools(tools)
        if tool_choice:
            request["toolConfig"] = {
                "functionCallingConfig": {"mode": tool_choice.upper() if isinstance(tool_choice, str) else "AUTO"},
            }

    body: dict[str, Any] = {
        "project": project_id,
        "model": model,
        "request": request,
    }
    headers = _get_gemini_cli_headers()
    headers.update(
        {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
    )

    try:
        async with httpx.AsyncClient(timeout=300) as client:
            response = await client.post(
                f"{GEMINI_CLI_ENDPOINT}/v1internal:streamGenerateContent?alt=sse",
                headers=headers,
                json=body,
            )
            if response.is_success:
                content, extracted_tools, finish_reason = await _consume_sse(response)
                return LLMResponse(
                    content=content,
                    tool_calls=extracted_tools,
                    finish_reason=finish_reason,
                )
            else:
                error_msg = f"API error ({response.status_code}): {response.text}"
    except Exception as e:
        error_msg = str(e)

    return LLMResponse(
        content=f"Error calling Gemini CLI: {error_msg}",
        finish_reason="error",
    )
