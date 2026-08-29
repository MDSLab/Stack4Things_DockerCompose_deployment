#!/usr/bin/env python3
"""
s4t_ditto_bootstrap.py

One shot container. Makes the twin layer usable, then exits.

Everything downstream declares

    depends_on:
      s4t-ditto-bootstrap:
        condition: service_completed_successfully

so the bridge, provisioner and predictor cannot start against a half
configured Ditto.

WHAT IT DOES, in order
----------------------
  1. wait until the Ditto gateway answers, bounded, and say so loudly if it
     never does
  2. declare the AMQP topology on RabbitMQ
  3. create or update the access policy
  4. create or update the AMQP connection, including the payload mapping
  5. VERIFY all of it by reading it back
  6. exit 0 only if step 5 passed

WHY IT IS SHAPED LIKE THIS
--------------------------
This deployment already contains a cautionary example. The Keystone bootstrap
in docker-compose.yml starts Apache in the background, immediately calls the
API it serves, does not check any result, prints "services created" and marks
itself healthy with an empty catalog. Horizon then fails with a cause several
steps removed from the symptom.

So this script does the opposite on purpose: wait for readiness rather than
assume it, check the end state rather than the individual calls, and fail
with a non zero exit rather than reporting a success it did not achieve.

IDEMPOTENCY
-----------
Every write is a create-or-replace. Running it twice changes nothing, which
matters because it runs on every `docker compose up`.

USAGE
-----
    ./s4t_ditto_bootstrap.py            # normal
    ./s4t_ditto_bootstrap.py --dry-run  # print, send nothing
    ./s4t_ditto_bootstrap.py --verify-only
"""

import argparse
import base64
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

LOG = logging.getLogger("bootstrap")


# ---------------------------------------------------------------- helpers
def env(name, default=None, required=False):
    v = os.environ.get(name, default)
    if required and not v:
        raise SystemExit(f"missing required environment variable {name}")
    return v


class Http:
    def __init__(self, base, dry_run=False):
        self.base = base.rstrip("/")
        self.dry_run = dry_run

    def call(self, method, path, body=None, user=None, password=None, mutating=True):
        url = f"{self.base}{path}"
        if self.dry_run and mutating:
            LOG.info("DRY RUN %s %s", method, path)
            return 200, {}
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if user is not None:
            tok = base64.b64encode(f"{user}:{password}".encode()).decode()
            req.add_header("Authorization", f"Basic {tok}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, (json.loads(raw) if raw else {})
            except ValueError:
                return e.code, {"raw": raw.decode(errors="replace")[:300]}
        except urllib.error.URLError as e:
            return 0, {"error": str(e.reason)}
        except (TimeoutError, OSError) as e:
            # A socket timeout raises TimeoutError, which is NOT a subclass of
            # URLError, so without this the whole bootstrap dies with a
            # traceback instead of reporting a failure the caller can retry.
            return 0, {"error": f"timeout or socket error: {e}"}


# ------------------------------------------------------------ 1. readiness
def wait_for_ditto(http, devops_user, devops_pass, attempts, interval):
    """Bounded wait for the parts we actually use.

    Waiting for the gateway alone is not enough and was a real bug: the
    gateway answers /status as soon as it is up, but every connection call is
    routed to the CONNECTIVITY shard. If connectivity is not in the cluster
    yet, GET /api/2/connections simply hangs until the socket times out.

    So this waits for both, and the second check is the one that matters.
    """
    LOG.info("waiting for Ditto (gateway, then connectivity), up to %ds",
             int(attempts * interval))
    gateway_ok = False
    for i in range(1, attempts + 1):
        if not gateway_ok:
            code, _ = http.call("GET", "/status", user=devops_user,
                                password=devops_pass, mutating=False)
            if code == 200:
                LOG.info("gateway answered after %d attempt(s)", i)
                gateway_ok = True
            else:
                if i % 6 == 0:
                    LOG.info("  waiting for gateway, last status %s (%d/%d)",
                             code, i, attempts)
                time.sleep(interval)
                continue

        # routed to the connectivity service, so a 200 proves it is reachable
        code, _ = http.call("GET", "/api/2/connections", user=devops_user,
                            password=devops_pass, mutating=False)
        if code == 200:
            LOG.info("connectivity answered after %d attempt(s)", i)
            return True
        if i % 6 == 0:
            LOG.info("  waiting for connectivity, last status %s (%d/%d)",
                     code, i, attempts)
        time.sleep(interval)

    LOG.error("Ditto did not become ready after %d attempts. "
              "Is ditto-connectivity running? Nothing was configured.", attempts)
    return False


# --------------------------------------------------------------- 2. AMQP
def declare_amqp(opts):
    """Ditto consumes from a queue that must already exist, and publishes to an
    exchange that must already exist. Ditto declares neither."""
    if opts.dry_run:
        LOG.info("DRY RUN would declare queue %s; exchange %s -> %s; "
                 "exchange %s -> %s",
                 opts.inbound_queue, opts.exchange, opts.commands_queue,
                 opts.events_exchange, opts.events_queue)
        return True
    try:
        import pika
    except ImportError:
        LOG.error("pika is required: pip install pika")
        return False

    # virtual_host must match the connection URI, otherwise the queues are
    # declared in one vhost and Ditto looks for them in another.
    params = pika.ConnectionParameters(
        host=opts.amqp_host, port=opts.amqp_port, virtual_host=opts.amqp_vhost,
        credentials=pika.PlainCredentials(opts.amqp_user, opts.amqp_pass),
        connection_attempts=opts.amqp_attempts, retry_delay=opts.amqp_retry,
        socket_timeout=10)
    try:
        conn = pika.BlockingConnection(params)
    except Exception as exc:
        LOG.error("cannot reach RabbitMQ at %s:%s: %s",
                  opts.amqp_host, opts.amqp_port, exc)
        return False
    try:
        ch = conn.channel()
        # inbound: telemetry and prediction write backs INTO Ditto.
        # Published to the default exchange with the queue name as routing key,
        # so no exchange of our own is needed for this direction.
        ch.queue_declare(queue=opts.inbound_queue, durable=True)

        # outbound: Ditto targets publish to "<exchange>/<routing key>", so both
        # the exchange and a bound queue have to exist for anything to be kept.
        ch.exchange_declare(exchange=opts.exchange, exchange_type="direct",
                            durable=True)
        ch.queue_declare(queue=opts.commands_queue, durable=True)
        ch.queue_bind(queue=opts.commands_queue, exchange=opts.exchange,
                      routing_key=opts.commands_key)
        # A SECOND exchange, not a second routing key on the first one.
        # Ditto's RabbitMQPublisherActor.declareExchangesPassive collects the
        # connection's targets into a Map keyed by EXCHANGE NAME using
        # Collectors.toMap, which throws IllegalStateException("Duplicate key")
        # if two targets share an exchange. The supervisor then restarts the
        # channel actor, which re-declares, which throws again: an unbounded
        # crash loop that pins a CPU. One exchange per target is the only
        # arrangement Ditto's RabbitMQ publisher accepts.
        ch.exchange_declare(exchange=opts.events_exchange,
                            exchange_type="direct", durable=True)
        ch.queue_declare(queue=opts.events_queue, durable=True)
        ch.queue_bind(queue=opts.events_queue, exchange=opts.events_exchange,
                      routing_key=opts.events_key)
        LOG.info("AMQP topology declared in vhost <%s>: queue %s; "
                 "exchange %s -> %s; exchange %s -> %s",
                 opts.amqp_vhost, opts.inbound_queue,
                 opts.exchange, opts.commands_queue,
                 opts.events_exchange, opts.events_queue)
        return True
    except Exception as exc:
        LOG.error("failed declaring AMQP topology: %s", exc)
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ------------------------------------------------------------- 3. policy
def policy_body(subjects):
    return {
        "entries": {
            "owner": {
                "subjects": {s: {"type": "s4t twin layer"} for s in subjects},
                "resources": {
                    "thing:/":   {"grant": ["READ", "WRITE"], "revoke": []},
                    "policy:/":  {"grant": ["READ", "WRITE"], "revoke": []},
                    "message:/": {"grant": ["READ", "WRITE"], "revoke": []},
                },
            }
        }
    }


def put_policy(http, opts):
    path = f"/api/2/policies/{urllib.parse.quote(opts.policy_id)}"
    code, body = http.call("PUT", path, policy_body(opts.subjects),
                           user=opts.api_user, password=opts.api_pass)
    if code in (200, 201, 204):
        LOG.info("policy %s %s", opts.policy_id,
                 "created" if code == 201 else "already current")
        return True
    LOG.error("could not write policy %s (%s): %s", opts.policy_id, code,
              json.dumps(body)[:300])
    return False


# --------------------------------------------------------- 4. connection
INCOMING_JS = """function mapToDittoProtocolMsg(headers, textPayload, bytePayload, contentType) {
    var p = JSON.parse(textPayload);
    if (!p.board_uuid || !p.state) { return null; }
    return Ditto.buildDittoProtocolMsg(
        'NAMESPACE', p.board_uuid, 'things', 'twin', 'commands', 'merge',
        '/features/telemetry/properties',
        // content-type is MANDATORY for a merge. Ditto validates it against
        // RFC 7396 and rejects the command with 415 mediatype.unsupported
        // before it ever reaches the Things service. Without it the source
        // consumes happily, acknowledges the AMQP message, and the twin never
        // changes: a silent total failure of the inbound path.
        { 'response-required': false,
          'content-type': 'application/merge-patch+json' },
        p.state
    );
}
"""


def connection_body(opts):
    return {
        "id": opts.connection_id,
        "name": "Stack4Things over RabbitMQ",
        "connectionType": "amqp-091",
        "connectionStatus": "open",
        "failoverEnabled": True,
        # The path after the port is the VHOST, URL encoded. A bare trailing
        # slash means the EMPTY vhost, not the default one, and RabbitMQ
        # answers 530 NOT_ALLOWED - vhost  not found (note the double space,
        # that gap is the empty name). The default vhost "/" must be "%2F".
        # pika hides this by defaulting virtual_host to "/" when unset, which
        # is why this script can reach RabbitMQ while Ditto cannot.
        "uri": (f"amqp://{urllib.parse.quote(opts.amqp_user, safe='')}"
                f":{urllib.parse.quote(opts.amqp_pass, safe='')}"
                f"@{opts.amqp_host}:{opts.amqp_port}"
                f"/{urllib.parse.quote(opts.amqp_vhost, safe='')}"),
        "sources": [{
            "addresses": [opts.inbound_queue],
            "consumerCount": 1,
            "authorizationContext": [opts.subject],
            "payloadMapping": ["s4t-telemetry"],
            "replyTarget": {"enabled": False},
        }],
        "targets": [
            {
                # desired state changes out to devices. The bridge filters on the
                # changed path, because an RQL filter matches the state of the
                # thing and not which path changed.
                "address": f"{opts.exchange}/{opts.commands_key}",
                "topics": [
                    "_/_/things/twin/events"
                    "?filter=exists(features/telemetry/desiredProperties)"
                    "&extraFields=features/telemetry/properties"
                ],
                "authorizationContext": [opts.subject],
            },
            {
                # change events for the predictor. extraFields carries the
                # pipeline descriptor in band, so the predictor never has to
                # call back to Ditto for its own configuration.
                # separate exchange, see declare_amqp(): two targets on one
                # exchange crash the Ditto publisher on every channel open
                "address": f"{opts.events_exchange}/{opts.events_key}",
                "topics": [
                    "_/_/things/twin/events"
                    "?extraFields=attributes/pipeline,features/telemetry/properties"
                ],
                "authorizationContext": [opts.subject],
            },
        ],
        "mappingDefinitions": {
            "s4t-telemetry": {
                "mappingEngine": "JavaScript",
                "options": {
                    "incomingScript": INCOMING_JS.replace("NAMESPACE", opts.namespace)
                },
            }
        },
    }


def put_connection(http, opts):
    """Create or replace the connection, keeping a STABLE id.

    The two routes are not interchangeable and this cost an afternoon:

      POST /api/2/connections          creates, but Ditto GENERATES the id and
                                       rejects an explicit one with
                                       connectivity:id.notsettable
      PUT  /api/2/connections/{id}     modifies only. The docs are explicit:
                                       "The connection must already exist."
      piggyback createConnection       accepts a full connection object
                                       INCLUDING an explicit id

    A generated UUID would work, but then every runbook, the Explorer UI and
    the bridge and predictor in later stages would refer to an id that differs
    per deployment. So: piggyback to create with the id we choose, and the
    recommended HTTP API to modify afterwards. If Ditto ever drops piggyback,
    only this function changes.
    """
    path = f"/api/2/connections/{urllib.parse.quote(opts.connection_id)}"
    code, _ = http.call("GET", path, user=opts.devops_user,
                        password=opts.devops_pass, mutating=False)
    body = connection_body(opts)

    if code == 200:
        code, resp = http.call("PUT", path, body, user=opts.devops_user,
                               password=opts.devops_pass)
        if code in (200, 204):
            LOG.info("connection %s updated", opts.connection_id)
            return True
        LOG.error("could not modify connection %s (%s): %s", opts.connection_id,
                  code, json.dumps(resp)[:400])
        return False

    piggyback = {
        "targetActorSelection": "/system/sharding/connection",
        "headers": {"aggregate": False, "is-group-topic": False},
        "piggybackCommand": {
            "type": "connectivity.commands:createConnection",
            "connection": body,
        },
    }
    code, resp = http.call("POST", "/devops/piggyback/connectivity", piggyback,
                           user=opts.devops_user, password=opts.devops_pass)
    if code in (200, 201, 204):
        LOG.info("connection %s created with a fixed id via piggyback",
                 opts.connection_id)
        return True
    LOG.error("could not create connection %s (%s): %s", opts.connection_id, code,
              json.dumps(resp)[:400])
    return False


# ---------------------------------------------------------- 5. verify
def verify(http, opts):
    """Check the END STATE. Individual calls returning 2xx is not the same
    thing as the system being configured, which is the exact failure mode of
    the Keystone bootstrap in this repository."""
    ok = True

    code, _ = http.call("GET", f"/api/2/policies/{urllib.parse.quote(opts.policy_id)}",
                        user=opts.api_user, password=opts.api_pass, mutating=False)
    if code == 200:
        LOG.info("verified: policy %s exists", opts.policy_id)
    else:
        LOG.error("VERIFY FAILED: policy %s not readable (%s)", opts.policy_id, code)
        ok = False

    path = f"/api/2/connections/{urllib.parse.quote(opts.connection_id)}"
    code, conn = http.call("GET", path, user=opts.devops_user,
                           password=opts.devops_pass, mutating=False)
    if code != 200:
        LOG.error("VERIFY FAILED: connection %s not readable (%s)",
                  opts.connection_id, code)
        return False
    LOG.info("verified: connection %s exists, status <%s>, %d source(s), %d target(s)",
             opts.connection_id, conn.get("connectionStatus"),
             len(conn.get("sources") or []), len(conn.get("targets") or []))
    if conn.get("connectionStatus") != "open":
        LOG.error("VERIFY FAILED: connection is not open")
        ok = False

    # live status, which is where a bad credential or a missing queue shows up
    code, st = http.call("GET", path + "/status", user=opts.devops_user,
                         password=opts.devops_pass, mutating=False)
    if code == 200:
        live = st.get("liveStatus") or st.get("connectionStatus")
        LOG.info("verified: connection liveStatus <%s>", live)
        if live not in (None, "open"):
            LOG.error("VERIFY FAILED: liveStatus is <%s>, not open.", live)
            for c in (st.get("clientStatus") or []):
                LOG.error("  client %s: %s", c.get("status"),
                          c.get("statusDetails", "no detail"))
            code, lg = http.call("GET", path + "/logs", user=opts.devops_user,
                                 password=opts.devops_pass, mutating=False)
            if code == 200:
                for e in (lg.get("connectionLogs") or [])[-4:]:
                    if e.get("level") == "failure":
                        LOG.error("  log: %s", (e.get("message") or "")[:220])
            ok = False
    return ok


# ---------------------------------------------------------------- main
def build_parser():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verify-only", action="store_true")
    p.add_argument("--skip-amqp", action="store_true",
                   help="Do not declare the AMQP topology")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def load_opts(args):
    class O: pass
    o = O()
    o.dry_run = args.dry_run
    o.ditto_url = env("DITTO_URL", "http://ditto-nginx:80")
    o.api_user = env("DITTO_API_USER", "s4t")
    o.api_pass = env("DITTO_API_PASSWORD", required=not args.dry_run)
    o.devops_user = env("DITTO_DEVOPS_USER", "devops")
    o.devops_pass = env("DITTO_DEVOPS_PASSWORD", required=not args.dry_run)
    o.namespace = env("DITTO_NAMESPACE", "s4t")
    o.policy_id = env("DITTO_POLICY", "s4t:fleet-unime-lab")
    o.subject = f"nginx:{o.api_user}"
    o.subjects = [s.strip() for s in
                  env("DITTO_POLICY_SUBJECTS", o.subject).split(",") if s.strip()]
    o.connection_id = env("DITTO_CONNECTION_ID", "s4t-rabbitmq")

    o.amqp_host = env("S4T_AMQP_HOST", "rabbitmq")
    o.amqp_port = int(env("S4T_AMQP_PORT", "5672"))
    o.amqp_user = env("S4T_AMQP_USER", "openstack")
    o.amqp_pass = env("S4T_AMQP_PASS", "unime")
    o.amqp_vhost = env("S4T_AMQP_VHOST", "/")
    o.amqp_attempts = int(env("S4T_AMQP_ATTEMPTS", "30"))
    o.amqp_retry = int(env("S4T_AMQP_RETRY", "5"))

    o.inbound_queue = env("DITTO_INBOUND_QUEUE", "ditto.inbound")
    o.exchange = env("DITTO_OUTBOUND_EXCHANGE", "ditto")
    o.commands_key = env("DITTO_OUTBOUND_KEY", "outbound")
    o.commands_queue = env("DITTO_OUTBOUND_QUEUE", "ditto.commands")
    o.events_key = env("DITTO_EVENTS_KEY", "twin.events")
    o.events_exchange = env("DITTO_EVENTS_EXCHANGE", "ditto.events")
    o.events_queue = env("DITTO_EVENTS_QUEUE", "ditto.twin.events")

    o.wait_attempts = int(env("BOOTSTRAP_WAIT_ATTEMPTS", "60"))
    o.wait_interval = float(env("BOOTSTRAP_WAIT_INTERVAL", "5"))
    return o


def main(argv=None):
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")
    o = load_opts(args)
    http = Http(o.ditto_url, o.dry_run)

    LOG.info("bootstrap starting: ditto=%s policy=%s connection=%s subject=%s",
             o.ditto_url, o.policy_id, o.connection_id, o.subject)

    if not wait_for_ditto(http, o.devops_user, o.devops_pass,
                          o.wait_attempts, o.wait_interval):
        return 1

    if args.verify_only:
        return 0 if verify(http, o) else 1

    if not args.skip_amqp and not declare_amqp(o):
        return 1
    if not put_policy(http, o):
        return 1
    if not put_connection(http, o):
        return 1

    if o.dry_run:
        LOG.info("dry run complete, nothing was sent")
        return 0

    time.sleep(3)  # let the connection actually open before asking
    if not verify(http, o):
        LOG.error("=" * 62)
        LOG.error("BOOTSTRAP FAILED. The twin layer is NOT configured.")
        LOG.error("Dependent services will not start, which is intended.")
        LOG.error("=" * 62)
        return 1

    LOG.info("bootstrap complete, twin layer configured")
    return 0


if __name__ == "__main__":
    sys.exit(main())
