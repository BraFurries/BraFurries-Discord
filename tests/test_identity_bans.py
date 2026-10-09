import asyncio
from types import SimpleNamespace

import discord

from core.identity_api import ConfirmedIdentity, IdentitySummary
from core.identity_bans import (
    compensate_unrecorded_propagated_bans,
    propagate_confirmed_ban,
    propagate_new_confirmed_identity_ban,
)


def http_error(error_type, status):
    return error_type(
        response=SimpleNamespace(status=status, reason="test"),
        message="test",
    )


class FakeGuild:
    def __init__(
        self,
        guild_id,
        *,
        present=(),
        banned=(),
        forbidden_bans=(),
        failed_bans=(),
        failed_unbans=(),
    ):
        self.id = guild_id
        self.present = set(present)
        self.banned = set(banned)
        self.forbidden_bans = set(forbidden_bans)
        self.failed_bans = set(failed_bans)
        self.failed_unbans = set(failed_unbans)
        self.ban_calls = []
        self.unban_calls = []
        self.member_lookups = []

    async def fetch_ban(self, target):
        if target.id in self.banned:
            return SimpleNamespace(user=target)
        raise http_error(discord.NotFound, 404)

    async def fetch_member(self, discord_user_id):
        self.member_lookups.append(discord_user_id)
        if discord_user_id in self.present:
            return SimpleNamespace(id=discord_user_id)
        raise http_error(discord.NotFound, 404)

    async def ban(self, target, **kwargs):
        self.ban_calls.append((target.id, kwargs))
        if target.id in self.forbidden_bans:
            raise http_error(discord.Forbidden, 403)
        if target.id in self.failed_bans:
            raise RuntimeError("synthetic failure")
        self.banned.add(target.id)

    async def unban(self, target, **kwargs):
        self.unban_calls.append((target.id, kwargs))
        if target.id in self.failed_unbans:
            raise RuntimeError("synthetic unban failure")
        if target.id not in self.banned:
            raise http_error(discord.NotFound, 404)
        self.banned.remove(target.id)


def identity_summary(*identities, requested_user_id=1):
    return IdentitySummary(
        requested_user_id=requested_user_id,
        confirmed_identities=tuple(
            ConfirmedIdentity(user_id, tuple(discord_ids))
            for user_id, discord_ids in identities
        ),
        other_account_count=max(len(identities) - 1, 0),
        warning_count=0,
    )


def run(guild, summary):
    return asyncio.run(
        propagate_confirmed_ban(
            guild,
            111,
            summary,
            reason="regra",
            delete_message_seconds=3600,
        )
    )


def outcomes(effects):
    return {effect.discord_user_id: effect.outcome for effect in effects}


def test_present_confirmed_accounts_are_all_processed():
    guild = FakeGuild(10, present={111, 222})

    effects = run(guild, identity_summary((1, [111]), (2, [222])))

    assert outcomes(effects) == {111: "APPLIED", 222: "APPLIED"}
    assert [discord_id for discord_id, _ in guild.ban_calls] == [111, 222]
    assert guild.ban_calls[0][1]["delete_message_seconds"] == 3600
    assert guild.ban_calls[1][1]["delete_message_seconds"] == 0


def test_confirmed_account_outside_guild_is_banned_by_snowflake():
    guild = FakeGuild(10, present={111, 222})

    effects = run(
        guild,
        identity_summary((1, [111]), (2, [222]), (3, [333])),
    )

    assert outcomes(effects)[333] == "APPLIED"
    assert [discord_id for discord_id, _ in guild.ban_calls] == [111, 222, 333]
    assert guild.member_lookups == []


def test_new_confirmed_identity_extends_existing_ban_without_membership_lookup():
    guild = FakeGuild(10, banned={111})

    effects = asyncio.run(
        propagate_new_confirmed_identity_ban(
            guild,
            [(1, 111), (2, 222), (3, 333)],
            reason="ban ativo",
        )
    )

    assert outcomes(effects) == {
        111: "ALREADY_BANNED",
        222: "APPLIED",
        333: "APPLIED",
    }
    assert [discord_id for discord_id, _ in guild.ban_calls] == [222, 333]
    assert guild.member_lookups == []


def test_origin_discord_id_must_belong_to_requested_identity():
    guild = FakeGuild(10, present={111, 222})
    summary = identity_summary(
        (1, [222]),
        (2, [111]),
        requested_user_id=1,
    )

    try:
        run(guild, summary)
    except ValueError as error:
        assert "User solicitado" in str(error)
    else:
        raise AssertionError("expected inconsistent origin binding to be rejected")

    assert guild.ban_calls == []
    assert guild.member_lookups == []


def test_suspected_account_is_never_a_candidate():
    guild = FakeGuild(10, present={111, 444})
    summary = identity_summary((1, [111]))

    effects = run(guild, summary)

    assert outcomes(effects) == {111: "APPLIED"}
    assert 444 not in guild.member_lookups
    assert all(discord_id != 444 for discord_id, _ in guild.ban_calls)


def test_already_banned_account_is_idempotent():
    guild = FakeGuild(10, present={111, 222}, banned={222})

    effects = run(guild, identity_summary((1, [111]), (2, [222])))

    assert outcomes(effects)[222] == "ALREADY_BANNED"
    assert [discord_id for discord_id, _ in guild.ban_calls] == [111]


def test_origin_failure_stops_before_linked_accounts_are_processed():
    guild = FakeGuild(
        10,
        present={111, 222},
        forbidden_bans={111},
    )

    effects = run(guild, identity_summary((1, [111]), (2, [222])))

    assert outcomes(effects) == {111: "FORBIDDEN"}
    assert [discord_id for discord_id, _ in guild.ban_calls] == [111]
    assert guild.member_lookups == []


def test_compensation_reverts_only_unrecorded_non_origin_applied_bans():
    guild = FakeGuild(10, present={111, 222, 333}, banned={111, 222, 333})
    effects = [
        SimpleNamespace(
            identity_user_id=1,
            discord_user_id=111,
            is_origin=True,
            outcome="APPLIED",
        ),
        SimpleNamespace(
            identity_user_id=2,
            discord_user_id=222,
            is_origin=False,
            outcome="APPLIED",
        ),
        SimpleNamespace(
            identity_user_id=3,
            discord_user_id=333,
            is_origin=False,
            outcome="ALREADY_BANNED",
        ),
    ]

    failed = asyncio.run(compensate_unrecorded_propagated_bans(guild, effects))

    assert failed == []
    assert [discord_id for discord_id, _ in guild.unban_calls] == [222]
    assert guild.banned == {111, 333}


def test_compensation_reports_failed_unbans_without_touching_origin():
    guild = FakeGuild(
        10,
        present={111, 222},
        banned={111, 222},
        failed_unbans={222},
    )
    effects = [
        SimpleNamespace(
            identity_user_id=1,
            discord_user_id=111,
            is_origin=True,
            outcome="APPLIED",
        ),
        SimpleNamespace(
            identity_user_id=2,
            discord_user_id=222,
            is_origin=False,
            outcome="APPLIED",
        ),
    ]

    failed = asyncio.run(compensate_unrecorded_propagated_bans(guild, effects))

    assert failed == [222]
    assert [discord_id for discord_id, _ in guild.unban_calls] == [222]
    assert 111 in guild.banned


def test_forbidden_account_does_not_stop_remaining_accounts():
    guild = FakeGuild(10, present={111, 222, 333}, forbidden_bans={222})

    effects = run(
        guild,
        identity_summary((1, [111]), (2, [222]), (3, [333])),
    )

    assert outcomes(effects) == {
        111: "APPLIED",
        222: "FORBIDDEN",
        333: "APPLIED",
    }
    assert [discord_id for discord_id, _ in guild.ban_calls] == [111, 222, 333]


def test_unexpected_account_failure_is_recorded_and_does_not_stop_remaining():
    guild = FakeGuild(10, present={111, 222, 333}, failed_bans={222})

    effects = run(
        guild,
        identity_summary((1, [111]), (2, [222]), (3, [333])),
    )

    assert outcomes(effects) == {
        111: "APPLIED",
        222: "FAILED",
        333: "APPLIED",
    }


def test_operation_never_touches_another_community_guild():
    current_guild = FakeGuild(10, present={111, 222})
    other_guild = FakeGuild(20, present={111, 222})

    run(current_guild, identity_summary((1, [111]), (2, [222])))

    assert [discord_id for discord_id, _ in current_guild.ban_calls] == [111, 222]
    assert other_guild.ban_calls == []
    assert other_guild.member_lookups == []


def test_transitive_abc_cluster_from_api_includes_c_without_graph_resolution():
    guild = FakeGuild(10, present={111, 222, 333})
    api_transitive_cluster = identity_summary(
        (1, [111]),
        (2, [222]),
        (3, [333]),
    )

    effects = run(guild, api_transitive_cluster)

    assert outcomes(effects) == {
        111: "APPLIED",
        222: "APPLIED",
        333: "APPLIED",
    }
