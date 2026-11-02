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
# The number orders how Horizon PROCESSES these files, not where the panel
# appears. A panel added this way is appended to the end of its group; the
# position is set by UPDATE_HORIZON_CONFIG at the bottom of this file.
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
#
# 'default', NOT 'iot'. The IoT dashboard declares its panels as a flat tuple,
# so Boards, Plugins and the rest live in Horizon's unnamed 'default' group.
# There is no group called 'iot': Horizon failed to find it, logged a warning,
# and put this panel in a catch-all group it labels "Other", which showed in
# the sidebar and as "IoT / Other" in the breadcrumb.
PANEL_GROUP = 'default'
# If set, it will update the default panel of the PANEL_DASHBOARD.
DEFAULT_PANEL = ''

# Python panel class of the PANEL to be added.
ADD_PANEL = 'iotronic_ui.iot.iot_overview.panel.IotOverview'

# Puts Overview FIRST in the IoT sidebar. Horizon appends enabled-file panels
# to the end of their group and has no position setting, but it imports
# HORIZON_CONFIG["customization_module"] after every panel is registered, and
# that module reorders the group. See iot_overview/sidebar_order.py.
#
# Supported since Horizon Juno. NOTE: there is ONE customization_module per
# Horizon. If local_settings.py or another plugin ever sets its own, this line
# replaces it (or it replaces this one), and Overview drops back to last.
UPDATE_HORIZON_CONFIG = {
    'customization_module': 'iotronic_ui.iot.iot_overview.sidebar_order',
}
