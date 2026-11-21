"""Regression tests for the legacy /eventos and /evento data queries.

SQLite exercises the selection logic without requiring a running Discord bot or
a shared MariaDB datasource. The table and column names mirror the Database
repository's 2026-10-20 structural baseline.
"""

import sqlite3
from datetime import datetime

from core.event_queries import (
    fetch_event_by_name,
    list_events_by_owner,
    list_upcoming_events,
)


NOW = datetime(2026, 11, 21, 12, 0, 0)


class SQLiteCursor:
    """Adapt MySQL's parameter marker for isolated SQL behavior tests."""

    def __init__(self, database):
        self.database = database
        self.cursor = None
        self.executed = []

    def execute(self, sql, params):
        assert "events.starting_datetime" not in sql
        assert "events.ending_datetime" not in sql
        assert "JOIN event_scheduling AS schedule" in sql
        self.executed.append((sql, params))
        values = tuple(
            value.strftime("%Y-%m-%d %H:%M:%S")
            if isinstance(value, datetime) else value
            for value in params
        )
        self.cursor = self.database.execute(sql.replace("%s", "?"), values)

    def fetchall(self):
        return [dict(row) for row in self.cursor.fetchall()]


def make_database():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL);
        CREATE TABLE locale (id INTEGER PRIMARY KEY, locale_name TEXT, locale_abbrev TEXT);
        CREATE TABLE events (
            id INTEGER PRIMARY KEY, host_user_id INTEGER, locale_id INTEGER,
            event_name TEXT, address TEXT, point_name TEXT, price REAL,
            max_price REAL, description TEXT, group_chat_link TEXT,
            city TEXT, website TEXT, event_logo_url TEXT,
            out_of_tickets INTEGER, sales_ended INTEGER, approved INTEGER
        );
        CREATE TABLE event_scheduling (
            id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL,
            starting_datetime TEXT NOT NULL, ending_datetime TEXT NOT NULL
        );
        INSERT INTO users VALUES (1, 'Host');
        INSERT INTO locale VALUES (10, 'São Paulo', 'SP');
        INSERT INTO locale VALUES (20, 'Rio de Janeiro', 'RJ');
    """)
    return db


def add_event(db, id, name, locale=10, approved=1):
    db.execute(
        """INSERT INTO events VALUES
        (?, 1, ?, ?, 'Rua Exemplo', NULL, 10.0, 15.0, NULL,
         'chat.example.com', 'São Paulo', 'https://site.example.com',
         NULL, 0, 0, ?)""",
        (id, locale, name, approved),
    )


def add_schedule(db, id, event_id, start, end):
    db.execute(
        "INSERT INTO event_scheduling VALUES (?, ?, ?, ?)",
        (id, event_id, start, end),
    )


def test_list_chooses_one_ongoing_or_next_occurrence_and_filters_state():
    with make_database() as db:
        add_event(db, 1, "Furry Fest")
        add_event(db, 2, "Patas")
        add_event(db, 3, "Evento não aprovado", approved=0)
        add_event(db, 4, "Evento RJ", locale=20)

        add_schedule(db, 1, 1, "2026-10-01 10:00:00", "2026-10-02 11:00:00")
        add_schedule(db, 2, 1, "2026-11-20 09:00:00", "2026-11-22 18:00:00")
        add_schedule(db, 3, 1, "2026-12-09 10:00:00", "2026-12-10 18:00:00")
        add_schedule(db, 4, 2, "2026-12-01 09:00:00", "2026-12-01 19:00:00")
        add_schedule(db, 5, 3, "2026-11-22 09:00:00", "2026-11-22 18:00:00")
        add_schedule(db, 6, 4, "2026-11-24 09:00:00", "2026-11-24 18:00:00")

        cursor = SQLiteCursor(db)
        all_events = list_upcoming_events(cursor, now=NOW)
        assert [e["id"] for e in all_events] == [1, 4, 2]
        assert all_events[0]["starting_datetime"] == datetime(2026, 11, 20, 9)
        assert all_events[0]["group_chat_link"] == "https://chat.example.com"
        assert isinstance(all_events[1]["ending_datetime"], datetime)

        sp_events = list_upcoming_events(cursor, locale_id=10, now=NOW)
        assert [e["id"] for e in sp_events] == [1, 2]
        assert cursor.executed[-1][1] == (NOW, 10)


def test_detail_prefers_exact_then_partial_name_and_never_interpolates_input():
    with make_database() as db:
        add_event(db, 1, "Patas")
        add_event(db, 2, "Patas de Inverno")
        add_schedule(db, 1, 1, "2026-11-25 09:00:00", "2026-11-25 18:00:00")
        add_schedule(db, 2, 2, "2026-12-05 09:00:00", "2026-12-05 18:00:00")
        cursor = SQLiteCursor(db)

        assert fetch_event_by_name(cursor, "Patas", now=NOW)["id"] == 1
        assert fetch_event_by_name(cursor, "Inverno", now=NOW)["id"] == 2
        assert fetch_event_by_name(cursor, "' OR 1=1 --", now=NOW) is None
        assert all("' OR 1=1 --" not in sql for sql, _ in cursor.executed)


def test_detail_and_owner_preserve_access_to_last_historical_occurrence():
    with make_database() as db:
        add_event(db, 1, "Festival antigo", approved=0)
        add_schedule(db, 1, 1, "2026-10-01 09:00:00", "2026-10-02 18:00:00")
        add_schedule(db, 2, 1, "2026-11-01 09:00:00", "2026-11-02 18:00:00")
        cursor = SQLiteCursor(db)

        assert list_upcoming_events(cursor, now=NOW) == []
        owner_events = list_events_by_owner(cursor, "Host", now=NOW)
        assert len(owner_events) == 1
        assert owner_events[0]["ending_datetime"] == datetime(2026, 11, 2, 18)

        # Public detail still requires approval, even for historic records.
        assert fetch_event_by_name(cursor, "Festival antigo", now=NOW) is None
        db.execute("UPDATE events SET approved = 1 WHERE id = 1")
        assert fetch_event_by_name(cursor, "Festival antigo", now=NOW)["id"] == 1


def test_equal_starts_use_schedule_id_as_stable_tiebreaker():
    with make_database() as db:
        add_event(db, 1, "Evento recorrente")
        add_schedule(db, 15, 1, "2026-12-01 09:00:00", "2026-12-01 18:00:00")
        add_schedule(db, 11, 1, "2026-12-01 09:00:00", "2026-12-01 16:00:00")
        cursor = SQLiteCursor(db)
        events = list_upcoming_events(cursor, now=NOW)
        assert len(events) == 1
        assert events[0]["ending_datetime"] == datetime(2026, 12, 1, 16)
