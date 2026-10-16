# Validation disposition

The first archive-adapter invocation stopped before checks: Python's safe tar
filter rejected an unrelated absolute symlink in a historical tracked
validation artifact. The adapter was narrowed to the finding's `sro_robot/`
and `tests/` trees. No source or patch content changed for this repair.

`results-initial` then completed all checks. Its adapter kept raw streams in a
temporary directory, so it was superseded by `results-final`, which records
stream paths and hashes in the result manifest. The final run completed all
seven evidence categories with no findings or gaps. After reviewing the final
assessment and a successful dry run, the repository's
`scripts/dev/prune-validation-logs.py` removed 30 raw streams (2,087 bytes)
and wrote `pruned-outputs.json` plus an updated artifact manifest.
The 30 superseded `results-initial` streams were also removed from the
temporary directory after every file matched its recorded SHA-256; the
per-stream hashes remain in `results-initial/result.json`.

The validation executes synthetic local code only. It does not establish
accuracy on real office documents or production readiness. Human review is
still required.
