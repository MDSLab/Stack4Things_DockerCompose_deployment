---
output:
  word_document: default
  html_document: default
---
# Audit Log API Reference

Author: Mohsen Ghalem
Date: 10 September 2026
Audience: the developer building the distributed ledger component
Base URL: `http://<host>:8890`
Authentication: `Authorization: Bearer <AUDIT_API_TOKEN>` on every route except `/health`

Everything here is read only. No endpoint can create, modify or delete a record, and `POST`, `PUT` and `DELETE` return 405 on every path.

## Summary

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness. No token required. |
| GET | `/audit/events` | The main query. History, filtering and paging. |
| GET | `/audit/events/{event_id}` | One record, returned as the exact stored bytes. |
| GET | `/audit/stats` | Counts, sequence range, time range. |
| GET | `/audit/stream` | Server Sent Events. Live, with optional replay. |
| GET | `/audit/ws` | WebSocket. Live, with optional replay. |

There is also a message queue, `s4t.audit` on RabbitMQ, carrying the identical bytes. Use it when you want the broker to hold a backlog while your service is down.

## The record

```json
{
  "schema_version": "1.0",
  "sequence": 10432,
  "event_id": "8f14e45f-ceea-467a-9c1e-9e0f2b3a1d77",
  "timestamp": "2026-09-10T12:34:56.789Z",
  "operator_name": "admin",
  "machine_name": "Camera_01",
  "machine_action": "board.delete",
  "actor": { "type": "human", "name": "admin", "project": "3397fed0..." },
  "machine": { "id": "65f0bbf4-...", "name": "Camera_01", "namespace": null },
  "action": { "verb": "delete", "target": "/v1/boards/65f0bbf4-...",
              "detail": { "method": "DELETE" },
              "outcome": "success", "status_code": 204 },
  "source": "horizon",
  "twin_revision": null,
  "correlation_id": "83aceec8-..."
}
```

The four fields you asked for are top level: `timestamp`, `operator_name`, `machine_name`, `machine_action`. Everything else is context you can ignore.

**`operator_name` is `null` when no human was involved**, which is the case for all telemetry. Check `actor.type` to distinguish `human`, `device`, `system` and `unknown`. This was deliberate: filling the field with a device identifier would make human and machine activity indistinguishable to anyone later counting operator actions.

**`sequence` is gapless and monotonic.** It is your cursor for incremental reading, and a gap means records were lost.

## GET /audit/events

The main query. All parameters are optional and combine with AND.

| Parameter | Type | Meaning |
|---|---|---|
| `from_seq` | integer | Records with `sequence` strictly greater than this. The cursor. |
| `to_seq` | integer | Upper bound, inclusive |
| `from` | RFC 3339 | `timestamp` greater than or equal |
| `to` | RFC 3339 | `timestamp` less than or equal |
| `machine_id` | string | Exact match on the board UUID |
| `machine_name` | string | Exact match on the readable name |
| `machine_action` | string | Exact match, for example `telemetry.report` |
| `actor_type` | string | `human`, `device`, `system` or `unknown` |
| `operator_name` | string | Exact match on the Keystone username |
| `limit` | integer | Page size. Default 1000, silently capped at 10000. |

Response:

```json
{
  "count": 1000,
  "next_seq": 11432,
  "has_more": true,
  "items": [ "...records..." ]
}
```

Ordering is always ascending by `sequence` and cannot be changed. A query API that can reorder is one whose results cannot be compared between two calls.

```bash
TOKEN=... ; API=http://localhost:8890

# everything a machine did today
curl -s -H "Authorization: Bearer $TOKEN" \
  "$API/audit/events?machine_name=Camera_01&from=2026-09-10T00:00:00Z"

# only human actions
curl -s -H "Authorization: Bearer $TOKEN" \
  "$API/audit/events?actor_type=human&limit=100"

# what one operator did
curl -s -H "Authorization: Bearer $TOKEN" \
  "$API/audit/events?operator_name=admin"
```

### Reading everything, exactly once

Start at zero and follow `next_seq` until `has_more` is false. That is the whole protocol.

```python
import requests

API, TOKEN = "http://localhost:8890", "..."
H = {"Authorization": f"Bearer {TOKEN}"}
cursor = 0                                  # persist this between runs

while True:
    r = requests.get(f"{API}/audit/events",
                     params={"from_seq": cursor, "limit": 1000},
                     headers=H).json()
    for rec in r["items"]:
        handle(rec)
    cursor = r["next_seq"]                   # save it
    if not r["has_more"]:
        break
```

Storing `cursor` between runs gives you resumable incremental ingestion with no duplicates and no gaps. There are no opaque cursors, so you can also resume by hand.

## GET /audit/events/{event_id}

Returns one record as the **exact bytes stored**, not re-serialised.

This matters for you specifically. You hash these records. The bytes returned here are identical to the bytes on the `s4t.audit` queue and identical to what the streaming endpoints send, so a hash computed from any transport matches a hash computed from any other. Rebuilding the JSON yourself from the `items` array of a list query may reorder keys and produce a different hash, so for hashing use this endpoint or the raw queue or stream bytes.

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "$API/audit/events/8f14e45f-ceea-467a-9c1e-9e0f2b3a1d77"
```

Returns 404 if the identifier is unknown.

## GET /audit/stats

```json
{
  "count": 26,
  "min_sequence": 1,
  "max_sequence": 26,
  "first_timestamp": "2026-09-10T01:37:32.023Z",
  "last_timestamp": "2026-09-10T01:37:32.081Z",
  "by_actor_type": { "device": 25, "human": 1 },
  "schema_version": "1.0"
}
```

Useful for sizing an anchoring interval and for checking that your cursor is not falling behind: compare your stored cursor against `max_sequence`.

## GET /audit/stream, Server Sent Events

Standard SSE. Each event carries the sequence as the SSE `id` and the record as `data`.

```
id: 10432
data: {"action":{...},"actor":{...},...}

: keep-alive
```

A comment line is sent every twenty seconds so that proxies do not close an idle connection.

```bash
curl -N -H "Authorization: Bearer $TOKEN" "$API/audit/stream"

# catch up from a known point, then stay live
curl -N -H "Authorization: Bearer $TOKEN" "$API/audit/stream?from_seq=10000"
```

With `from_seq` the endpoint replays everything after that sequence first and then continues live, with no gap between the two phases. That is the pattern to use after a restart.

## GET /audit/ws, WebSocket

Same content, same bytes, for a long running consumer.

```javascript
const ws = new WebSocket("ws://localhost:8890/audit/ws?from_seq=10000", [], {
  headers: { Authorization: "Bearer " + TOKEN }
});
ws.onmessage = (m) => handle(JSON.parse(m.data));
```

```python
import aiohttp, asyncio, json

async def main():
    H = {"Authorization": f"Bearer {TOKEN}"}
    async with aiohttp.ClientSession(headers=H) as s:
        async with s.ws_connect(f"{API}/audit/ws?from_seq=0") as ws:
            async for msg in ws:
                handle(json.loads(msg.data))

asyncio.run(main())
```

Sending the text `ping` returns `{"pong":true}`. The server also sends protocol level heartbeats every twenty seconds.

A slow consumer is disconnected rather than allowed to stall the writer. Reconnect with your last `sequence` as `from_seq` and you lose nothing.

## Which transport to use

| Situation | Use |
|---|---|
| Backfill, or replaying a range to verify an anchor | `GET /audit/events` |
| Continuous ingestion where downtime must not lose data | the `s4t.audit` queue |
| A live view in a browser or a simple script | `/audit/stream` |
| A long running service that wants a push feed | `/audit/ws` |
| Hashing a single record | `/audit/events/{event_id}`, or the raw queue or stream bytes |

## Action vocabulary

A closed set of nineteen values, so you can filter without parsing free text.

Seven come from the twin layer and describe machine and system activity:

| `machine_action` | `actor.type` | Meaning |
|---|---|---|
| `telemetry.report` | device | A device reported measured state |
| `desired.set` | unknown | Somebody set a desired value. A twin event carries no identity, so `unknown` is the honest answer |
| `prediction.write` | system | A prediction was written to the twin |
| `twin.create` | system | A twin was created, normally by the provisioner |
| `twin.update` | system | Twin attributes changed |
| `twin.modify` | system | Any other twin change |
| `twin.delete` | system | A twin was removed |

Twelve come from operator requests through the proxy. All have `actor.type: human` and `source: horizon`:

| `machine_action` | Triggered by |
|---|---|
| `board.create` | `POST /v1/boards` |
| `board.read` | `GET` on a board |
| `board.update` | `PATCH` or `PUT` on a board |
| `board.delete` | `DELETE` on a board |
| `board.action` | any path containing `/action` under a board |
| `plugin.inject` | `POST` under `/plugins` |
| `plugin.execute` | `PUT` under `/plugins`, or any `/plugins/.../action` |
| `plugin.remove` | `DELETE` under `/plugins` |
| `plugin.read` | `GET` under `/plugins` |
| `service.expose` | `POST` or `PUT` under `/services` |
| `service.remove` | `DELETE` under `/services` |
| `service.read` | `GET` under `/services` |

Plus `api.<method>`, the fallback for a request the vocabulary does not recognise. Unrecognised actions still produce a record, because an action nobody anticipated is exactly what an auditor wants to see.

Two notes that matter when you filter.

**Nesting resolves to the inner noun.** A plugin and a service both live under a board, so `/v1/boards/<id>/plugins/<id>` contains both markers. The most specific one wins, so that path is a `plugin.*` action, not a `board.*` one. `machine_id` still carries the board identifier, because it answers "which machine was this done to". The plugin identifier stays in `action.target`.

**The three read actions are not recorded by default.** `board.read`, `plugin.read` and `service.read` exist in the vocabulary but the proxy skips `GET` unless `AUDIT_RECORD_READS=1`, because Horizon generates thousands of page loads that would bury the real actions. If you need reads, ask for that flag to be turned on and expect the volume to rise sharply.

This table is checked against the code by `scripts/test_audit_vocabulary.py`, which fails if a verb exists in one and not the other.

## Errors

| Status | Meaning |
|---|---|
| 401 | Missing or wrong bearer token |
| 404 | Unknown `event_id` |
| 405 | A write verb was attempted. The API is read only. |

## Not an API, but relevant

The audit proxy sits in front of the device management API on port 8812 and forwards every request unchanged. It adds no endpoints and changes no responses. It exists only to observe who is acting, and it is transparent to Horizon.

## What the log can and cannot attest to

**Operator actions are strongly attributed.** The username comes from a token validated with Keystone, not from a client supplied header.

**Device attributed records are only as trustworthy as the message bus.** The router currently accepts anonymous connections, so a `telemetry.report` record states what the platform was told, not that the named machine produced it.

**Loss is visible.** A gap in `sequence` means records were lost. There is no claim that loss is impossible.

**One upstream gap.** Ditto acknowledges messages whose conversion failed and discards them, so telemetry that fails conversion never reaches the log. The log is complete with respect to what Ditto accepted.
