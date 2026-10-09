"""Regression coverage for the canonical call-log compatibility contract."""
from core.log_types import effective_call_config
from core import log_targets
from core.log_targets import selectable_log_targets
from unittest.mock import Mock, patch
import discord
import os
import subprocess
import sys


def _configs(**entries):
    return lambda _guild_id, kind: entries.get(kind)


def test_status_api_import_is_database_lazy():
    # Importing the status API must not import core.database (which initializes
    # the legacy connection pool as an import side effect).
    import sys
    sys.modules.pop("core.database", None)
    import message_services.bot_status_api  # noqa: F401
    assert "core.database" not in sys.modules


def test_status_api_import_isolated_subprocess_without_database():
    code = "import sys; import message_services.bot_status_api; assert 'core.database' not in sys.modules"
    result = subprocess.run([sys.executable, "-c", code], cwd=os.getcwd(), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_disabled_canonical_call_suppresses_enabled_voice_alias():
    call = {"enabled": False, "log_channel": 1}
    assert effective_call_config(_configs(call=call, voice={"enabled": True, "log_channel": 2}), 1) is call


def test_voice_is_fallback_only_when_canonical_row_absent():
    voice = {"enabled": True, "log_channel": 2}
    assert effective_call_config(_configs(voice=voice), 1) is voice


def test_voice_call_is_final_legacy_fallback():
    voice_call = {"enabled": True, "log_channel": 3}
    assert effective_call_config(_configs(voice_call=voice_call), 1) is voice_call


def test_enabled_canonical_call_wins_over_voice_alias():
    call = {"enabled": True, "log_channel": 1}
    assert effective_call_config(_configs(call=call, voice={"enabled": True, "log_channel": 2}), 1) is call


def test_selectable_targets_follow_by_category_and_insert_forum_threads():
    class Text: pass
    class Forum: pass
    class Thread: pass
    def channel(cls, ident, name, parent=None):
        value = cls(); value.id = ident; value.name = name; value.parent = parent
        value.permissions_for = Mock(return_value=Mock(view_channel=True, send_messages=True, embed_links=True, send_messages_in_threads=True))
        return value
    guild = Mock(spec=discord.Guild); guild.me = Mock(); guild.me.id = 99
    forum = channel(Forum, 20, "Forum Logs")
    before, after = (channel(Text, i, n) for i, n in [(1, "antes"), (4, "depois")])
    warn = channel(Thread, 2, "Warn", forum); calls = channel(Thread, 3, "Calls", forum)
    guild.threads = [warn, calls]; guild.by_category.return_value = [(None, [before, forum, after])]
    with patch.object(log_targets.discord, "TextChannel", Text), patch.object(log_targets.discord, "ForumChannel", Forum), patch.object(log_targets.discord, "Thread", Thread):
        assert [(item["id"], item["type"]) for item in selectable_log_targets(guild)] == [("1", "text"), ("2", "forum_thread"), ("3", "forum_thread"), ("4", "text")]
