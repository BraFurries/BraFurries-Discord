import asyncio
import logging
import time
from types import SimpleNamespace

import httpx
from openai import APIStatusError, BadRequestError
import pytest

from core.AI_Functions.terceiras import openAI
from core.runtime_config import get_ai_sponsored_fallback_guilds


class RecordingClient:
    primary_results = {}
    groq_result = "groq-response"
    groq_finish_reason = None
    groq_usage = None
    instances = []
    primary_calls = []
    groq_calls = []

    def __init__(self, *, api_key=None, base_url=None, **_kwargs):
        self.api_key = api_key
        self.base_url = base_url
        self.instances.append({
            "base_url": base_url,
            "has_api_key": bool(api_key),
            "max_retries": _kwargs.get("max_retries"),
        })
        if base_url is None:
            self.responses = SimpleNamespace(create=self.primary_create)
        elif base_url == openAI.GROQ_BASE_URL:
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.groq_create))

    async def primary_create(self, **kwargs):
        self.primary_calls.append({"has_api_key": bool(self.api_key), "kwargs": kwargs})
        result = self.primary_results.get(self.api_key, "openai-response")
        if isinstance(result, Exception):
            raise result
        return SimpleNamespace(output_text=result)

    async def groq_create(self, **kwargs):
        self.groq_calls.append({"has_api_key": bool(self.api_key), "kwargs": kwargs})
        if isinstance(self.groq_result, Exception):
            raise self.groq_result
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=self.groq_result),
                finish_reason=self.groq_finish_reason,
            )],
            usage=self.groq_usage,
        )


class Channel:
    def __init__(self, guild_id, history_entries=()):
        self.guild = SimpleNamespace(id=guild_id)
        self._history_entries = history_entries

    def history(self, **_kwargs):
        async def entries():
            for entry in self._history_entries:
                yield entry

        return entries()


def _provider_error(status_code, *, code=None, error_type=None, message="provider failure"):
    error = Exception(message)
    error.status_code = status_code
    error.code = code
    error.type = error_type
    error.body = None
    error.response = None
    return error


def _api_status_error(status_code, *, bad_request=False):
    response = httpx.Response(
        status_code,
        request=httpx.Request("POST", "https://api.openai.com/v1/responses"),
    )
    error_class = BadRequestError if bad_request else APIStatusError
    return error_class("provider failure", response=response, body=None)


@pytest.fixture(autouse=True)
def fallback_environment(monkeypatch):
    monkeypatch.setattr(openAI, "AsyncOpenAI", RecordingClient)
    monkeypatch.setenv("AI_SPONSORED_FALLBACK_ENABLED", "true")
    monkeypatch.setenv("AI_SPONSORED_FALLBACK_GUILDS", "101,202")
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    monkeypatch.setenv("GROQ_FALLBACK_MODEL", "openai/gpt-oss-20b")
    monkeypatch.delenv("CREATOR_ID", raising=False)
    RecordingClient.primary_results = {}
    RecordingClient.groq_result = "groq-response"
    RecordingClient.groq_finish_reason = None
    RecordingClient.groq_usage = None
    RecordingClient.instances = []
    RecordingClient.primary_calls = []
    RecordingClient.groq_calls = []
    openAI._openai_chat_circuits.clear()


def _chat(guild_id=101, token="openai-token", *, history_entries=()):
    channel = Channel(guild_id, history_entries)
    bot = SimpleNamespace(user=SimpleNamespace(id=1), get_channel=lambda _id: channel)
    return asyncio.run(
        openAI.retornaRespostaGPT(
            "user-message-sentinel", "Membro", [], bot, 55, "Discord", "gpt-test", "direct", token
        )
    )


def test_openai_success_never_calls_groq():
    result = _chat()
    assert result == openAI.CasualAiResult("openai-response")
    assert len(RecordingClient.primary_calls) == 1
    assert RecordingClient.groq_calls == []


def test_casual_openai_client_disables_sdk_retries():
    _chat()
    assert RecordingClient.instances[0]["max_retries"] == 0


@pytest.mark.parametrize(
    "error",
    [
        _provider_error(429, code="credit_balance_exhausted"),
        _provider_error(429, code="rate_limit_exceeded"),
        _provider_error(401),
    ],
)
def test_eligible_openai_failures_use_groq_for_authorized_guild(error):
    RecordingClient.primary_results["openai-token"] = error
    result = _chat()
    assert result.text == "groq-response"
    assert result.openai_token_invalid is (error.status_code == 401)
    assert len(RecordingClient.primary_calls) == 1
    assert len(RecordingClient.groq_calls) == 1


def test_missing_openai_token_uses_groq_only_for_authorized_guild():
    result = _chat(token="")
    assert result == openAI.CasualAiResult("groq-response")
    assert RecordingClient.primary_calls == []
    assert len(RecordingClient.groq_calls) == 1


@pytest.mark.parametrize(
    ("setting", "value"),
    [("AI_SPONSORED_FALLBACK_GUILDS", "999"), ("AI_SPONSORED_FALLBACK_ENABLED", "false")],
)
def test_not_opted_in_or_disabled_never_calls_groq(monkeypatch, setting, value):
    monkeypatch.setenv(setting, value)
    assert _chat(token="").text == "Estou sem chave de IA configurada neste servidor :c"
    assert RecordingClient.groq_calls == []


def test_missing_groq_key_fails_gracefully_without_call(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY")
    assert _chat(token="").text == "Estou sem chave de IA configurada neste servidor :c"
    assert RecordingClient.groq_calls == []


def test_default_groq_model_is_used_when_not_configured(monkeypatch):
    monkeypatch.delenv("GROQ_FALLBACK_MODEL")
    _chat(token="")
    assert RecordingClient.groq_calls[0]["kwargs"]["model"] == "openai/gpt-oss-20b"


def test_invalid_guild_list_fails_closed(monkeypatch):
    monkeypatch.setenv("AI_SPONSORED_FALLBACK_GUILDS", "101,not-an-id")
    assert get_ai_sponsored_fallback_guilds() == frozenset()
    assert _chat(token="").text == "Estou sem chave de IA configurada neste servidor :c"


def test_programming_error_propagates_without_groq_or_circuit():
    RecordingClient.primary_results["openai-token"] = ValueError("internal-bug-sentinel")
    with pytest.raises(ValueError, match="internal-bug-sentinel"):
        _chat()
    assert RecordingClient.groq_calls == []
    assert openAI._openai_chat_circuits == {}


def test_bad_request_propagates_without_groq_or_circuit():
    RecordingClient.primary_results["openai-token"] = _api_status_error(400, bad_request=True)

    with pytest.raises(BadRequestError):
        _chat()

    assert RecordingClient.groq_calls == []
    assert openAI._openai_chat_circuits == {}


def test_unexpected_api_status_error_propagates_without_groq_or_circuit():
    RecordingClient.primary_results["openai-token"] = _api_status_error(418)

    with pytest.raises(APIStatusError):
        _chat()

    assert RecordingClient.groq_calls == []
    assert openAI._openai_chat_circuits == {}


def test_transient_5xx_api_status_error_uses_groq():
    RecordingClient.primary_results["openai-token"] = _api_status_error(503)

    assert _chat() == openAI.CasualAiResult("groq-response")
    assert len(RecordingClient.groq_calls) == 1


def test_groq_failure_returns_friendly_result_without_retrying_openai():
    RecordingClient.primary_results["openai-token"] = _provider_error(429, code="credit_balance_exhausted")
    RecordingClient.groq_result = _provider_error(503)
    result = _chat()
    assert "problemas" in result.text
    assert len(RecordingClient.primary_calls) == 1
    assert len(RecordingClient.groq_calls) == 1


def test_circuit_isolated_by_guild_and_success_does_not_close_other_guild():
    RecordingClient.primary_results["guild-a-token"] = _provider_error(429, code="credit_balance_exhausted")
    assert _chat(101, "guild-a-token").text == "groq-response"
    state_a = asyncio.run(openAI._get_openai_chat_circuit(101))
    assert state_a is not None and state_a.reason == openAI.QUOTA_OR_BILLING

    RecordingClient.primary_results["guild-b-token"] = "guild-b-openai-response"
    assert _chat(202, "guild-b-token").text == "guild-b-openai-response"
    assert len(RecordingClient.primary_calls) == 2
    assert asyncio.run(openAI._get_openai_chat_circuit(101)) == state_a


def test_unauthorized_guild_circuit_cannot_consume_groq_for_other_guild(monkeypatch):
    monkeypatch.setenv("AI_SPONSORED_FALLBACK_GUILDS", "202")
    RecordingClient.primary_results["guild-a-token"] = _provider_error(401)
    result_a = _chat(101, "guild-a-token")
    assert result_a.openai_token_invalid is True
    assert RecordingClient.groq_calls == []

    RecordingClient.primary_results["guild-b-token"] = "guild-b-openai-response"
    assert _chat(202, "guild-b-token").text == "guild-b-openai-response"
    assert RecordingClient.groq_calls == []


def test_auth_error_metadata_survives_groq_success_and_circuit_open():
    RecordingClient.primary_results["openai-token"] = _provider_error(401)
    first = _chat()
    assert first == openAI.CasualAiResult("groq-response", openai_token_invalid=True)
    RecordingClient.groq_result = "groq-after-circuit"
    second = _chat()
    assert second == openAI.CasualAiResult("groq-after-circuit", openai_token_invalid=True)
    assert len(RecordingClient.primary_calls) == 1


def test_auth_error_metadata_survives_groq_failure():
    RecordingClient.primary_results["openai-token"] = _provider_error(401)
    RecordingClient.groq_result = _provider_error(503)
    result = _chat()
    assert result.openai_token_invalid is True
    assert "problemas" in result.text


def test_quota_circuit_does_not_mark_token_invalid():
    RecordingClient.primary_results["openai-token"] = _provider_error(429, code="credit_balance_exhausted")
    first = _chat()
    second = _chat()
    assert first.openai_token_invalid is False
    assert second.openai_token_invalid is False
    assert asyncio.run(openAI._get_openai_chat_circuit(101)).reason == openAI.QUOTA_OR_BILLING


def test_expired_circuit_is_removed_only_for_its_guild():
    asyncio.run(openAI._openai_chat_circuit_open(101, openAI.AUTH_ERROR))
    asyncio.run(openAI._openai_chat_circuit_open(202, openAI.QUOTA_OR_BILLING))
    expired = time.monotonic() - openAI.OPENAI_CHAT_CIRCUIT_LONG_COOLDOWN_SECONDS - 1
    openAI._openai_chat_circuits[101] = openAI.OpenAiChatCircuitState(expired, openAI.AUTH_ERROR)
    assert asyncio.run(openAI._get_openai_chat_circuit(101)) is None
    assert asyncio.run(openAI._get_openai_chat_circuit(202)).reason == openAI.QUOTA_OR_BILLING


def test_groq_client_request_is_complete_and_compatible():
    _chat(token="")
    assert RecordingClient.instances == [{
        "base_url": openAI.GROQ_BASE_URL,
        "has_api_key": True,
        "max_retries": None,
    }]
    request = RecordingClient.groq_calls[0]["kwargs"]
    assert request["model"] == "openai/gpt-oss-20b"
    assert request["reasoning_effort"] == "low"
    assert request["max_completion_tokens"] == 512
    assert request["extra_body"] == {"reasoning_format": "hidden"}
    assert "reasoning_format" not in request
    assert request["messages"][0]["role"] == "system"
    assert request["messages"][1] == {"role": "user", "content": "user-message-sentinel"}


def test_groq_empty_content_logs_empty_response_and_returns_friendly_result(caplog):
    caplog.set_level(logging.INFO, logger=openAI.__name__)
    RecordingClient.groq_result = ""

    result = _chat(token="")

    assert "problemas" in result.text
    assert "outcome=empty_response" in caplog.text
    assert "outcome=success" not in caplog.text


def test_groq_success_logs_safe_metadata_when_available(caplog):
    caplog.set_level(logging.INFO, logger=openAI.__name__)
    RecordingClient.groq_result = "groq-response-sentinel"
    RecordingClient.groq_finish_reason = "stop"
    RecordingClient.groq_usage = SimpleNamespace(total_tokens=123, completion_tokens=45)

    assert _chat(token="") == openAI.CasualAiResult("groq-response-sentinel")

    assert "outcome=success" in caplog.text
    assert "finish_reason=stop" in caplog.text
    assert "total_tokens=123" in caplog.text
    assert "completion_tokens=45" in caplog.text
    for sensitive_value in ("test-groq-key", "user-message-sentinel", "groq-response-sentinel"):
        assert sensitive_value not in caplog.text


def test_groq_success_without_optional_metadata_fields_does_not_break():
    assert _chat(token="") == openAI.CasualAiResult("groq-response")


def test_quota_circuit_uses_fifteen_minute_cooldown_then_retries_openai(monkeypatch):
    monotonic_now = 1000.0
    monkeypatch.setattr(openAI.time, "monotonic", lambda: monotonic_now)

    RecordingClient.primary_results["openai-token"] = _provider_error(
        429, code="credit_balance_exhausted"
    )
    _chat()
    state = asyncio.run(openAI._get_openai_chat_circuit(101))

    assert state == openAI.OpenAiChatCircuitState(
        opened_at=1000.0,
        reason=openAI.QUOTA_OR_BILLING,
    )
    assert asyncio.run(openAI._get_openai_chat_circuit(
        101, now=1899.999
    )) == state
    assert asyncio.run(openAI._get_openai_chat_circuit(
        101, now=1900.0
    )) is None

    monotonic_now = 1900.0
    RecordingClient.primary_results["openai-token"] = "openai-after-cooldown"
    assert _chat() == openAI.CasualAiResult("openai-after-cooldown")


def test_safe_logs_exclude_all_sensitive_sentinels(caplog):
    caplog.set_level(logging.INFO, logger=openAI.__name__)
    history_message = SimpleNamespace(
        author=SimpleNamespace(id=2, display_name="history-author"),
        content="history-sentinel",
        reference=None,
        id=2,
    )
    RecordingClient.primary_results["openai-token-secret"] = _provider_error(
        429,
        code="credit_balance_exhausted",
        message="exception-sentinel",
    )
    RecordingClient.groq_result = _provider_error(503, message="groq-exception-sentinel")
    result = _chat(101, "openai-token-secret", history_entries=(history_message,))
    assert "problemas" in result.text
    rendered = caplog.text
    for secret in (
        "openai-token-secret",
        "test-groq-key",
        "user-message-sentinel",
        "history-sentinel",
        "groq-response",
        "exception-sentinel",
        "groq-exception-sentinel",
    ):
        assert secret not in rendered
