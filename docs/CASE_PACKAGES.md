# Case packages

Case packages are employee-curated workspaces for documents that belong to one
court case. Create a package with a staff-facing title and an optional case
number. A case number is a search/display aid; it never links tasks by itself.
Search the package list by title or case number to find an existing workspace.
Search links retain the filter when moving between package-list pages.

Open a package, search tasks by the task's case number or source filename, check
the source occurrence, and add each task explicitly. Packages can contain
multiple court acts. Each add/remove is attributed to the employee and retained
as immutable membership history. A stale browser form cannot reverse a later
membership change. The package view shows the source filename and links to the
task and original document; existing debtor-card and assignment links appear
when those records already refer to the task. A separate paginated history
shows every add/remove transition with its actor, source occurrence and time.
Each task review page also links to every package where its latest membership
event is an add. Removing the task hides that link; re-adding restores it.
Matching case numbers alone never create task-to-package links.

Existing internal assignments can also be added deliberately from the package
page. Search by debtor name or case number, or enter an exact assignment ID as
`#ID`, inspect its debtor and case, and choose Add. This is a separate,
append-only membership history; it does not change the assignment, its debtor
card, source task, or evidence. The assignment page links back to its active
packages. Assignment and task searches remain available when paging either
membership history or candidate lists, and assignment states use the same
Russian labels as the assignment editor.

An assignment editor whose `source_task_id` is explicitly linked shows that
source act beside the form and offers acts from the source's currently active
case packages. The source act opens initially; staff select other acts
themselves. Each option names its task and exact intake filename. Switching
changes only the sandboxed, hash-verified preview and original-download link,
so unsaved assignment fields remain in the form. The membership set is read
from one database snapshot; a removed act no longer appears after refresh.
Manual assignments without an explicit source task show no source evidence,
even when their debtor card was prepared from a task. Reviewing an act does not
copy OCR values or modify assignment, document, or membership history.

The package list and matching task candidates are paginated in batches of 50
and 25. This feature does not decide whether two acts belong together and does not create, modify,
or send an SROArbitr record.

## Verification

`tests/test_case_packages_postgres.py` exercises replay, attributed history,
concurrent adds, and immutable events against disposable PostgreSQL.
`scripts/dev/case_packages_browser_e2e.py` exercises package creation/search,
explicit add/remove, stale replay rejection, task-page links across removal and
re-add, visibility to a second employee, source filename display, and both
pagination paths plus the visible, paginated membership audit in loopback
Chromium using synthetic rows only. It also opens two synthetic court acts in
an assignment, checks exact renamed intake filenames and unsaved-field
retention, confirms removed members disappear after refresh, and verifies that
a manual assignment does not borrow its debtor card's source. Explicit
assignment membership checks same-card identity separation, add/remove/re-add
history, stable assignment contents, assignment backlinks, visible status
labels, dual-search state retention, and second-employee visibility. Preview is served
by the existing hash-verifying private document route. The full local gate also
tests migration, restore, and populated-schema adoption. These checks do not
establish accuracy on representative office documents or staff acceptance.
