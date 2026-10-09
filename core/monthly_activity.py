from datetime import datetime, timedelta
from typing import Dict, List

from dateutil import relativedelta

from core.database import pooled_connection
from core.time_functions import now


def current_month() -> str:
    """Return the current month formatted as YYYY-MM."""
    return datetime.now().strftime('%Y-%m')


def current_week() -> str:
    """Return the start date of the current week formatted as YYYY-MM-DD."""
    now = datetime.now()
    monday = now - timedelta(days=now.weekday())
    return monday.strftime('%Y-%m-%d')


def previous_months(amount: int) -> List[str]:
    """Return a list of the last ``amount`` months including the current one."""
    months: List[str] = []
    now = datetime.now()
    for i in range(amount):
        dt = now - relativedelta.relativedelta(months=i)
        months.append(dt.strftime('%Y-%m'))
    return months


def add_time(
    game: str,
    seconds: int,
    guild_id: int,
    month: str | None = None,
    week: str | None = None,
    update_time: datetime | None = None,
) -> None:
    """Record play time for a game in the given month and week for a guild."""
    month = month or current_month()
    week = week or current_week()
    update_time = update_time or now()
    with pooled_connection() as cursor:
        cursor.execute(
            """
            SELECT id FROM stats_monthly_game_activity
             WHERE server_guild_id = %s AND month = %s AND game_name = %s
            """,
            (guild_id, month, game),
        )
        if cursor.fetchone():
            cursor.execute(
                """
                UPDATE stats_monthly_game_activity
                   SET seconds = seconds + %s,
                       last_update = %s
                 WHERE server_guild_id = %s AND month = %s AND game_name = %s
                """,
                (seconds, update_time, guild_id, month, game),
            )
        else:
            cursor.execute(
                """
                INSERT INTO stats_monthly_game_activity
                    (server_guild_id, month, game_name, seconds, last_update)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (guild_id, month, game, seconds, update_time),
            )

        cursor.execute(
            """
            SELECT id FROM stats_weekly_game_activity
             WHERE server_guild_id = %s AND week = %s AND game_name = %s
            """,
            (guild_id, week, game),
        )
        if cursor.fetchone():
            cursor.execute(
                """
                UPDATE stats_weekly_game_activity
                   SET seconds = seconds + %s,
                       last_update = %s
                 WHERE server_guild_id = %s AND week = %s AND game_name = %s
                """,
                (seconds, update_time, guild_id, week, game),
            )
        else:
            cursor.execute(
                """
                INSERT INTO stats_weekly_game_activity
                    (server_guild_id, week, game_name, seconds, last_update)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (guild_id, week, game, seconds, update_time),
            )


def add_time_batch(entries: List[tuple[str, int, int, datetime]]) -> None:
    """Persist a batch of play-time entries using a single transaction.

    Each entry follows: ``(game_name, seconds, guild_id, update_time)``.
    """
    if not entries:
        return

    monthly_totals: dict[tuple[int, str, str], tuple[int, datetime]] = {}
    weekly_totals: dict[tuple[int, str, str], tuple[int, datetime]] = {}

    for game_name, seconds, guild_id, update_time in entries:
        if seconds <= 0:
            continue

        month = update_time.strftime('%Y-%m')
        monday = update_time - timedelta(days=update_time.weekday())
        week = monday.strftime('%Y-%m-%d')

        monthly_key = (guild_id, month, game_name)
        weekly_key = (guild_id, week, game_name)

        current_monthly = monthly_totals.get(monthly_key)
        if current_monthly is None:
            monthly_totals[monthly_key] = (seconds, update_time)
        else:
            monthly_totals[monthly_key] = (
                current_monthly[0] + seconds,
                max(current_monthly[1], update_time),
            )

        current_weekly = weekly_totals.get(weekly_key)
        if current_weekly is None:
            weekly_totals[weekly_key] = (seconds, update_time)
        else:
            weekly_totals[weekly_key] = (
                current_weekly[0] + seconds,
                max(current_weekly[1], update_time),
            )

    if not monthly_totals and not weekly_totals:
        return

    monthly_params = [
        (guild_id, month, game_name, seconds, update_time)
        for (guild_id, month, game_name), (seconds, update_time) in monthly_totals.items()
    ]
    weekly_params = [
        (guild_id, week, game_name, seconds, update_time)
        for (guild_id, week, game_name), (seconds, update_time) in weekly_totals.items()
    ]

    with pooled_connection() as cursor:
        if monthly_params:
            cursor.executemany(
                """
                INSERT INTO stats_monthly_game_activity
                    (server_guild_id, month, game_name, seconds, last_update)
                VALUES (%s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    seconds = seconds + VALUES(seconds),
                    last_update = GREATEST(last_update, VALUES(last_update))
                """,
                monthly_params,
            )

        if weekly_params:
            cursor.executemany(
                """
                INSERT INTO stats_weekly_game_activity
                    (server_guild_id, week, game_name, seconds, last_update)
                VALUES (%s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    seconds = seconds + VALUES(seconds),
                    last_update = GREATEST(last_update, VALUES(last_update))
                """,
                weekly_params,
            )


def get_trending_games(guild_id: int, month: str = None) -> Dict[str, int]:
    """Return a mapping of game names to total seconds for a guild.

    If ``month`` is provided the data is taken from ``stats_monthly_game_activity``
    for that month. Otherwise the function aggregates the play time from the last
    four weeks stored in ``stats_weekly_game_activity``. This ensures a non empty
    result even when the current month has no entries yet.
    """
    with pooled_connection() as cursor:
        if month is not None:
            sql = """
            SELECT game_name, seconds
              FROM stats_monthly_game_activity
             WHERE server_guild_id = %s AND month = %s
             ORDER BY seconds DESC;
            """
            cursor.execute(sql, (guild_id, month))
            rows = cursor.fetchall()
            return {row["game_name"]: int(row["seconds"]) for row in rows}

        # Aggregate from the last four weeks
        now = datetime.now()
        monday = now - timedelta(days=now.weekday())
        start = monday - timedelta(weeks=3)
        start_week = start.strftime('%Y-%m-%d')
        sql = """
        SELECT game_name, SUM(seconds) AS total
          FROM stats_weekly_game_activity
         WHERE server_guild_id = %s AND week >= %s
         GROUP BY game_name
         ORDER BY total DESC;
        """
        cursor.execute(sql, (guild_id, start_week))
        rows = cursor.fetchall()
        return {row["game_name"]: int(row["total"]) for row in rows}


def get_total_games(guild_id: int) -> Dict[str, int]:
    """Return total seconds played for each game for a guild across all months."""
    with pooled_connection() as cursor:
        sql = """
        SELECT game_name, SUM(seconds) AS total
          FROM stats_monthly_game_activity
         WHERE server_guild_id = %s
         GROUP BY game_name
         ORDER BY total DESC;
        """
        cursor.execute(sql, (guild_id,))
        rows = cursor.fetchall()
        return {row["game_name"]: int(row["total"]) for row in rows}


def get_games_for_months(guild_id: int, months: List[str]) -> Dict[str, int]:
    """Return the total seconds played for the given months in a guild."""
    if not months:
        return {}
    placeholders = ", ".join(["%s"] * len(months))
    with pooled_connection() as cursor:
        sql = f"""
        SELECT game_name, SUM(seconds) AS total
          FROM stats_monthly_game_activity
         WHERE server_guild_id = %s AND month IN ({placeholders})
         GROUP BY game_name
         ORDER BY total DESC;
        """
        cursor.execute(sql, (guild_id, *months))
        rows = cursor.fetchall()
        return {row["game_name"]: int(row["total"]) for row in rows}


def cleanup_old_weekly_entries(max_age_weeks: int = 4) -> None:
    """Delete weekly records older than ``max_age_weeks`` weeks."""
    threshold = datetime.now() - timedelta(weeks=max_age_weeks)
    monday = threshold - timedelta(days=threshold.weekday())
    limit = monday.strftime('%Y-%m-%d')
    with pooled_connection() as cursor:
        sql = """
        DELETE FROM stats_weekly_game_activity
              WHERE week < %s;
        """
        cursor.execute(sql, (limit,))
