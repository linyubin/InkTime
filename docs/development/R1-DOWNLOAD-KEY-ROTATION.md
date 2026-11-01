# R1 / PR-02 — ESP32 download-key server-side rotation (staging only)

This package adds **server-side** dual-key compatibility. It does not rotate
real credentials, update ESP32 firmware, or remove compiled-in device keys.
Treat historically published secrets as compromised.

## Configuration and compatibility

- With no environment overrides, the existing config.py DOWNLOAD_KEY works.
- INKTIME_DOWNLOAD_KEY explicitly overrides the active key; setting it to an
  empty string fails startup instead of falling back to the old config value.
- INKTIME_DOWNLOAD_KEY_PREVIOUS is optional. When configured, only that one
  additional legacy key is accepted. It must differ from the active key.
- Keys are case-sensitive URL path segments using ASCII letters, digits,
  underscore or hyphen, length 1–128. This permits legacy keys but **does not
  guarantee key entropy**. Generate new secrets using:
  python -c 'import secrets; print(secrets.token_urlsafe(32))'
- Store secrets in restricted process configuration, not Git, shell history,
  logs, CI output or screenshots.

## Staged rollout plan (not executed)

1. Back up configuration and capture each ESP32 firmware/NVS state. Arrange
   an isolated staging service and a physical device recovery method.
2. In staging, configure INKTIME_DOWNLOAD_KEY=<new> and
   INKTIME_DOWNLOAD_KEY_PREVIOUS=<old>. Restart staging only; verify both
   keys work and an unrelated key returns HTTP 404.
3. Update firmware to use a validated, provisioned NVS download key
   (**separate pending work package**). Test each device before deployment.
4. When every device has migrated, remove INKTIME_DOWNLOAD_KEY_PREVIOUS,
   restart staging and verify old key returns 404 while new key succeeds.
   Current transfer logs redact keys and cannot distinguish which key was
   used; add nonsecret old-key usage metrics before production rotation.
5. Rotate any historically exposed secrets and review archived firmware.

## Risks and rollback

- Dual-key mode is a temporary compatibility bridge and extends the period
  when an exposed key is accepted. It is not a security improvement by itself.
- Keys are bearer tokens in URL paths; plain HTTP allows interception on
  the LAN and proxies may log URL paths. Device authentication remains weak.
- Device-log files are named after keys. File permissions and historic
  filenames require separate remediation.
- Revert the implementation commit on the independent development branch
  and rerun CI if needed. Do not roll back production without assessing the
  resulting credential exposure.
- Automated tests cover the pure helper, real server route bodies in an
  isolated Flask test client and all-device-route AST checks; they do not
  establish full server startup, HTTPS browser, NAS or physical ESP32 success.


## 2026-10-27 — Device-side NVS provisioning (source-only, no flashing)

- Firmware no longer contains a built-in download-key path literal. A key is read from `dashcfg/dl_key` and validated as a single ASCII URL segment, 24–128 characters in `[A-Za-z0-9_-]`.
- Missing/invalid keys force the device into the existing per-device-password AP provisioning portal, even when Wi-Fi STA profiles exist. The AP form accepts a new key without echoing existing values. LAN `/save` requests cannot change the key.
- On AP save, the new key is validated and written to NVS before Wi-Fi settings; failure returns HTTP 500 without restarting. Empty key with no valid saved key returns HTTP 400. Existing valid key is preserved if the field is blank.
- Device photo, time, registration, calibration and journal upload URLs derive from the NVS key. Debug, fetch and journal logs no longer print the full bearer URL; HTTP itself remains **unencrypted**.
- **Migration is not automatic**. Stage server with new `INKTIME_DOWNLOAD_KEY` and optional previous key, provision each device physically via its unique AP password, verify it, then revoke the old key. A missing key on an upgraded device will prevent normal photo retrieval until manually provisioned.
- **Security limitations**: the AP form is not a cryptographically authenticated management API beyond WPA2 association; NVS is not encrypted unless ESP32 Flash Encryption is enabled; the device's LAN HTTP control endpoints remain separately unauthenticated. A physical attacker or LAN sniffer can still obtain bearer credentials. Previously published keys and old firmware images remain compromised.
- **No Arduino compile, NVS hardware, AP WPA2, EPD display, device HTTP, NAS, or HTTPS browser tests performed**. Only source-level regression tests and Actions CI can be recorded here.
- **Rollback**: revert the source commit on `refactor/architecture-v1` only; reverting to the old firmware would restore a known exposed download key, **not a safe production rollback**. Preserve a sanitized firmware recovery image and NVS backup for physical migration.
