# Bulk document processing

This guide describes the local staff workflow for uploading court documents, checking extraction, and preparing internal debtor cards. The workflow does not create or change records in SROArbitr.

## Upload and resume

1. Sign in and open the task dashboard. Select PDF or image documents, or drop them into the upload area.
2. The browser submits bounded batches sequentially. Current defaults allow up to 100 files per selection, 20 files per request, 25 MiB per file, and 100 MiB per selection. Deployment settings can lower or raise the per-request file count and byte limits; the selection limit cannot exceed 100 files.
3. Keep the same browser tab/profile and user when resuming an interrupted identical selection. The browser retains its upload identity and confirmed operation IDs for that ordered selection. It tries persistent site storage, then tab storage, then memory for the open page. If only page memory is available and the response is unknown, keep the tab open until you have checked the task list; the page displays this reminder. Reopening the dashboard shows the linked work to staff members who select the same operation IDs.
4. Use the dashboard counters and status filter to monitor the selected intake. It refreshes while any selected task is queued or processing. A completed upload can still have tasks awaiting human review or marked failed.

Any authenticated employee can open **Intake history** from the dashboard to
recover previously accepted uploads. Each row identifies one exact accepted
upload operation, its submitting employee, receipt time, source filenames, and
current task states. Selecting a row opens the existing dashboard filtered by
that operation ID; this navigation does not submit files or start OCR. A
selection of many files can be split into several accepted request operations,
so a history row represents one accepted request and may cover only part of
the original selection. Separate operations remain separate even when names,
bytes, or case numbers match.

When an accepted upload operation still has tasks in the **принят** state, an
employee can request **Восстановить доставку OCR** from that exact history row.
The service serializes requests for the operation, records the requesting
employee, and restores or reuses the durable OCR outbox intent for existing
queued task IDs. It sends no file bytes and creates no task or document. It
does not explicitly lock task rows; intent creation commits before dispatch,
and a durable delayed intent is kept if an active worker skips a locked task.
The dispatcher checks current task state and active Redis deliveries; active
or unreadable queue state keeps the intent pending for a bounded retry. The
page distinguishes confirmed queue admission, a saved request whose admission
is still pending, and tasks that already advanced so no new delivery is
needed. KAD operations and tasks that have already started, failed, completed,
or otherwise left the queued state are not eligible. Content-addressed
documents are shared across source occurrences and retain their original
document-level source; if an upload occurrence points to a shared document
whose source is not upload/batch, history marks it withheld for manual
provenance review because the OCR worker also enforces that document-source
boundary. This recovers lost queue delivery for untouched eligible uploads; it
does not restart OCR or reconcile a worker that already entered `recognizing`.

Transport retries for an identical request reuse its intake identity. Changing file contents or ordering creates a different request identity. To deliberately run OCR again on the same selected documents, select **Intentionally start a new processing run** before uploading. This control requires any previous matching upload to have a confirmed operation ID; an unknown previous response must be reconciled in the task list first. Reprocessing creates new tasks and does not clear debtor-card duplicate checks. Content-level document reuse does not mean semantic deduplication: separately uploaded copies, renamed documents, or documents describing the same debtor may create separate tasks. The system flags some matching debtor identifiers for review, but staff must check those cases.

The ordinary-browser KAD helper records the exact employee-selected PDF URL under its KAD card URL as the source identity. Another employee selecting that same source and submitting identical PDF bytes is linked to the existing task. A different selected PDF in the same case remains separate, even when its bytes match. Different bytes for one selected URL are held for review unless the employee explicitly chooses a separate reprocessing run; that run gets a new task and a link to its parent run. The service rejects a conflicting case-number or act-date claim for the same selected source. This is source-occurrence identity, not proof that the selected document is the legally correct act; the employee still chooses and reviews it.

Upload retry metadata stores file hashes and opaque request keys, not file contents or OCR text. Confirmed retry history is bounded to 100 old chunk entries; unresolved entries are retained so a retry cannot accidentally create another operation. If local storage is unavailable or full, the uploader falls back to tab storage and then page memory instead of blocking before sending files. If the page-memory fallback is active, keep the page open until the server confirms the result.

## Extraction and review

OCR and extraction run locally. A clear, high-confidence person debtor match can prepare an internal debtor-card draft. Preparation is not approval and does not contact SROArbitr. The dashboard distinguishes prepared cards from tasks that still need review, failed tasks, queued work, and other statuses.

Open a task and compare its extracted fields with the source document using **Download source document**. Correct the fields before approval. For a task with a prepared internal card, open that card to inspect or correct its values; saved corrections advance its version and retain an attributed revision history. If the identifier matches an existing draft, the task remains in review and the interface reports a possible duplicate for staff investigation.

Use **Confirm and prepare internal card** only after checking the task fields. It saves the corrections, records the staff approval, and prepares an internal card as one operation. A stale page is rejected; reload and review the current values before retrying. If a duplicate conflict is reported, the submitted corrections and approval are retained on the task for follow-up, while no second card is created.

The ordinary **Save** action stores corrections without approving the task. **Confirm** records task approval. Neither action alone writes to SROArbitr. SRO-facing controls are outside this bulk internal-card workflow and must follow the separate office procedure and authorization.

When a task rooted in an upload/batch receipt has an initial confirmed OCR
failure, staff can select **Повторить локальный OCR с исходного документа** on
its review page. The action verifies the retained source bytes and exact
immutable failure record, then creates a clean child task with an attributed
link to the parent and the same source occurrence. The failed parent and its
history are retained;
the retry does not create another upload, receipt, or document and does not
copy recognized fields, corrections, approval, or SRO execution state. The new
task uses the normal local OCR queue and opens in the ordinary review flow.
Later confirmed OCR failures can be retried from their own child task, forming
an attributed chain. A stale failure, missing/changed source, KAD operation, or
task with prior SRO/preview execution is not eligible. A retry remains pending
for automatic queue-delivery recovery if Redis admission is temporarily
unavailable; staff can follow the parent/child links to see each attempt and
who initiated it. This is synthetic verification evidence, not office OCR
accuracy or staff acceptance.

## Operational limits

The dashboard displays tasks associated with the selected intake operations, across staff accounts. Counters summarize the complete selection, not only the visible page. Status filters affect the task list while counters continue to summarize the full selection. Queued and processing tasks are counted separately; prepared, review, failed, and other categories are exclusive. When any task is queued, the dashboard also shows the age of the oldest queued task measured from task creation. This is a durable staff-facing wait indicator, not exact Redis queue age, a configured alert threshold, or an SLA.

Task-list next/previous links use the immutable task creation time and ID as a keyset cursor. This keeps a page boundary stable when new intakes arrive; the page number remains an approximate position when tasks change status while staff browse. Intake and status filters are preserved by navigation and background polling.

The indicator was also exercised through the staff upload flow in Chromium against a fresh synthetic PostgreSQL/Redis runtime. The upload was accepted, the queued count and age appeared, the existing status-list poll refreshed the fragment, and the database task remained queued with its matching OCR delivery. The reproducible local check is `scripts/dev/nightly/verify_worklist_queue_age_browser.py`; it requires an explicitly empty private environment file and loopback-only disposable services and does not run an OCR worker.

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
