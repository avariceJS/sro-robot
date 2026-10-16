# Bulk document processing

This guide describes the local staff workflow for uploading court documents, checking extraction, and preparing internal debtor cards. The workflow does not create or change records in SROArbitr.

## Upload and resume

1. Sign in and open the task dashboard. Select PDF or image documents, or drop them into the upload area.
2. The browser submits bounded batches sequentially. Current defaults allow up to 100 files per selection, 20 files per request, 25 MiB per file, and 100 MiB per selection. Deployment settings can lower or raise the per-request file count and byte limits; the selection limit cannot exceed 100 files.
3. Keep the same browser tab/profile and user when resuming an interrupted identical selection. The browser retains its upload identity and confirmed operation IDs for that ordered selection. It tries persistent site storage, then tab storage, then memory for the open page. If only page memory is available and the response is unknown, keep the tab open until you have checked the task list; the page displays this reminder. Reopening the dashboard shows the linked work to staff members who select the same operation IDs.
4. Use the dashboard counters and status filter to monitor the selected intake. It refreshes while any selected task is queued or processing. A completed upload can still have tasks awaiting human review or marked failed.

Transport retries for an identical request reuse its intake identity. Changing file contents or ordering creates a different request identity. To deliberately run OCR again on the same selected documents, select **Intentionally start a new processing run** before uploading. This control requires any previous matching upload to have a confirmed operation ID; an unknown previous response must be reconciled in the task list first. Reprocessing creates new tasks and does not clear debtor-card duplicate checks. Content-level document reuse does not mean semantic deduplication: separately uploaded copies, renamed documents, or documents describing the same debtor may create separate tasks. The system flags some matching debtor identifiers for review, but staff must check those cases.

Upload retry metadata stores file hashes and opaque request keys, not file contents or OCR text. Confirmed retry history is bounded to 100 old chunk entries; unresolved entries are retained so a retry cannot accidentally create another operation. If local storage is unavailable or full, the uploader falls back to tab storage and then page memory instead of blocking before sending files. If the page-memory fallback is active, keep the page open until the server confirms the result.

## Extraction and review

OCR and extraction run locally. A clear, high-confidence person debtor match can prepare an internal debtor-card draft. Preparation is not approval and does not contact SROArbitr. The dashboard distinguishes prepared cards from tasks that still need review, failed tasks, queued work, and other statuses.

Open a task and compare its extracted fields with the source document using **Download source document**. Correct the fields before approval. For a task with a prepared internal card, open that card to inspect or correct its values; saved corrections advance its version and retain an attributed revision history. If the identifier matches an existing draft, the task remains in review and the interface reports a possible duplicate for staff investigation.

Use **Confirm and prepare internal card** only after checking the task fields. It saves the corrections, records the staff approval, and prepares an internal card as one operation. A stale page is rejected; reload and review the current values before retrying. If a duplicate conflict is reported, the submitted corrections and approval are retained on the task for follow-up, while no second card is created.

The ordinary **Save** action stores corrections without approving the task. **Confirm** records task approval. Neither action alone writes to SROArbitr. SRO-facing controls are outside this bulk internal-card workflow and must follow the separate office procedure and authorization.

## Operational limits

The dashboard displays tasks associated with the selected intake operations, across staff accounts. Counters summarize the complete selection, not only the visible page. Status filters affect the task list while counters continue to summarize the full selection. Queued and processing tasks are counted separately; prepared, review, failed, and other categories are exclusive.

The configured file and byte limits are request guards, not throughput guarantees. No performance target or service-level guarantee is implied. For a reproducible local check, run the full verifier, which creates and checks its own disposable PostgreSQL identity before enabling integration tests:

```bash
MIGRATION_DATABASE_URL='' bash scripts/dev/verify.sh
```

Never use real client documents, identifiers, OCR text, or a live SRO account as test input.

## Isolated OCR worker concurrency

The development worker launcher defaults to one RQ worker and accepts at most four. More than one worker is admitted only for the `ocr` queue when durable OCR task leases are enabled; mixed queues, SRO queues, disabled leases, and out-of-range values are rejected before schema or Redis access. Parallel workers use RQ's worker pool. This setting does not change the service worker configuration.

On the isolated 12-core, 24 GiB host, four OCR workers were the best-performing configuration among those tested (1, 2 and 4). The four workers' sampled peak combined RSS was approximately 1.5 GiB. This is a local synthetic workload observation, not a server recommendation or capacity guarantee.

Only run the command below after verifying that the process points to an isolated local database and Redis, OCR leases are enabled, and the database is at the exact current application schema revision. The launcher repeats the schema and Redis preflights before starting:

Reconcile queued/started work first and stop older unleased workers before changing lease mode. Never mix unleased and lease-aware workers against the same queue.

```bash
OCR_TASK_LEASES_ENABLED=1 .venv/bin/python scripts/dev/run_worker.py --workers 4 ocr
```

Do not use this command against the office service or to change its worker count.
