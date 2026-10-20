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

"""Names that moved to {mod}`~repomatic.github.outputs`,
{mod}`~repomatic.github.event`, {mod}`~repomatic.github.runs` and
{mod}`repomatic.tabular`, kept here for the modules that still import them from
this one."""

from __future__ import annotations

# 8.0.0 shim: re-export for callers not migrated yet.
from ..tabular import ReportAction  # noqa: F401

# 8.0.0 shim: re-export for callers not migrated yet.
from .event import (  # noqa: F401
    NULL_SHA,
    WorkflowEvent,
    extract_workflow_filename,
    get_default_author,
    get_default_number,
    get_event_pull_request,
    get_event_subject,
    get_github_event,
    is_pull_request,
)

# 8.0.0 shim: re-export for callers not migrated yet.
from .outputs import (  # noqa: F401
    MAX_STEP_OUTPUT_BYTES,
    AnnotationLevel,
    emit_annotation,
    emit_report,
    format_file_output,
    format_multiline_output,
    generate_delimiter,
    read_file_output,
    report_finding,
    trim_to_budget,
    trim_to_byte_budget,
    write_output_file,
)

# 8.0.0 shim: re-export for callers not migrated yet.
from .runs import (  # noqa: F401
    RUN_LISTING_WINDOW,
    cancel_superseded_runs,
    runs_created_since,
    workflow_runs,
)
