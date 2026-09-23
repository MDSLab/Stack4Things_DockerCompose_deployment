# Audit Logger Service: Design and Data Contract for the DLT Integration

Author: Mohsen Ghalem
Date: 10 September 2026
Project: Stack4Things, Industry 5.0 extension work
Audience: supervisor, and the developer building the distributed ledger component
Companion: `docs/Audit_Read_Endpoints_for_DLT_Integration.md`

## 1. What is being asked for

The ledger developer needs four fields for every recorded event:

| Field | Meaning |
|---|---|
| Operator name | Who performed the action |
| Machine name | Which device it was performed on |
| Machine action | What was done |
| Timestamp | When |

This document specifies a logger service that produces exactly those four fields, defines the record format as a stable contract between the two components, and states plainly what the system can and cannot attest to.

## 2. The finding that shapes the design

**Stack4Things cannot currently identify who did anything.** This was verified in the running configuration, not assumed.

The Ditto connection authenticates every write as a single shared subject, `nginx:s4t`, set in `scripts/s4t_ditto_bootstrap.py`. The bridge, the provisioner and any human using the Explorer UI all appear as that same identity. Ditto's own history therefore records what changed but never who changed it.

The IoTronic `boards` table carries `owner` and `project` as Keystone identifiers, but those describe who owns a board, not who acted on it.

And no action log exists anywhere in the schema. There is no table, stream or file that records "someone did something to a machine".

The consequence is that this cannot be delivered by exposing an existing log or by changing a configuration flag. Operator identity has to be captured at the moment an operator acts, and carried from there. That is what the service below does.

## 3. A tension in the requested scope, and how it is resolved

The agreed scope is to log everything, including telemetry. Telemetry has no operator. A temperature reading is produced by a machine, not by a person.

Two ways to handle that, and the choice matters more than it appears.

The tempting option is to fill `operator_name` with something for every record, for example the device identifier or the string `system`, so the field is never empty. This is the wrong choice. It produces a log in which a human name and a machine identifier occupy the same field, and any consumer that later counts operator actions will silently include machine activity. That is the same class of error this project has already been bitten by four times: a field that looks like an answer while meaning something else.

The design therefore sets `operator_name` to `null` for events with no human actor, and adds an explicit `actor.type` of `human`, `device` or `system`. The ledger developer keeps the four flat fields he asked for, and a null is an honest statement that no operator was involved rather than a gap in the data.

If he prefers a non null placeholder, that is a one line change on his side of the contract and it should be his decision, made knowingly.

## 4. Architecture

Two capture points, one output. Both are additive: no existing image is rebuilt and no existing service definition is edited, consistent with the constraint the whole twin layer was built under.

```
   Operator in Horizon
          |
          |  Keystone authenticated HTTP
          v
   [ s4t-audit-logger ]  ---- forwards ---->  iotronic-conductor :8812
          |  (captures operator, machine, action, time)
          |
          |                    Ditto twin events
          |                          ^
          |                          |  AMQP queue ditto.twin.events
          |                          |
          +--------------------------+
          |
          v
   append only store  +  queue s4t.audit  +  REST read API
```

### 4.1 Capture point one: operator actions

The conductor is configured with `auth_strategy=keystone`, so every API call carries an `X-Auth-Token` that identifies the caller. Horizon does not hold a hardcoded conductor URL. It resolves the service through the Keystone catalog, which currently registers:

```
openstack endpoint create --region RegionOne iot public http://iotronic-conductor:8812
```

This is the opening. Repointing the **public** endpoint at the logger makes Horizon call the logger, which records the request and forwards it unchanged to the conductor. The `internal` and `admin` endpoints stay pointed at the conductor directly, so internal services are unaffected.

```bash
openstack endpoint set <public-endpoint-id> --url http://s4t-audit-logger:8812
```

One catalog update. No image rebuild, no change to Horizon, no change to the conductor.

For each request the logger extracts the token, resolves it to a username and project by validating it against Keystone, caches that mapping for the token's lifetime, derives the machine and the action from the request path and method, forwards the request, and records the outcome including the response status. An action that failed is as interesting to an auditor as one that succeeded, so failures are recorded too.

### 4.2 Capture point two: device and system events

The queue `ditto.twin.events` already exists and already carries every twin modification. It was created during Stage B and currently has no consumer. The logger binds to it and records each event with `actor.type` of `device`.

This is where telemetry enters the log, and where twin changes made by the provisioner are recorded as `system`.

### 4.3 Output

Three interfaces, so the ledger developer can choose:

- **A queue**, `s4t.audit`, that he consumes as an ordinary AMQP consumer. Recommended, because it is push based and needs no polling.
- **A REST endpoint**, `GET /audit/events?from=<seq>&to=<seq>`, for backfill and for re-reading a range during verification.
- **An append only file** in JSON Lines format, one record per line, for offline inspection and for the case where he wants to process a day in bulk.

## 5. The record format

This is the contract. The four requested fields are top level with exactly the requested meaning. Everything else is additional context that can be ignored without breaking anything.

```json
{
  "event_id": "8f14e45f-ceea-467a-9c1e-9e0f2b3a1d77",
  "sequence": 10432,

  "timestamp": "2026-09-10T12:34:56.789Z",
  "operator_name": "admin",
  "machine_name": "Camera_01",
  "machine_action": "board.reboot",

  "actor": {
    "type": "human",
    "id": "349874cdca424d1aa0efdaaaf3e7caf1",
    "name": "admin",
    "project": "3397fed027024003b4084815a6fb84cc"
  },
  "machine": {
    "id": "65f0bbf4-37cb-47d3-84cb-1d7902e7c1ad",
    "name": "Camera_01",
    "fleet": "unime-lab"
  },
  "action": {
    "verb": "reboot",
    "target": "board",
    "detail": { "delay": 0 },
    "outcome": "success",
    "status_code": 200
  },

  "source": "horizon",
  "correlation_id": "83aceec8-79c4-4291-a0a8-917562a169f0",
  "prev_hash": "sha256:9f2c...",
  "hash": "sha256:41ab..."
}
```

A telemetry record from the same log:

```json
{
  "event_id": "b2c9...",
  "sequence": 10433,
  "timestamp": "2026-09-10T12:34:58.113Z",
  "operator_name": null,
  "machine_name": "Camera_01",
  "machine_action": "telemetry.report",
  "actor": { "type": "device", "id": "65f0bbf4-37cb-47d3-84cb-1d7902e7c1ad", "name": "Camera_01" },
  "machine": { "id": "65f0bbf4-37cb-47d3-84cb-1d7902e7c1ad", "name": "Camera_01", "fleet": "unime-lab" },
  "action": { "verb": "report", "target": "features/telemetry/properties",
              "detail": { "temperature": 22.4, "fan_on": false }, "outcome": "success" },
  "source": "ditto",
  "twin_revision": 512,
  "prev_hash": "sha256:41ab...",
  "hash": "sha256:7d30..."
}
```

Field rules worth stating precisely, because a contract is only useful if the edge cases are defined:

- `timestamp` is RFC 3339 in UTC with milliseconds, always. It is the time the event occurred, not the time it was logged.
- `operator_name` is a Keystone username, or `null` when `actor.type` is not `human`.
- `machine_name` is the human readable board name. `machine.id` carries the UUID, which is the stable key. Names can be changed by an operator; the UUID cannot.
- `machine_action` is a dotted verb from a closed vocabulary, so the ledger side can filter without parsing free text.
- `sequence` is monotonic and gapless within one logger instance. A gap means records were lost, which is exactly what an auditor needs to be able to detect.
- `outcome` is `success` or `failure`. Failed operator actions are recorded.

### 5.1 Tamper evidence

Each record carries the hash of the previous record and its own hash over the canonical serialisation. The log is therefore a hash chain: altering or removing any record breaks every hash after it.

This matters for the ledger design. It means the ledger does not need one transaction per event. Anchoring a periodic root is enough, because the chain proves the integrity of everything between two anchors. That keeps on chain cost fixed regardless of telemetry volume, which is the point raised in section 7.

## 6. Action vocabulary

A first cut, to be agreed with the ledger developer rather than imposed.

| `machine_action` | Actor | Source |
|---|---|---|
| `board.create` | human | horizon |
| `board.delete` | human | horizon |
| `board.update` | human | horizon |
| `board.reboot` | human | horizon |
| `plugin.inject` | human | horizon |
| `plugin.execute` | human | horizon |
| `service.expose` | human | horizon |
| `desired.set` | human | horizon or ditto |
| `telemetry.report` | device | ditto |
| `twin.create` | system | provisioner |
| `twin.update` | system | provisioner |
| `twin.orphan` | system | provisioner |

The human rows are the ones the ledger developer's four fields were designed for. The others exist because the agreed scope is everything.

## 7. Volume, and what should actually go on the ledger

Telemetry dominates the record count. The rate is roughly `boards divided by reporting interval`. Two boards reporting every two seconds produce about 86,000 records per day. Operator actions, by contrast, are perhaps tens per day.

Writing that to a ledger record by record would be expensive and pointless. The recommendation is that the ledger anchors a Merkle root or a chain head periodically, for example hourly, together with the sequence range it covers. Verification then works by replaying that range from the logger and recomputing, which the hash chain makes cheap.

The logger stores every record. The ledger records that the log has not been altered. Those are different jobs and should stay separate.

## 8. What this can and cannot attest to

Worth writing into any publication rather than leaving implied.

**Operator actions are strongly attributed.** They come from a Keystone authenticated request, and the username is resolved by validating the token with Keystone rather than trusting a client supplied header. This is a real attribution claim.

**Device attributed records are only as trustworthy as the bus underneath.** Crossbar currently accepts anonymous WAMP connections with wildcard permissions, so any process able to reach it can publish telemetry claiming to be any board. A `telemetry.report` record faithfully states what the platform was told. It does not establish that the named machine produced it. Fixing that requires per device authentication on Crossbar, which is open and deliberate.

**The log can have gaps that the log itself will show.** The `sequence` field makes loss detectable, which is the honest alternative to claiming loss is impossible.

**One known upstream gap.** Ditto acknowledges messages whose payload conversion failed and discards them, so telemetry that fails conversion never becomes a twin event and never reaches the logger through capture point two. Adding a dead letter exchange on `ditto.inbound` closes this, and it is on the Stage E list. Until then the log is complete with respect to what Ditto accepted, not with respect to what devices sent.

## 9. Implementation plan

1. Define the record schema and the action vocabulary, and agree both with the ledger developer before writing code. The schema is the interface; changing it later is expensive for both sides.
2. Build the logger as a container, `s4t-audit-logger`, in the existing overlay. Two inputs, three outputs, append only storage.
3. Wire capture point two first. It needs no changes to anything, since `ditto.twin.events` is already published and unconsumed. This proves the pipeline end to end and gives the ledger developer real records to work against within days.
4. Wire capture point one by repointing the public catalog endpoint. Verify that Horizon still functions unchanged and that every operator action appears in the log with the correct username.
5. Verify by delivery, not by status. Perform a known action in Horizon, then confirm that exact record arrives on `s4t.audit`. This is the same discipline used for the twin layer, and for the same reason.

## 10. Open questions for the ledger developer

1. Null or a placeholder for `operator_name` on non human events. Section 3 recommends null, and the decision is his.
2. Which of the three delivery interfaces he wants, and whether he needs replay from an arbitrary sequence.
3. The anchoring interval, and whether he wants a Merkle root or a simple chain head.
4. Whether machine readable action codes need to be stable across future versions, which would make the vocabulary in section 6 a versioned contract rather than a convention.

## References

- `docs/Audit_Read_Endpoints_for_DLT_Integration.md`, the inventory of readable surfaces
- `docs/Digital_Twin_Layer_Complete_Report.md`, sections 17 and 23
- `scripts/s4t_ditto_bootstrap.py`, where the shared Ditto subject and the events target are defined
- `conf_conductor/iotronic.conf`, where Keystone authentication is enabled
