# Report Stack4Things 21 may 2026

Author : Mohsen Ghalem

This report documents two technical problems that were found during the deployment and study of the Stack4Things IoT middleware platform on a WSL (Windows Subsystem for Linux) environment. Both problems were identified through careful reading of the docker-compose.yml file and by tracing how messages travel through the system at runtime.

The two problems are:

- The WAMP message bus used by the system accepts connections from any process without requiring any credentials. This means an attacker who can reach port 8181 on the host machine has full access to every device procedure and sensor topic in the system **(Postponed until PRODUCTION)**.

- The iotronic-wagent container, which acts as a translator between two message buses, has no healthcheck, starts at the wrong time relative to its dependencies, and silently drops device commands when it cannot deliver them. This directly conflicts with the offline-first resilience requirement of the proposed multi-site federation architecture.

For each problem, this report explains how it was found, what the technical risk is, and what changes were applied to fix it. The solutions described in this report are exactly the changes that were implemented in the accompanying implementation guide.

The key communication chain for sending a command to a device is:

> Operator → iotronic-conductor → RabbitMQ → iotronic-wagent → crossbar → lightning-rod → physical IoT device

Understanding this chain is important because both problems identified in this report occur within it.

---

## 3. Problem 1: WAMP Anonymous Authentication

### 3.1 How the Problem Was Found

While studying the docker-compose.yml file to understand how lightning-rod communicates with iotronic-conductor, the crossbar service configuration was examined. Crossbar does not load a static configuration file. Instead, it writes its own configuration at startup using a shell command inside the container. The relevant section of docker-compose.yml is:

```yaml
command:
  - |
    cat <<EOF > /node/.crossbar/config.json
    {
      "workers": [{
        "type": "router",
        "realms": [{
          "name": "s4t",
          "roles": [{
            "name": "anonymous",
            "permissions": [{
              "uri": "*",
              "allow": {
                "publish": true,
                "subscribe": true,
                "call": true,
                "register": true
              }
            }]
          }]
        }]
      }]
    }
    EOF
```

Two values in this configuration immediately raised a concern.

The first is `"name": "anonymous"`. In the WAMP protocol specification, `anonymous` is a reserved role name that means no credentials are required. Any process that opens a WebSocket connection to port 8181 is automatically placed into the `s4t` realm and given whatever permissions that role has.

The second is `"uri": "*"`. The asterisk is a wildcard that matches every single procedure URI and topic URI in the entire realm. Combined with all four permission flags set to `true`, this means any anonymous connection gets full read and write access to everything.

### 3.2 Why It Is a Security Risk

WAMP is the backbone of the Stack4Things SDIO system. The system's main capability — being able to inject code into remote devices and rewire their behaviour at runtime — is delivered through WAMP RPC calls. The following things happen over WAMP in normal operation:

- lightning-rod registers procedures such as `io.devices.<boardId>.plugin.inject` on the WAMP realm. These procedures allow code to be pushed to a device.
- iotronic-conductor calls those procedures to send commands and code to devices.
- Devices publish sensor readings as events on topics such as `io.events.<boardId>.temperature`.

Because the configuration uses anonymous authentication with wildcard permissions, any process that can reach port 8181 (which Docker exposes to the host by default) can perform any of the following actions without providing any credentials:

| Unauthorized Action | Consequence |
| --- | --- |
| Subscribe to `io.events.*` | Receive live telemetry from every device in the deployment |
| Call `io.devices.*.plugin.inject` | Push arbitrary executable code to any device |
| Register a fake procedure at a legitimate URI | Intercept and redirect calls that were meant for a real device |
| Publish false events to any topic | Feed incorrect sensor data to any monitoring or automation logic |

In a single-site lab environment, this risk is manageable because access to the local network is limited. However, in the multi-site federation model being developed as part of this research (where multiple Stack4Things installations are coordinated by a higher-level Federator), this configuration creates a critical cross-site attack surface. A compromised device at one site could reach into another site's WAMP realm and take over devices there.

### 3.4 The Solution: WAMP-CRA with Role-Based Permissions

WAMP-CRA (Challenge-Response Authentication) replaces the anonymous connection model with a cryptographic handshake. The client must prove that it knows a shared secret before it is accepted into the realm.

The handshake works in four steps:

1. The client sends a `HELLO` message announcing its identity: for example, `authid = "lrod-site-a"`.
2. Crossbar responds with a `CHALLENGE` message containing a random nonce string.
3. The client computes `HMAC-SHA256(nonce, shared_secret)` and sends it back in an `AUTHENTICATE` message.
4. Crossbar performs the same computation independently. If the two results match, the client is authenticated and assigned a named role with specific, limited permissions.

The shared secret never travels over the network in plaintext. An attacker who intercepts the `AUTHENTICATE` message cannot reverse-engineer the secret from the HMAC signature.

The sequence above shows the four-step handshake and the result when a rogue process tries to connect without valid credentials.

> **Note:** The permission changes will be postponed until production.

---

## 4. Problem 2: The Wagent as an Architectural Bottleneck

### 4.1 How the Problem Was Found

To understand the role of iotronic-wagent, the full path of a device command was traced through docker-compose.yml. When an operator issues a command to a device, the message must pass through five separate systems before it reaches the physical device:

- **Step 1:** Operator sends HTTP/CLI command to iotronic-conductor
- **Step 2:** iotronic-conductor publishes an AMQP message to RabbitMQ
- **Step 3:** iotronic-wagent consumes the AMQP message from RabbitMQ
- **Step 4:** iotronic-wagent re-issues the command as a WAMP RPC call to crossbar
- **Step 5:** crossbar routes the WAMP call to lightning-rod on the device

The wagent is the translator in the middle of this chain. It exists because iotronic-conductor speaks AMQP (the OpenStack internal bus protocol used by RabbitMQ) and lightning-rod speaks WAMP (the protocol used by Crossbar). The wagent bridges the two.

After identifying this role, the wagent service definition in docker-compose.yml was compared against every other critical service in the file. Three specific problems were found.

The approach found is to add a **wagent with an outbox queue (store-and-forward)**.

This solution keeps the wagent but makes it resilient. Instead of immediately trying to deliver a command, it first writes every incoming command to a local SQLite database (the outbox). A retry loop then attempts delivery. If delivery fails, the row stays in the outbox and is retried on the next cycle.

A new file `conf_wagent/wagent_outbox.py` is mounted into the container at `/etc/iotronic/wagent_outbox.py`. It provides three functions:

| Function | Purpose |
| --- | --- |
| `init_db()` | Creates the SQLite outbox table at startup |
| `enqueue(uri, args)` | Persists a WAMP call so it survives a restart |
| `drain_outbox(session)` | Retries all pending rows using a live WAMP session |

The DB lives on a named Docker volume (`wagent_outbox`) at `/var/lib/wagent/outbox.db` and survives container restarts.

**The wagent command block was updated to call `init_db()` at every startup:**

| Before | After |
| --- | --- |
| `/bin/bash -c "echo '[INFO] Avvio del Wagent...'; exec /usr/local/bin/iotronic-wamp-agent --config-file /etc/iotronic/iotronic.conf"` | `/bin/bash -c "echo '[INFO] Inizializzazione outbox...'; mkdir -p /var/lib/wagent; python3 -c 'from wagent_outbox import init_db; init_db()'; echo '[INFO] Avvio del Wagent...'; exec /usr/local/bin/iotronic-wamp-agent --config-file /etc/iotronic/iotronic.conf"` |

### Verification

```bash
# DB file exists
docker exec iotronic-wagent ls -lh /var/lib/wagent/
# -rw-r--r-- 1 root root 12K  outbox.db

# Schema is correct
docker exec iotronic-wagent python3 -c "
import sqlite3
con = sqlite3.connect('/var/lib/wagent/outbox.db')
cols = [r[1] for r in con.execute('PRAGMA table_info(outbox)').fetchall()]
print('Columns:', cols)"
# Columns: ['id', 'uri', 'args', 'status', 'created_at', 'sent_at', 'attempts']

# Enqueue a test message
docker exec iotronic-wagent python3 -c "
import sys; sys.path.insert(0, '/etc/iotronic')
from wagent_outbox import enqueue
enqueue('io.iotronic.boards.test-board.test_cmd', ['hello'])"
# [(1, 'io.iotronic.boards.test-board.test_cmd', 'pending', 0)]

# Verify DB survives a restart
docker compose restart iotronic-wagent
# Row still present after restart — volume persisted the SQLite DB
```

---

## Problem: Stale Wampagent Registrations (Conductor Crash Loop)

Every time the stack restarted, iotronic-conductor crashed in a loop with:

```
CRITICAL iotronic-conductor Unhandled error:
  sqlalchemy.exc.MultipleResultsFound:
    Multiple rows were found when exactly one was required
  -> get_registration_wampagent().filter_by(ragent=True, online=True)
```

Each container restart left an orphaned row in the `wampagents` table with `ragent=1`, `online=1`. The conductor expects exactly one such row and aborts when it finds more.

**Before: `wampagents` table after several restarts:**

```
id  hostname      ragent  online
1   3b4b1af388c0  1       1      <- stale
2   abbe3a6e1311  1       1      <- stale
3   1a7dcb5d08d1  1       1      <- current
```

### Fix Applied

Added a cleanup step to the wagent command that runs before `iotronic-wamp-agent` starts, marking all existing registrations offline:

```python
python3 -c 'import pymysql
conn = pymysql.connect(host="iotronic-db", user="iotronic",
                       password="unime", database="iotronic")
conn.cursor().execute("UPDATE wampagents SET online=0 WHERE ragent=1")
conn.commit(); conn.close()
print("[INFO] Stale wampagent registrations cleared.")'
```

**After: `wampagents` table on every startup:**

```
id  hostname         ragent  online
7   iotronic-wagent  1       1      <- always exactly one
```

---

## Problem: Lightning-rod Restart Loop (Hostname Problem)

### Problem

After every `docker compose up` or `--force-recreate`, the wagent container received a new random Docker hostname. Lightning-rod's `settings.json` and the `boards` DB table stored the old hostname. The result was a permanent restart loop:

```
IoTronic Authentication:
  Iotronic Connection RPC error: ApplicationError(
    error=<wamp.error.no_such_procedure>,
    args=['no callee registered for procedure
           <abbe3a6e1311.stack4things.connection>'])
Lightning-rod restarting in few seconds...
```

### Root Cause

The wagent registers WAMP procedures named `<hostname>.stack4things.*`. When its hostname changed on recreation, the procedure name changed but lightning-rod and the DB still referenced the old name.

### Fix Applied

Added `hostname: iotronic-wagent` to the wagent service in docker-compose.yml:

| Before | After |
| --- | --- |
| `iotronic-wagent:` `  container_name: iotronic-wagent` `  platform: linux/amd64` | `iotronic-wagent:` `  container_name: iotronic-wagent` `  hostname: iotronic-wagent  # fixed hostname` `  platform: linux/amd64` |

Then updated `boards` and `wampagents` tables once to the stable name:

```bash
docker exec iotronic-db mariadb -uroot -punime iotronic -e "
  DELETE FROM wampagents WHERE online=0;
  UPDATE wampagents SET hostname='iotronic-wagent' WHERE ragent=1;
  UPDATE boards SET agent='iotronic-wagent';"
```

And lightning-rod's settings file on its volume:

```bash
docker exec lightning-rod sed -i 's/"agent": "old-hostname"/"agent": "iotronic-wagent"/' \
  /etc/iotronic/settings.json
```

**After:** `Lightning-rod modules loaded. Listening...`

No more restart loop. The procedure name `iotronic-wagent.stack4things.connection` is now stable across any number of container recreations.

---

## Problem: Duplicate IoT Services in Keystone (Horizon "Invalid service catalog: iot")

### Problem

Opening any IoTronic panel in Horizon returned:

```
Error: Invalid service catalog: iot
```

The Apache error log confirmed:

```
WARNING horizon.exceptions Recoverable error: Invalid service catalog: iot
```

### Root Cause: Setup Script Not Idempotent

The keystone container runs its setup script on every restart. The script always called `openstack service create iot` unconditionally. Each restart added a new duplicate `iot` service. After several restarts the catalog looked like this:

```
+----------------------------------+----------+----------+
| ID                               | Name     | Type     |
+----------------------------------+----------+----------+
| 0bd6d0bb7f1b4c5c95bdea5df9e0e944 | Iotronic | iot      | <- stale
| 0feef07a088049c793e6c6536b30ecea | Iotronic | iot      | <- stale
| 444b5b6e5a6e47729fd69775660c2c37 | Iotronic | iot      | <- real
+----------------------------------+----------+----------+
```

The Horizon plugin calls `base.url_for(request, 'iot')` to look up the conductor endpoint. When multiple services share the same type, the lookup raises `ServiceCatalogException`.

Even after deleting the duplicates, Horizon kept showing the error because the user's session token was issued when the duplicates existed — the stale catalog was embedded in the token and cached in memcached. A fresh login was needed to get a clean token.

### Fix Applied: Make the Setup Script Idempotent

Wrapped the entire service/endpoint creation block in a check in docker-compose.yml:

| Before | After |
| --- | --- |
| `# Runs on every restart — creates duplicates` `su -s /bin/sh -c 'openstack service create iot --name Iotronic' keystone;` `su -s /bin/sh -c 'openstack endpoint create --region RegionOne iot public ...' keystone;` | `# Only runs on first boot` `if ! openstack service list \| grep -q '^iot$'; then` `  su -s /bin/sh -c 'openstack service create iot --name Iotronic' keystone;` `  su -s /bin/sh -c 'openstack endpoint create ...' keystone;` `else` `  echo 'Iotronic service already exists, skipping.';` `fi` |

### Flush Memcached to Invalidate Stale Tokens

```python
docker exec iotronic-ui python2.7 -c "
import socket
s = socket.socket()
s.connect(('localhost', 11211))
s.send('flush_all\r\n')
print(s.recv(100))
s.close()"
# OK
```

After flushing, log out of Horizon and log back in. The new session token is issued with the clean catalog and the error disappears.

**Verification after restart:**

```bash
docker logs keystone 2>&1 | grep -E 'Iotronic|skip|already'
# [INFO] Iotronic service already exists, skipping creation.

docker exec keystone openstack service list
# +----------------------------------+----------+----------+
# | 444b5b6e5a6e47729fd69775660c2c37 | Iotronic | iot      |
# | 7f7f2882a667418bbd8f1832d736d3ba | keystone | identity |
# +----------------------------------+----------+----------+
```
