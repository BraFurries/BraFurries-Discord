import asyncio
import logging
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest


with patch("mysql.connector.pooling.MySQLConnectionPool"):
    from core import monthly_bumps


def run(coroutine):
    return asyncio.run(coroutine)


class HistoryChannel:
    def __init__(self, messages):
        self.messages = messages
        self.kwargs = None

    def history(self, **kwargs):
        self.kwargs = kwargs

        async def iterate():
            for message in self.messages:
                yield message

        return iterate()


def message(user_id, created_at, *, author_id=monthly_bumps.DISBOARD_BOT_ID, interaction=True):
    return SimpleNamespace(
        author=SimpleNamespace(id=author_id),
        interaction=(
            SimpleNamespace(user=SimpleNamespace(id=user_id)) if interaction else None
        ),
        created_at=created_at,
    )


def test_previous_closed_month_uses_sao_paulo_calendar_across_year_boundary():
    period = monthly_bumps.previous_closed_month(
        datetime(2027, 1, 2, 12, tzinfo=monthly_bumps.SAO_PAULO)
    )

    assert period.start == date(2026, 12, 1)
    assert period.end == date(2027, 1, 1)
    assert period.start_utc == datetime(2026, 12, 1, 3, tzinfo=timezone.utc)
    assert period.end_utc == datetime(2027, 1, 1, 3, tzinfo=timezone.utc)


def test_preparation_window_is_limited_to_first_three_local_days():
    assert monthly_bumps.is_monthly_preparation_window(
        datetime(2026, 10, 3, 23, tzinfo=monthly_bumps.SAO_PAULO)
    )
    assert not monthly_bumps.is_monthly_preparation_window(
        datetime(2026, 10, 4, 0, tzinfo=monthly_bumps.SAO_PAULO)
    )


def test_collect_ranking_filters_period_and_uses_deterministic_tiebreak():
    period = monthly_bumps.previous_closed_month(
        datetime(2026, 10, 2, 12, tzinfo=monthly_bumps.SAO_PAULO)
    )
    channel = HistoryChannel(
        [
            message(30, period.start_utc),
            message(20, period.start_utc),
            message(10, period.start_utc.replace(hour=4)),
            message(10, period.start_utc.replace(day=2)),
            message(20, period.start_utc.replace(day=3)),
            message(30, period.start_utc.replace(day=4)),
            message(99, period.end_utc),
            message(88, period.start_utc.replace(day=5), author_id=123),
            message(77, period.start_utc.replace(day=5), interaction=False),
        ]
    )

    ranking = run(monthly_bumps.collect_monthly_bump_ranking(channel, period))

    assert [item["discord_user_id"] for item in ranking] == [20, 30, 10]
    assert [item["bump_count"] for item in ranking] == [2, 2, 2]
    assert all(item["first_bump_at"].tzinfo is None for item in ranking)
    assert channel.kwargs == {
        "limit": None,
        "after": period.start_utc - timedelta(milliseconds=1),
        "before": period.end_utc,
        "oldest_first": True,
    }


def test_collect_ranking_enforces_half_open_period_boundaries():
    period = monthly_bumps.previous_closed_month(
        datetime(2026, 10, 2, 12, tzinfo=monthly_bumps.SAO_PAULO)
    )
    channel = HistoryChannel(
        [
            message(1, period.start_utc - timedelta(milliseconds=1)),
            message(2, period.start_utc),
            message(3, period.end_utc - timedelta(milliseconds=1)),
            message(4, period.end_utc),
        ]
    )

    ranking = run(monthly_bumps.collect_monthly_bump_ranking(channel, period))

    assert [entry["discord_user_id"] for entry in ranking] == [2, 3]
    assert channel.kwargs["after"] == period.start_utc - timedelta(milliseconds=1)
    assert channel.kwargs["before"] == period.end_utc


def test_collect_ranking_uses_snowflake_as_final_tiebreak():
    period = monthly_bumps.previous_closed_month(
        datetime(2026, 10, 1, tzinfo=monthly_bumps.SAO_PAULO)
    )
    instant = period.start_utc.replace(hour=5)
    ranking = run(
        monthly_bumps.collect_monthly_bump_ranking(
            HistoryChannel([message(200, instant), message(100, instant)]), period
        )
    )
    assert [item["discord_user_id"] for item in ranking] == [100, 200]


class ScriptedCursor:
    def __init__(self, rows=(), rowcount=1):
        self.rows = list(rows)
        self.executed = []
        self.rowcount = rowcount

    def execute(self, query, params=None):
        self.executed.append((" ".join(query.split()), params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        return self.rows.pop(0) if self.rows else []


def connection_for(cursor):
    @contextmanager
    def connection(*_args, **_kwargs):
        yield cursor

    return connection


def test_get_or_create_keeps_existing_snapshot_immutable(monkeypatch):
    existing = {
        "id": 7,
        "server_guild_id": 9,
        "source_channel_id": 111,
        "reward_role_id": 222,
    }
    cursor = ScriptedCursor([existing])
    monkeypatch.setattr(monthly_bumps, "pooled_connection", connection_for(cursor))
    period = monthly_bumps.previous_closed_month(
        datetime(2026, 10, 2, tzinfo=monthly_bumps.SAO_PAULO)
    )

    result = monthly_bumps.get_or_create_monthly_reward_run(
        {
            "guildId": 9,
            "disboardChannelId": 999,
            "rewardRoleId": 888,
            "rewardDays": [1, 2, 3],
            "rewardCoins": [4, 5, 6],
        },
        period,
    )

    assert result is existing
    assert cursor.executed[0][0].startswith("INSERT IGNORE")
    assert not any(
        query.startswith("UPDATE bump_monthly_reward_runs")
        for query, _params in cursor.executed
    )


def test_prepare_ranking_locks_resets_stats_and_freezes_top_three(monkeypatch):
    run_row = {
        "id": 7,
        "server_guild_id": 9,
        "ranking_prepared_at": None,
        "reward_role_id": 500,
        "reward_days_1": 30,
        "reward_days_2": 20,
        "reward_days_3": 10,
        "reward_coins_1": 300,
        "reward_coins_2": 200,
        "reward_coins_3": 100,
    }
    identities = [{"user_id": 101}, {"user_id": 102}, {"user_id": 103}, {"user_id": 104}]
    cursor = ScriptedCursor([run_row, *identities])
    monkeypatch.setattr(monthly_bumps, "pooled_connection", connection_for(cursor))
    ranking = [
        {
            "discord_user_id": user_id,
            "bump_count": 10 - index,
            "first_bump_at": datetime(2026, 9, index + 1),
        }
        for index, user_id in enumerate((1, 2, 3, 4))
    ]

    assert monthly_bumps.prepare_monthly_reward_ranking(7, ranking)

    queries = [query for query, _params in cursor.executed]
    assert queries[0].endswith("FOR UPDATE")
    assert sum(query.startswith("UPDATE user_records SET bumps = 0") for query in queries) == 1
    grant_inserts = [
        params
        for query, params in cursor.executed
        if query.startswith("INSERT INTO bump_monthly_reward_grants")
    ]
    assert len(grant_inserts) == 3
    assert grant_inserts[0][-3:] == (300, 500, 30)
    assert grant_inserts[2][-3:] == (100, 500, 10)


def test_prepared_ranking_does_not_reset_stats_or_insert_grants(monkeypatch):
    cursor = ScriptedCursor([{"id": 7, "ranking_prepared_at": datetime(2026, 10, 1)}])
    monkeypatch.setattr(monthly_bumps, "pooled_connection", connection_for(cursor))

    assert not monthly_bumps.prepare_monthly_reward_ranking(7, [])
    assert len(cursor.executed) == 1
    assert cursor.executed[0][0].endswith("FOR UPDATE")


def test_completion_requires_notification_attempt_for_rewarded_grant(monkeypatch):
    state = {
        "coins_amount": 25,
        "coins_applied_at": datetime(2026, 10, 1),
        "role_id": None,
        "role_days": 0,
        "notification_attempted_at": None,
    }

    class CompletionCursor(ScriptedCursor):
        def execute(self, query, params=None):
            super().execute(query, params)
            normalized = " ".join(query.split())
            assert "reward_grant.notification_attempted_at IS NULL" in normalized
            assert "reward_grant.coins_amount > 0" in normalized
            effects_pending = (
                state["coins_amount"] > 0 and state["coins_applied_at"] is None
            )
            notification_pending = (
                state["coins_amount"] > 0
                or (state["role_id"] is not None and state["role_days"] > 0)
            ) and state["notification_attempted_at"] is None
            self.rowcount = 0 if effects_pending or notification_pending else 1

    cursor = CompletionCursor()
    monkeypatch.setattr(monthly_bumps, "pooled_connection", connection_for(cursor))

    assert not monthly_bumps.try_complete_monthly_reward_run(7)
    state["notification_attempted_at"] = datetime(2026, 10, 1)
    assert monthly_bumps.try_complete_monthly_reward_run(7)


def test_no_reward_grant_does_not_require_or_attempt_notification(monkeypatch):
    grant = {
        "id": 1,
        "run_id": 7,
        "discord_user_id": 2,
        "rank_position": 1,
        "bump_count": 10,
        "coins_amount": 0,
        "coins_applied_at": None,
        "role_id": None,
        "role_days": 0,
        "role_applied_at": None,
        "role_skipped_at": None,
        "notification_attempted_at": None,
    }
    member = SimpleNamespace(send=AsyncMock())
    guild = SimpleNamespace(get_member=lambda _id: member)
    reserve = Mock()
    monkeypatch.setattr(monthly_bumps, "_apply_grant_coins", AsyncMock())
    monkeypatch.setattr(monthly_bumps, "_apply_grant_role", AsyncMock(return_value=False))
    monkeypatch.setattr(monthly_bumps, "list_monthly_reward_grants", lambda _id: [grant])
    monkeypatch.setattr(monthly_bumps, "reserve_monthly_notification", reserve)

    run(monthly_bumps.process_monthly_reward_grant(guild, grant))

    assert not monthly_bumps.monthly_grant_has_reward(grant)
    reserve.assert_not_called()
    member.send.assert_not_awaited()

    completion_cursor = ScriptedCursor(rowcount=1)
    monkeypatch.setattr(
        monthly_bumps, "pooled_connection", connection_for(completion_cursor)
    )
    assert monthly_bumps.try_complete_monthly_reward_run(7)
    completion_sql = completion_cursor.executed[0][0]
    assert "reward_grant.notification_attempted_at IS NULL" in completion_sql
    assert "reward_grant.coins_amount > 0 OR" in completion_sql


def test_empty_ranking_is_prepared_and_completed(monkeypatch):
    cursor = ScriptedCursor(
        [
            {
                "id": 7,
                "server_guild_id": 9,
                "ranking_prepared_at": None,
                "reward_role_id": None,
            }
        ]
    )
    monkeypatch.setattr(monthly_bumps, "pooled_connection", connection_for(cursor))

    assert monthly_bumps.prepare_monthly_reward_ranking(7, [])
    final_query, params = cursor.executed[-1]
    assert "ranking_prepared_at = NOW()" in final_query
    assert "CASE WHEN %s = 0 THEN NOW()" in final_query
    assert params == (0, 7)


def test_apply_coins_uses_one_locked_transaction_and_marks_after_balance(monkeypatch):
    cursor = ScriptedCursor(
        [
            {
                "id": 17,
                "run_id": 7,
                "discord_user_id": 99,
                "server_guild_id": 9,
                "coins_amount": 25,
                "coins_applied_at": None,
            },
            {"user_id": 42},
        ]
    )
    monkeypatch.setattr(monthly_bumps, "pooled_connection", connection_for(cursor))
    ensure = Mock(return_value={"id": 88, "bank_balance": 10})
    monkeypatch.setattr(monthly_bumps, "_ensure_economy_entry", ensure)

    assert monthly_bumps.apply_monthly_reward_coins(17)

    assert cursor.executed[0][0].endswith("FOR UPDATE")
    ensure.assert_called_once_with(cursor, 42, 9, lock=True)
    assert cursor.executed[-2] == (
        "UPDATE user_economy SET bank_balance = %s WHERE id = %s",
        (35, 88),
    )
    assert "coins_applied_at = NOW()" in cursor.executed[-1][0]


def test_apply_coins_is_noop_when_marker_already_exists(monkeypatch):
    cursor = ScriptedCursor(
        [
            {
                "id": 17,
                "coins_amount": 25,
                "coins_applied_at": datetime(2026, 10, 1),
            }
        ]
    )
    monkeypatch.setattr(monthly_bumps, "pooled_connection", connection_for(cursor))
    ensure = Mock()
    monkeypatch.setattr(monthly_bumps, "_ensure_economy_entry", ensure)

    assert not monthly_bumps.apply_monthly_reward_coins(17)
    ensure.assert_not_called()
    assert len(cursor.executed) == 1


def test_coin_marker_failure_rolls_back_balance_and_retry_applies_once(monkeypatch):
    state = {"balance": 10, "marker": False}
    fail_marker = {"value": True}

    class TransactionCursor:
        def __init__(self, working):
            self.working = working
            self.selected = None

        def execute(self, query, params=None):
            normalized = " ".join(query.split())
            if normalized.startswith("SELECT reward_grant.*"):
                self.selected = "grant"
            elif normalized.startswith("SELECT user_id"):
                self.selected = "identity"
            elif normalized.startswith("UPDATE user_economy"):
                self.working["balance"] = params[0]
            elif "SET coins_applied_at = NOW()" in normalized:
                if fail_marker["value"]:
                    raise RuntimeError("marker write failed")
                self.working["marker"] = True

        def fetchone(self):
            if self.selected == "identity":
                return {"user_id": 42}
            return {
                "id": 17,
                "run_id": 7,
                "discord_user_id": 99,
                "server_guild_id": 9,
                "coins_amount": 25,
                "coins_applied_at": datetime(2026, 10, 1) if self.working["marker"] else None,
            }

    @contextmanager
    def transactional_connection(*_args, **_kwargs):
        working = dict(state)
        try:
            yield TransactionCursor(working)
        except Exception:
            raise
        else:
            state.update(working)

    monkeypatch.setattr(monthly_bumps, "pooled_connection", transactional_connection)
    monkeypatch.setattr(
        monthly_bumps,
        "_ensure_economy_entry",
        lambda cursor, *_args, **_kwargs: {
            "id": 88,
            "bank_balance": cursor.working["balance"],
        },
    )

    with pytest.raises(RuntimeError, match="marker write failed"):
        monthly_bumps.apply_monthly_reward_coins(17)
    assert state == {"balance": 10, "marker": False}

    fail_marker["value"] = False
    assert monthly_bumps.apply_monthly_reward_coins(17)
    assert state == {"balance": 35, "marker": True}
    assert not monthly_bumps.apply_monthly_reward_coins(17)
    assert state == {"balance": 35, "marker": True}


def test_role_expiration_is_persisted_once_and_reused(monkeypatch):
    existing_expiry = datetime(2026, 10, 31, 12)
    first_cursor = ScriptedCursor(
        [{"id": 1, "role_id": 5, "role_days": 30, "role_expires_at": None}]
    )
    second_cursor = ScriptedCursor(
        [{"id": 1, "role_id": 5, "role_days": 30, "role_expires_at": existing_expiry}]
    )
    connections = iter([connection_for(first_cursor), connection_for(second_cursor)])
    monkeypatch.setattr(monthly_bumps, "pooled_connection", lambda: next(connections)())
    reference = datetime(2026, 10, 1, 12, tzinfo=monthly_bumps.SAO_PAULO)

    first = monthly_bumps.ensure_monthly_role_expiration(1, reference)
    second = monthly_bumps.ensure_monthly_role_expiration(1, reference.replace(day=2))

    assert first == existing_expiry
    assert second == existing_expiry
    assert any("SET role_expires_at" in query for query, _ in first_cursor.executed)
    assert not any("SET role_expires_at" in query for query, _ in second_cursor.executed)


def test_role_application_rechecks_locked_marker_before_discord(monkeypatch):
    cursor = ScriptedCursor(
        [{"id": 1, "role_applied_at": datetime(2026, 10, 1), "role_skipped_at": None}]
    )
    monkeypatch.setattr(monthly_bumps, "pooled_connection", connection_for(cursor))
    assign = AsyncMock()
    monkeypatch.setattr(monthly_bumps, "assignTempRole", assign)

    applied = run(
        monthly_bumps.apply_monthly_reward_role(
            1,
            SimpleNamespace(id=9),
            object(),
            5,
            datetime(2026, 10, 31),
        )
    )

    assert not applied
    assign.assert_not_awaited()
    assert cursor.executed[0][0].endswith("FOR UPDATE")


def test_removed_role_is_permanently_skipped_after_expiry_is_persisted(monkeypatch):
    grant = {
        "id": 1,
        "discord_user_id": 2,
        "role_id": 5,
        "role_days": 7,
        "role_applied_at": None,
        "role_skipped_at": None,
    }
    guild = SimpleNamespace(
        id=9,
        get_member=lambda _id: object(),
        get_role=lambda _id: None,
    )
    expiration = datetime(2026, 10, 8, 12)
    ensure = Mock(return_value=expiration)
    skipped = Mock()
    monkeypatch.setattr(monthly_bumps, "ensure_monthly_role_expiration", ensure)
    monkeypatch.setattr(monthly_bumps, "mark_monthly_role_skipped", skipped)

    assert not run(
        monthly_bumps._apply_grant_role(
            guild,
            grant,
            datetime(2026, 10, 1, 12, tzinfo=monthly_bumps.SAO_PAULO),
        )
    )
    ensure.assert_called_once()
    skipped.assert_called_once_with(1, "Cargo de recompensa não existe mais")


def test_temporarily_unassignable_role_remains_pending(monkeypatch):
    role = object()
    grant = {
        "id": 1,
        "discord_user_id": 2,
        "role_id": 5,
        "role_days": 7,
        "role_applied_at": None,
        "role_skipped_at": None,
    }
    guild = SimpleNamespace(
        id=9,
        get_member=lambda _id: object(),
        get_role=lambda _id: role,
    )
    skipped = Mock()
    monkeypatch.setattr(
        monthly_bumps,
        "ensure_monthly_role_expiration",
        lambda *_args: datetime(2026, 10, 8, 12),
    )
    monkeypatch.setattr(monthly_bumps, "resolve_assignable_bump_role", lambda *_args: None)
    monkeypatch.setattr(monthly_bumps, "mark_monthly_role_skipped", skipped)

    with pytest.raises(RuntimeError, match="não está atribuível"):
        run(
            monthly_bumps._apply_grant_role(
                guild,
                grant,
                datetime(2026, 10, 1, 12, tzinfo=monthly_bumps.SAO_PAULO),
            )
        )
    skipped.assert_not_called()


def test_monthly_source_channel_recovers_legacy_thread_from_guild_cache():
    thread = HistoryChannel([])
    thread.guild = SimpleNamespace(id=9)
    guild = SimpleNamespace(
        id=9,
        get_channel_or_thread=lambda channel_id: thread if channel_id == 50 else None,
        get_channel=lambda _id: None,
    )
    bot = SimpleNamespace()

    resolved = run(monthly_bumps.resolve_monthly_source_channel(bot, guild, 50))

    assert resolved is thread


def test_monthly_source_channel_fetches_when_cold_cache_misses():
    channel = HistoryChannel([])
    channel.guild = SimpleNamespace(id=9)
    fetch_channel = AsyncMock(return_value=channel)
    guild = SimpleNamespace(
        id=9,
        get_channel_or_thread=lambda _id: None,
        get_channel=lambda _id: None,
    )
    bot = SimpleNamespace(get_channel=lambda _id: None, fetch_channel=fetch_channel)

    resolved = run(monthly_bumps.resolve_monthly_source_channel(bot, guild, 50))

    assert resolved is channel
    fetch_channel.assert_awaited_once_with(50)


def test_monthly_run_error_log_exposes_stage_and_error(monkeypatch, caplog):
    run_row = {
        "id": 7,
        "server_guild_id": 9,
        "source_channel_id": 50,
        "period_start": date(2026, 9, 1),
        "period_end": date(2026, 10, 1),
        "ranking_prepared_at": None,
        "completed_at": None,
    }
    guild = SimpleNamespace(
        id=9,
        get_channel_or_thread=lambda _id: None,
        get_channel=lambda _id: None,
    )
    bot = SimpleNamespace(get_guild=lambda _id: guild)
    monkeypatch.setattr(monthly_bumps, "start_monthly_reward_attempt", lambda _id: run_row)
    record_error = Mock()
    monkeypatch.setattr(monthly_bumps, "_record_run_error", record_error)

    with caplog.at_level(logging.ERROR, logger=monthly_bumps.__name__):
        run(monthly_bumps.process_monthly_reward_run(bot, run_row))

    record_error.assert_called_once()
    assert "stage=resolve_source_channel" in caplog.text
    assert "source_channel_id=50" in caplog.text
    assert "error=RuntimeError:" in caplog.text


def test_prepared_retry_never_scans_discord_history(monkeypatch):
    run_row = {
        "id": 7,
        "server_guild_id": 9,
        "ranking_prepared_at": datetime(2026, 10, 1),
        "completed_at": None,
    }
    get_member = Mock()
    guild = SimpleNamespace(id=9, get_member=get_member)
    bot = SimpleNamespace(get_guild=lambda _guild_id: guild)
    monkeypatch.setattr(monthly_bumps, "start_monthly_reward_attempt", lambda _id: run_row)
    collector = AsyncMock()
    monkeypatch.setattr(monthly_bumps, "collect_monthly_bump_ranking", collector)
    monkeypatch.setattr(monthly_bumps, "list_monthly_reward_grants", lambda _id: [])
    complete = Mock(return_value=True)
    monkeypatch.setattr(monthly_bumps, "try_complete_monthly_reward_run", complete)

    run(monthly_bumps.process_monthly_reward_run(bot, run_row))

    collector.assert_not_awaited()
    get_member.assert_not_called()
    complete.assert_called_once_with(7)


def test_unprepared_ranking_excludes_members_who_left_before_freeze(monkeypatch):
    run_row = {
        "id": 7,
        "server_guild_id": 9,
        "source_channel_id": 50,
        "period_start": date(2026, 9, 1),
        "period_end": date(2026, 10, 1),
        "ranking_prepared_at": None,
        "completed_at": None,
    }
    raw_ranking = [
        {"discord_user_id": 1, "bump_count": 20},
        {"discord_user_id": 2, "bump_count": 15},
        {"discord_user_id": 3, "bump_count": 10},
        {"discord_user_id": 4, "bump_count": 5},
    ]
    current_members = {2: object(), 3: object(), 4: object()}
    source_channel = HistoryChannel([])
    source_channel.guild = SimpleNamespace(id=9)
    guild = SimpleNamespace(
        id=9,
        get_channel=lambda _id: source_channel,
        get_member=lambda user_id: current_members.get(user_id),
    )
    bot = SimpleNamespace(get_guild=lambda _id: guild)
    monkeypatch.setattr(monthly_bumps, "start_monthly_reward_attempt", lambda _id: run_row)
    monkeypatch.setattr(
        monthly_bumps,
        "collect_monthly_bump_ranking",
        AsyncMock(return_value=raw_ranking),
    )
    prepare = Mock()
    monkeypatch.setattr(monthly_bumps, "prepare_monthly_reward_ranking", prepare)
    monkeypatch.setattr(monthly_bumps, "list_monthly_reward_grants", lambda _id: [])
    monkeypatch.setattr(monthly_bumps, "try_complete_monthly_reward_run", Mock())

    run(monthly_bumps.process_monthly_reward_run(bot, run_row))

    frozen_ranking = prepare.call_args.args[1]
    assert [entry["discord_user_id"] for entry in frozen_ranking] == [2, 3, 4]
    assert [entry["bump_count"] for entry in frozen_ranking] == [15, 10, 5]


def test_one_grant_failure_does_not_block_other_grants(monkeypatch):
    run_row = {
        "id": 7,
        "server_guild_id": 9,
        "ranking_prepared_at": datetime(2026, 10, 1),
        "completed_at": None,
    }
    bot = SimpleNamespace(get_guild=lambda _id: SimpleNamespace(id=9))
    grants = [{"id": 1}, {"id": 2}]
    monkeypatch.setattr(monthly_bumps, "start_monthly_reward_attempt", lambda _id: run_row)
    monkeypatch.setattr(monthly_bumps, "list_monthly_reward_grants", lambda _id: grants)
    process = AsyncMock(side_effect=[RuntimeError("first failed"), None])
    monkeypatch.setattr(monthly_bumps, "process_monthly_reward_grant", process)
    record_error = Mock()
    monkeypatch.setattr(monthly_bumps, "_record_grant_error", record_error)
    monkeypatch.setattr(monthly_bumps, "try_complete_monthly_reward_run", Mock())

    run(monthly_bumps.process_monthly_reward_run(bot, run_row))

    assert process.await_count == 2
    record_error.assert_called_once()


def test_cycle_resumes_incomplete_run_after_day_three_without_new_preparation(monkeypatch):
    pending = {"id": 7}
    bot = object()
    monkeypatch.setattr(monthly_bumps, "list_incomplete_monthly_reward_runs", lambda: [pending])
    process = AsyncMock()
    monkeypatch.setattr(monthly_bumps, "process_monthly_reward_run", process)
    configs = Mock()
    monkeypatch.setattr(monthly_bumps, "listBumpMonthlyRewardConfigs", configs)
    current = datetime(2026, 10, 20, 12, tzinfo=monthly_bumps.SAO_PAULO)

    run(monthly_bumps.run_monthly_reward_cycle(bot, current))

    process.assert_awaited_once_with(bot, pending, current)
    configs.assert_not_called()


def test_notification_failure_is_attempted_at_most_once(monkeypatch):
    member = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("DM blocked")))
    guild = SimpleNamespace(get_member=lambda _id: member)
    reserve = Mock(side_effect=[True, False])
    monkeypatch.setattr(monthly_bumps, "reserve_monthly_notification", reserve)
    monkeypatch.setattr(monthly_bumps, "mark_monthly_notification_sent", Mock())
    grant = {
        "id": 1,
        "discord_user_id": 2,
        "rank_position": 1,
        "bump_count": 10,
        "coins_amount": 5,
        "role_id": 6,
        "role_days": 7,
    }

    with pytest.raises(RuntimeError):
        run(monthly_bumps._attempt_grant_notification(guild, grant, True))
    run(monthly_bumps._attempt_grant_notification(guild, grant, True))

    assert member.send.await_count == 1
    assert reserve.call_count == 2


def test_notification_reservation_failure_retries_but_send_failure_does_not(monkeypatch):
    grant = {
        "id": 1,
        "run_id": 7,
        "discord_user_id": 2,
        "rank_position": 1,
        "bump_count": 10,
        "coins_amount": 25,
        "coins_applied_at": datetime(2026, 10, 1),
        "role_id": None,
        "role_days": 0,
        "role_applied_at": None,
        "role_skipped_at": None,
        "notification_attempted_at": None,
        "notification_sent_at": None,
    }
    member = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("DM blocked")))
    guild = SimpleNamespace(get_member=lambda _id: member)
    monkeypatch.setattr(monthly_bumps, "_apply_grant_coins", AsyncMock())
    monkeypatch.setattr(monthly_bumps, "_apply_grant_role", AsyncMock(return_value=False))
    monkeypatch.setattr(monthly_bumps, "list_monthly_reward_grants", lambda _id: [grant])
    monkeypatch.setattr(monthly_bumps, "_record_grant_error", Mock())

    reserve_calls = 0

    def reserve(_grant_id):
        nonlocal reserve_calls
        reserve_calls += 1
        if reserve_calls == 1:
            raise RuntimeError("marker unavailable")
        if grant["notification_attempted_at"] is not None:
            return False
        grant["notification_attempted_at"] = datetime(2026, 10, 2)
        return True

    monkeypatch.setattr(monthly_bumps, "reserve_monthly_notification", reserve)
    monkeypatch.setattr(monthly_bumps, "mark_monthly_notification_sent", Mock())

    run(monthly_bumps.process_monthly_reward_grant(guild, grant))
    assert grant["notification_attempted_at"] is None
    assert member.send.await_count == 0

    run(monthly_bumps.process_monthly_reward_grant(guild, grant))
    assert grant["notification_attempted_at"] is not None
    assert grant["notification_sent_at"] is None
    assert member.send.await_count == 1

    run(monthly_bumps.process_monthly_reward_grant(guild, grant))
    assert member.send.await_count == 1
    assert reserve_calls == 3


def test_notification_mentions_role_only_when_it_was_applied(monkeypatch):
    member = SimpleNamespace(send=AsyncMock())
    guild = SimpleNamespace(get_member=lambda _id: member)
    monkeypatch.setattr(monthly_bumps, "reserve_monthly_notification", lambda _id: True)
    monkeypatch.setattr(monthly_bumps, "mark_monthly_notification_sent", Mock())
    grant = {
        "id": 1,
        "discord_user_id": 2,
        "rank_position": 1,
        "bump_count": 10,
        "coins_amount": 5,
        "role_id": 6,
        "role_days": 7,
    }

    run(monthly_bumps._attempt_grant_notification(guild, grant, False))

    assert "<@&6>" not in member.send.await_args.args[0]


def test_sanitize_error_is_single_line_and_limited_to_database_column():
    result = monthly_bumps.sanitize_error(RuntimeError("bad\n" + "x" * 600))
    assert "\n" not in result
    assert len(result) == 512
