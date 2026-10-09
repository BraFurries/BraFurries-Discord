import ast
from pathlib import Path
from types import SimpleNamespace


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _load_functions(path: Path, function_names: set[str], namespace=None):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in function_names
    ]
    module = ast.Module(body=functions, type_ignores=[])
    loaded_namespace = dict(namespace or {})
    exec(compile(module, str(path), "exec"), loaded_namespace)
    return loaded_namespace


class RecordingLogger:
    def __init__(self):
        self.warnings = []

    def warning(self, message, *args):
        self.warnings.append((message, args))


def _load_levelup_helpers():
    return _load_functions(
        REPOSITORY_ROOT / "core" / "routine_functions.py",
        {"_levelup_warning_enabled", "_resolve_levelup_channel"},
        {
            "discord": SimpleNamespace(
                Member=object,
                abc=SimpleNamespace(Messageable=object),
            ),
            "logging": RecordingLogger(),
        },
    )


def _load_vip_helper(configured_role_ids, logger):
    return _load_functions(
        REPOSITORY_ROOT / "cogs" / "vip.py",
        {"_get_configured_vip_roles"},
        {
            "discord": SimpleNamespace(Guild=object, Role=object),
            "getGuildVipRoleIds": lambda _guild_id: configured_role_ids,
            "logger": logger,
        },
    )["_get_configured_vip_roles"]


def test_disabled_levelup_warning_has_no_destination_or_warning():
    helpers = _load_levelup_helpers()
    logger = helpers["logging"]
    fallback = object()
    guild = SimpleNamespace(id=1, get_channel=lambda _id: None, get_thread=lambda _id: None)
    member = SimpleNamespace(guild=guild)

    assert helpers["_levelup_warning_enabled"]({"levelupWarning": False}) is False
    assert helpers["_resolve_levelup_channel"](
        member, {"levelupWarning": False}, fallback
    ) is None
    assert logger.warnings == []


def test_enabled_levelup_warning_resolves_configured_channel():
    helpers = _load_levelup_helpers()
    configured_channel = object()
    fallback = object()
    guild = SimpleNamespace(
        id=1,
        get_channel=lambda channel_id: configured_channel if channel_id == 42 else None,
        get_thread=lambda _id: None,
    )
    member = SimpleNamespace(guild=guild)

    assert helpers["_resolve_levelup_channel"](
        member,
        {"levelup_warning": True, "levelup_warning_channel": "42"},
        fallback,
    ) is configured_channel


def test_enabled_levelup_warning_uses_fallback_channel():
    helpers = _load_levelup_helpers()
    fallback = object()
    guild = SimpleNamespace(id=1, get_channel=lambda _id: None, get_thread=lambda _id: None)
    member = SimpleNamespace(guild=guild)

    assert helpers["_resolve_levelup_channel"](
        member, {"levelupWarning": True}, fallback
    ) is fallback


def test_enabled_levelup_warning_without_destination_remains_reportable():
    helpers = _load_levelup_helpers()
    guild = SimpleNamespace(id=1, get_channel=lambda _id: None, get_thread=lambda _id: None)
    member = SimpleNamespace(guild=guild)

    assert helpers["_levelup_warning_enabled"]({"levelupWarning": True}) is True
    assert helpers["_resolve_levelup_channel"](
        member, {"levelupWarning": True}, None
    ) is None


def test_guild_without_configured_vip_roles_is_silent():
    logger = RecordingLogger()
    helper = _load_vip_helper([], logger)
    guild = SimpleNamespace(id=1, get_role=lambda _id: None)

    assert helper(guild) == []
    assert logger.warnings == []


def test_configured_existing_vip_role_is_returned_without_warning():
    logger = RecordingLogger()
    role = SimpleNamespace(id=42)
    helper = _load_vip_helper([42], logger)
    guild = SimpleNamespace(id=1, get_role=lambda role_id: role if role_id == 42 else None)

    assert helper(guild) == [role]
    assert logger.warnings == []


def test_missing_configured_vip_role_emits_warning():
    logger = RecordingLogger()
    helper = _load_vip_helper([42], logger)
    guild = SimpleNamespace(id=1, get_role=lambda _id: None)

    assert helper(guild) == []
    assert len(logger.warnings) == 1
    assert logger.warnings[0][1] == (1, [42])


def test_partially_stale_vip_configuration_keeps_valid_roles():
    logger = RecordingLogger()
    role = SimpleNamespace(id=42)
    helper = _load_vip_helper([42, 99], logger)
    guild = SimpleNamespace(id=1, get_role=lambda role_id: role if role_id == 42 else None)

    assert helper(guild) == [role]
    assert logger.warnings == []
