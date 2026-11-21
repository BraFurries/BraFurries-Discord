"""Read-only queries for the shared event catalogue.

Dates belong to event_scheduling, not events. One event may have multiple
schedule entries; Discord commands display one occurrence per event.
"""

from datetime import datetime


_EVENT_SELECT = """
    SELECT events.id,
           events.event_name,
           events.address,
           events.point_name,
           events.price,
           events.max_price,
           schedule.starting_datetime,
           schedule.ending_datetime,
           events.description,
           events.group_chat_link,
           users.username,
           locale.locale_name AS state,
           locale.locale_abbrev AS state_abbrev,
           events.city,
           events.website,
           events.event_logo_url,
           events.out_of_tickets,
           events.sales_ended
    FROM events
    JOIN users ON events.host_user_id = users.id
    JOIN locale ON events.locale_id = locale.id
"""

# Prefer an ongoing event, then the next upcoming occurrence. The scheduling
# indexes include event_id, dates and id to support this deterministic lookup.
_UPCOMING_SCHEDULE = """
    JOIN event_scheduling AS schedule ON schedule.id = (
        SELECT candidate.id
        FROM event_scheduling AS candidate
        WHERE candidate.event_id = events.id
          AND candidate.ending_datetime >= %s
        ORDER BY candidate.starting_datetime ASC, candidate.id ASC
        LIMIT 1
    )
"""

# Detail and owner queries historically allowed past events. Preserve that
# behavior by falling back to the most recently finished occurrence.
_CURRENT_OR_LAST_SCHEDULE = """
    JOIN event_scheduling AS schedule ON schedule.id = COALESCE(
        (
            SELECT upcoming.id
            FROM event_scheduling AS upcoming
            WHERE upcoming.event_id = events.id
              AND upcoming.ending_datetime >= %s
            ORDER BY upcoming.starting_datetime ASC, upcoming.id ASC
            LIMIT 1
        ),
        (
            SELECT previous.id
            FROM event_scheduling AS previous
            WHERE previous.event_id = events.id
            ORDER BY previous.ending_datetime DESC, previous.id DESC
            LIMIT 1
        )
    )
"""


def _read_events(cursor, query: str, parameters: tuple) -> list[dict]:
    cursor.execute(query, parameters)
    rows = cursor.fetchall()
    result = []
    for original in rows:
        event = dict(original)
        for key in ("starting_datetime", "ending_datetime"):
            value = event[key]
            if isinstance(value, str):
                event[key] = datetime.fromisoformat(value)

        for key in ("website", "group_chat_link"):
            value = event[key]
            if value and not value.lower().startswith(("http://", "https://")):
                event[key] = f"https://{value}"
        result.append(event)
    return result


def list_upcoming_events(cursor, locale_id: int | None = None, now: datetime | None = None) -> list[dict]:
    """List approved events once each, using their nearest non-ended date."""
    current = now if now is not None else datetime.now()
    condition = "WHERE events.approved = 1"
    parameters = [current]
    if locale_id is not None:
        condition += " AND events.locale_id = %s"
        parameters.append(locale_id)
    query = (
        _EVENT_SELECT + _UPCOMING_SCHEDULE + condition +
        " ORDER BY schedule.starting_datetime ASC, events.id ASC"
    )
    return _read_events(cursor, query, tuple(parameters))


def fetch_event_by_name(cursor, event_name: str, now: datetime | None = None) -> dict | None:
    """Prefer an exact approved name, then a partial match."""
    current = now if now is not None else datetime.now()
    base = _EVENT_SELECT + _CURRENT_OR_LAST_SCHEDULE + " WHERE events.approved = 1"
    for condition, name in (
        (" AND events.event_name = %s", event_name),
        (" AND events.event_name LIKE %s", f"%{event_name}%"),
    ):
        query = base + condition + " ORDER BY events.event_name ASC, events.id ASC LIMIT 1"
        matches = _read_events(cursor, query, (current, name))
        if matches:
            return matches[0]
    return None


def list_events_by_owner(cursor, owner_name: str, now: datetime | None = None) -> list[dict]:
    """Keep owner-visible historical events, choosing one date per event."""
    current = now if now is not None else datetime.now()
    query = (
        _EVENT_SELECT + _CURRENT_OR_LAST_SCHEDULE +
        " WHERE users.username = %s ORDER BY schedule.starting_datetime ASC, events.id ASC"
    )
    return _read_events(cursor, query, (current, owner_name))
