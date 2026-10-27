# R1 / PR-03 — Explicit SQLite schema migration (offline/staging only)

## Scope and version contract

This workstream introduced `db_schema.py` and `scripts/migrate_sqlite.py`.
The server and analyzer now require v2 schema at startup and never perform
implicit schema migrations. The CLI remains an opt-in, backed-up migration
primitive; no real photo libraries were touched or deployed. PR-03 remains
incomplete until config validation, remaining connection policy, dependency
installation, and Docker/systemd acceptance.

- **v0 (legacy)**: `photo_scores` exists with `path`; some optional columns may
  be missing. Migration adds missing columns using `ALTER TABLE ... ADD COLUMN`,
  preserving existing columns and all records. `PRAGMA user_version = 1`.
- **v1**: creates `transfer_history` if absent, plus the two existing
  `photo_scores` indexes used by the server. `user_version = 2`.
- **v2**: current schema. A repeat run checks the schema and does not create
  another backup or modify the DB. Future versions and incompatible schemas
  fail closed. Existing `photo_scores` table is mandatory: an unexpected
  empty/wrong DB is **not** silently initialized.

No column is dropped, renamed or rewritten. Neither `server.py` nor
`analyze_photos.py` runs the migration CLI automatically: both require an
existing, explicitly migrated v2 database.

## Safe staging procedure

1. Stop **all** writers to a *copy* of the database. Keep production and NAS
   untouched. Ensure free space for a full backup plus WAL and temporary files.
2. Read-only preflight (no schema writes):

   ```bash
   python -m scripts.migrate_sqlite --database /staging/photos.db
   ```

3. Explicitly migrate, with an independently verified backup:

   ```bash
   python -m scripts.migrate_sqlite --database /staging/photos.db \
       --backup-dir /staging/backups --apply
   ```

4. Confirm `PRAGMA user_version` is 2, `PRAGMA integrity_check` returns
   `ok`, compare `photo_scores` row counts, and test the actual application
   on the **staging copy** before planning any rollout.

The migration acquires `BEGIN IMMEDIATE` and keeps SQLite's single-writer lock
through the online backup, isolated restore drill, and atomic schema changes.
If backup/restore verification fails, it does not start DDL. SQLite DDL and
`user_version` are committed together; a failure rolls back both. The backup
is deliberately retained after an unsuccessful migration for diagnosis.
SQLite WAL mode is preserved, not forcibly changed by this CLI.

## Recovery and limitations

On an **isolated staging DB only**, stop all processes, preserve the failed
database and its WAL/SHM sidecars, then restore the verified backup to a
**new filename** using `scripts.backup_sqlite.restore_drill`. Check integrity,
row counts, and app behavior before any operator-controlled replacement.
Never overwrite a live SQLite file, and never copy only the main DB while
writers or WAL are active. Restoring a backup discards later writes: require
a maintenance window and explicit approval for any production recovery.

Example recovery into a separate staging path:

```bash
python - <<'PY'
from pathlib import Path
from scripts.backup_sqlite import restore_drill
restore_drill(Path("/staging/backups/verified-pre-v0.db"),
              Path("/staging/restored-new.db"))
PY
```

Replace the example backup filename with the actual printed path.

**Verification scope**: synthetic SQLite/WAL and CLI tests in GitHub Actions.
No real NAS database, multi-process service restart, Docker/systemd, browser,
ESP32 hardware, or production rollback has been tested. R1 Gate remains open.

## Server startup v2 gate (R1/PR-03 follow-up, source/CI only)

The server now requires an **existing, already-migrated v2** SQLite database.
On import/start it performs a read-only URI preflight (including v2 table,
columns and indexes). Missing, legacy v0/v1, future or corrupt schemas block
startup with a migration instruction. It does not create an empty DB or
automatically migrate it. After the schema gate, server enables WAL, and WAL
initialization errors also block startup. The old server-side implicit index
creation and transfer_history table creation are removed.

**Breaking change**: a fresh install or old database will no longer start the
server until the operator has initialized the database through the legacy
analyzer and migrated an isolated staging copy using the explicit CLI. Do not
point this branch at a production database before planning an authorized
maintenance window and backup/restore rehearsal.

The analyzer now has its own read-only preflight and existing-only writable
connection gate (see below). SQLite connections used by request handlers and
WebUI still need a common busy_timeout/URI-mode policy. These are separate
remaining PR-03 work packages.

**Rollback**: revert the server startup-gate implementation commit on the
independent branch, rerun tests, and restore code only; no database rollback is
needed because the preflight is read-only. This re-enables unsafe implicit DDL
on server startup and is not an approved production security rollback.

**Validation boundary**: synthetic SQLite/WAL tests and GitHub Actions only;
no real NAS database, full server process, HTTPS browser, Docker/systemd,
physical ESP32, or production rollout has been tested.

## Analyzer startup v2 gate (R1/PR-03 follow-up, CI-only)

`analyze_photos.py` no longer creates or ALTERs `photo_scores` on launch.
It checks the existing v2 database **before scanning or rewriting filelist.txt**.
Its subsequent writable SQLite connection opens with `mode=rw` (never
`mode=rwc`), verifies the schema again, uses a 5-second busy timeout and
requires WAL mode. A missing, legacy, malformed or future DB fails closed.
Only the explicit, backed-up operator migration CLI can change schema.

**New installation**: this branch cannot bootstrap an empty photo database
through the analyzer. An operator must prepare a legacy-compatible
`photo_scores` database on a staging copy and run the migration CLI. A
dedicated, backed-up initialization workflow remains a separate design item.
Existing deployments require a staged DB migration before adopting this code.

**Validation boundary**: isolated SQLite tests and source-level startup order
checks; no actual photo scan, model invocation, full analyzer process, NAS
concurrent writers, Docker/systemd, browser or ESP32 hardware exercised.

**Rollback**: revert the analyzer gate implementation on the isolated
development branch and rerun CI. This restores unversioned implicit DDL,
so it is **not** an approved production rollback. Data recovery, if needed,
must use a verified pre-migration backup on a stopped staging DB.

**CI evidence**: analyzer gate implementation commit
`a560f2c3b3f421462c16adaf6030d4851af40011`; [Push CI
40542143006](https://github.com/linyubin/InkTime/actions/runs/40542143006)
and [PR CI 40542151490](https://github.com/linyubin/InkTime/actions/runs/40542151490)
both SUCCESS, 85/85 Python and 4/4 Node tests, Python compile and requirements
checks. This is synthetic/source testing, **not a real analyzer startup**.


## WebUI read-only connection policy (R1/PR-03; source/CI only)

\`webui._get_conn()\` now delegates to \`db_schema.open_current_readonly_database()\`.
Every WebUI DB read uses an existing-only \`mode=ro\` SQLite URI (including
percent-encoded \`#\`/\`?\` filenames), \`PRAGMA query_only=ON\`, 5-second
\`busy_timeout\`, and a same-connection schema v2 validation. It never falls
back to \`sqlite3.connect(DB_PATH)\` on an error; missing, old, future and
inconsistent databases fail closed. This deliberately replaces the legacy
empty-response fallback with an error until the operator migrates the DB.

**Verification boundary**: isolated synthetic SQLite tests and WebUI AST
checks; not a complete Flask/browser startup, real NAS database, HTTPS proxy
or ESP32 device acceptance. Request-handler SQLite connections in
\`server.py\` still need to adopt the same policy. This change does not
migrate or create databases, alter NVS, or touch production.

**Rollback**: revert the specific WebUI policy commit on the development
branch and rerun tests. That reintroduces a potentially writable fallback
and is not an approved production security rollback. No DB rollback is
required because this policy performs no schema writes.


## R1 PR-03 server request connection policy (2026-10-28)

The server now routes its five read paths through `open_current_readonly_database` (`mode=ro`, `query_only=ON`, v2 revalidation, 5s busy timeout), and transfer-history inserts plus startup WAL initialization through `open_current_database` (`mode=rw`, v2 revalidation, WAL, 5s busy timeout). Raw `sqlite3.connect(DB_PATH)` calls are removed from `server.py`. This does not change the HTTP routes or intentionally change query results. The transfer-log insert remains best-effort: if the DB rejects writes, the request still returns its original response and logs an error; no implicit DB creation or migration occurs.

**Validation**: synthetic SQLite connection tests and source-policy regression tests only. Full Flask server request traffic, simultaneous writers, real NAS WAL behavior, Docker/systemd and HTTPS browser remain untested. **Rollback**: revert the server connection-policy commit only on `refactor/architecture-v1`, rerun CI; this restores implicit writable SQLite opens and is not a production-safe rollback. No database migration or data rollback is required for this source-only change.
