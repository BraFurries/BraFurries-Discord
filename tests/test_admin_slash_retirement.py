"""Regression guards for retired Discord slash command entrypoints.

Checks command registrations without initializing the bot or external services.
"""

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def command_names(path: str) -> set[str]:
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            func = decorator.func
            if not isinstance(func, ast.Attribute) or func.attr != "command":
                continue
            for keyword in decorator.keywords:
                if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                    names.add(keyword.value.value)
    return names


def test_retired_admin_slash_commands_are_not_registered():
    assert {"simular", "voice-inspect"}.isdisjoint(command_names("cogs/xp.py"))
    assert {"gerar", "sincronizar"}.isdisjoint(command_names("cogs/backup.py"))
    assert "configurar_portaria" not in command_names("cogs/forms.py")
    assert {"portaria_aprovar", "portaria_liberar_conta"}.isdisjoint(
        command_names("cogs/moderation.py")
    )


def test_retained_admin_slash_commands_still_exist():
    assert "listar" in command_names("cogs/backup.py")
    assert {"listar", "auditar_ficha_portaria"}.issubset(
        command_names("cogs/forms.py")
    )
    assert {"config", "ajustar"}.issubset(command_names("cogs/xp.py"))


def test_backup_runtime_and_current_portaria_approval_remain_available():
    backup = (ROOT / "cogs/backup.py").read_text(encoding="utf-8")
    moderation = (ROOT / "cogs/moderation.py").read_text(encoding="utf-8")
    portaria = (ROOT / "core/portaria_views.py").read_text(encoding="utf-8")
    assert "self._periodic_backup_loop.start()" in backup
    assert "self.backup_runtime = BackupRestoreEngine(bot)" in backup
    assert "async def _finalize_portaria_approval(" in moderation
    assert "_finalize_portaria_approval(" in portaria
