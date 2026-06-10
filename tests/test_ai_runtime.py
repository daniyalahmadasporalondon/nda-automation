from __future__ import annotations

import io
import json
import os
import unittest
from unittest.mock import patch

from nda_automation import ai_runtime


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
        return False


def _mock_urlopen(response_bytes, captured_requests):
    def urlopen(request, *args, **kwargs):
        captured_requests.append(request)
        return _FakeResponse(response_bytes)

    return urlopen


class AIRuntimeConfigTests(unittest.TestCase):
    def test_resolve_ai_settings_uses_persisted_enabled_and_env_overrides(self):
        with (
            patch.object(ai_runtime.app_settings, "ai_settings", return_value={"enabled": False, "provider": "openrouter"}),
            patch.dict(os.environ, {
                "NDA_AI_REVIEW_ENABLED": "true",
                "NDA_AI_PROVIDER": "",
                "NDA_AI_MODEL": "openrouter/custom-model",
                "NDA_AI_TIMEOUT_SECONDS": "45",
                "NDA_AI_REVIEW_THRESHOLD": "0.6",
                "NDA_AI_REVIEW_CLAUSES": "mutuality",
            }, clear=False),
        ):
            settings = ai_runtime.resolve_ai_settings()

        self.assertEqual(settings["enabled"], False)
        self.assertEqual(settings["provider"], "openrouter")
        self.assertEqual(settings["model"], "openrouter/custom-model")
        self.assertEqual(settings["timeout_seconds"], 45)
        self.assertEqual(settings["confidence_threshold"], 0.6)
        self.assertEqual(settings["clause_ids"], "mutuality")

    def test_configured_api_key_prefers_environment_over_local_settings(self):
        with (
            patch.object(ai_runtime.app_settings, "stored_ai_api_key", return_value="stored-key"),
            patch.dict(os.environ, {"OPENROUTER_API_KEY": "env-key"}, clear=False),
        ):
            self.assertEqual(ai_runtime.configured_api_key("openrouter"), "env-key")
            self.assertEqual(ai_runtime.api_key_source("openrouter"), "environment")


class OpenRouterJSONChatAdapterTests(unittest.TestCase):
    def test_request_body_sanitizes_model_and_uses_json_response_format(self):
        body = ai_runtime.openrouter_json_chat_request_body(
            model="../../etc/passwd?inject=1",
            messages=[{"role": "user", "content": "{}"}],
        )

        self.assertNotIn("?", body["model"])
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["response_format"], {"type": "json_object"})

    def test_adapter_round_trip_returns_parsed_json_object(self):
        captured = []
        verdict = {"decision": "pass", "confidence": 0.9}
        response = json.dumps(
            {"choices": [{"message": {"content": json.dumps(verdict)}}]}
        ).encode("utf-8")

        with patch.object(ai_runtime.urllib.request, "urlopen", _mock_urlopen(response, captured)):
            adapter = ai_runtime.OpenRouterJSONChatAdapter(api_key="k", model="x-ai/grok-4.3")
            parsed = adapter.complete_json(messages=[{"role": "user", "content": "{}"}])

        self.assertEqual(parsed, verdict)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].headers["Authorization"], "Bearer k")
        request_body = json.loads(captured[0].data.decode("utf-8"))
        self.assertEqual(request_body["model"], "x-ai/grok-4.3")
        self.assertEqual(request_body["response_format"], {"type": "json_object"})


if __name__ == "__main__":
    unittest.main()
