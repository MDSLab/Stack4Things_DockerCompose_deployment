# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Registers the IoT Overview panel in Horizon's sidebar.
#
# This file is mounted into openstack_dashboard/enabled/, which is one of the
# two directories Horizon scans at startup. It mirrors
# _6010_iot_boards_panel.py exactly; the only differences are the slug and the
# panel class.
#
# THE NUMBER IS THE SIDEBAR POSITION. Horizon processes these files in filename
# order, so 6005 puts Overview first in the IoT group: after _6000_iot.py,
# which registers the dashboard itself, and before boards at 6010. Renaming
# this file is the whole mechanism for reordering it.
#
# DEFAULT_PANEL is deliberately left empty. Being first in the list is not the
# same as being the panel the IoT section opens on. Set it to 'iot_overview'
# if you want that too, but it changes existing behaviour for anyone who
# expects to land on Boards.

# The slug of the panel to be added to HORIZON_CONFIG. Required.
PANEL = 'iot_overview'
# The slug of the dashboard the PANEL associated with. Required.
PANEL_DASHBOARD = 'iot'
# The slug of the panel group the PANEL is associated with.
PANEL_GROUP = 'iot'
# If set, it will update the default panel of the PANEL_DASHBOARD.
DEFAULT_PANEL = ''

# Python panel class of the PANEL to be added.
ADD_PANEL = 'iotronic_ui.iot.iot_overview.panel.IotOverview'
