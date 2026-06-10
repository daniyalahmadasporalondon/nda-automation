from __future__ import annotations

import json
import os
import re
import ssl
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence

from . import app_settings

DEFAULT_OPENROUTER_MODEL = "x-ai/grok-4.3"
DEFAULT_AI_REVIEW_THRESHOLD = 0.75
DEFAULT_AI_TIMEOUT_SECONDS = 20

AI_REVIEW_ENV_ENABLED = "NDA_AI_REVIEW_ENABLED"
AI_REVIEW_ENV_PROVIDER = "NDA_AI_PROVIDER"
AI_REVIEW_ENV_MODEL = "NDA_AI_MODEL"
AI_REVIEW_ENV_TIMEOUT = "NDA_AI_TIMEOUT_SECONDS"
AI_REVIEW_ENV_THRESHOLD = "NDA_AI_REVIEW_THRESHOLD"
AI_REVIEW_ENV_CLAUSES = "NDA_AI_REVIEW_CLAUSES"
AI_REVIEW_ENV_BACKUP_PROVIDER = "NDA_AI_BACKUP_PROVIDER"
AI_REVIEW_ENV_BACKUP_MODEL = "NDA_AI_BACKUP_MODEL"

OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"
OPENROUTER_CHAT_COMPLETIONS_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_API_KEY_PREFIX = "sk-or-"
GEMINI_DIRECT_API_KEY_PREFIX = "AIza"

STORED_KEY_MIGRATION_CODE = "gemini_direct_stored_key"
STORED_KEY_MIGRATION_MESSAGE = (
    "The stored AI API key looks like a Google/Gemini API key, but AI review now "
    "runs through OpenRouter (model x-ai/grok-4.3). Replace it with an "
    "OpenRouter API key (it starts with \"sk-or-\") from openrouter.ai to re-enable AI review."
)


class AIRuntimeError(RuntimeError):
    pass


def provider_for_api_key(api_key: str) -> str:
    return "openrouter"


def default_model_for_provider(provider: str) -> str:
    return DEFAULT_OPENROUTER_MODEL


def configured_api_key(provider: str = "openrouter") -> str:
    normalized_provider = str(provider).strip().lower()
    if normalized_provider == "openrouter":
        return os.environ.get(OPENROUTER_API_KEY_ENV, "").strip() or stored_key_for_provider("openrouter")
    return ""


def api_key_source(provider: str = "openrouter") -> str:
    normalized_provider = str(provider).strip().lower()
    if normalized_provider == "openrouter" and os.environ.get(OPENROUTER_API_KEY_ENV, "").strip():
        return "environment"
    if stored_key_for_provider(normalized_provider):
        return "local_settings"
    return ""


def stored_key_for_provider(provider: str = "openrouter") -> str:
    normalized_provider = str(provider).strip().lower()
    stored_key = app_settings.stored_ai_api_key()
    if not stored_key:
        return ""
    return stored_key if normalized_provider == "openrouter" else ""


def resolve_ai_settings() -> dict[str, object]:
    stored = app_settings.ai_settings()
    stored_enabled = stored.get("enabled")
    env_enabled_value = env_enabled(AI_REVIEW_ENV_ENABLED)
    provider = configured_provider(stored)
    return {
        "enabled": stored_enabled if isinstance(stored_enabled, bool) else env_enabled_value,
        "provider": provider,
        "model": configured_model(provider, stored),
        "timeout_seconds": env_int(AI_REVIEW_ENV_TIMEOUT, DEFAULT_AI_TIMEOUT_SECONDS),
        "confidence_threshold": env_float(AI_REVIEW_ENV_THRESHOLD, DEFAULT_AI_REVIEW_THRESHOLD),
        "clause_ids": os.environ.get(AI_REVIEW_ENV_CLAUSES, ""),
    }


def ai_review_settings() -> dict[str, object]:
    return resolve_ai_settings()


def configured_provider(stored: Mapping[str, object]) -> str:
    env_provider = os.environ.get(AI_REVIEW_ENV_PROVIDER, "").strip().lower()
    if env_provider == "openrouter":
        return env_provider
    stored_provider = str(stored.get("provider") or "").strip().lower()
    if stored_provider == "openrouter":
        return stored_provider
    return "openrouter"


def configured_model(provider: str, stored: Mapping[str, object]) -> str:
    env_model = os.environ.get(AI_REVIEW_ENV_MODEL, "").strip()
    if env_model:
        return env_model
    stored_provider = str(stored.get("provider") or "").strip().lower()
    stored_model = str(stored.get("model") or "").strip() if stored_provider == provider else ""
    if stored_model:
        return stored_model
    return default_model_for_provider(provider)


def env_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, fallback: int) -> int:
    try:
        return int(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return fallback


def env_float(name: str, fallback: float) -> float:
    try:
        return float(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return fallback


def sanitize_model_name(model: str) -> str:
    cleaned = str(model or DEFAULT_OPENROUTER_MODEL).strip()
    cleaned = re.sub(r"[^A-Za-z0-9._/-]", "", cleaned)
    return cleaned or DEFAULT_OPENROUTER_MODEL


def openrouter_json_chat_request_body(
    *,
    model: str,
    messages: Sequence[Mapping[str, str]],
    response_format: bool = True,
    temperature: int | float = 0,
    **extra_fields: object,
) -> dict[str, object]:
    body: dict[str, object] = {
        "model": sanitize_model_name(model or DEFAULT_OPENROUTER_MODEL),
        "messages": [dict(message) for message in messages],
        "temperature": temperature,
    }
    if response_format:
        body["response_format"] = {"type": "json_object"}
    body.update(extra_fields)
    return body


def openrouter_response_text(payload: Mapping[str, object]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes)) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, Mapping):
        return ""
    message = first.get("message")
    if not isinstance(message, Mapping):
        return ""
    return str(message.get("content") or "").strip()


def trusted_https_context() -> ssl.SSLContext | None:
    try:
        import certifi  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        return ssl.create_default_context(cafile=certifi.where())
    except (OSError, ssl.SSLError):
        return None


class OpenRouterJSONChatAdapter:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_OPENROUTER_MODEL,
        timeout_seconds: int = DEFAULT_AI_TIMEOUT_SECONDS,
        user_agent: str = "nda-automation/1.0",
    ) -> None:
        cleaned_key = str(api_key or "").strip()
        if not cleaned_key:
            raise AIRuntimeError("OpenRouter API key is not configured.")
        self.api_key = cleaned_key
        self.model = sanitize_model_name(model or DEFAULT_OPENROUTER_MODEL)
        self.timeout_seconds = max(1, int(timeout_seconds or DEFAULT_AI_TIMEOUT_SECONDS))
        self.user_agent = str(user_agent or "nda-automation/1.0")

    def chat(self, body: Mapping[str, object]) -> Mapping[str, object]:
        request = urllib.request.Request(
            OPENROUTER_CHAT_COMPLETIONS_ENDPOINT,
            data=json.dumps(dict(body)).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": self.user_agent,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds, context=trusted_https_context()
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            message = error.read().decode("utf-8", errors="replace")[:500]
            raise AIRuntimeError(f"OpenRouter API returned HTTP {error.code}: {message}") from error
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            raise AIRuntimeError(f"OpenRouter API request failed: {error}") from error
        if not isinstance(payload, Mapping):
            raise AIRuntimeError("OpenRouter API returned a non-object response.")
        return payload

    def complete_json(
        self,
        *,
        messages: Sequence[Mapping[str, str]],
        response_format: bool = True,
        **extra_fields: object,
    ) -> dict[str, object] | None:
        body = openrouter_json_chat_request_body(
            model=self.model,
            messages=messages,
            response_format=response_format,
            **extra_fields,
        )
        response_text = openrouter_response_text(self.chat(body))
        if not response_text:
            raise AIRuntimeError("OpenRouter API returned no message content.")
        try:
            parsed = json.loads(response_text)
        except json.JSONDecodeError as error:
            raise AIRuntimeError("OpenRouter API returned non-JSON text.") from error
        return dict(parsed) if isinstance(parsed, Mapping) else None
