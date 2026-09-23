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
The view fetches from the aggregator SERVER SIDE, from inside this container.

That is the whole reason this panel is worth building rather than opening a
separate page. The browser never sees the Ditto password or the audit token,
there is no cross origin request to configure, and the dashboard works from any
machine that can reach Horizon rather than only from the Docker host.

PYTHON 2.7. No f-strings.
"""

import json
import logging
import os

from django.http import JsonResponse
from django.utils.translation import ugettext_lazy as _
from django.views.generic import TemplateView

import requests

LOG = logging.getLogger(__name__)

BASE = os.environ.get("S4T_DASHBOARD_URL", "http://s4t-audit-logger:8891")
TIMEOUT = float(os.environ.get("S4T_DASHBOARD_TIMEOUT", "6"))


def fetch(path="/data", default=None):
    """(payload, error_message).

    Never raises. If the aggregator is down, slow, or returns something
    unreadable, the panel must still render with a message. A 500 here would
    make the whole IoT section look broken over an optional feature.
    """
    try:
        r = requests.get(BASE.rstrip("/") + path, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json(), None
    except Exception as exc:
        LOG.warning("iot_overview: aggregator unreachable for %s: %s", path, exc)
        if default is None:
            default = {"boards": [], "health": [], "totals": {}, "errors": {}}
        return default, str(exc)


class IndexView(TemplateView):
    # THREE parts: <dashboard>/<panel>/<file>. Horizon's template loader splits
    # the name, uses the first two to locate the panel, and then looks for the
    # remainder inside that panel's templates/<panel>/ directory. So this
    # resolves to iot_overview/templates/iot_overview/index.html.
    #
    # Dropping the dashboard slug gives TemplateDoesNotExist, because the
    # loader cannot work out which panel to search and Django's app loader
    # never sees this directory: only iotronic_ui.iot is in INSTALLED_APPS,
    # not the individual panels.
    template_name = 'iot/iot_overview/index.html'
    page_title = _("IoT Overview")

    def get_context_data(self, **kwargs):
        context = super(IndexView, self).get_context_data(**kwargs)
        payload, error = fetch()
        context['payload'] = payload
        context['boards'] = payload.get('boards', [])
        context['health'] = payload.get('health', [])
        context['totals'] = payload.get('totals', {})
        context['generated_at'] = payload.get('generated_at')
        context['error'] = error
        # Partial failures inside the aggregator, for example the twin layer
        # being unreachable while the registry is fine, are reported per
        # source rather than collapsed into one "something went wrong".
        context['source_errors'] = payload.get('errors', {})
        context['payload_json'] = json.dumps(payload)
        return context


def data(request):
    """The same payload as JSON, for the page's refresh timer."""
    payload, error = fetch()
    if error:
        payload = dict(payload)
        payload['error'] = error
    return JsonResponse(payload)


def board(request, uuid):
    """One board in full, for an expanded row.

    The uuid is passed straight through to the aggregator, which parameterises
    its SQL, so there is nothing to inject here. It is still worth noting that
    this view accepts whatever the URL pattern matched.
    """
    limit = request.GET.get('limit', '50')
    try:
        limit = str(min(int(limit), 500))
    except ValueError:
        limit = '50'
    payload, error = fetch('/board/%s?limit=%s' % (uuid, limit),
                           default={'uuid': uuid, 'twin': None,
                                    'events': [], 'errors': {}})
    if error:
        payload = dict(payload)
        payload['error'] = error
    return JsonResponse(payload)
