# InkTime R1–R3 Architecture Improvement Tracker

Baseline: \`local-fix\` at \`378036ee22db9aa99e7e5656e4dedad5c30c90f5\`.
Working branch: \`refactor/architecture-v1\`.
Repository Issues are disabled (GitHub API HTTP 410), so this tracked document is the canonical issue ledger.

Detailed work packages, dependency order, test matrices, acceptance gates and rollback: [R1-R3-IMPLEMENTATION.md](R1-R3-IMPLEMENTATION.md).

Status legend: [x] code changed (not necessarily deployed), [ ] pending. Hardware/production tests must be recorded separately.

## R1 — Data integrity and safety

- [x] PR-00 (partial): Independent branch, safety CI, unit tests, online WAL-aware SQLite backup; **new isolated restore drill** verifies integrity, photo_scores schema, row count and SHA-256 fingerprint of every row/column (including BLOB/NULL). Local synthetic test suite 5/5 passed; `--restore-drill` CLI smoke passed. **Pending**: production/NAS restore drill, environment dependency installation, human acceptance. No production data accessed.
- [x] PR-01 (safety hotfix): Fail closed on unreadable folders and DirEntry errors; stop automatically deleting missing photo scores; count discrepancies in dry-run. **Pending**: backup + real NAS failure-injection validation; two-successful-scan revalidation/optional manual purge workflow.
- [ ] PR-02 (partial): Fix safe path handling in server and WebUI; redact DOWNLOAD_KEY in transfer logs and daily orchestrator log. **Partially implemented**: admin HTTP Basic with no-password fail-closed behavior; Origin guard; removal of legacy /shutdown. **Implemented in branch (not deployed)**: signed double-submit CSRF token on all non-device unsafe HTTP methods; Secure/Strict host-only cookie, same-origin browser fetch/form adapter, password rotation invalidates token. **Verified in CI**: unified guard for `server.py` and standalone `webui.py`; independent entrypoint no longer runs Werkzeug debugger or binds non-loopback. **Pending**: browser/HTTPS staging acceptance, device identity, Wi-Fi credential rotation on real devices, AP password source hardening (hardware acceptance pending) and firmware download-key NVS provisioning and server dual-key rotation (source change, CI pending), production HTTPS proxy. **Wi-Fi firmware source cleanup CI verified**: built-in fallback credentials and NVS auto-seeding removed, existing NVS slots preserved (static tests only). **Endpoint IP/port and egress hardening verified in CI** (36/36 Python, 4/4 Node; still no physical-device validation). Plain HTTP admin writes now fail closed because Secure cookies require HTTPS.
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
| 2026-10-27 | PR-00 isolated SQLite restore drill: `df736b4502e48da092ad89cd456589342603d381`; `scripts/backup_sqlite.py`, `tests/test_sqlite_backup.py` | refactor/architecture-v1 | 5/5 local SQLite tests; CLI synthetic WAL backup+restore smoke passed; GitHub Actions push run 40387866390 and PR run 40387875186 succeeded; latest tracker head runs 40387921740 / 40387931738 succeeded (19/19 total tests) | Not deployed; no real NAS/production DB tested |

| 2026-10-27 | PR-02 ESP32 built-in Wi-Fi credentials + NVS auto-seed removed: `6fa754e9`; source-test parser fix: `6ee54738` | refactor/architecture-v1 | 42/42 Python + 4/4 Node tests; [push CI](https://github.com/linyubin/InkTime/actions/runs/40449873529) and [PR CI](https://github.com/linyubin/InkTime/actions/runs/40449878018) success; **static firmware tests only**, Arduino compile and hardware not run | Not deployed, not flashed; previously seeded NVS entries remain until manually removed |
| 2026-10-27 | PR-02 device endpoint RFC1918 validation, no-redirect/no-proxy egress `d2199e83` | refactor/architecture-v1 | 36/36 Python + 4/4 Node tests; Push CI `40432411447` and PR CI `40432417096` success; isolated Flask route bodies, not full server | Not deployed; ESP32 and NAS untested |
| 2026-10-27 | PR-02 standalone WebUI authorization consolidated: `a7bd9d40`; cookie domain test fix `8feff86a` | refactor/architecture-v1 | 29/29 Python + 4/4 Node tests; Push CI `40425111257`, PR CI `40425117265` both succeeded | Not deployed; no HTTPS browser or ESP32 validation |
| 2026-10-27 | PR-02 CSRF enforcement `c97c2ceccd40e1c3d6c33367c4ca5e6d7a4740d3`: signed cookie/header/form check in `admin_security.py` and `server.py`; browser adapter `static/admin_csrf.js` included in 10 admin HTML pages; redacted startup `DOWNLOAD_KEY` | refactor/architecture-v1 | 24/24 Python and 4/4 Node tests passed; Python compile and requirements checks passed; [push CI](https://github.com/linyubin/InkTime/actions/runs/40412885654) and [PR CI](https://github.com/linyubin/InkTime/actions/runs/40412890996) both success | Not deployed; HTTPS browser and ESP32 not tested |

| 2026-10-27 | PR-02 random per-device AP passphrase `8c30d3b8`, saved in dashcfg NVS and displayed on EPD | refactor/architecture-v1 | 47/47 Python + 4/4 Node tests; [push CI](https://github.com/linyubin/InkTime/actions/runs/40458555711) and [PR CI](https://github.com/linyubin/InkTime/actions/runs/40458561039) SUCCESS; **static firmware tests only** | Not deployed/flashed |

| 2026-10-27 | PR-02 server-side staged download-key rotation: env active override and explicit previous key, constant-time checks on 10 device routes | refactor/architecture-v1 | New isolated helper/Flask tests; CI pending; no server/ESP32/NAS deployment | Not deployed |

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
- Cross-site unsafe methods with mismatched Origin are rejected. Additionally all non-device POST/PUT/PATCH/DELETE requests require a valid password-bound signed CSRF cookie and matching X-InkTime-CSRF header (or csrf_token form field). The browser adapter sends it on same-origin fetch and HTML form submits; CLI clients must GET with Basic auth, retain the cookie and send the token on writes.
- CSRF cookie is __Host-inktime-csrf with Secure, SameSite=Strict, Path=/ and no Domain. **Administrator write UI requires HTTPS**, including behind a TLS-terminating reverse proxy. Browser/HTTPS staging and proxy behavior remain unverified; do not deploy.
- Rollback this PR-02 subpackage with git revert c97c2ceccd40e1c3d6c33367c4ca5e6d7a4740d3 on the independent branch only; this restores previous Origin-only behavior, which is less secure. Never revert on production without evaluating the security downgrade.
- Previously enabled \`/shutdown\` endpoint is removed; legacy clients must not depend on that route.

## CI scope at latest verified implementation

- **CSRF change CI verified at `c97c2cec`:** 24/24 Python tests and 4/4 Node tests passed, with Python compile and requirements checks successful (push `40412885654`, PR `40412890996`). This does **not** establish HTTPS browser or device acceptance.
- **Prior verified baseline:** 15 standard-library unit tests passed (GitHub Actions run `40358010523`, head `ac6ec854`). The restore-drill change adds four tests (5 in `test_sqlite_backup.py` total). **Latest 19/19 unit tests passed**, with Python compilation and requirements syntax checks successful at head `0c7b6249` (Actions runs `40387921740` / `40387931738`).
- Tests cover admin auth policies, photo scanning errors, directory traversal, SQLite WAL backup, batch manifest/digest/dimensions, and pointer rollback.
- Syntax compile checks pass for Python legacy entrypoints and the new helper files exercised by tests.
- **Now covered**: synthetic Flask HTTP test-client integration for admin Basic, CSRF cookie/form/header enforcement, Origin policy, device-path separation and independent WebUI creation. **Not covered**: full `server.py` startup with real config and DB, browser HTTPS reverse-proxy and cookie forwarding, Docker/systemd integration, complete requirements installation, VLM calls, physical EPD, LAN/BLE round trips, end-to-end delivery ACK.
- **Next R1 work**: audit and design safe replacement of fixed AP provisioning password and embedded download-path key (with offline provisioning and staged firmware compatibility); then PR-03 schema/config validation and staging HTTPS/browser acceptance when infrastructure is available. Real NAS/EPD hardware verification remains blocked pending controlled test environments.


## 2026-10-27 — PR-02 standalone entrypoint guard consolidation

- Unified the administrator Basic/Origin/signed double-submit CSRF policy in \`admin_security.install_admin_security()\`. Both \`server.py\` and \`webui.create_standalone_app()\` use it; the independent WebUI never enables legacy device-path exemptions.
- The independent CLI now fails closed when \`INKTIME_ADMIN_PASSWORD\` is unset, binds only \`127.0.0.1\`, and runs with \`debug=False\` (no Werkzeug interactive debugger).
- Added Flask test client tests for 503/401/403/200 handling, cookie attributes, CSRF write enforcement, cross-origin rejection, device-route scope and independent WebUI behavior. CI installs Flask and Pillow exclusively in the sandbox runner.
- **Validation**: implementation `a7bd9d409064d6ed8fe8fdd43c85b9d3ce5c9ed2`; test-only correction `8feff86a578402b29de3435d8415f345cb29cb97`. Push CI [40425111257](https://github.com/linyubin/InkTime/actions/runs/40425111257) and PR CI [40425117265](https://github.com/linyubin/InkTime/actions/runs/40425117265) both **SUCCESS**: 29/29 Python tests and 4/4 Node tests passed, Python compilation and requirements checks passed. First implementation run found a test-client cookie-host lookup error, fixed in follow-up test-only commit. No HTTPS staging, production photo library, NAS, or real ESP32 validation performed.
- **Rollback**: on this independent branch only, `git revert 8feff86a578402b29de3435d8415f345cb29cb97` (test correction), then `git revert a7bd9d409064d6ed8fe8fdd43c85b9d3ce5c9ed2` (security implementation), inspect/test result. The previous independent WebUI had an authentication bypass and debug-enabled exposure risk; this rollback is **not safe for production**.
- **Deployment**: none. Keep PR #1 Draft and do not merge pending broader R1 acceptance.

## 2026-10-27 — PR-02 device endpoint egress validation (CI verified)

- **Scope**: `device_endpoint_security.py`, `server.py`, `tests/test_device_endpoint_security.py`, CI compile coverage. No ESP32 source changes or flashing.
- **Security**: device auto-registration, manual endpoint setting, and persisted `endpoint.json` reads all reject non-RFC1918 or malformed IPv4/ports. Device callbacks no longer follow HTTP redirects or inherit environment HTTP proxies. Only literal IPv4 addresses are accepted; `X-Forwarded-For` is intentionally ignored.
- **Compatibility risk**: installations registering through a loopback reverse proxy, using Tailscale CGNAT (100.64.0.0/10), IPv6, or public device IPs will now fail closed; configure direct LAN device registration or revisit network policy after a separate threat-model review. Invalid persisted endpoints require re-registration. This is not cryptographic device identity or complete SSRF isolation.
- **Validation**: implementation commit `d2199e8351cd16fc092adca2a58bea966e187fca`; [Push CI 40432411447](https://github.com/linyubin/InkTime/actions/runs/40432411447) and [PR CI 40432417096](https://github.com/linyubin/InkTime/actions/runs/40432417096) both **SUCCESS**. 36/36 Python tests and 4/4 Node tests passed; Python compile and requirements syntax checks passed. The new seven tests cover RFC1918 address/port parsing, malformed persisted endpoints, actual Flask registration/manual endpoint function bodies with isolated temporary storage, and redirect/proxy egress policy. The tests do **not** boot the complete `server.py` process or exercise real network hardware. NAS, browser HTTPS proxy, and real ESP32 are **not** tested.
- **Rollback**: on `refactor/architecture-v1` only, `git revert d2199e8351cd16fc092adca2a58bea966e187fca` after reviewing the diff, then rerun CI. This restores unsafe endpoint/redirect behavior and is **not a safe production security rollback**. No production endpoint file or device state was changed.

### Firmware credential finding (next work; no firmware change in this commit)

- The current source `esp32/ink-display-wifi-epd/ink-display-wifi-epd.ino` defines non-empty `BUILTIN_WIFI_SSID` and `BUILTIN_WIFI_PASS` and seeds NVS when an empty profile is found. These literals must be treated as compromised if previously published; removing them from future firmware does **not** erase copies from git history or existing NVS devices.
- Next package must remove built-in credential seeding, preserve saved NVS profiles, document AP provisioning/recovery, and require explicit credential rotation by the owner. Never print or copy real secrets into tests, tracker, PR body, or logs.


## 2026-10-27 — PR-02 Wi-Fi fallback credential removal (CI verified; hardware unverified)

- **Implementation**: `6fa754e9624b4222c1395c201c75988bb1ea6ee7` changes `esp32/ink-display-wifi-epd/ink-display-wifi-epd.ino` to remove the hardcoded fallback Wi-Fi SSID/password, `ensureBuiltinWifiProfile` and the boot-time NVS injection call. The existing 3 NVS slot keys, `loadConfig`, `saveConfig`, `startConfigPortal` and BOOT factory-reset path remain in source.
- **Tests**: six source-level regression tests added in `tests/test_firmware_wifi_credentials.py`. First implementation CI **failed 2/42** because the test helper incorrectly matched `setup()` mentioned in a comment instead of the actual function. Corrected by anchoring on C++ function declarations in test-only commit `6ee547382401e42625f41db63f384eabe6fb5b57`. Final [Push CI 40449873529](https://github.com/linyubin/InkTime/actions/runs/40449873529) and [PR CI 40449878018](https://github.com/linyubin/InkTime/actions/runs/40449878018) both **SUCCESS**: 42/42 Python and 4/4 Node tests; Python compile and requirements checks passed. **No Arduino firmware compilation was performed.**
- **Behavioral caveat**: existing NVS slots are **not** automatically cleared; previously injected credentials persist until the operator removes them. A device with no saved Wi-Fi now enters the existing AP provisioning path. This is a source-level expectation, **not real-device evidence**.
- **Credential security**: the exposed network password must be rotated at the access point and on devices. Removing a string from HEAD does not remove it from Git history, prior firmware images or NVS. A fixed AP provisioning password and embedded download-path key remain as **separate unresolved risks**.
- **Migration / operator guidance**: [R1-WIFI-CREDENTIAL-MIGRATION.md](R1-WIFI-CREDENTIAL-MIGRATION.md). Real NVS backup, slot cleanup, first-boot/AP flow, reset, reconnect, deep sleep, physical EPD and server compatibility are **NOT tested**.
- **Rollback**: only on the isolated development branch, after reviewing the diff: `git revert 6ee547382401e42625f41db63f384eabe6fb5b57` (test fix), then `git revert 6fa754e9624b4222c1395c201c75988bb1ea6ee7` (firmware source + tests + migration guide), and rerun CI. **Do not deploy the rollback firmware**: it restores publicly exposed Wi-Fi fallback credentials and automatic NVS injection. A production-safe rollback requires a sanitized known-good firmware image and controlled NVS recovery.
- **Deployment**: no merge, no production changes, no firmware flashing. Draft PR #1 remains open. **R1 Gate not accepted**.


## 2026-10-27 — AP password hardening acceptance and rollback

- **Implementation**: removed common AP passphrase; random 16-hex-character passphrase generated after Wi-Fi RF start, stored in existing NVS namespace, displayed only on EPD. NVS failure prevents starting AP.
- **Compatibility**: existing STA slots untouched; BOOT factory reset clears \`dashcfg/ap_pwd\` and generates a new AP password at the next provisioning attempt.
- **Validation**: code commit `8c30d3b867fcef0d1530873a8cb6742d4e16f830`. Push Actions run `40458555711` and PR Actions run `40458561039` succeeded, 47/47 Python plus 4/4 Node tests; these static firmware checks are not a firmware compile or ESP32 hardware test.
- **Rollback**: `git revert 8c30d3b867fcef0d1530873a8cb6742d4e16f830` on this development branch only; doing so would restore a public shared AP password. No such downgrade should be deployed. Do not revert the device itself without a secure local recovery path.
- **Next**: migrate the hardcoded photo download key to NVS provisioning with server-side dual-key transition and explicit device compatibility checks. R1 Gate remains blocked by HTTPS-browser, NAS-recovery and ESP32-hardware tests.


## R1 / PR-02 — Server-side download-key rotation (2026-10-27)

- **Code**: `download_key_security.py`, `server.py`, `config-example.py`, new `tests/test_download_key_security.py`, updated `tests/test_device_endpoint_security.py`.
- **Compatibility**: no environment overrides preserves `config.py DOWNLOAD_KEY`. Explicit `INKTIME_DOWNLOAD_KEY` overrides it; optional `INKTIME_DOWNLOAD_KEY_PREVIOUS` temporarily accepts one old key. No credentials changed.
- **Validation**: CI pending. Isolated unit/Flask route tests and all-route AST checks; no full server startup, browser, NAS or ESP32 hardware verification.
- **Risks**: old firmware still embeds exposed download path; dual-key overlap extends exposure. Device HTTP paths remain bearer tokens on cleartext LAN, and device log filenames can expose the key.
- **Rollback**: revert this implementation commit on `refactor/architecture-v1` only and rerun CI. Reverting a real service without a device migration plan risks outages and secret reuse.
- **Next**: firmware NVS download-key provisioning, secret-free device logging, server-side old-key usage metrics and removal. Then R1/PR-03 migrations/config validation and R1 Gate acceptance.
