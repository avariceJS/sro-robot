# Blank-line debtor-field validation

The baseline is exact commit `15138ee`. `patch.diff` changes only
`sro_robot/core/extract.py` and `tests/test_core.py`; `plan.json` pins both
the patch and side-blind checks by SHA-256. The archive adapter extracts the
tracked Python source/tests into temporary directories, patches one copy,
and never creates a branch, clone, worktree, or Git mutation.

The local synthetic `synthetic-071.pdf` exposed the failure: OCR preserved
the same checksum-valid INN in every variant, but some variants separated it
from the named debtor with a blank line. A later address line also split the
last word of the city from the street/house line. The check file contains
minimal synthetic reproductions; the benchmark PDF and raw OCR are not
retained or exported here.

The patched parser recovers only immediate labeled personal fields after a
formatting blank. Automatic use additionally requires the same value in at least
one directly bound variant without that blank; otherwise it retains a warning.
A wrapped city fragment is joined only when
the next line contains a street label and house number. A field mentioned
without a safe boundary receives a warning. On synthetic 071, the INN is
now fully corroborated and the address value is complete, while the address
remains `role_partial` because every address occurrence crosses a blank.
The document therefore still requires employee review; the patch does not
weaken the automatic-card gate.

The final PPV run passed all nine checks across control, exploit, variants,
behavior, regression, security, and core/OCR suite. Original exploit/variant
assertions failed on the baseline and passed on the patched archive; the
standalone address API produced byte-identical output. The suite passed 47
tests. The result retains compact outputs, exit codes, hashes, and a report.
Raw streams were hash-checked with `scripts/dev/prune-validation-logs.py`
(`--dry-run`, then `--apply`) and pruned. Human review remains required by
the post-patch-validation evidence model.
