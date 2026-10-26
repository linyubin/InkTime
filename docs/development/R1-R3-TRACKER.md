# InkTime R1–R3 Architecture Improvement Tracker

Baseline: \`local-fix\` at \`378036ee22db9aa99e7e5656e4dedad5c30c90f5\`.
Working branch: \`refactor/architecture-v1\`.
Repository Issues are disabled (GitHub API HTTP 410), so this tracked document is the canonical issue ledger.

Status legend: [x] code changed (not necessarily deployed), [ ] pending. Hardware/production tests must be recorded separately.

## R1 — Data integrity and safety

- [x] PR-00 (partial): Create independent branch, safety CI, unit tests, online SQLite backup. **Pending**: CI result, production restore drill, environment dependency installation.
- [x] PR-01 (safety hotfix): Fail closed on unreadable folders and DirEntry errors; stop automatically deleting missing photo scores; count discrepancies in dry-run. **Pending**: backup + real NAS failure-injection validation; two-successful-scan revalidation/optional manual purge workflow.
- [ ] PR-02 (partial): Fix safe path handling in server and WebUI; redact DOWNLOAD_KEY in transfer logs and daily orchestrator log. **Pending**: authenticated admin routes, CSRF, device identity, Wi-Fi secret migration, secret rotation, endpoint address validation.
- [ ] PR-03 (partial): Split broken requirements entry and repair cache .gitignore. **Pending**: versioned database migrations, configuration validation, repository cleanup.

## R2 — Reliable publish and delivery

- [ ] PR-04: Separate download events from display acknowledgments; eliminate frozen module-level business date.
- [ ] PR-05: Immutable versioned image batches; staging, manifest, hash checks, atomic pointer, GC, old endpoint compatibility.
- [ ] PR-06: ESP32 versioned protocol, stream validation, persistent ACK event and real hardware fault injection.
- [ ] PR-07: Durable jobs, single worker, cross-process locks, retry and restart recovery.

## R3 — Modularity and operations

- [ ] PR-08: Incremental refactor of analysis/selection/render/delivery into testable application services.
- [ ] PR-09: Incremental firmware module extraction and explicit state machine without changing EPD driver.
- [ ] PR-10: End-to-end event tracing, integration tests, documentation, production rollout and rollback.

## Change record

| Date | Change | Branch | Validation | Deployment |
|---|---|---|---|---|
| 2026-10-27 | Initial safety changes, backup, tests, CI and tracker | refactor/architecture-v1 | Static inspection; CI pending | Not deployed |

## Rollout, verification and rollback

1. Deploy server before any new-device protocol, keeping legacy routes during the transition.
2. Back up database using \`python scripts/backup_sqlite.py --database photos.db --out-dir backups\`. Backups should be stored separately from the active host.
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
- Issues cannot be used until GitHub Issues is enabled for this repository.
