# Administrative log cursor contract

`GET /logs` keeps its authentication and existing `limit`, `level` and `after`
parameters. This follow-up to PR #514 adds `nextSequence`; it does not redefine
`latestSequence`. The API proxies the metadata to the administrative frontend.

| Field | Meaning |
| --- | --- |
| `oldestSequence` | Lowest sequence retained in the captured buffer, or `null` when empty. |
| `latestSequence` | Highest sequence in that snapshot, or `0` in a new empty buffer. |
| `nextSequence` | Safe cursor for the next `after` request using the same level filter. |
| `cursorExpired` | A supplied cursor is below `oldestSequence - 1` or above `latestSequence`. |

The handler copies records, count and boundaries while holding one lock. Filtering,
pagination and JSON serialization happen after releasing it. Logs emitted later
belong to a subsequent snapshot.

Without `after`, the response contains the last `limit` matching records, and
`nextSequence` equals `latestSequence`. This initializes a view of recent logs.

With `after`, only matching records with `sequence > after` are considered, and
the **first** `limit` are returned. When matching records remain beyond the page,
`nextSequence` is the last returned sequence. Otherwise it equals `latestSequence`,
including when no records match. Thus filtered-out records do not cause endless
rescans. For example, `after=0&limit=500` against records 1..600 returns 1..500,
`nextSequence=500`, `latestSequence=600`; the next request returns 501..600.

There is no `hasMore` field. A consumer can poll one page per cycle with
`nextSequence`; it must never use `latestSequence` as a consumed-page cursor.
When `cursorExpired` is true, discard the cursor and reload without `after`.
Do not consume that response as a continuation, even if it contains retained logs.
Changing the level filter also requires a full load.

The API adds `status=available|unavailable`. On unavailable responses its cursor
metadata is `null`; consumers retain an existing cursor and retry later, or retry
a full load when no cursor exists. Operational bot status belongs exclusively to
`/admin/bot/status`, not the logs response.

Coordinate availability of the new producer and proxy contract before the new
frontend. A legacy producer has no safe pagination cursor; the proxy does not
invent one. The updated frontend retries a full load when `nextSequence` is absent.
Overflow still means records were lost from the bounded buffer; a full reload
recovers the current view, not discarded history. A restart is detected by a cursor
above the new head; detecting resets that have already caught up with the old
cursor would require a separate process-generation identifier.
