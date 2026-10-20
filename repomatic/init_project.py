# Copyright Kevin Deldycke <kevin@deldycke.com> and contributors.
#
# This program is Free Software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA  02111-1307, USA.

"""Former home of `repomatic init`, now the {mod}`repomatic.init` package."""

from __future__ import annotations

# 8.0.0 shim: re-export for callers not migrated yet.
from .bundle import get_data_content  # noqa: F401

# 8.0.0 shim: re-export for callers not migrated yet.
from .init.run import run_init  # noqa: F401

# 8.0.0 shim: re-export for callers not migrated yet.
from .init.tool_config import init_config  # noqa: F401

# 8.0.0 shim: re-export for callers not migrated yet.
from .init.upgrade import upgrade_invite  # noqa: F401

# 8.0.0 shim: re-export for callers not migrated yet.
from .upstream import is_source_repo  # noqa: F401
