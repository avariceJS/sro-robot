# Address-boundary validation notes

The new failure was observed on the local synthetic PDF
`/tmp/sro-bulk-benchmark-baseline/corpus/synthetic-001.pdf`: the previous
role-scoped extractor included subsequent document prose in the debtor address.
That temporary benchmark input is not retained here; `checks.py` contains the
minimal synthetic reproduction.

`results-initial` compared the original repository HEAD with the full updated
patch. It completed successfully, but is superseded by `results-final`, which
uses the pinned previous role-extraction patch as its baseline. This isolates
the address defect introduced by the previous patch. The final patched side
uses the new full patch, both applied to exact `git archive` snapshots of
`8c46c9ac9b8a8009246b92f43bbad2b45142de4d`. No branch, clone, worktree,
Git mutation, external service, or non-synthetic document was involved.

All eight final checks passed. The side-blind exploit and variants reached
their assertions, failed on the previous role patch, and passed on the address
fix. The standalone address API produced identical output on both sides. The
patched archive's core/OCR suite passed 38 tests. The result retains exit
codes, concise output excerpts, hashes, and manifests. Raw streams from both
complete runs were hash-checked with `scripts/dev/prune-validation-logs.py`
(`--dry-run`, then `--apply`) and pruned. Human review remains required by
the post-patch-validation evidence model.
