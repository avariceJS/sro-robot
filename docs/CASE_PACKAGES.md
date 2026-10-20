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
when those records already refer to the task.

The package list and matching task candidates are paginated in batches of 50
and 25. This feature does not decide whether two acts belong together and does not create, modify,
or send an SROArbitr record.

## Verification

`tests/test_case_packages_postgres.py` exercises replay, attributed history,
concurrent adds, and immutable events against disposable PostgreSQL.
`scripts/dev/case_packages_browser_e2e.py` exercises package creation/search,
explicit add/remove, stale replay rejection, source filename display, and both
pagination paths in loopback Chromium using synthetic rows only. The full local gate also tests
migration, restore, and populated-schema adoption. These checks do not establish
accuracy on representative office documents or staff acceptance.
