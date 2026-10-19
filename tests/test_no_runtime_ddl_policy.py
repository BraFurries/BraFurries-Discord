"""CI guard against reintroducing literal schema-changing SQL in Coddy.

Static defense in depth, not proof against statements supplied from variables,
external scripts, stored procedures, or arbitrary runtime SQL construction.
Database/Flyway is the sole authority for shared schema changes.
"""

import ast
from pathlib import Path
import re

import pytest


ROOT = Path(__file__).resolve().parents[1]
DDL = re.compile(
    r"\b(?:CREATE(?:\s+OR\s+REPLACE)?|ALTER|DROP|RENAME|TRUNCATE)"
    r"\s+(?:(?:TEMPORARY|UNIQUE)\s+)?"
    r"(?:TABLE|INDEX|DATABASE|SCHEMA|COLUMN|VIEW|TRIGGER)\b",
    re.IGNORECASE,
)


def _static_fragments(node: ast.AST) -> str:
    """Reassemble constant and f-string pieces; unknown values become spaces."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(_static_fragments(item) for item in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _static_fragments(node.left) + _static_fragments(node.right)
    return " "


def _ddl_literals(source: str) -> list[tuple[int, str]]:
    tree = ast.parse(source)
    findings = set()

    # Docstrings describe the schema policy; they are not executable SQL.
    docstrings = set()
    for owner in ast.walk(tree):
        if isinstance(owner, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if owner.body and isinstance(owner.body[0], ast.Expr):
                expression = owner.body[0].value
                if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
                    docstrings.add(id(expression))

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings and DDL.search(node.value):
                findings.add((node.lineno, node.value.strip().splitlines()[0][:100]))
        elif isinstance(node, (ast.JoinedStr, ast.BinOp)):
            text = _static_fragments(node)
            if DDL.search(text):
                findings.add((node.lineno, text.strip().splitlines()[0][:100]))
    return sorted(findings)


def _runtime_python_files():
    """All shipped Python code, excluding test-only fixtures and virtualenvs."""
    excluded_roots = {"tests", ".git", ".venv", "venv", "__pycache__", ".tox"}
    for path in sorted(ROOT.rglob("*.py")):
        relative = path.relative_to(ROOT)
        if any(part in excluded_roots for part in relative.parts):
            continue
        yield path


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("CREATE TABLE example (id INT)", True),
        ("ALTER TABLE example ADD COLUMN x INT", True),
        ("DROP INDEX old_idx ON example", True),
        ("DROP COLUMN old_flag", True),
        ("RENAME TABLE before TO after", True),
        ("TRUNCATE TABLE example", True),
        ("CREATE OR REPLACE VIEW v AS SELECT 1", True),
        ("SELECT column_name FROM information_schema.COLUMNS", False),
        ("UPDATE config_server_settings SET enabled = 1", False),
        ("INSERT INTO config_economy (server_guild_id) VALUES (123)", False),
    ],
)
def test_ddl_classification(statement, expected):
    assert bool(DDL.search(statement)) is expected


def test_split_string_and_fstring_schema_mutations_are_detected():
    assert _ddl_literals('q = "ALTER " + "TABLE x ADD COLUMN y INT"')
    assert _ddl_literals('q = f"CREATE TABLE {table_name} (id INT)"')
    assert _ddl_literals('q = "DROP " + action + " TABLE users"')  # conservative fragments


def test_docstrings_are_not_mistaken_for_schema_commands():
    assert _ddl_literals('def f():\\n    """never alter schema"""\\n    return 1') == []


def test_runtime_contains_no_literal_schema_mutations():
    scanned = list(_runtime_python_files())
    assert scanned, "Runtime Python source discovery unexpectedly returned no files"
    violations = []
    for path in scanned:
        source = path.read_text(encoding="utf-8")
        for line, snippet in _ddl_literals(source):
            violations.append(f"{path.relative_to(ROOT)}:{line}: {snippet}")
    assert not violations, (
        "Schema DDL is owned by BraFurries-Database, never Coddy runtime.\n"
        + "\n".join(violations)
    )
