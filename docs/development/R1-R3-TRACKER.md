# InkTime R1–R3 Architecture Improvement Tracker

Baseline: \`local-fix\` at \`378036ee22db9aa99e7e5656e4dedad5c30c90f5\`.
Working branch: \`refactor/architecture-v1\`.
Repository Issues are disabled (GitHub API HTTP 410), so this tracked document is the canonical issue ledger.

Detailed work packages, dependency order, test matrices, acceptance gates and rollback: [R1-R3-IMPLEMENTATION.md](R1-R3-IMPLEMENTATION.md).

Status legend: [x] code changed (not necessarily deployed), [ ] pending. Hardware/production tests must be recorded separately.

## R1 — Data integrity and safety

- [x] PR-00 (partial): Independent branch, safety CI, unit tests, online WAL-aware SQLite backup; **new isolated restore drill** verifies integrity, photo_scores schema, row count and SHA-256 fingerprint of every row/column (including BLOB/NULL). Local synthetic test suite 5/5 passed; `--restore-drill` CLI smoke passed. **Pending**: production/NAS restore drill, environment dependency installation, human acceptance. No production data accessed.
- [x] PR-01 (safety hotfix): Fail closed on unreadable folders and DirEntry errors; stop automatically deleting missing photo scores; count discrepancies in dry-run. **Pending**: backup + real NAS failure-injection validation; two-successful-scan revalidation/optional manual purge workflow.
- [ ] PR-02 (partial): Fix safe path handling in server and WebUI; redact DOWNLOAD_KEY in transfer logs and daily orchestrator log. **Partially implemented**: admin HTTP Basic with no-password fail-closed behavior; Origin guard; removal of legacy /shutdown. **Pending**: CSRF tokens, device identity, Wi-Fi credential migration/rotation, endpoint validation, production HTTPS proxy.
- [ ] PR-03 (partial): Split broken requirements entry and repair cache .gitignore. **Pending**: versioned database migrations, configuration validation, repository cleanup.

## R2 — Reliable publish and delivery

- [ ] PR-04 (partial): Removed frozen module-level TODAY uses (render selection, recency penalty and used_at updates now evaluate date at call time). **Pending**: download/display events, device ACK and legacy compatibility.
- [ ] PR-05 (partial): Added independently testable batch_storage validator/publisher, immutable version directory, manifest, SHA-256 and atomic pointer. **Pending**: render into isolated batch directories, integrate both pipelines and HTTP endpoints, multi-process serialization, durable fsync, GC and legacy endpoint compatibility.
- [ ] PR-06: ESP32 versioned protocol, stream validation, persistent ACK event and real hardware fault injection.
- [ ] PR-07: Durable jobs, single worker, cross-process locks, retry and restart recovery.

## R3 — Modularity and operations

- [ ] PR-08: Incremental refactor of analysis/selection/render/delivery into testable application services.
- [ ] PR-09: Incremental firmware module extraction and explicit state machine without changing EPD driver.
- [ ] PR-10: End-to-end event tracing, integration tests, documentation, production rollout and rollback.

## Change record

| Date | Change | Branch | Validation | Deployment |
|---|---|---|---|---|
| 2026-10-27 | Initial scanner/backup/path safety baseline | refactor/architecture-v1 | 7 tests passed after SQL escape fix; GitHub Actions run 40356478311 succeeded | Not deployed |
| 2026-10-27 | Added admin authentication, unsafe Origin check, removed /shutdown | refactor/architecture-v1 | GitHub Actions succeeded (runs 40357191539 / 40357202612); browser/HTTPS staging untested | Not deployed |
| 2026-10-27 | PR-04 business-date calculation and indexed palette fast path | refactor/architecture-v1 | GitHub Actions succeeded (runs 40357547887 / 40357556226); actual rendering not tested | Not deployed |
| 2026-10-27 | PR-05 standalone immutable-batch storage primitives | refactor/architecture-v1 | 15/15 tests passed; Actions runs 40358002983 and 40358010523 succeeded | Not integrated into live pipeline |
| 2026-10-27 | PR-00 isolated SQLite restore drill: `df736b4502e48da092ad89cd456589342603d381`; `scripts/backup_sqlite.py`, `tests/test_sqlite_backup.py` | refactor/architecture-v1 | 5/5 local SQLite tests; CLI synthetic WAL backup+restore smoke passed; Actions push run 40387866390 **queued at time of record**; full-suite CI not yet confirmed | Not deployed; no real NAS/production DB tested |

## Rollout, verification and rollback

1. Deploy server before any new-device protocol, keeping legacy routes during the transition.
2. Back up database using \`python scripts/backup_sqlite.py --database photos.db --out-dir backups\`. Backups should be stored separately from the active host.
   For an isolated automated restore rehearsal on an authorized **staging copy only**, add `--restore-drill`; this restores into a disposable temporary directory and compares every `photo_scores` column/row plus schema. It does **not** perform a production recovery or prove NAS hardware reliability.
3. Confirm \`PRAGMA integrity_check\` of the backup, then restore to a separate test database and compare row counts.
4. Verify image scanner with root and nested directory failures before enabling regular schedules.
5. Run \`python -m unittest discover -s tests -p 'test_*.py' -v\` and check GitHub Actions.
6. Keep branch unmerged until code review; no scripts here automatically touch production photos.
7. On server rollback, restore code from prior deployment; database Schema migrations, when introduced, require their own rollback plan.
8. Keep ESP32 firmware image and NVS config snapshots before changing firmware.

## Known risk / limits

- **R1 hotfix changes behavior**: missing photos are retained in the photo score DB rather than automatically purged.
- Repeated forced scans require a mounted photo library; no production testing is performed by GitHub-only editing.
- New CI executes standard-library tests; it does not replace full dependency installation, image rendering tests or physical EPD verification.
- Restore drill is a synthetic/offline validation only; a production recovery exercise still requires an operator, an off-host backup, disk-capacity checks, and independent verification. Roll back the helper with `git revert df736b4502e48da092ad89cd456589342603d381` on this branch; original DB files and existing backups are not modified by the drill.
- Issues cannot be used until GitHub Issues is enabled for this repository.

## Admin authentication deployment gate

- Configure \`INKTIME_ADMIN_PASSWORD\` before server deployment; optional \`INKTIME_ADMIN_USERNAME\` defaults to \`admin\`. Without password, non-device routes deliberately respond 503.
- Device routes under \`/static/inktime/\`, \`/api/device_log/\`, \`/api/device_register/\` keep existing DOWNLOAD_KEY semantics during compatibility period.
- HTTP Basic authentication must only be used over HTTPS via reverse proxy; credentials are otherwise exposed over unencrypted LAN.
- Cross-site POSTs with mismatched Origin are rejected, but full CSRF-token integration remains pending. This is **not** production authorization acceptance.
- Previously enabled \`/shutdown\` endpoint is removed; legacy clients must not depend on that route.

## CI scope at latest verified implementation

- **Prior verified baseline:** 15 standard-library unit tests passed (GitHub Actions run `40358010523`, head `ac6ec854`). The restore-drill change adds four tests (5 in `test_sqlite_backup.py` total); full-suite CI for `df736b45` was queued at the time of this record, not yet confirmed.
- Tests cover admin auth policies, photo scanning errors, directory traversal, SQLite WAL backup, batch manifest/digest/dimensions, and pointer rollback.
- Syntax compile checks pass for Python legacy entrypoints and the new helper files exercised by tests.
- **Not covered**: full dependency installation, Docker/systemd integration, VLM calls, physical EPD, LAN/BLE round trips, end-to-end delivery ACK.
