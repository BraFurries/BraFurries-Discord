import ast
import re
from datetime import timedelta
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MODERATION_PATH = REPOSITORY_ROOT / "cogs" / "moderation.py"


def _load_timeout_parser():
    tree = ast.parse(MODERATION_PATH.read_text(encoding="utf-8"), filename=str(MODERATION_PATH))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "parse_timeout_duration"
    )
    module = ast.Module(body=[function], type_ignores=[])
    namespace = {"re": re, "timedelta": timedelta}
    exec(compile(module, str(MODERATION_PATH), "exec"), namespace)
    return namespace["parse_timeout_duration"]


def test_timeout_duration_accepts_expected_units_and_combinations():
    parse = _load_timeout_parser()

    cases = {
        "30m": 30 * 60,
        "2h": 2 * 60 * 60,
        "3d": 3 * 24 * 60 * 60,
        "1w": 7 * 24 * 60 * 60,
        "1d12h": 36 * 60 * 60,
        "2h 30m": 150 * 60,
        "1W": 7 * 24 * 60 * 60,
        "28d": 28 * 24 * 60 * 60,
        "4w": 28 * 24 * 60 * 60,
    }

    for value, expected_seconds in cases.items():
        assert parse(value) == timedelta(seconds=expected_seconds)


def test_timeout_duration_rejects_ambiguous_legacy_or_out_of_range_values():
    parse = _load_timeout_parser()

    for value in (
        "",
        "0m",
        "5s",
        "1a",
        "1mes",
        "1sem",
        "29d",
        "4w1m",
        "5w",
        "?m",
        "abc",
        "1h30",
    ):
        assert parse(value) is None


def test_warn_exposes_optional_castigo_and_mute_command_has_duration():
    tree = ast.parse(MODERATION_PATH.read_text(encoding="utf-8"), filename=str(MODERATION_PATH))
    moderation_cog = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ModerationCog"
    )
    methods = {
        node.name: node
        for node in moderation_cog.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    warn = methods["warn"]
    warn_args = [arg.arg for arg in warn.args.args]
    assert warn_args == ["self", "ctx", "membro", "motivo", "castigo", "notificar"]
    assert isinstance(warn.args.defaults[-2], ast.Constant)
    assert warn.args.defaults[-2].value is None

    mute = methods["mute"]
    mute_args = [arg.arg for arg in mute.args.args]
    assert mute_args == ["self", "ctx", "membro", "duracao", "motivo", "notificar"]
