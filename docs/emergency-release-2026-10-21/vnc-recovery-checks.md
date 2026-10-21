# VNC release preparation and recovery qualification

The owner authorized use of the existing authenticated root Aeza console and
configured a read-only GitHub Deploy Key. Root confirmed the production pointer,
release marker and local version all select
`2827cd232ff6cc4287a6b880c2d310e21756e073`. Web/OCR/preview/Nginx were active,
queues/outbox/registries were empty and worker heartbeats were current. The root
filesystem had 109 GiB free. The VPS subsequently restarted outside agent actions;
the owner restored its root shell. The existing backup service was started and
returned `Result=success`. This is backup creation, not restore acceptance.

## Isolated local qualification

`production_clone_rehearsal.py` was exercised inside Colima Ubuntu 24.04 ARM64,
Python 3.12, PostgreSQL 16.15, using its real network-disabled clone and Unix
socket. Only the source container constant selected the synthetic source.
The fixture contained one synthetic user, document, task and debtor card.
Source revision `ea912b30c761` migrated to `20261021_reproc`; 13 old tables,
12 sequences and four rows were preserved. Baseline card revision was 1/1,
source signatures stayed unchanged, rollback restore and the old source's schema
check passed, and clone removal was verified. Twelve helper tests passed.

`backup_store_restore_drill.py` was exercised on a root-owned synthetic backup
inside the same Linux VM. One 63-byte document was restored and hashed; an
isolated Redis container read three keys across two databases. Missing and
corrupt RDB inputs failed safely. Disposable containers were removed with
readback. Nine focused tests cover archive/privacy boundaries and cleanup.

Strict hash installation exposed missing Linux cryptography 50.0.2 artifacts.
The exact-version published hashes were verified against official PyPI JSON and
added without bypassing hash checks. Linux ARM64 installation then passed.

## Remaining production conditions

These local runs qualify the helpers, not production data or VPS runtime.
Restore the fresh actual document/Redis backup, run the pinned candidate's
migration and rollback rehearsal on a private production database clone, and
repeat the final clean exact-candidate full gate before main/tag promotion.
Activation must retain its separate quiesced checkpoint/restore proof. No
production schema, release pointer, main/tag or SROArbitr record was changed.
