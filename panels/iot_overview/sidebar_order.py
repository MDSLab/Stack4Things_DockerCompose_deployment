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
Puts Overview FIRST in the IoT sidebar.

WHY THIS MODULE EXISTS
----------------------
A panel added through an enabled file is APPENDED to its panel group, so
Overview would be listed after Fleets whatever its filename number. Horizon
has no setting for a panel's position inside a group.

What it does have is HORIZON_CONFIG["customization_module"]: a module Horizon
imports once, AFTER every dashboard and every enabled-file panel has been
registered (horizon/base.py, Site._urls). At that point the IoT dashboard's
'default' group holds its panel names as a plain list, and the sidebar is
drawn in that list's order. This module moves 'iot_overview' to the front.

It is switched on by UPDATE_HORIZON_CONFIG in _6005_iot_overview_panel.py, so
local_settings.py is not touched.

SAFE BY CONSTRUCTION
--------------------
It only reorders names that are already there. If the dashboard, the group or
the panel is missing it does nothing, and nothing it does can raise into
Horizon's startup: a sidebar in the old order is far better than no Horizon.

PYTHON 2.7.
"""

import logging

LOG = logging.getLogger(__name__)


def _move_overview_first():
    import horizon
    try:
        iot = horizon.get_dashboard("iot")
        group = iot.get_panel_group("default")
        if group is None or "iot_overview" not in group.panels:
            LOG.warning("sidebar_order: IoT default group or iot_overview "
                        "not found; leaving the sidebar order unchanged")
            return
        group.panels.remove("iot_overview")
        group.panels.insert(0, "iot_overview")
    except Exception as exc:
        LOG.warning("sidebar_order: could not reorder the IoT sidebar: %s", exc)


_move_overview_first()
