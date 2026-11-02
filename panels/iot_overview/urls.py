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

from django.conf.urls import url

from iotronic_ui.iot.iot_overview import views

UUID = r'(?P<uuid>[0-9a-fA-F-]{36})'

# One page. Every other route is a JSON endpoint the page calls on its own
# origin, so the browser never needs a credential besides the Horizon session.
urlpatterns = [
    url(r'^$', views.IndexView.as_view(), name='index'),
    # Machines, totals and path health as JSON, for the refresh timer.
    url(r'^data/$', views.data, name='data'),
    # Device actions: POST {"action": "ping|echo|reboot|restart_lr"}
    url(r'^board/' + UUID + r'/action/$', views.device_action, name='action'),
    # Recent audit records for one board, from the aggregator.
    url(r'^board/' + UUID + r'/activity/$', views.board_activity, name='activity'),
    # The same records as a page of their own: the machine's activity log.
    url(r'^board/' + UUID + r'/log/$', views.ActivityView.as_view(), name='log'),
    # Twin actions, relayed server side to s4t-twin-api.
    url(r'^board/' + UUID + r'/twin/$', views.twin_status, name='twin'),
    url(r'^board/' + UUID + r'/twin/set/$', views.twin_set, name='twin_set'),
    url(r'^board/' + UUID + r'/twin/clear/(?P<prop>[A-Za-z_][A-Za-z0-9_]{0,63})/$',
        views.twin_clear, name='twin_clear'),
]
