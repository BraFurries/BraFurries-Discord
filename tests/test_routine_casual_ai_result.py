import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

from core.AI_Functions.terceiras.openAI import CasualAiResult, OPENAI_TOKEN_INVALID_MARKER


ROUTINE_SOURCE = Path(__file__).resolve().parents[1] / "core" / "routine_functions.py"


def _load_result_resolver(notifications):
    tree = ast.parse(ROUTINE_SOURCE.read_text(encoding="utf-8"), filename=str(ROUTINE_SOURCE))
    function = next(
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_resolve_casual_ai_result"
    )

    async def notify(message):
        notifications.append(message)

    namespace = {
        "CasualAiResult": CasualAiResult,
        "OPENAI_TOKEN_INVALID_MARKER": OPENAI_TOKEN_INVALID_MARKER,
        "_notify_invalid_ai_token": notify,
        "discord": SimpleNamespace(Message=object),
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(ROUTINE_SOURCE), "exec"), namespace)
    return namespace["_resolve_casual_ai_result"]


def test_groq_text_and_invalid_token_metadata_notify_once():
    notifications = []
    resolver = _load_result_resolver(notifications)
    message = SimpleNamespace()

    response = asyncio.run(
        resolver(CasualAiResult("resposta-vinda-do-groq", openai_token_invalid=True), message)
    )

    assert response == "resposta-vinda-do-groq"
    assert notifications == [message]


def test_legacy_marker_keeps_existing_notification_and_user_message():
    notifications = []
    resolver = _load_result_resolver(notifications)
    message = SimpleNamespace()

    response = asyncio.run(resolver(OPENAI_TOKEN_INVALID_MARKER, message))

    assert "Token OpenAI inválido" in response
    assert notifications == [message]


def test_structured_legacy_marker_does_not_duplicate_notification():
    notifications = []
    resolver = _load_result_resolver(notifications)
    message = SimpleNamespace()

    response = asyncio.run(
        resolver(CasualAiResult(OPENAI_TOKEN_INVALID_MARKER, openai_token_invalid=True), message)
    )

    assert "Token OpenAI inválido" in response
    assert notifications == [message]
