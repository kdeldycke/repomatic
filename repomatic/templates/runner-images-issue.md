---
args: [announcement_table, tracked_runners]
---

# GitHub runner image announcements

This issue exists because at least one open announcement names a runner image this repository runs on. **Labels** lists every image an announcement is about; **Affects** narrows that to the ones in use here, and those rows sort first.

\$announcement_table

Rows marked 🔴 with an entry under **Affects** carry a deadline. Read the announcement for the dates: a retirement usually schedules *brownouts*, where builds on the image fail outright, months before it is fully unsupported.

Deciding what to move to is a judgement call, not a lookup: the `repomatic-test-matrix` skill covers which images earn a matrix cell, and `repomatic job-timings` reports what each one currently costs in whole-job wall-clock. `repomatic sync-runner-images` opens a pull request proposing the mechanical part of the change.

Images this repository tracks: \$tracked_runners

This issue closes on its own once no open announcement names an image in use here.
