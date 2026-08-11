---
args: [announcement_table, tracked_runners]
---

# GitHub runner image announcements

Open announcements from [actions/runner-images](https://github.com/actions/runner-images/issues?q=is%3Aissue+is%3Aopen+label%3AAnnouncement). Rows naming an image this repository runs on sort first and carry it in **Affects**.

\$announcement_table

A `runs-on:` value is the one dependency in a workflow that nothing bumps automatically, so a retirement lands as a failing build unless it is acted on here. Rows marked 🔴 with an entry under **Affects** are the ones with a deadline.

Images this repository tracks: \$tracked_runners

This issue closes on its own once GitHub has closed every open announcement.
