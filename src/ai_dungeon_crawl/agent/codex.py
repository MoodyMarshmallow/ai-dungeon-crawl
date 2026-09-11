from contextlib import asynccontextmanager
import json
import os
from pathlib import Path

import httpx2
from openai import AsyncOpenAI
from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.profiles.openai import OpenAIModelProfile


CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"


class CodexResponsesModel(OpenAIResponsesModel):
    """Adapt the streaming-only subscription endpoint to Agent.run()."""

    async def request(self, messages, model_settings, model_request_parameters):
        async with self.request_stream(messages, model_settings, model_request_parameters) as stream:
            async for _ in stream:
                pass
            return stream.get()

    @asynccontextmanager
    async def request_stream(self, messages, model_settings, model_request_parameters, run_context=None):
        """Apply subscription settings for both observed and unobserved requests."""
        settings = dict(model_settings or {})
        for key in ("max_tokens", "temperature", "top_p"):
            settings.pop(key, None)
        settings["openai_store"] = False
        settings["openai_send_reasoning_ids"] = True
        settings["openai_truncation"] = "disabled"
        async with super().request_stream(messages, settings, model_request_parameters, run_context) as stream:
            yield stream
            response = stream.get()
        if response.finish_reason not in ("stop", "tool_call"):
            raise UnexpectedModelBehavior("Codex response did not complete; no script will run")


def _load_login(auth_path):
    """Read a file-backed ChatGPT login without logging or modifying credentials.

    Keychain-only logins and token refresh are deliberately not implemented.
    Never use OPENAI_API_KEY or fall back to pay-per-token authentication.
    """
    path = Path(auth_path) if auth_path is not None else (
        Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"
    )
    try:
        with path.open(encoding="utf-8") as source:
            raw = source.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError
        login = json.loads(raw)
        if login.get("auth_mode") != "chatgpt":
            raise ValueError
        tokens = login["tokens"]
        token, account = tokens["access_token"], tokens["account_id"]
        if not isinstance(token, str) or not token or not isinstance(account, str) or not account:
            raise ValueError
        return token, account
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise RuntimeError(
            "Codex requires a file-backed ChatGPT login. Run codex login; "
            "API-key and keychain-only logins are not supported. No API fallback."
        ) from None


@asynccontextmanager
async def codex_model(model_name, auth_path=None, http_client=None):
    """Own one authenticated client; callers must not reuse an injected client.

    Credentials go only to the fixed subscription endpoint. Disable inherited
    proxies and redirects on the default client. Leave refresh-token management
    to Codex rather than rotating shared credentials behind the CLI's back.
    """
    token, account = _load_login(auth_path)
    http = http_client if http_client is not None else httpx2.AsyncClient(trust_env=False)
    async with AsyncOpenAI(
        api_key=token, base_url=CODEX_BASE_URL, http_client=http, max_retries=0,
        default_headers={"ChatGPT-Account-Id": account},
    ) as client:
        try:
            # Subscription aliases may be newer than Pydantic's model catalog.
            # Stateless continuation requires the encrypted reasoning payload.
            yield CodexResponsesModel(model_name, provider=OpenAIProvider(openai_client=client),
                profile=OpenAIModelProfile(openai_supports_encrypted_reasoning_content=True))
        except ModelHTTPError as exc:
            body = exc.body if isinstance(exc.body, dict) else {}
            error = body.get('error', body)
            if isinstance(error, dict) and error.get('code') in {
                'context_length_exceeded', 'context_window_exceeded',
            }:
                raise RuntimeError(
                    "Codex context window exceeded. Conversation was not truncated; no fallback."
                ) from None
            raise RuntimeError(
                f"Codex subscription request failed (HTTP {exc.status_code}). "
                "Check model access and usage limits; for expired authentication run codex login. "
                "No API fallback."
            ) from None
