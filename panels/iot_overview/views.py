# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""
IoT Overview: every machine, its digital twin, and every action on it, on one
page.

It was two panels, a read-only Overview and a separate Device Control. They
showed the same machines, so an operator had to look in one place and act in
another. Now the selected machine's state, its controls and its activity sit
side by side, and the page keeps the Overview's system view (totals and
whether each data path is delivering).

WHERE EACH CALL GOES
--------------------
All of them are made SERVER SIDE, from inside this container. The browser
only ever talks to Horizon, with its session cookie and Django's CSRF token.

  * the machine list, totals and path health: the dashboard aggregator in
    s4t-audit-logger, read only, which holds the Ditto and audit credentials
  * device actions (ping, echo, restart agent, reboot): the IoTronic conductor
    with the OPERATOR'S OWN Keystone token, found through the Keystone catalog.
    The public `iot` endpoint is the audit proxy, so each action is recorded
    under the operator's name with no extra code here
  * twin actions (set or clear a desired value): s4t-twin-api, with the
    operator's token; it checks the role and the allow list and records who

THE CATALOGUE
-------------
ACTIONS below is the whole list of device actions this page offers. The
checks on it are enforced here, on the server, not only in the page:

  * `admin` actions need one of S4T_DEVICE_ADMIN_ROLES (default `admin`)
  * every device action needs the board to be `online`, read from the
    conductor at the moment of the request, not from a cached list

The conductor applies its own policy on top of this, so this page can only
narrow what an operator may do, never widen it.

PYTHON 2.7. This runs inside the iotronic-ui container. Keep to
`ugettext_lazy`, old style `url()` patterns and no f-strings.
"""

import json
import logging
import os
import re
import time

from django.http import JsonResponse
from django.utils.translation import ugettext_lazy as _
from django.views.decorators.http import require_GET
from django.views.decorators.http import require_POST
from django.views.generic import TemplateView

import requests

LOG = logging.getLogger(__name__)

DASHBOARD = os.environ.get("S4T_DASHBOARD_URL", "http://s4t-audit-logger:8891")
TWIN_API = os.environ.get("S4T_TWIN_API_URL", "http://s4t-twin-api:8892")
# Empty means "ask the Keystone catalog", which is what Horizon itself does
# and is what puts the audit proxy in the path. Set it only to debug.
IOT_URL = os.environ.get("S4T_IOT_URL", "")
READ_TIMEOUT = float(os.environ.get("S4T_DASHBOARD_TIMEOUT", "6"))
# A device action waits for Lightning-Rod to answer through the conductor.
ACTION_TIMEOUT = float(os.environ.get("S4T_ACTION_TIMEOUT", "30"))
ADMIN_ROLES = set(r.strip() for r in os.environ.get(
    "S4T_DEVICE_ADMIN_ROLES", "admin").split(",") if r.strip())

# key -> what the page shows and what the conductor is asked to do.
# `rpc` is the Lightning-Rod action name, sent in the request body; the audit
# proxy derives the recorded verb from it (DeviceReboot -> device.reboot).
ACTIONS = {
    "ping": {"rpc": "DevicePing", "label": "Ping",
             "group": "diagnostics", "admin": False, "confirm": False,
             "help": "Checks the agent on the device answers. Changes nothing."},
    "echo": {"rpc": "DeviceEcho", "label": "Echo",
             "group": "diagnostics", "admin": False, "confirm": False,
             "help": "Sends a message to the device and back. Changes nothing."},
    "restart_lr": {"rpc": "DeviceRestartLR", "label": "Restart agent",
                   "group": "maintenance", "admin": True, "confirm": True,
                   "help": "Restarts the Lightning-Rod agent. The board is "
                           "offline for a few seconds while it reconnects."},
    "reboot": {"rpc": "DeviceReboot", "label": "Reboot device",
               "group": "maintenance", "admin": True, "confirm": True,
               "help": "Reboots the device. It is offline until it boots "
                       "and its agent reconnects."},
}
ORDER = ["ping", "echo", "restart_lr", "reboot"]
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                     r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")


# =========================================================================
# Helpers
# =========================================================================
def user_roles(request):
    try:
        return set(r.get("name") for r in (request.user.roles or [])
                   if r.get("name"))
    except Exception:
        return set()


def is_admin(request):
    return bool(user_roles(request) & ADMIN_ROLES)


def token_of(request):
    try:
        return request.user.token.id
    except Exception:
        return None


def iot_base(request):
    """The conductor's /v1 URL, from the Keystone catalog like Horizon does."""
    base = IOT_URL
    if not base:
        from openstack_dashboard.api import base as api_base
        base = api_base.url_for(request, "iot")
    base = base.rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return base


def as_json(resp):
    """Always a dict, whatever the other side sent."""
    try:
        data = resp.json()
    except ValueError:
        return {"text": resp.text[:500]}
    return data if isinstance(data, dict) else {"data": data}


def fetch_boards():
    """(payload, error). Never raises: if the aggregator is down the page still
    renders, says so, and the actions that do not need it keep working."""
    try:
        r = requests.get(DASHBOARD.rstrip("/") + "/data", timeout=READ_TIMEOUT)
        r.raise_for_status()
        return r.json(), None
    except Exception as exc:
        LOG.warning("iot_overview: aggregator unreachable: %s", exc)
        return {"boards": [], "health": [], "totals": {}, "errors": {}}, str(exc)


# Keystone user id -> (name or None, expiry). A board's owner is stored as a
# user id; operators recognise names. Cached because the board list refreshes
# every few seconds and owners rarely change.
_USER_NAMES = {}
NAME_TTL = 600          # a name found
MISS_TTL = 60           # not found or not allowed: retry a minute later


def user_name(request, user_id):
    """The Keystone name for a user id, or None.

    Asked with the operator's own session, so it shows only what that
    operator may see; a role that cannot read users simply gets the id. Never
    raises: a missing name must not break the page.
    """
    if not user_id or request is None:
        return None
    now = time.time()
    hit = _USER_NAMES.get(user_id)
    if hit and hit[1] > now:
        return hit[0]
    name = None
    try:
        from openstack_dashboard.api import keystone
        name = getattr(keystone.user_get(request, user_id, admin=True), "name", None)
    except Exception as exc:
        LOG.info("iot_overview: no name for user %s: %s", user_id, exc)
    _USER_NAMES[user_id] = (name, now + (NAME_TTL if name else MISS_TTL))
    if len(_USER_NAMES) > 5000:
        _USER_NAMES.clear()
    return name


def board_rows(payload, request=None):
    """Only what this page needs, so the page never carries more than it shows."""
    rows = []
    for b in payload.get("boards") or []:
        twin = b.get("twin") or {}
        attrs = twin.get("attributes") or {}
        audit = b.get("audit") or {}
        rows.append({
            "uuid": b.get("uuid"),
            "name": b.get("name") or b.get("uuid"),
            "type": b.get("type"),
            "fleet": b.get("fleet"),
            "status": b.get("status"),
            "online": bool(b.get("online")),
            "lr_version": b.get("lr_version"),
            "twin": bool(twin.get("present")),
            "revision": twin.get("revision"),
            "desired": twin.get("desired") or {},
            "reported": twin.get("telemetry") or {},
            "attributes": attrs,
            # shown as "name (id)" wherever the owner appears
            "owner_name": user_name(request, attrs.get("owner")),
            # Freshness as the aggregator judged it. `stale` is None, not
            # False, when there is no timestamp: no evidence is not "fresh".
            "age": twin.get("age_human"),
            "stale": twin.get("stale"),
            "audit_count": audit.get("count") or 0,
            "last_action": audit.get("last_action"),
            "last_operator": audit.get("last_operator"),
        })
    return rows


def fail(message, status):
    return JsonResponse({"ok": False, "error": message}, status=status)


def body_json(request):
    try:
        data = json.loads(request.body or "{}")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


# =========================================================================
# Page
# =========================================================================
class IndexView(TemplateView):
        # THREE parts: <dashboard>/<panel>/<file>. Horizon's template loader
    # splits the name and uses the first two to find the panel, so this
    # resolves to iot_overview/templates/iot_overview/index.html. Dropping the
    # dashboard slug gives TemplateDoesNotExist.
    template_name = 'iot/iot_overview/index.html'
    page_title = _("Overview")

    def get_context_data(self, **kwargs):
        context = super(IndexView, self).get_context_data(**kwargs)
        # Horizon's breadcrumb ends with context['page_title']. A plain Django
        # TemplateView never puts the class attribute there (Horizon's own
        # views do it in PageTitleMixin), so the breadcrumb read "None".
        context['page_title'] = self.page_title
        payload, error = fetch_boards()
        admin = is_admin(self.request)
        context['boards'] = board_rows(payload, self.request)
        context['error'] = error
        context['source_errors'] = payload.get('errors') or {}
        context['is_admin'] = admin
        context['actions'] = [dict(ACTIONS[k], key=k) for k in ORDER]
        cfg = json.dumps({
            "is_admin": admin,
            "admin_roles": ", ".join(sorted(ADMIN_ROLES)),
            # The first render needs no round trip; the page refreshes it.
            "boards": context['boards'],
            "totals": payload.get("totals") or {},
            "health": payload.get("health") or [],
            "list_error": bool(error),
            "actions": [dict(ACTIONS[k], key=k) for k in ORDER],
            # Only a well formed uuid, because this value is written into
            # a <script> block on the page.
            "open_board": (self.request.GET.get("board")
                           if UUID_RE.match(self.request.GET.get("board") or "")
                           else None),
        })
        # Written into a <script> block, and board names are user input: a
        # name containing "</script>" must not end the block early.
        context['config_json'] = (cfg.replace("<", "\\u003c")
                                  .replace(">", "\\u003e")
                                  .replace("&", "\\u0026"))
        return context


def safe_script_json(obj):
    """JSON for a <script> block: no value can close the block early."""
    return (json.dumps(obj).replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("&", "\\u0026"))


class ActivityView(TemplateView):
    """One machine's whole audit history on its own page: search, filters,
    paging, row detail and CSV export. Opened from the machine's header on the
    Overview. The rows come from board_activity, on this same origin."""
    template_name = 'iot/iot_overview/activity.html'
    page_title = _("Activity log")

    def get_context_data(self, **kwargs):
        context = super(ActivityView, self).get_context_data(**kwargs)
        uuid = kwargs.get("uuid")
        payload, error = fetch_boards()
        board = None
        for b in board_rows(payload, self.request):
            if b.get("uuid") == uuid:
                board = b
                break
        name = (board or {}).get("name") or uuid
        try:
            from django.core.urlresolvers import reverse        # Django < 2
        except ImportError:
            from django.urls import reverse
        try:
            overview = reverse("horizon:iot:iot_overview:index")
        except Exception:
            overview = "../../../"
        back = overview + "?board=" + uuid
        context['page_title'] = self.page_title
        # Horizon's breadcrumb: IoT / Overview / <machine> / Activity log
        context['custom_breadcrumb'] = [(name, back)]
        context['board'] = board or {"uuid": uuid, "name": uuid}
        context['back_url'] = back
        context['error'] = error
        context['config_json'] = safe_script_json({"uuid": uuid, "name": name})
        return context


@require_GET
def data(request):
    payload, error = fetch_boards()
    out = {"boards": board_rows(payload, request),
           "totals": payload.get("totals") or {},
           "health": payload.get("health") or [],
           "source_errors": payload.get("errors") or {},
           "generated_at": payload.get("generated_at")}
    if error:
        out["error"] = error
    return JsonResponse(out)


# The filters the activity search accepts, passed through to the aggregator
# and nothing else. The aggregator validates each value again.
ACTIVITY_PARAMS = ("q", "category", "hide_telemetry", "actor", "outcome",
                   "operator", "since", "before_seq", "limit")


def summarise(detail, limit=160):
    """One line describing what a record changed, for the activity table."""
    if detail is None or detail == {}:
        return ""
    try:
        text = json.dumps(detail, sort_keys=True, separators=(", ", ": "))
    except (TypeError, ValueError):
        text = u"%s" % (detail,)
    return text if len(text) <= limit else text[:limit - 1] + u"\u2026"


@require_GET
def board_activity(request, uuid):
    """One page of a board's audit records, searched and filtered on the
    server, over the whole history rather than the rows already on screen.
    Read from the same aggregator as the board list, so it holds no
    credential of its own."""
    params = {}
    for k in ACTIVITY_PARAMS:
        v = request.GET.get(k)
        if v:
            params[k] = v[:128]
    try:
        r = requests.get("%s/board/%s/events" % (DASHBOARD.rstrip("/"), uuid),
                         params=params, timeout=READ_TIMEOUT)
        r.raise_for_status()
        data = r.json() or {}
    except Exception as exc:
        LOG.warning("iot_overview: activity unavailable for %s: %s", uuid, exc)
        return JsonResponse({"events": [], "has_more": False, "operators": [],
                             "error": "activity unavailable"})
    out = []
    for e in data.get("events") or []:
        a = e.get("action") or {}
        out.append({
            "sequence": e.get("sequence"),
            "timestamp": e.get("timestamp"),
            "machine_action": e.get("machine_action"),
            "operator": e.get("operator_name"),
            "actor": (e.get("actor") or {}).get("type"),
            "outcome": a.get("outcome"),
            "status_code": a.get("status_code"),
            "summary": summarise(a.get("detail")),
            # the full detail for an expanded row on the activity page,
            # bounded so one huge record cannot bloat every response
            "detail": summarise(a.get("detail"), limit=4000),
            "target": a.get("target"),
            "source": e.get("source"),
            "correlation_id": e.get("correlation_id"),
        })
    return JsonResponse({"events": out,
                         "has_more": bool(data.get("has_more")),
                         "next_before": data.get("next_before"),
                         "operators": data.get("operators") or []})


# =========================================================================
# Device actions -> conductor (through the audit proxy)
# =========================================================================
@require_POST
def device_action(request, uuid):
    req = body_json(request)
    if req is None:
        return fail("body must be a JSON object", 400)
    key = req.get("action")
    spec = ACTIONS.get(key)
    if not spec:
        return fail("unknown action", 400)
    if spec["admin"] and not is_admin(request):
        return fail("%s needs one of the roles: %s"
                    % (spec["label"], ", ".join(sorted(ADMIN_ROLES))), 403)
    token = token_of(request)
    if not token:
        return fail("no Keystone token in this session", 401)

    try:
        base = iot_base(request)
    except Exception as exc:
        return fail("the iot endpoint is not in the Keystone catalog: %s" % exc, 502)
    headers = {"X-Auth-Token": token, "Content-Type": "application/json",
               "Accept": "application/json"}

    # The board must be online NOW. Read from the conductor at the moment of
    # the request, not from the page, which may be several seconds old.
    try:
        r = requests.get("%s/boards/%s" % (base, uuid), headers=headers,
                         timeout=READ_TIMEOUT)
    except Exception as exc:
        return fail("conductor unreachable: %s" % exc, 502)
    if r.status_code == 404:
        return fail("no such board", 404)
    if r.status_code != 200:
        return fail("conductor answered %s reading the board" % r.status_code, 502)
    status = (as_json(r).get("status") or "").lower()
    if status != "online":
        return fail("the board is %s; device actions need it online"
                    % (status or "in an unknown state"), 409)

    params = {}
    if key == "echo":
        params = {"message": u"%s" % (req.get("message") or "hello from Horizon")}
        params["message"] = params["message"][:200]
    try:
        r = requests.post("%s/boards/%s/action" % (base, uuid), headers=headers,
                          data=json.dumps({"action": spec["rpc"],
                                           "parameters": params}),
                          timeout=ACTION_TIMEOUT)
    except requests.Timeout:
        return fail("%s sent, but the device did not answer within %ss"
                    % (spec["label"], int(ACTION_TIMEOUT)), 504)
    except Exception as exc:
        return fail("conductor unreachable: %s" % exc, 502)

    LOG.info("iot_overview: %s on %s by %s -> %s", spec["rpc"], uuid,
             getattr(request.user, "username", "?"), r.status_code)
    ok = 200 <= r.status_code < 300
    return JsonResponse({"ok": ok, "action": key, "rpc": spec["rpc"],
                         "status": r.status_code, "result": as_json(r)},
                        status=200 if ok else 502)


# =========================================================================
# Twin actions -> s4t-twin-api
# =========================================================================
def relay(request, method, path, payload=None):
    """Forward to s4t-twin-api with the operator's token and pass its answer
    back unchanged. That service makes every decision; this is only a pipe."""
    token = token_of(request)
    if not token:
        return fail("no Keystone token in this session", 401)
    try:
        r = requests.request(
            method, TWIN_API.rstrip("/") + path,
            headers={"X-Auth-Token": token, "Content-Type": "application/json"},
            data=json.dumps(payload) if payload is not None else None,
            timeout=READ_TIMEOUT)
    except Exception as exc:
        LOG.warning("iot_overview: twin api unreachable: %s", exc)
        return fail("the twin command service is unreachable", 502)
    return JsonResponse(as_json(r), status=r.status_code, safe=False)


@require_GET
def twin_status(request, uuid):
    return relay(request, "GET", "/twin/%s/status" % uuid)


@require_POST
def twin_set(request, uuid):
    req = body_json(request)
    if req is None or not isinstance(req.get("properties"), dict):
        return fail("body must be {\"properties\": {...}}", 400)
    return relay(request, "POST", "/twin/%s/desired" % uuid,
                 {"properties": req["properties"]})


@require_POST
def twin_clear(request, uuid, prop):
    return relay(request, "DELETE", "/twin/%s/desired/%s" % (uuid, prop))
