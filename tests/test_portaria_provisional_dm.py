"""Regression tests for guild-specific Portaria provisional access DMs."""
import ast
from pathlib import Path


MODERATION_FILE = Path(__file__).resolve().parents[1] / "cogs" / "moderation.py"


def _load_provisional_message_helpers():
    source = MODERATION_FILE.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(MODERATION_FILE))
    moderation = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ModerationCog"
    )
    names = {
        "_get_provisional_account_age_limit_days",
        "_build_provisional_access_message",
    }
    functions = [
        node for node in moderation.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    assert {node.name for node in functions} == names
    for function in functions:
        function.decorator_list = []
    namespace = {}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(MODERATION_FILE), "exec"), namespace)
    return (
        namespace["_get_provisional_account_age_limit_days"],
        namespace["_build_provisional_access_message"],
    )


def test_provisional_dm_uses_configured_account_age_and_duration():
    account_age_limit, message_for = _load_provisional_message_helpers()
    config = {
        "idade_minima_conta_acesso_provisorio_ativa": 1,
        "idade_minima_conta_acesso_provisorio_dias": 60,
    }
    days = account_age_limit(config)
    message = message_for(7, days)

    assert days == 60
    assert "**60 dia(s)**" in message
    assert "**7 dia(s)**" in message
    assert "menos de 30 dias" not in message
    assert "liberação completa" in message


def test_provisional_dm_respects_non_default_config_and_legacy_fallback():
    account_age_limit, message_for = _load_provisional_message_helpers()

    assert account_age_limit({
        "idade_minima_conta_acesso_provisorio_ativa": True,
        "idade_minima_conta_acesso_provisorio_dias": 14,
    }) == 14
    assert "**14 dia(s)**" in message_for(3, 14)

    assert account_age_limit({
        "idade_minima_conta_acesso_provisorio_ativa": False,
        "idade_minima_conta_acesso_provisorio_dias": 60,
    }) == 30
    assert account_age_limit({
        "idade_minima_conta_acesso_provisorio_ativa": True,
        "idade_minima_conta_acesso_provisorio_dias": None,
    }) == 30
    assert account_age_limit({
        "idade_minima_conta_acesso_provisorio_ativa": True,
        "idade_minima_conta_acesso_provisorio_dias": -2,
    }) == 30
