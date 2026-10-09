import ast
from pathlib import Path


RUNTIME_DIRECTORIES = ('cogs', 'core', 'message_services')


def test_runtime_modules_import_logging_before_using_it():
    repository_root = Path(__file__).resolve().parents[1]
    missing_imports = []

    for directory in RUNTIME_DIRECTORIES:
        for path in (repository_root / directory).rglob('*.py'):
            tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
            uses_logging = any(
                isinstance(node, ast.Name) and node.id == 'logging'
                for node in ast.walk(tree)
            )
            imports_logging = any(
                isinstance(node, ast.Import)
                and any(alias.name == 'logging' and alias.asname is None for alias in node.names)
                for node in ast.walk(tree)
            )
            if uses_logging and not imports_logging:
                missing_imports.append(str(path.relative_to(repository_root)))

    assert missing_imports == []


def test_startup_configures_application_logging_before_discord_logging():
    repository_root = Path(__file__).resolve().parents[1]
    path = repository_root / 'message_services' / 'discord_service.py'
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    run_function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == 'run_discord_client'
    )
    direct_calls = [
        node.value.func.id
        for node in run_function.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
    ]

    assert direct_calls.index('configure_application_logging') < direct_calls.index(
        'configure_discord_logging'
    )
