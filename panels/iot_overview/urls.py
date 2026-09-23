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


urlpatterns = [
    url(r'^$', views.IndexView.as_view(), name='index'),
    # Same payload as the page, as JSON, for the refresh timer. Same origin,
    # so it uses the Horizon session and needs no token of its own.
    url(r'^data/$', views.data, name='data'),
    # One board in full: the whole twin document and its audit history.
    # Fetched on demand when a row is expanded, so the main table stays small
    # even with a board that has thousands of records.
    url(r'^board/(?P<uuid>[^/]+)/$', views.board, name='board'),
]
