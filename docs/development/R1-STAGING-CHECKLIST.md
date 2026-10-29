# R1 isolated staging acceptance checklist (NOT an approval to deploy)

This checklist documents what must be demonstrated on an operator-owned **isolated
copy** of the database and a non-production image tree. Passing CI or the local
preflight does not satisfy the R1 gate. Keep Draft PR #1 open.

## A. Prepare a reproducible staging environment

1. Check out only `refactor/architecture-v1` in an isolated worktree, verify
   the exact commit SHA, and ensure the staging host has no production mounts
   writable by the test user. Never run this branch with production credentials.
2. Create a disposable Python 3.11 environment and install the declared runtime
   dependencies. CI now installs the same requirements but has no NAS/BLE device:

   ```bash
   python3.11 -m venv .venv
   .venv/bin/python -m pip install -r requirements.txt
   .venv/bin/python -m pip check
   .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
   ```

3. Use a verified *staging* SQLite backup/restore, not a raw copy of a live
   WAL database. Follow [R1-SQLITE-MIGRATION.md](R1-SQLITE-MIGRATION.md).
   For a new empty *staging* installation only, use the explicit initializer.
   Preserve WAL/SHM sidecars and operator logs when diagnosing any failure.
4. Run the **read-only application prerequisite check** with all arguments
   pointing to isolated paths:

   ```bash
   .venv/bin/python -m scripts.staging_preflight \
       --database /staging/inktime/photos.db \
       --images /staging/inktime/photos \
       --output /staging/inktime/output
   ```

   This checks SQLite v2 and integrity, plus the existence of the image and
   output directories. It does **not** probe output write permission, NAS
   disconnect handling, symlink/mount isolation, directory durability or
   HTTPS. SQLite read-only WAL opening may rely on filesystem sidecars;
   only run this on a safe test copy.

## B. Required operator-driven acceptance evidence

| Gate check | Evidence to retain | CI coverage |
|---|---|---|
| Staged database recovery | Backup fingerprint, row-count comparison, integrity check, separate restored filename | Synthetic SQLite only |
| NAS root/child unreadable or disconnect | Repro steps, scan error, unchanged photo scores/filelist and no deletion | Synthetic fault injection only |
| Two-scan missing-photo reconciliation | Two independent complete scans, mount identity, minimum expected count, matching sets, private report, manual candidate review | Synthetic read-only SQLite and local filesystem only; no NAS acceptance |
| Database concurrent writer + restart | WAL state, busy-timeout/failure response, intact restored copy | Synthetic only |
| Full Flask startup against staging v2 | Server logs, authenticated and unauthenticated HTTP status, no implicit migration | Disposable SQLite and test client |
| HTTPS reverse proxy + browser | TLS certificate/hostname verification, Basic authentication only over HTTPS, CSRF cookie/form handling, same-origin checks | Unit/test client only |
| Docker/systemd install, restart and permissions | Exact image/unit configuration, service account, umask, mount permissions, reproducible restart and rollback | Not exercised |
| ESP32 Wi-Fi/NVS and download-key rotation | Physical-device results, owner-approved credential rotation and firmware build artifact | Firmware source tests only |

The currently bundled Flask launch path uses Werkzeug; it is not a recommended
public production HTTP server. Do not expose Basic credentials over plain HTTP.
Choose, secure and test an appropriate HTTPS reverse-proxy/application-server
combination in staging before any production deployment proposal. Keep
admin credentials out of Git, process arguments and test logs.

## C. Approval and rollback boundary

- R1 stays **OPEN** until the operator records every required acceptance
  result and a human reviewer signs off. CI alone must not transition the gate.
- Failed startup/configuration test: revert the responsible isolated-branch
  commit, rerun CI; do not replace live SQLite files.
- Failed schema migration: stop all *staging* writers and restore the verified
  backup to a **new staging filename**; verify integrity and fingerprints
  before any authorized replacement.
- Failed browser/HTTPS test: disable staging access, do not fall back to
  exposing HTTP Basic authentication over plain HTTP.
- Failed device test: preserve original firmware/NVS and stop deployment;
  do not flash real hardware without separate owner authorization.
- Nothing in this checklist authorizes a merge into `local-fix`/`main`,
  production deployment, restoration over live databases, or ESP32 flashing.
