SRO daily backup restore drill helper (private local runtime)

Scope
- Requires a root-owned backup directory with mode 0700 and exactly these mode-0600 files:
  SHA256SUMS, postgres.dump, documents.tar.gz, redis.rdb.
- Validates all three SHA-256 entries before reading payloads.
- Restores documents into a new mode-0700 evidence directory; directories are 0700 and
  restored files are 0600. Rejects archive links, special files, path escape, duplicate
  paths, oversized entries, and excessive member/count/expanded-byte totals. Each restored
  file is hashed again and compared to the exact archive stream; the report records count,
  byte total, and an aggregate digest without printing document names.
- PostgreSQL is intentionally hash-verified only. Run the separate PostgreSQL clone
  rehearsal; this helper never invokes pg_restore or connects to a database.
- Redis uses only the image ID of the existing deploy-redis-1 container. It creates a
  disposable --network none container with bounded memory/CPU/PIDs, docker-copies the RDB
  into its private writable layer, starts Redis with persistence writes disabled, checks
  readiness/loading state, and aggregates DBSIZE across nonempty DB indexes. It force
  removes the exact generated container and verifies it is gone. No host bind, published
  port, volume, production Redis command, or SRO client is used.

Before use
1. Review the exact fresh backup path and confirm it is the expected daily backup. Do not
   use production document directories or arbitrary archives as input.
2. Confirm the backup directory owner/modes and filesystem free space. The helper performs
   its own strict checks and requires extracted bytes plus at least 128 MiB / 10% reserve.
   On Linux it also checks Docker's data-root free space for the RDB copy; if that path
   is inside a Desktop VM and unavailable to the host, the report records that boundary.
3. Confirm Docker has the intended deploy-redis-1 container and image, and enough memory for
   Redis; default container memory limit is 2048 MiB (allowed range 256..4096 MiB).
4. Run as root only on the machine that already has the backup and existing Redis image.

Example (do not substitute a live application storage path):
  python3 scripts/ops/backup_store_restore_drill.py /path/to/root-owned/daily-backup

Optional document-only drill (still hash-verifies Redis RDB but skips loading it):
  python3 scripts/ops/backup_store_restore_drill.py --skip-redis /path/to/root-owned/daily-backup

The helper has a 1-hour total wall-clock bound, 15-second Docker command timeouts, and a
90-second Redis readiness bound. Evidence is created under /tmp/sro-backup-drill-* with
mode 0700; reports/logs are mode 0600. Successful document restoration remains in that
private directory and may contain personal documents; protect it as sensitive data and
remove it only after its required review/retention decision.

Focused offline tests (do not need Docker or production data):
  python3 -m unittest discover -s scripts/ops/tests -p 'test_backup_store_restore_drill.py' -v
