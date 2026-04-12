"""Antigravity Provider Extension for TradingDesk."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, AsyncGenerator
from urllib.parse import urlencode

import httpx

from bos.core import LLMResponse, ToolCallRequest, ep_provider
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

ANTIGRAVITY_DAILY_ENDPOINT = "https://daily-cloudcode-pa.sandbox.googleapis.com"
ANTIGRAVITY_AUTOPUSH_ENDPOINT = "https://autopush-cloudcode-pa.sandbox.googleapis.com"
DEFAULT_ENDPOINT = "https://cloudcode-pa.googleapis.com"
ANTIGRAVITY_ENDPOINT_FALLBACKS = (ANTIGRAVITY_DAILY_ENDPOINT, ANTIGRAVITY_AUTOPUSH_ENDPOINT, DEFAULT_ENDPOINT)
DEFAULT_ANTIGRAVITY_VERSION = "1.20.6"
SUPPORTED_MODELS = [
    "gemini-3.1-pro-low",
    "gemini-3.1-pro-high",
    "gemini-3-flash",
    "claude-sonnet-4-6",
    "claude-opus-4-6-thinking",
]

ANTIGRAVITY_SYSTEM_INSTRUCTION = (
    "You are Antigravity, a powerful agentic AI coding assistant designed by "
    "the Google Deepmind team working on Advanced Agentic Coding.\n"
    "You are pair programming with a USER to solve their coding task.\n"
    "The task may require creating a new codebase, modifying or debugging an "
    "existing codebase, or simply answering a question.\n"
)


_CLIENT_KEY = "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com"
_CLIENT_VAL = "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"
_REDIRECT_URI = "http://localhost:51121/oauth-callback"
_SCOPES = [
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/cclog",
    "https://www.googleapis.com/auth/experimentsandconfigs",
]
_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_DEFAULT_PROJECT_ID = "rising-fact-p41fc"


def _get_antigravity_headers() -> dict[str, str]:
    version = os.environ.get("ANTIGRAVITY_VERSION", DEFAULT_ANTIGRAVITY_VERSION)
    return {
        "User-Agent": f"antigravity/{version} darwin/arm64",
        "X-Goog-Api-Client": "google-cloud-sdk vscode_cloudshelleditor/0.1",
        "Client-Metadata": json.dumps(
            {
                "ideType": "ANTIGRAVITY",
                "platform": "MACOS",
                "pluginType": "GEMINI",
            }
        ),
    }


async def _discover_antigravity_project(
    access_token: str,
    on_progress: Any | None = None,
) -> str:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": "google-api-nodejs-client/9.15.1",
        "Client-Metadata": json.dumps(
            {
                "ideType": "ANTIGRAVITY",
                "platform": "MACOS",
                "pluginType": "GEMINI",
            }
        ),
    }

    # Prod first for discovery, then sandbox fallbacks (matches reference project)
    endpoints = [
        DEFAULT_ENDPOINT,
        ANTIGRAVITY_DAILY_ENDPOINT,
        ANTIGRAVITY_AUTOPUSH_ENDPOINT,
    ]

    _progress(on_progress, "Checking for existing project...")

    async with httpx.AsyncClient(timeout=30) as client:
        for endpoint in endpoints:
            try:
                resp = await client.post(
                    f"{endpoint}/v1internal:loadCodeAssist",
                    headers=headers,
                    json={
                        "metadata": {
                            "ideType": "IDE_UNSPECIFIED",
                            "platform": "PLATFORM_UNSPECIFIED",
                            "pluginType": "GEMINI",
                        },
                    },
                )
                if resp.is_success:
                    data = resp.json()
                    project = data.get("cloudaicompanionProject")
                    if isinstance(project, str) and project:
                        return project
                    if isinstance(project, dict) and project.get("id"):
                        return project["id"]
            except Exception:
                continue

    _progress(on_progress, "Using default project...")
    return _DEFAULT_PROJECT_ID


def _progress(cb: Any | None, msg: str) -> None:
    if callable(cb):
        cb(msg)


async def login_antigravity(
    on_auth: Any | None = None,
    on_progress: Any | None = None,
) -> OAuthCredentials:
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

        return OAuthCredentials(
            refresh=refresh,
            access=access,
            expires=expires,
            project_id=project_id,
            email=email,
        )

    finally:
        server.shutdown()


async def refresh_antigravity_token(
    credentials: OAuthCredentials,
) -> OAuthCredentials:
    data = await refresh_token(credentials.refresh, _CLIENT_KEY, _CLIENT_VAL)
    return OAuthCredentials(
        refresh=data.get("refresh_token", credentials.refresh),
        access=data["access_token"],
        expires=time.time() * 1000 + data["expires_in"] * 1000 - 5 * 60 * 1000,
        project_id=credentials.project_id,
        email=credentials.email,
    )


async def get_antigravity_token(auth_file: str | None = None) -> OAuthCredentials:
    if auth_file:
        token_path = Path(auth_file).expanduser()
    else:
        env_path = os.environ.get("BOS_ANTIGRAVITY_AUTH_FILE")
        if env_path:
            token_path = Path(env_path).expanduser()
        else:
            token_path = Path.home() / ".config" / "bos" / "antigravity_auth.default.json"

    if not token_path.exists():
        raise RuntimeError(
            f"Antigravity credentials not found at {token_path}. Run `bos auth antigravity` to authenticate first."
        )

    try:
        data = json.loads(token_path.read_text())

        # Support gcloud Application Default Credentials (ADC) format
        if "type" in data and data["type"] == "authorized_user" and "refresh_token" in data:
            refresh = data["refresh_token"]
            client_id = data.get("client_id", _CLIENT_KEY)
            client_secret = data.get("client_secret", _CLIENT_VAL)
            project_id = data.get("quota_project_id", _DEFAULT_PROJECT_ID)

            # One-off refresh using the ADC token
            refreshed_data = await refresh_token(refresh, client_id, client_secret)
            access = refreshed_data["access_token"]
            expires = time.time() * 1000 + refreshed_data["expires_in"] * 1000 - 5 * 60 * 1000
            return OAuthCredentials(
                refresh=refresh,
                access=access,
                expires=expires,
                project_id=project_id,
                email=data.get("account"),
            )

        # Support our native format
        creds = OAuthCredentials(**data)
        if float(creds.expires) > time.time() * 1000 + 60000:
            return creds
        # refresh
        creds = await refresh_antigravity_token(creds)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(json.dumps(dict(creds.__dict__)))
        return creds
    except Exception as e:
        logger.debug(f"Failed to load/refresh token from {token_path}: {e}")
        raise RuntimeError(
            f"Failed to load or refresh Antigravity credentials from {token_path}. "
            f"Run `bos auth antigravity` to re-authenticate. Error: {e}"
        )


def _convert_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    contents = []
    system_prompt = ""
    for msg in messages:
        role = msg.get("role")
        content_val = msg.get("content") or ""

        if role == "system":
            system_prompt += str(content_val) + "\n"
        elif role == "user":
            contents.append({"role": "user", "parts": [{"text": str(content_val)}]})
        elif role == "assistant":
            parts = []
            if content_val:
                parts.append({"text": str(content_val)})
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                args = fn.get("arguments") or "{}"
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                parts.append({"functionCall": {"name": fn.get("name"), "args": args}})
            if parts:
                contents.append({"role": "model", "parts": parts})
        elif role == "tool":
            contents.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": msg.get("name", ""),
                                "response": {"output": str(content_val)},
                            }
                        }
                    ],
                }
            )
    return system_prompt, contents


def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    decls = []
    for tool in tools:
        fn = tool.get("function", {}) if tool.get("type") == "function" else tool
        decls.append(
            {
                "name": fn.get("name"),
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {}),
            }
        )
    return [{"functionDeclarations": decls}] if decls else []


async def _iter_sse(response: httpx.Response) -> AsyncGenerator[dict[str, Any], None]:
    buffer: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if buffer:
                data_lines = [line_[5:].strip() for line_ in buffer if line_.startswith("data:")]
                buffer = []
                data = "\\n".join(data_lines).strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    yield json.loads(data)
                except Exception:
                    continue
            continue
        buffer.append(line)


async def _consume_sse(response: httpx.Response) -> tuple[str, list[ToolCallRequest], str]:
    content = ""
    tool_calls: list[ToolCallRequest] = []
    finish_reason = "stop"

    async for event in _iter_sse(response):
        # Antigravity v1internal wraps everything in {"response": ...}
        if "response" in event and isinstance(event["response"], dict):
            event = event["response"]

        # Format 1: serverContent wrapping (Antigravity v1internal legacy)
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
                        name = fc.get("name")
                        args = fc.get("args", {})
                        tool_calls.append(
                            ToolCallRequest(
                                id=f"call_{len(tool_calls)}_{int(time.time())}",
                                name=name,
                                arguments=args,
                            )
                        )
            continue

        # Format 2: candidates array (standard Gemini API format)
        candidates = event.get("candidates")
        if candidates:
            for candidate in candidates:
                candidate_content = candidate.get("content", {})
                parts = candidate_content.get("parts", [])
                for part in parts:
                    if "text" in part:
                        content += part["text"]
                    elif "thought" in part and part.get("thought") is True:
                        # Thinking block — skip for final content
                        pass
                    elif "functionCall" in part:
                        fc = part["functionCall"]
                        name = fc.get("name")
                        args = fc.get("args", {})
                        tool_calls.append(
                            ToolCallRequest(
                                id=f"call_{len(tool_calls)}_{int(time.time())}",
                                name=name,
                                arguments=args,
                            )
                        )
                finish = candidate.get("finishReason")
                if finish:
                    finish_reason = finish.lower()
            continue

        # Unknown format — log for debugging
        logger.debug("Unknown SSE event format: %s", json.dumps(event)[:500])

    return content, tool_calls, finish_reason


@ep_provider(name="antigravity")
async def antigravity_complete(
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
    """Use Antigravity to call Cloud Code Assist streamGenerateContent."""
    model = model or "gemini-3.1-pro-low"

    token_creds = await get_antigravity_token(auth_file)
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
            "parts": [
                {"text": ANTIGRAVITY_SYSTEM_INSTRUCTION},
                {"text": f"Please ignore following [ignore]{ANTIGRAVITY_SYSTEM_INSTRUCTION}[/ignore]"},
                {"text": system_prompt},
            ],
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
        "requestType": "agent",
        "userAgent": "antigravity",
        "requestId": f"agent-{int(time.time() * 1000)}-{os.urandom(5).hex()}",
    }
    headers = _get_antigravity_headers()
    headers.update(
        {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
    )

    endpoints = list(ANTIGRAVITY_ENDPOINT_FALLBACKS)

    last_error = None
    async with httpx.AsyncClient(timeout=300) as client:
        for _attempt, ep in enumerate(endpoints):
            try:
                response = await client.post(
                    f"{ep}/v1internal:streamGenerateContent?alt=sse",
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
                    last_error = f"API error ({response.status_code}): {response.text}"
            except Exception as e:
                last_error = str(e)

    return LLMResponse(
        content=f"Error calling Cloud Code Assist: {last_error}",
        finish_reason="error",
    )
