# Production release: `sro-v1.1.0`

## Candidate and verification

The annotated `sro-v1.1.0` tag, `main`, and HTTPS `/version` agree on commit
`0eaf5e6d3174e91664ace7c0b8c2a123f99d0c46`. The accepted source tree is
`4f0767ecb9db38be1d9b75f1db38cff097f7d78e`.

The clean exact-candidate gate passed at 12:24 UTC. It covered 990 discovery
tests; 172 PostgreSQL tests were skipped in that run and then executed in
dedicated runs of 11 case-package tests and 161 workflow tests. It also covered
21 operations tests, 162 Node tests, Chromium, 100,000 synthetic tasks, and
100,001 files. Gate log SHA-256:
`9e5108d73857d86d70dfac42bb4313f149e603683ef23dac8b0e293ec71a1ba1`.

## Deployment and recovery

The VPS pull deployment returned exit code 0. The release database state was
`snapshot_verified`, and the deployed pointer matched the release candidate.
Maintenance was turned off; web, OCR, and preview services were active.

A fresh production backup was created. Its document archive and Redis snapshot
were restored in isolation; the document set was empty. A production database
clone was migrated from `ea912b30c761` to `20261021_reproc`. Candidate schema
validation, rollback, and old-schema restore passed. The clone preserved 13
tables, 9 rows, and 12 sequences; the source database remained unchanged.

## Post-deployment health

At 12:49:45 UTC all seven HTTPS readiness checks were healthy. Post-operation
queues, outbox, and registries were empty. OCR, preview, and SRO workers
reported 1, 1, and 0 respectively. The active workers' latest heartbeat was
230 seconds old against a 420-second threshold.

The authenticated Brave production browser verified rendering and navigation
of debtor, assignment and case-package lists and the document/OCR intake screen.
No production document upload or record save was used for this check. The full
upload/OCR/card/assignment flow was exercised separately with synthetic data in
the local Chrome demonstration. Production business lists remain empty.
The release and HTTPS identity/readiness readback are complete.

## Safety and product limits

No SROArbitr records were mutated, and external writes remained blocked.
Inner forms, intake, and OCR retain legacy presentation. The built-in PDF
viewer was blank in the checked local Chrome, while original files downloaded.
Office OCR accuracy has not been qualified. Further UI redesign and risky
changes are frozen before the office demonstration.

The synthetic Chrome demo remains separate from production at
`http://127.0.0.1:64586`.

GitHub hosted Actions billing remains exhausted. No paid Actions budget was
enabled; the supported manual release path completed successfully.
