import ast
import asyncio
from dataclasses import dataclass
from datetime import datetime
import os
import time
from pathlib import Path
from types import SimpleNamespace


OPENAI_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "core"
    / "AI_Functions"
    / "terceiras"
    / "openAI.py"
)


class FakeRateLimitError(Exception):
    pass


class FakeAuthenticationError(Exception):
    pass


class FakePermissionDeniedError(Exception):
    pass


class RecordingLogger:
    def __init__(self):
        self.records = []

    def warning(self, message, *args):
        self.records.append(("warning", message, args))

    def error(self, message, *args):
        self.records.append(("error", message, args))


@dataclass(frozen=True)
class FakeCasualAiResult:
    text: str
    openai_token_invalid: bool = False


def _load_error_helpers(logger=None):
    tree = ast.parse(OPENAI_SOURCE.read_text(encoding="utf-8"), filename=str(OPENAI_SOURCE))
    function_names = {
        "_safe_getattr",
        "_safe_mapping_get",
        "_extract_openai_error_details",
        "_classify_openai_error",
        "_log_openai_error",
        "_is_openai_auth_error",
    }
    constant_names = {
        "TEMPORARY_RATE_LIMIT",
        "QUOTA_OR_BILLING",
        "UNKNOWN_RATE_LIMIT",
        "AUTH_ERROR",
        "UNEXPECTED_ERROR",
        "QUOTA_ERROR_CODES",
    }
    body = [
        node
        for node in tree.body
        if (
            isinstance(node, ast.FunctionDef)
            and node.name in function_names
        )
        or (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id in constant_names
                for target in node.targets
            )
        )
    ]
    namespace = {
        "AuthenticationError": FakeAuthenticationError,
        "PermissionDeniedError": FakePermissionDeniedError,
        "RateLimitError": FakeRateLimitError,
        "APITimeoutError": type("FakeTimeoutError", (Exception,), {}),
        "APIConnectionError": type("FakeConnectionError", (Exception,), {}),
        "logger": logger or RecordingLogger(),
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), str(OPENAI_SOURCE), "exec"), namespace)
    return namespace


def _rate_limit_error(*, code=None, error_type=None, request_id=None, body=None):
    error = FakeRateLimitError("rate limited")
    error.status_code = 429
    error.code = code
    error.type = error_type
    error.request_id = request_id
    error.body = body
    error.response = None
    return error


def test_explicit_rate_limit_is_temporary():
    helpers = _load_error_helpers()
    error = _rate_limit_error(code="rate_limit_exceeded")
    details = helpers["_extract_openai_error_details"](error)

    assert helpers["_classify_openai_error"](error, details) == "temporary_rate_limit"


def test_quota_error_codes_are_classified_as_billing():
    helpers = _load_error_helpers()
    quota_codes = {
        "credit_balance_exhausted",
        "organization_usage_limit_exceeded",
        "organization_spend_limit_exceeded",
        "project_spend_limit_exceeded",
    }

    for code in quota_codes:
        error = _rate_limit_error(code=code)
        details = helpers["_extract_openai_error_details"](error)
        assert helpers["_classify_openai_error"](error, details) == "quota_or_billing"


def test_insufficient_quota_type_is_classified_as_billing():
    helpers = _load_error_helpers()
    error = _rate_limit_error(error_type="insufficient_quota")
    details = helpers["_extract_openai_error_details"](error)

    assert helpers["_classify_openai_error"](error, details) == "quota_or_billing"


def test_explicit_rate_limit_code_takes_precedence_over_generic_type():
    helpers = _load_error_helpers()
    error = _rate_limit_error(
        code="rate_limit_exceeded",
        error_type="insufficient_quota",
    )
    details = helpers["_extract_openai_error_details"](error)

    assert helpers["_classify_openai_error"](error, details) == "temporary_rate_limit"


def test_unknown_429_is_not_classified_as_exhausted_credit():
    helpers = _load_error_helpers()
    error = _rate_limit_error()
    details = helpers["_extract_openai_error_details"](error)

    assert helpers["_classify_openai_error"](error, details) == "unknown_rate_limit"
    assert "credit" not in helpers["_classify_openai_error"](error, details)


def test_details_use_body_and_response_fallbacks():
    helpers = _load_error_helpers()
    response = SimpleNamespace(
        status_code=429,
        headers={"x-request-id": "req-response"},
        json=lambda: {
            "error": {
                "code": "organization_usage_limit_exceeded",
                "type": "insufficient_quota",
            }
        },
    )
    error = Exception("request failed")
    error.response = response

    assert helpers["_extract_openai_error_details"](error) == {
        "status_code": 429,
        "error_code": "organization_usage_limit_exceeded",
        "error_type": "insufficient_quota",
        "request_id": "req-response",
    }


def test_missing_request_id_and_malformed_response_do_not_raise():
    helpers = _load_error_helpers()

    class BrokenResponse:
        status_code = 429
        headers = None

        def json(self):
            raise ValueError("not json")

    error = Exception("request failed")
    error.body = "not a dictionary"
    error.response = BrokenResponse()

    assert helpers["_extract_openai_error_details"](error) == {
        "status_code": 429,
        "error_code": None,
        "error_type": None,
        "request_id": None,
    }


def test_auth_errors_remain_distinct_from_rate_limits():
    helpers = _load_error_helpers()
    auth_error = FakeAuthenticationError("invalid token")
    auth_error.status_code = 401
    details = helpers["_extract_openai_error_details"](auth_error)

    assert helpers["_is_openai_auth_error"](auth_error) is True
    assert helpers["_classify_openai_error"](auth_error, details) == "auth_error"


def test_safe_log_contains_only_diagnostic_fields():
    logger = RecordingLogger()
    helpers = _load_error_helpers(logger)
    secret = "sensitive-value-must-not-appear"
    error = _rate_limit_error(
        code="rate_limit_exceeded",
        request_id="req-safe",
        body={"prompt": secret, "authorization": secret},
    )

    helpers["_log_openai_error"]("chat_response", error)
    rendered_record = repr(logger.records)

    assert logger.records[0][0] == "warning"
    assert "req-safe" in rendered_record
    assert "rate_limit_exceeded" in rendered_record
    assert secret not in rendered_record


def _load_chat_response_function(error, logger):
    helpers = _load_error_helpers(logger)
    tree = ast.parse(OPENAI_SOURCE.read_text(encoding="utf-8"), filename=str(OPENAI_SOURCE))
    functions = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in {"_compact_history_entry", "retornaRespostaGPT"}
    ]

    class Responses:
        async def create(self, **_kwargs):
            raise error

    class Client:
        def __init__(self, **_kwargs):
            self.responses = Responses()

    helpers.update(
        {
            "AsyncOpenAI": Client,
            "commands": SimpleNamespace(Bot=object),
            "datetime": datetime,
            "pytz": SimpleNamespace(timezone=lambda _name: None),
            "os": os,
            "time": time,
            "MAX_HISTORY_MESSAGES": 8,
            "MAX_HISTORY_MESSAGE_CHARS": 180,
            "OPENAI_TOKEN_INVALID_MARKER": "__OPENAI_TOKEN_INVALID__",
            "CasualAiResult": FakeCasualAiResult,
            "AUTH_ERROR": "auth_error",
            "QUOTA_OR_BILLING": "quota_or_billing",
            "TEMPORARY_RATE_LIMIT": "temporary_rate_limit",
            "UNKNOWN_RATE_LIMIT": "unknown_rate_limit",
            "TRANSIENT_ERROR": "transient_error",
            "_get_groq_fallback_config": lambda _guild_id: None,
            "_is_fallback_eligible_classification": lambda _classification: False,
            "_get_openai_chat_circuit": lambda _guild_id: asyncio.sleep(0, result=None),
            "_openai_chat_circuit_close": lambda _guild_id: asyncio.sleep(0),
            "_openai_chat_circuit_open": lambda _guild_id, _reason: asyncio.sleep(0),
            "_is_operational_provider_error": lambda _error, _classification: True,
            "_log_chat_provider_event": lambda **_kwargs: None,
        }
    )
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(OPENAI_SOURCE), "exec"), helpers)
    return helpers["retornaRespostaGPT"]


def _chat_response_for(error):
    logger = RecordingLogger()
    function = _load_chat_response_function(error, logger)

    class Channel:
        def history(self, **_kwargs):
            async def empty_history():
                if False:
                    yield None

            return empty_history()

    bot = SimpleNamespace(
        user=SimpleNamespace(id=1),
        get_channel=lambda _channel_id: Channel(),
    )
    result = asyncio.run(
        function(
            "mensagem",
            "usuário",
            [],
            bot,
            1,
            None,
            "model",
            "direct",
            openai_token="test-token",
        )
    )
    return result.text


def test_chat_uses_temporary_message_for_explicit_rate_limit():
    result = _chat_response_for(_rate_limit_error(code="rate_limit_exceeded"))

    assert "daqui a pouquinho" in result
    assert "crédit" not in result


def test_chat_uses_quota_message_for_billing_limit():
    result = _chat_response_for(_rate_limit_error(code="credit_balance_exhausted"))

    assert "cota de IA" in result


def test_chat_uses_generic_message_for_unknown_429():
    result = _chat_response_for(_rate_limit_error())

    assert "temporariamente indisponível" in result
    assert "crédit" not in result
