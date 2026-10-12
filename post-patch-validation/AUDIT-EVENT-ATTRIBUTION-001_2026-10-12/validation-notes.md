
## Runtime PPV first attempt

The pinned baseline/patch run reproduced both attribution bypasses on the baseline and rejected them on the patch; focused regressions, full unit suite, valid-event control and behavior comparison passed. The permitted-event security control did not reach its assertion because SQLAlchemy parsed the literal JSON `:1` as a bind parameter. Result SHA-256: `ece25c8718daf4faf4a5573b2247a3f99a285ac7c1d5352ae2079fe22e22324c`; report SHA-256: `490a73e175ba0cc1bd7e09a5e2c2cb00c5e3258ec82997a37105070a1042dc92`. This is a harness gap, so the full seven-category plan is being rerun with JSON passed through a bind parameter. The incomplete result and raw diagnostics are retained.

## Runtime PPV retry

The corrected permitted-event harness passed on both revisions. The pinned baseline accepted approved and fields-saved events without the employee actor or task respectively; the patch rejected each with SQLSTATE `23514`. Valid fully attributed approval, actorless task-bound approval invalidation, and actorless taskless OCR system claims remained accepted. Focused regressions and the complete unit suite passed on both required sides. Assessment complete with no supplied-check findings/gaps; human review required. Result SHA-256: `aec9f2d170c28f7f69e292c08b9613f2045d0646ae3feef77b7ad73f43d48d06`; report SHA-256: `6845dfcea97c7f0e9627c0bf5177902f7a02d9557483fd36168dd6e84267f5d2`.

The safe pruner dry-run listed 26 raw streams totaling 31,009 bytes for this complete run. After review, `--apply` pruned the completed streams and refreshed the artifact manifest. The first incomplete run remains unpruned with its diagnosed harness error. Synthetic disposable PostgreSQL only; no shared database, queue, worker, account or SRO record was used.
